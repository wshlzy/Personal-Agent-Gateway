"""租户上下文解析与访客守卫。

- user：只能访问自己密钥绑定的 tenant；显式传入别的 tenant 一律 404（不泄露存在性）
- admin：默认操作自身 tenant，需显式 ?tenant= 才跨租户
- 数据层越权统一 404；网关层模型越权仍是 403（两条独立链路）
"""

from __future__ import annotations

from src.security.exceptions import ApiError
from src.security.keys import AuthContext
from src.storage.base import (
    ForbiddenWrite,
    InvalidTenantId,
    is_guest,
    validate_tenant_id,
)


def resolve_tenant(ctx: AuthContext, requested: str | None = None) -> str:
    """按调用方角色解析最终 tenant_id；非法或越权抛 ApiError(404)。"""
    if ctx.role == "admin":
        tenant = (requested or ctx.tenant_id or "").strip()
    else:
        if requested and requested != ctx.tenant_id:
            raise ApiError(404, "资源不存在")
        tenant = ctx.tenant_id
    try:
        return validate_tenant_id(tenant)
    except InvalidTenantId as exc:
        raise ApiError(404, "资源不存在") from exc


def guard_write(tenant_id: str, resource: str) -> None:
    """访客写画像 / 知识 → 403（与数据层 404 区分开）。"""
    try:
        from src.storage.base import assert_writable

        assert_writable(tenant_id, resource)
    except ForbiddenWrite as exc:
        raise ApiError(403, f"访客租户不允许写入 {resource}") from exc


__all__ = ["resolve_tenant", "guard_write", "is_guest"]
