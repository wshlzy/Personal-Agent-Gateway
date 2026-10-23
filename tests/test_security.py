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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.security.admin import create_admin_router
from src.security.exceptions import ApiError
from src.security.keys import AuthContext, KeyStore, authenticate, authorize
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
        # 模拟当次成本（CNY），由中间件在响应后记账
        try:
            cost = float(request.headers.get("x-test-cost", "0"))
        except ValueError:
            cost = 0.0
        request.state.cost_cny = cost
        # 返回一个带 model 字段的桩响应，含 messages
        model = body.get("model") or (body.get("models") or ["model-a"])[0]
        return JSONResponse({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok", "model": model},
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 10},
        })

    @app.post("/demo/register", status_code=201)
    async def demo_register_stub(request: Request):
        from datetime import datetime, timedelta, timezone
        from pydantic import BaseModel

        class DummyReq(BaseModel):
            name: str | None = None

        # 限流：每 IP 每天 2 把，全局每天 50 把（fake redis）
        try:
            body_json = await request.json()
        except Exception:
            body_json = {}
        body = DummyReq(**body_json) if body_json else DummyReq()

        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "127.0.0.1"
        )
        now_utc = datetime.now(timezone.utc)
        day = now_utc.strftime("%Y%m%d")
        sec_to_midnight = int(
            (
                (now_utc + timedelta(days=1))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                - now_utc
            ).total_seconds()
        ) + 60

        redis_client = limiter._redis

        async def _check(label, val, limit):
            key = f"register:{label}:{val}:{day}"
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, sec_to_midnight)
            if count > limit:
                raise ApiError(429, "register_rate_limited", retry_after=sec_to_midnight)

        try:
            await _check("ip", ip, 2)
            await _check("total", "", 50)
        except ApiError as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
            return JSONResponse(
                {"detail": exc.detail},
                status_code=429,
                headers=headers,
            )

        expires_at = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
        row, plaintext = await store.create_key(
            role="user", allowed_models="model-a", rpm_limit=3, daily_budget_cny=0.02,
            expires_at=expires_at,
        )
        result = {
            "api_key": plaintext,
            "rpm_limit": 3,
            "daily_budget_cny": 0.02,
            "allowed_models": ["model-a"],
            "expires_at": row["expires_at"],
            "notice": "测试注册 key",
        }
        if body.name:
            result["name"] = body.name
        return JSONResponse(result, status_code=201)

    # 显式添加 ApiError handler（中间件以外的端点也能返回规范 JSON）
    @app.exception_handler(ApiError)
    async def _api_err_handler(request: Request, exc: ApiError):
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

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
def test_no_auth_returns_401(env):
    r = env.client.post("/v1/chat/completions", json={"model": "model-a", "messages": []})
    assert r.status_code == 401
    assert "Authorization" in r.json()["detail"]


# ---------- 验收 2：无效 / 已吊销 key → 401 ----------
def test_invalid_key_returns_401(env):
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth("sk_live_invalid_000"),
    )
    assert r.status_code == 401
    assert "无效" in r.json()["detail"]


def test_revoked_key_returns_401(env):
    """创建 key → 正常调用 → 吊销 → 401。"""
    _, plaintext = _make_key(env.store, role="user", allowed_models="*")
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(plaintext),
    )
    assert r.status_code == 200
    # 吊销后 key 立即失效
    rows = _make_key(env.store, role="user", allowed_models="*")  # 另一个 key 用来拿 admin id
    key_id2, _ = rows
    # 直接吊销刚才的那个——但我们没拿到 id；换一种方式：创建 key → 吊销 → 调
    import asyncio
    row, pt = asyncio.run(env.store.create_key(role="user", allowed_models="*"))
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(pt),
    )
    assert r.status_code == 200
    ok = asyncio.run(env.store.revoke(row["id"]))
    assert ok
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(pt),
    )
    assert r.status_code == 401 and "已吊销" in r.json()["detail"]


# ---------- 验收 3：user 调 allowed_models 外模型 → 403 ----------
def test_user_model_not_allowed_returns_403(env):
    _, plaintext = _make_key(env.store, role="user", allowed_models="model-a")
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-b", "messages": []},
        headers=_auth(plaintext),
    )
    assert r.status_code == 403
    assert "无权调用" in r.json()["detail"]


