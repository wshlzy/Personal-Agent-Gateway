"""虚拟密钥存储与鉴权/授权（双后端：本地 SQLite / 部署 Neon Postgres）。

后端选择（工厂 open_key_store）：
- 设置环境变量 DATABASE_URL（postgresql://...，Neon 需带 ?sslmode=require）
  → PgKeyStore（asyncpg 连接池，首次使用时懒建池，适配 Neon 自动休眠）
- 否则 → KeyStore（SQLite 文件，标准库 sqlite3，本地开发与测试默认）

行为约定（两后端一致）：
- 明文密钥 sk_live_* 只在创建时返回一次，库中只存 SHA-256 哈希
- 吊销（revoked_at 非空）立即 401
- 角色：admin 全权；user 只能调用 allowed_models 中的模型
- 对外接口统一 async；SQLite 内部为微秒级同步操作，直接薄包装不另起线程
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.security.exceptions import ApiError

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS keys (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT,
  key_hash         TEXT NOT NULL UNIQUE,
  role             TEXT NOT NULL CHECK(role IN ('admin','user')),
  allowed_models   TEXT NOT NULL DEFAULT '*',
  rpm_limit        INTEGER NOT NULL DEFAULT 60,
  daily_budget_usd REAL NOT NULL DEFAULT 1.0,
  revoked_at       TEXT,
  created_at       TEXT NOT NULL
)
"""

# Postgres 方言：REAL → DOUBLE PRECISION，其余保持一致（时间仍存 ISO 文本）
_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS keys (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT,
  key_hash         TEXT NOT NULL UNIQUE,
  role             TEXT NOT NULL CHECK(role IN ('admin','user')),
  allowed_models   TEXT NOT NULL DEFAULT '*',
  rpm_limit        INTEGER NOT NULL DEFAULT 60,
  daily_budget_usd DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  revoked_at       TEXT,
  created_at       TEXT NOT NULL
)
"""

_COLUMNS = (
    "id, tenant_id, key_hash, role, allowed_models, rpm_limit, daily_budget_usd,"
    " revoked_at, created_at"
)


def generate_api_key() -> tuple[str, str]:
    """生成虚拟密钥，返回 (明文, SHA-256)。明文只应在创建响应里出现一次。"""
    plaintext = "sk_live_" + secrets.token_urlsafe(24)
    return plaintext, hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass
class AuthContext:
    """一次请求通过鉴权后的调用方身份。"""

    key_id: str
    tenant_id: str                 # 数据隔离维度（默认 = key_id，可一用户多 key）
    role: str                      # "admin" | "user"
    allowed_models: list[str] = field(default_factory=list)  # ["*"] 表示全部
    rpm_limit: int = 60
    daily_budget_usd: float = 1.0

    def allows_model(self, model: str) -> bool:
        return "*" in self.allowed_models or model in self.allowed_models


def _new_key_row(role: str, allowed_models: str, rpm_limit: int,
                 daily_budget_usd: float, tenant_id: str | None = None) -> tuple[dict, str]:
    """两后端共用的建钥逻辑：返回 (记录, 明文)。"""
    if role not in ("admin", "user"):
        raise ValueError(f"role 只能是 admin/user，当前：{role}")
    plaintext, key_hash = generate_api_key()
    row = {
        "id": uuid.uuid4().hex[:12],
        "key_hash": key_hash,
        "role": role,
        "allowed_models": allowed_models.strip() or "*",
        "rpm_limit": int(rpm_limit),
        "daily_budget_usd": float(daily_budget_usd),
        "revoked_at": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    # tenant_id 未显式传入时默认等于 key.id（一 key 一租户）；显式传入可做一用户多 key
    from src.storage.base import validate_tenant_id
    row["tenant_id"] = validate_tenant_id(tenant_id) if tenant_id else row["id"]
    return row, plaintext


# ---------- SQLite 后端（本地开发 / 测试默认） ----------
class KeyStore:
    """keys 表存取（SQLite）；db_path 可注入临时文件便于测试。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQLITE)
            # 存量库迁移：老版本没有 tenant_id 列，补列并回填为 key.id
            cols = {r[1] for r in conn.execute("PRAGMA table_info(keys)")}
            if "tenant_id" not in cols:
                conn.execute("ALTER TABLE keys ADD COLUMN tenant_id TEXT")
                conn.execute("UPDATE keys SET tenant_id = id WHERE tenant_id IS NULL")
            conn.commit()

    # ---------- 查询 ----------
    async def find_by_hash(self, key_hash: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM keys WHERE key_hash = ?", (key_hash,)
            ).fetchone()
        return dict(row) if row else None

    async def list_keys(self) -> list[dict]:
        """管理端点列表用：不含 key_hash 本体。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, tenant_id, role, allowed_models, rpm_limit, daily_budget_usd,"
                " revoked_at, created_at FROM keys ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 写入 ----------
    async def create_key(
        self,
        role: str,
        allowed_models: str = "*",
        rpm_limit: int = 60,
        daily_budget_usd: float = 1.0,
        tenant_id: str | None = None,
    ) -> tuple[dict, str]:
        """创建密钥；返回 (记录, 明文)。明文仅此一次可得。"""
        row, plaintext = _new_key_row(
            role, allowed_models, rpm_limit, daily_budget_usd, tenant_id
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO keys (id, tenant_id, key_hash, role, allowed_models, rpm_limit,"
                " daily_budget_usd, revoked_at, created_at)"
                " VALUES (:id, :tenant_id, :key_hash, :role, :allowed_models, :rpm_limit,"
                " :daily_budget_usd, :revoked_at, :created_at)",
                row,
            )
        return row, plaintext

    async def revoke(self, key_id: str) -> bool:
        """吊销密钥（置 revoked_at）；返回是否存在该 id。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "UPDATE keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, key_id),
            )
            conn.commit()
            exists = conn.execute(
                "SELECT 1 FROM keys WHERE id = ?", (key_id,)
            ).fetchone() is not None
        return exists

    async def aclose(self) -> None:
        """统一关闭接口：SQLite 无长连接，空操作。"""


