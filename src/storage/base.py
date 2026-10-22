"""数据存储抽象：会话 / 知识 / 画像，全部按租户隔离。

两套实现（按 APP_MODE 切换，见 factory.build_data_store）：
- FileDataStore：APP_MODE=private，本地目录 DATA_DIR/<tenant_id>/
- PostgresDataStore：APP_MODE=demo，Neon Postgres

隔离规则：
- 所有方法第一个参数都是 tenant_id；实现内部必须先校验 tenant 再落到路径 / SQL
- 访客租户 = "guest-<8位>"，只能写会话，禁止写画像与知识
- 数据层越权统一表现为「查不到」（返回 None / False），由调用方映射成 404
"""

from __future__ import annotations

import re
import secrets
from abc import ABC, abstractmethod

TENANT_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
GUEST_PREFIX = "guest-"
GUEST_TTL_HOURS = 1

# 访客可写的资源（其余一律拒绝）
GUEST_WRITABLE = {"conversation"}


class InvalidTenantId(ValueError):
    """tenant_id 不符合 ^[a-zA-Z0-9_-]{1,64}$（防目录穿越 / SQL 注入面）。"""


class ForbiddenWrite(PermissionError):
    """访客租户尝试写入画像或知识库。"""


def validate_tenant_id(tenant_id: str) -> str:
    """校验租户名；不合法直接抛异常（拼接路径 / 拼 SQL 前必须调用）。"""
    if not isinstance(tenant_id, str) or not TENANT_RE.fullmatch(tenant_id):
        raise InvalidTenantId(
            f"非法 tenant_id：{tenant_id!r}（只允许字母数字下划线短横线，1-64 位）"
        )
    return tenant_id


def is_guest(tenant_id: str) -> bool:
    return tenant_id.startswith(GUEST_PREFIX)


def new_guest_tenant() -> str:
    """生成访客租户：guest-<8位十六进制>（每个访客独立，互不可见）。"""
    return GUEST_PREFIX + secrets.token_hex(4)


def assert_writable(tenant_id: str, resource: str) -> None:
    """访客写权限守卫；resource 取 conversation / knowledge / profile。"""
    if is_guest(tenant_id) and resource not in GUEST_WRITABLE:
        raise ForbiddenWrite(f"访客租户 {tenant_id} 不允许写入 {resource}")


class DataStore(ABC):
    """按租户隔离的数据访问接口；方法均以 tenant_id 为第一参数。"""

    # ---------- 会话 ----------
    @abstractmethod
    async def list_conversations(self, tenant_id: str) -> list[dict]:
        """返回 [{id, title, created_at, message_count}]，仅本租户。"""

    @abstractmethod
    async def get_conversation(self, tenant_id: str, conversation_id: str) -> dict | None:
        """返回 {id, tenant_id, title, messages, created_at}；不存在或跨租户均 None。"""

    @abstractmethod
    async def save_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        title: str,
        messages: list[dict],
        created_at: str | None = None,
    ) -> dict:
        """整篇覆盖写入（upsert）；created_at 仅在首次创建时采用。"""

    @abstractmethod
    async def delete_conversation(self, tenant_id: str, conversation_id: str) -> bool:
        """删除本租户会话；返回是否真的删除了记录（跨租户恒 False）。"""

    # ---------- 知识 ----------
    @abstractmethod
    async def list_knowledge(self, tenant_id: str) -> list[dict]:
        """返回 [{id, title, created_at}]（不含正文，列表页够用）。"""

    @abstractmethod
    async def get_knowledge(self, tenant_id: str, item_id: str) -> dict | None:
        """返回 {id, tenant_id, title, content, created_at}。"""

    @abstractmethod
    async def save_knowledge(
        self, tenant_id: str, item_id: str, title: str, content: str
    ) -> dict:
        """写入知识条目（访客抛 ForbiddenWrite）。"""

    # ---------- 画像 ----------
    @abstractmethod
    async def get_profile(self, tenant_id: str) -> dict | None:
        """返回 {tenant_id, profile, updated_at}；无画像返回 None。"""

    @abstractmethod
    async def save_profile(self, tenant_id: str, profile: dict) -> dict:
        """写入画像（访客抛 ForbiddenWrite）。"""

    # ---------- 维护 ----------
    @abstractmethod
    async def purge_expired_guests(self) -> int:
        """清理超过 GUEST_TTL_HOURS 的访客会话，返回删除条数。"""

    async def aclose(self) -> None:
        """释放连接；默认空操作。"""
