# -*- coding: utf-8 -*-
"""DataStore 契约测试 + 多租户隔离 / 访客策略测试。

同一组断言分别注入 FileDataStore 与 PostgresDataStore：
- FileDataStore 恒可用（临时目录）
- PostgresDataStore 需要 TEST_DATABASE_URL（Neon/Render 测试库），未配置则 skip

注意：asyncpg 连接池绑定事件循环，因此每个用例在同一个事件循环内完成，
避免逐调用 asyncio.run 导致 PG 后端连接池失效。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.security.exceptions import ApiError
from src.security.keys import AuthContext
from src.security.tenant import guard_write, resolve_tenant
from src.storage import (
    FileDataStore,
    ForbiddenWrite,
    InvalidTenantId,
    PostgresDataStore,
)


@pytest_asyncio.fixture(params=["file", "pg"])
async def store(request, tmp_path):
    """按后端参数化；PG 后端未配置 TEST_DATABASE_URL 时跳过。"""
    if request.param == "file":
        yield FileDataStore(tmp_path / "store")
        return

    dsn = os.environ.get("TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("未配置 TEST_DATABASE_URL，跳过 PostgresDataStore 契约测试")
    s = PostgresDataStore(dsn)
    yield s
    # 清理本次测试用到的租户数据，避免污染测试库
    names = list(getattr(s, "_test_tenants", ()))
    pool = await s._ensure()
    async with pool.acquire() as conn:
        for t in names:
            await conn.execute("DELETE FROM conversations WHERE tenant_id = $1", t)
            await conn.execute("DELETE FROM knowledge_items WHERE tenant_id = $1", t)
            await conn.execute("DELETE FROM profiles WHERE tenant_id = $1", t)
    await s.aclose()


def _tenant(store) -> str:
    """租户名统一带随机后缀，PG 后端并行/重跑也不冲突。"""
    name = f"t-{uuid.uuid4().hex[:12]}"
    store._test_tenants = getattr(store, "_test_tenants", []) + [name]
    return name


# ---------- 会话 ----------
@pytest.mark.asyncio
async def test_conversation_crud(store):
    t = _tenant(store)

    assert await store.list_conversations(t) == []
    assert await store.get_conversation(t, "主对话") is None

    saved = await store.save_conversation(t, "主对话", "主对话", [
        {"role": "user", "content": "你好"},
    ])
    assert saved["id"] == "主对话"
    assert saved["tenant_id"] == t

    got = await store.get_conversation(t, "主对话")
    assert got["messages"][0]["content"] == "你好"
    created_at = got["created_at"]

    listed = await store.list_conversations(t)
    assert len(listed) == 1
    assert listed[0]["id"] == "主对话"
    assert listed[0]["message_count"] == 1

    # 整篇覆盖：追加一条，created_at 不变
    await store.save_conversation(t, "主对话", "主对话", got["messages"] + [
        {"role": "assistant", "content": "你好呀", "model": "m"},
    ])
    again = await store.get_conversation(t, "主对话")
    assert len(again["messages"]) == 2
    assert again["created_at"] == created_at

    assert await store.delete_conversation(t, "主对话") is True
    assert await store.get_conversation(t, "主对话") is None
    assert await store.delete_conversation(t, "主对话") is False


@pytest.mark.asyncio
async def test_conversation_tenant_isolation(store):
    """tenant A 读 tenant B 的对话 → 查不到（端点层映射为 404）。"""
    a, b = _tenant(store), _tenant(store)
    await store.save_conversation(b, "秘密", "秘密", [
        {"role": "user", "content": "B 的私有内容"},
    ])

    assert await store.get_conversation(a, "秘密") is None
    assert await store.list_conversations(a) == []
    # 跨租户删除也不生效
    assert await store.delete_conversation(a, "秘密") is False
    assert await store.get_conversation(b, "秘密") is not None


# ---------- 知识 ----------
@pytest.mark.asyncio
async def test_knowledge_crud_and_isolation(store):
    a, b = _tenant(store), _tenant(store)

    await store.save_knowledge(b, "k1", "标题", "正文")
    assert await store.get_knowledge(a, "k1") is None
    assert await store.list_knowledge(a) == []

    got = await store.get_knowledge(b, "k1")
    assert got["content"] == "正文"
    assert (await store.list_knowledge(b))[0]["title"] == "标题"


# ---------- 画像 ----------
@pytest.mark.asyncio
async def test_profile_roundtrip_and_isolation(store):
    a, b = _tenant(store), _tenant(store)

    assert await store.get_profile(a) is None
    await store.save_profile(a, {"content": "所在地：杭州"})
    assert (await store.get_profile(a))["profile"]["content"] == "所在地：杭州"
    assert await store.get_profile(b) is None


# ---------- 访客策略 ----------
@pytest.mark.asyncio
async def test_guest_can_write_conversation_only(store):
    guest = f"guest-{uuid.uuid4().hex[:8]}"
    store._test_tenants = getattr(store, "_test_tenants", []) + [guest]

    # 会话可写
    await store.save_conversation(
        guest, "会话", "会话", [{"role": "user", "content": "hi"}]
    )
    assert await store.get_conversation(guest, "会话") is not None

    # 画像 / 知识禁止写入
    with pytest.raises(ForbiddenWrite):
        await store.save_profile(guest, {"content": "x"})
    with pytest.raises(ForbiddenWrite):
        await store.save_knowledge(guest, "k", "t", "c")

    # 访客互相不可见
    other = f"guest-{uuid.uuid4().hex[:8]}"
    store._test_tenants.append(other)
    assert await store.get_conversation(other, "会话") is None


@pytest.mark.asyncio
async def test_invalid_tenant_id_rejected(store):
    for bad in ("../etc", "a/b", "", "x" * 65, "租户"):
        with pytest.raises(InvalidTenantId):
            await store.list_conversations(bad)


# ---------- 网关语义：越权 404 / 访客写 403 ----------
def _ctx(tenant: str, role: str = "user") -> AuthContext:
    return AuthContext(
        key_id=f"key-{tenant}", tenant_id=tenant, role=role,
        allowed_models=["*"], rpm_limit=60, daily_budget_usd=1.0,
    )


def test_user_cross_tenant_returns_404():
    with pytest.raises(ApiError) as exc:
        resolve_tenant(_ctx("tenant-a"), "tenant-b")
    assert exc.value.status_code == 404


def test_admin_cross_tenant_requires_explicit_param():
    # admin 不带 ?tenant= 时只操作自身租户
    assert resolve_tenant(_ctx("tenant-a", role="admin")) == "tenant-a"
    # 显式指定才跨租户
    assert resolve_tenant(_ctx("tenant-a", role="admin"), "tenant-b") == "tenant-b"


def test_guest_write_profile_returns_403():
    with pytest.raises(ApiError) as exc:
        guard_write("guest-1234abcd", "profile")
    assert exc.value.status_code == 403
    with pytest.raises(ApiError) as exc:
        guard_write("guest-1234abcd", "knowledge")
    assert exc.value.status_code == 403
    # 会话可写
    guard_write("guest-1234abcd", "conversation")