# ---------- 验收 4：超 RPM → 429 + Retry-After ----------
def test_rpm_exceeded_returns_429(env):
    _, plaintext = _make_key(env.store, role="user", allowed_models="*", rpm_limit=3)
    # 连续 3 次成功，第 4 次被拒
    for i in range(3):
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
        rpm_limit=100, daily_budget_cny=0.001,
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
    # 正确 token → 201 创建 + 列表 + 吊销（注意默认 KEY_MAX_RPM=3，不能超）
    h = {"X-Admin-Token": ADMIN_TOKEN}
    created = env.client.post(
        "/admin/keys",
        json={"role": "user", "allowed_models": "model-a", "rpm_limit": 3},
        headers=h,
    )
    assert created.status_code == 201
    data = created.json()
    assert data["api_key"].startswith("sk_live_")
    assert data["rpm_limit"] == 3
    listed = env.client.get("/admin/keys", headers=h).json()
    assert len(listed) == 1 and "key_hash" not in listed[0]
    assert "spent_today_cny" in listed[0]
    assert env.client.post(
        f"/admin/keys/{data['id']}/revoke", headers=h
    ).json()["revoked"]
    # 吊销后 key 立即失效
    assert env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(data["api_key"]),
    ).status_code == 401


def test_admin_endpoints_503_without_env(monkeypatch, tmp_path):
    """未配置 ADMIN_TOKEN 时管理端点整体 503。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    store = KeyStore(tmp_path / "k.db")
    limiter = RateLimiter(fakeredis.aioredis.FakeRedis(decode_responses=True))
    app = FastAPI()
    app.include_router(create_admin_router(lambda: store, lambda: limiter))
    with TestClient(app) as client:
        assert client.get("/admin/keys").status_code == 503


# ---------- 硬性上限：rpm_limit / daily_budget_cny ----------
def test_key_creation_hard_caps(env, monkeypatch):
    """超硬性上限拒绝创建（400）；上限由环境变量配置，按请求读取。"""
    h = {"X-Admin-Token": ADMIN_TOKEN}
    # 默认上限：rpm 3 / 预算 1.0；测试里显式传 rpm_limit=3 以绕开 rpm 硬上限、专门测预算
    r = env.client.post("/admin/keys", json={"rpm_limit": 4}, headers=h)
    assert r.status_code == 400 and "KEY_MAX_RPM" in r.json()["detail"]
    r = env.client.post("/admin/keys", json={"rpm_limit": 3, "daily_budget_cny": 1.01}, headers=h)
    assert r.status_code == 400 and "KEY_MAX_DAILY_BUDGET_CNY" in r.json()["detail"]
    # 边界值（= 上限）放行；admin 角色同样受限
    ok = env.client.post("/admin/keys", json={"rpm_limit": 3, "daily_budget_cny": 1.0}, headers=h)
    assert ok.status_code == 201
    r = env.client.post("/admin/keys", json={"role": "admin", "rpm_limit": 999}, headers=h)
    assert r.status_code == 400
    # 环境变量调低上限立即生效
    monkeypatch.setenv("KEY_MAX_RPM", "2")
    r = env.client.post("/admin/keys", json={"rpm_limit": 3}, headers=h)
    assert r.status_code == 400


# ---------- demo register ----------
def test_demo_register_creates_key(env):
    """POST /demo/register 无 token → 201，返回含 key。"""
    r = env.client.post("/demo/register", json={"name": "测试"})
    assert r.status_code == 201
    data = r.json()
    assert data["api_key"].startswith("sk_live_")
    assert data["rpm_limit"] == 3
    assert data["daily_budget_cny"] == 0.02
    assert data["allowed_models"] == ["model-a"]  # demo_register_stub 返回 model-a
    assert data["expires_at"] is not None
    assert data["notice"]
    assert data["name"] == "测试"


def test_demo_register_ip_limit(env):
    """同 IP 连续第 3 次 → 429（每 IP 每天 2 把）。"""
    for i in range(2):
        assert env.client.post("/demo/register").status_code == 201
    r = env.client.post("/demo/register")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_registered_key_can_chat(env):
    """用 demo register 返回的 key 调 chat → 200；调未授权模型 → 403。"""
    r = env.client.post("/demo/register")
    key = r.json()["api_key"]
    r = env.client.post(
        "/v1/chat/completions",
        json={"model": "model-a", "messages": []},
        headers=_auth(key),
    )
    assert r.status_code == 200
    # 未授权模型
    r = env.client.post(
        "/v1/chat/completions",
        json={"model": "model-b", "messages": []},
        headers=_auth(key),
    )
    assert r.status_code == 403


def test_expired_key_returns_401(env, monkeypatch):
    """手工创建已过期 key → 鉴权返回 401。"""
    import asyncio
    from datetime import datetime, timedelta

    from src.security.keys import KeyStore as _KS
    from src.storage.base import new_guest_tenant
    db_path = env.store.db_path  # 同一 sqlite 文件，新增一条
    # 直接用 store.create_key 手工指定过去的 expires_at
    from src.security import keys as _keys_mod
    exp = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    row, plaintext = asyncio.run(
        env.store.create_key(
            role="user", allowed_models="*", rpm_limit=3, daily_budget_cny=0.01,
            expires_at=exp,
        )
    )
    r = env.client.post(
        "/v1/chat/completions", json={"model": "model-a", "messages": []},
        headers=_auth(plaintext),
    )
    assert r.status_code == 401 and "过期" in r.json()["detail"]


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
