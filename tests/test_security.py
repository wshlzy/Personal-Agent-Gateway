# -*- coding: utf-8 -*-
"""鉴权与限流六条验收标准的最小可执行测试（对应 design.md 第 7 节）。

测试不依赖真实 Redis / 真实模型：
- 密钥库存临时 SQLite 文件
- 限流器注入 fakeredis
- chat 端点用桩实现（不调用任何上游模型），通过请求头 X-Test-Cost 模拟当次成本
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.security.admin import create_admin_router
from src.security.keys import KeyStore
from src.security.middleware import SecurityMiddleware
from src.security.rate_limit import RateLimiter

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """搭建最小 app：安全中间件 + /admin 路由 + 桩端点。"""
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    store = KeyStore(tmp_path / "keys.db")
    limiter = RateLimiter(fakeredis.aioredis.FakeRedis(decode_responses=True))
    fake_config = SimpleNamespace(
        call=SimpleNamespace(default_models=["model-a"], model_groups={})
    )

    app = FastAPI()
    app.add_middleware(
        SecurityMiddleware,
        key_store_provider=lambda: store,
        rate_limiter_provider=lambda: limiter,
        config_provider=lambda: fake_config,
    )
    app.include_router(
        create_admin_router(lambda: store, lambda: limiter)
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_stub(request: Request):
        body = await request.json()
        # 模拟当次成本（USD），由中间件在响应后记账
        cost = float(request.headers.get("x-test-cost", "0"))
        request.state.cost_usd = cost
        return {"ok": True, "model": body.get("model"), "cost": cost}

    @app.post("/v1/admin/reload")
    async def admin_stub():
        return {"reloaded": True}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store, limiter=limiter)


def _make_key(store: KeyStore, **kw) -> tuple[str, str]:
    """直接建 key，返回 (id, 明文)。"""
    import asyncio

    row, plaintext = asyncio.run(store.create_key(**kw))
    return row["id"], plaintext


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------- 验收 1：无 key → 401 ----------
def test_no_key_returns_401(env):
    r = env.client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    assert r.status_code == 401


# ---------- 验收 2：无效 key / 已吊销 key → 401 ----------
def test_invalid_key_returns_401(env):
    r = env.client.post(
        "/v1/chat/completions",
        json={"model": "model-a", "messages": []},
        headers=_auth("sk_live_nonexistent_key_000"),
    )
    assert r.status_code == 401


def test_revoked_key_returns_401(env):
    key_id, plaintext = _make_key(env.store, role="user", allowed_models="model-a")
    ok = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(plaintext),
    )
    assert ok.status_code == 200
    import asyncio

    assert asyncio.run(env.store.revoke(key_id)) is True
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(plaintext),
    )
    assert r.status_code == 401


# ---------- 验收 3：user 越权模型 → 403 ----------
def test_user_forbidden_model_returns_403(env):
    _, plaintext = _make_key(env.store, role="user", allowed_models="model-a")
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-b", "messages": []},
        headers=_auth(plaintext),
    )
    assert r.status_code == 403
    # admin 不受 allowed_models 限制
    _, admin_key = _make_key(env.store, role="admin")
    ok = env.client.post(
        "/v1/chat/completions", json={"model": "model-b", "messages": []},
        headers=_auth(admin_key),
    )
    assert ok.status_code == 200


def test_user_cannot_access_admin_endpoint(env):
    _, user_key = _make_key(env.store, role="user")
    r = env.client.post("/v1/admin/reload", headers=_auth(user_key))
    assert r.status_code == 403
    _, admin_key = _make_key(env.store, role="admin")
    ok = env.client.post("/v1/admin/reload", headers=_auth(admin_key))
    assert ok.status_code == 200


# ---------- 验收 4：超 RPM → 429 + Retry-After ----------
def test_rpm_limit_returns_429_with_retry_after(env):
    _, plaintext = _make_key(env.store, role="user", allowed_models="model-a", rpm_limit=2)
    for _ in range(2):
        r = env.client.post(
            "/v1/chat/completions", json={"model": "model-a", "messages": []},
            headers=_auth(plaintext),
        )
        assert r.status_code == 200
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(plaintext),
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower(): v for k, v in r.headers.items()}
    assert 1 <= int(r.headers["Retry-After"]) <= 60


# ---------- 验收 5：日预算 100% → 429 ----------
def test_daily_budget_exceeded_returns_429(env):
    _, plaintext = _make_key(
        env.store, role="user", allowed_models="model-a",
        rpm_limit=100, daily_budget_usd=0.001,
    )
    # 第一次调用成功并记账 0.001（达到预算 100%）
    r1 = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers={**_auth(plaintext), "x-test-cost": "0.001"},
    )
    assert r1.status_code == 200
    # 第二次请求被预算阻断
    r2 = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(plaintext),
    )
    assert r2.status_code == 429
    assert int(r2.headers["Retry-After"]) > 60  # 到次日的秒数必然大于一分钟


# ---------- 验收 6：/health 无 key → 200 ----------
def test_health_no_key_returns_200(env):
    r = env.client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------- 管理平面：ADMIN_TOKEN 保护 ----------
def test_admin_endpoints_require_admin_token(env):
    # 无 token → 401
    assert env.client.get("/admin/keys").status_code == 401
    # 正确 token → 201 创建 + 列表 + 吊销
    h = {"X-Admin-Token": ADMIN_TOKEN}
    created = env.client.post(
        "/admin/keys", json={"role": "user", "allowed_models": "model-a"}, headers=h
    )
    assert created.status_code == 201
    data = created.json()
    assert data["api_key"].startswith("sk_live_")
    listed = env.client.get("/admin/keys", headers=h).json()
    assert len(listed) == 1 and "key_hash" not in listed[0]
    assert env.client.post(f"/admin/keys/{data['id']}/revoke", headers=h).json()["revoked"]


def test_admin_endpoints_503_without_env(monkeypatch, tmp_path):
    """未配置 ADMIN_TOKEN 时管理端点整体 503。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    store = KeyStore(tmp_path / "k.db")
    limiter = RateLimiter(fakeredis.aioredis.FakeRedis(decode_responses=True))
    app = FastAPI()
    app.include_router(create_admin_router(lambda: store, lambda: limiter))
    with TestClient(app) as client:
        assert client.get("/admin/keys").status_code == 503


# ---------- 错误顺序：无 key 且越权时先报 401 ----------
def test_auth_precedes_authorization(env):
    r = env.client.post("/v1/chat/completions", json={"model": "model-b", "messages": []})
    assert r.status_code == 401  # 不是 403


# ---------- GBK 请求体（Windows 终端内联中文）→ 400 而非 500 ----------
def test_gbk_body_returns_400(env):
    _, plaintext = _make_key(env.store, role="user", allowed_models="model-a")
    gbk_body = '{"model":"model-a","messages":[{"role":"user","content":"帮我"}]}'.encode("gbk")
    r = env.client.post(
        "/v1/chat/completions",
        content=gbk_body,
        headers={**_auth(plaintext), "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "UTF-8" in r.json()["detail"]