# ---------- Postgres 后端（Neon 免费版，部署用） ----------
class PgKeyStore:
    """keys 表存取（asyncpg 连接池）。

    懒建池：构造不同步连接（Neon 免费版计算会休眠，建池本身可能耗时 ~1s），
    首次查询时创建；min_size=0 不持有空闲连接，max_inactive_connection_lifetime
    让池主动回收闲置连接，规避 Neon 休眠期断连。
    """

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url
        self._pool = None

    async def _ensure_pool(self):
        if self._pool is None:
            import asyncpg  # 延迟导入：仅 Postgres 后端需要该依赖

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=0,
                max_size=4,
                max_inactive_connection_lifetime=60,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(_SCHEMA_PG)
                # 存量库迁移：补 tenant_id 列并回填
                await conn.execute(
                    "ALTER TABLE keys ADD COLUMN IF NOT EXISTS tenant_id TEXT"
                )
                await conn.execute(
                    "UPDATE keys SET tenant_id = id WHERE tenant_id IS NULL"
                )
        return self._pool

    async def find_by_hash(self, key_hash: str) -> dict | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM keys WHERE key_hash = $1", key_hash
            )
        return dict(row) if row else None

    async def list_keys(self) -> list[dict]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, tenant_id, role, allowed_models, rpm_limit, daily_budget_usd,"
                " revoked_at, created_at FROM keys ORDER BY created_at"
            )
        return [dict(r) for r in rows]

    async def create_key(
        self,
        role: str,
        allowed_models: str = "*",
        rpm_limit: int = 60,
        daily_budget_usd: float = 1.0,
        tenant_id: str | None = None,
    ) -> tuple[dict, str]:
        row, plaintext = _new_key_row(
            role, allowed_models, rpm_limit, daily_budget_usd, tenant_id
        )
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO keys (id, tenant_id, key_hash, role, allowed_models, rpm_limit,"
                " daily_budget_usd, revoked_at, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                row["id"], row["tenant_id"], row["key_hash"], row["role"],
                row["allowed_models"], row["rpm_limit"], row["daily_budget_usd"],
                row["revoked_at"], row["created_at"],
            )
        return row, plaintext

    async def revoke(self, key_id: str) -> bool:
        now = datetime.now().isoformat(timespec="seconds")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE keys SET revoked_at = $1 WHERE id = $2 AND revoked_at IS NULL",
                now, key_id,
            )
            exists = await conn.fetchval(
                "SELECT 1 FROM keys WHERE id = $1", key_id
            )
        return exists is not None

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def open_key_store(db_path: Path | str, database_url: str | None = None):
    """后端工厂：有 DATABASE_URL 用 Postgres，否则用 SQLite 文件。"""
    if database_url:
        return PgKeyStore(database_url)
    return KeyStore(db_path)


# ---------- 鉴权 / 授权（纯函数，便于单测） ----------
def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


async def authenticate(authorization: str | None, store) -> AuthContext:
    """解析 Bearer 虚拟密钥并返回身份；失败抛 ApiError(401)。"""
    token = _bearer(authorization)
    if not token:
        raise ApiError(401, "缺少 Authorization: Bearer <api_key>")
    row = await store.find_by_hash(hashlib.sha256(token.encode("utf-8")).hexdigest())
    if row is None:
        raise ApiError(401, "API key 无效")
    if row["revoked_at"]:
        raise ApiError(401, "API key 已吊销")
    models = [m.strip() for m in row["allowed_models"].split(",") if m.strip()]
    return AuthContext(
        key_id=row["id"],
        tenant_id=row.get("tenant_id") or row["id"],
        role=row["role"],
        allowed_models=models or ["*"],
        rpm_limit=int(row["rpm_limit"]),
        daily_budget_usd=float(row["daily_budget_usd"]),
    )


def authorize(ctx: AuthContext, models: list[str], *, admin_path: bool = False) -> None:
    """授权检查（在鉴权之后执行）；越权抛 ApiError(403)。

    admin_path=True 的端点仅 admin 角色可用；user 角色逐模型检查 allowed_models。
    """
    if admin_path and ctx.role != "admin":
        raise ApiError(403, f"角色 {ctx.role} 无权访问管理端点")
    if ctx.role == "admin":
        return  # admin 可调用所有模型
    for m in models:
        if not ctx.allows_model(m):
            raise ApiError(403, f"角色 user 无权调用模型：{m}")
