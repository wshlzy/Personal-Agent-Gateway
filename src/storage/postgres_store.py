"""PostgresDataStore：Render 演示环境（APP_MODE=demo），Neon 免费版。

三张表全部带 tenant_id 并建索引；所有读写强制 WHERE tenant_id = $1
（写操作用复合主键 (tenant_id, id) 兜底，避免跨租户覆盖）。

启动时若三张表都为空，则从 data/demo/seed/*.json 导入预置数据到 tenant_id='demo'。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.storage.base import (
    GUEST_TTL_HOURS,
    DataStore,
    assert_writable,
    validate_tenant_id,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  tenant_id  TEXT NOT NULL,
  id         TEXT NOT NULL,
  title      TEXT NOT NULL,
  messages   JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_conversations_tenant ON conversations (tenant_id);

CREATE TABLE IF NOT EXISTS knowledge_items (
  tenant_id  TEXT NOT NULL,
  id         TEXT NOT NULL,
  title      TEXT NOT NULL,
  content    TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON knowledge_items (tenant_id);

CREATE TABLE IF NOT EXISTS profiles (
  tenant_id  TEXT PRIMARY KEY,
  profile    JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _decode(value) -> list | dict:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if value is not None else []


class PostgresDataStore(DataStore):
    """asyncpg 连接池实现；懒建池以适配 Neon 计算自动休眠。"""

    def __init__(self, database_url: str, seed_dir: Path | str | None = None) -> None:
        self._dsn = database_url
        self._seed_dir = Path(seed_dir) if seed_dir else None
        self._pool = None
        self._ready = False

    # ---------- 连接 / 初始化 ----------
    async def _ensure(self):
        if self._ready:
            return self._pool
        if self._pool is None:
            import asyncpg  # 延迟导入：只在 Postgres 后端需要

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=0,
                max_size=4,
                max_inactive_connection_lifetime=60,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(_SCHEMA)
            await self._seed_if_empty()
            self._ready = True
        return self._pool

    async def _seed_if_empty(self) -> int:
        """表都为空时导入预置演示数据（幂等：只看 conversations/knowledge/profiles 是否全空）。"""
        if self._seed_dir is None or not self._seed_dir.is_dir():
            return 0
        async with self._pool.acquire() as conn:
            counts = await conn.fetchrow(
                "SELECT (SELECT count(*) FROM conversations) AS c,"
                " (SELECT count(*) FROM knowledge_items) AS k,"
                " (SELECT count(*) FROM profiles) AS p"
            )
            if counts["c"] or counts["k"] or counts["p"]:
                return 0

        conversations: list[dict] = []
        knowledge: list[dict] = []
        profiles: list[dict] = []
        for path in sorted(self._seed_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  [seed] 跳过无法解析的文件 {path.name}：{exc}")
                continue
            if isinstance(data, list):
                conversations.extend(data)
            elif isinstance(data, dict):
                conversations.extend(data.get("conversations") or [])
                knowledge.extend(data.get("knowledge") or [])
                if data.get("profile"):
                    profiles.append(data["profile"])

        added = 0
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for c in conversations:
                    cid = str(c.get("id") or c.get("title") or "seed")
                    await conn.execute(
                        "INSERT INTO conversations (tenant_id, id, title, messages, created_at)"
                        " VALUES ('demo', $1, $2, $3::jsonb, COALESCE($4::timestamptz, now()))"
                        " ON CONFLICT (tenant_id, id) DO NOTHING",
                        cid,
                        str(c.get("title") or cid),
                        _json(c.get("messages") or []),
                        _as_dt(c.get("created_at")),
                    )
                    added += 1
                for k in knowledge:
                    kid = str(k.get("id") or k.get("title") or "seed")
                    await conn.execute(
                        "INSERT INTO knowledge_items (tenant_id, id, title, content, created_at)"
                        " VALUES ('demo', $1, $2, $3, COALESCE($4::timestamptz, now()))"
                        " ON CONFLICT (tenant_id, id) DO NOTHING",
                        kid,
                        str(k.get("title") or kid),
                        str(k.get("content") or ""),
                        _as_dt(k.get("created_at")),
                    )
                    added += 1
                for p in profiles:
                    await conn.execute(
                        "INSERT INTO profiles (tenant_id, profile, updated_at)"
                        " VALUES ('demo', $1::jsonb, now())"
                        " ON CONFLICT (tenant_id) DO UPDATE SET profile = EXCLUDED.profile",
                        _json(p.get("profile") if isinstance(p, dict) and "profile" in p else p),
                    )
                    added += 1
        if added:
            print(f"  [seed] 已导入 {added} 条演示数据到 tenant_id=demo")
        return added

    # ---------- 会话 ----------
    async def list_conversations(self, tenant_id: str) -> list[dict]:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, created_at, jsonb_array_length(messages) AS message_count"
                " FROM conversations WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"].isoformat(timespec="seconds"),
                "message_count": int(r["message_count"]),
            }
            for r in rows
        ]

    async def get_conversation(self, tenant_id: str, conversation_id: str) -> dict | None:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, messages, created_at FROM conversations"
                " WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                conversation_id,
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "tenant_id": tenant_id,
            "title": row["title"],
            "messages": _decode(row["messages"]),
            "created_at": row["created_at"].isoformat(timespec="seconds"),
        }

    async def save_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        title: str,
        messages: list[dict],
        created_at: str | None = None,
    ) -> dict:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO conversations (tenant_id, id, title, messages, created_at)"
                " VALUES ($1, $2, $3, $4::jsonb, COALESCE($5::timestamptz, now()))"
                " ON CONFLICT (tenant_id, id) DO UPDATE"
                " SET title = EXCLUDED.title, messages = EXCLUDED.messages"
                " RETURNING id, title, messages, created_at",
                tenant_id,
                conversation_id,
                title,
                _json(messages),
                _as_dt(created_at),
            )
        return {
            "id": row["id"],
            "tenant_id": tenant_id,
            "title": row["title"],
            "messages": _decode(row["messages"]),
            "created_at": row["created_at"].isoformat(timespec="seconds"),
        }

    async def delete_conversation(self, tenant_id: str, conversation_id: str) -> bool:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM conversations WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                conversation_id,
            )
        return status.endswith("1")

    # ---------- 知识 ----------
    async def list_knowledge(self, tenant_id: str) -> list[dict]:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, created_at FROM knowledge_items"
                " WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"].isoformat(timespec="seconds"),
            }
            for r in rows
        ]

    async def get_knowledge(self, tenant_id: str, item_id: str) -> dict | None:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, content, created_at FROM knowledge_items"
                " WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                item_id,
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "tenant_id": tenant_id,
            "title": row["title"],
            "content": row["content"],
            "created_at": row["created_at"].isoformat(timespec="seconds"),
        }

    async def save_knowledge(
        self, tenant_id: str, item_id: str, title: str, content: str
    ) -> dict:
        validate_tenant_id(tenant_id)
        assert_writable(tenant_id, "knowledge")
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO knowledge_items (tenant_id, id, title, content)"
                " VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (tenant_id, id) DO UPDATE"
                " SET title = EXCLUDED.title, content = EXCLUDED.content"
                " RETURNING id, title, content, created_at",
                tenant_id,
                item_id,
                title,
                content,
            )
        return {
            "id": row["id"],
            "tenant_id": tenant_id,
            "title": row["title"],
            "content": row["content"],
            "created_at": row["created_at"].isoformat(timespec="seconds"),
        }

    # ---------- 画像 ----------
    async def get_profile(self, tenant_id: str) -> dict | None:
        validate_tenant_id(tenant_id)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT profile, updated_at FROM profiles WHERE tenant_id = $1", tenant_id
            )
        if row is None:
            return None
        return {
            "tenant_id": tenant_id,
            "profile": _decode(row["profile"]),
            "updated_at": row["updated_at"].isoformat(timespec="seconds"),
        }

    async def save_profile(self, tenant_id: str, profile: dict) -> dict:
        validate_tenant_id(tenant_id)
        assert_writable(tenant_id, "profile")
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO profiles (tenant_id, profile, updated_at)"
                " VALUES ($1, $2::jsonb, now())"
                " ON CONFLICT (tenant_id) DO UPDATE"
                " SET profile = EXCLUDED.profile, updated_at = now()"
                " RETURNING updated_at",
                tenant_id,
                _json(profile),
            )
        return {
            "tenant_id": tenant_id,
            "profile": profile,
            "updated_at": row["updated_at"].isoformat(timespec="seconds"),
        }

    # ---------- 维护 ----------
    async def purge_expired_guests(self) -> int:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM conversations"
                " WHERE tenant_id LIKE 'guest-%'"
                f" AND created_at < now() - interval '{GUEST_TTL_HOURS} hour'"
            )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._ready = False
