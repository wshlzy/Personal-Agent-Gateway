"""管理端点（/admin/*）：密钥的创建 / 列表 / 吊销。

鉴权方式：请求头 X-Admin-Token 必须等于环境变量 ADMIN_TOKEN；
ADMIN_TOKEN 未配置时管理端点整体 503（避免无保护暴露）。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.security.keys import KeyStore
from src.security.rate_limit import RateLimiter
from src.storage.base import new_guest_tenant, validate_tenant_id


class KeyCreateRequest(BaseModel):
    role: str = Field(default="user", pattern="^(admin|user)$")
    allowed_models: str = "*"          # 逗号分隔模型名；'*' 全部
    rpm_limit: int = Field(default=60, ge=1)
    daily_budget_usd: float = Field(default=1.0, ge=0)
    tenant_id: str | None = None       # 缺省 = key.id（一 key 一租户；显式传值可一用户多 key）
    guest: bool = False                # 创建访客密钥：tenant=guest-<8位随机>，只允许 user


def _check_admin(x_admin_token: str | None) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="管理端点未启用：未配置 ADMIN_TOKEN 环境变量")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="管理端点鉴权失败：X-Admin-Token 无效")


def create_admin_router(key_store_provider, rate_limiter_provider) -> APIRouter:
    """provider 延迟取实例（与中间件一致），便于热加载与测试替换。"""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post("/keys", status_code=201)
    async def create_key(
        body: KeyCreateRequest, x_admin_token: str | None = Header(default=None)
    ) -> dict:
        _check_admin(x_admin_token)
        role = body.role
        tenant_id = body.tenant_id
        if body.guest:
            if role == "admin":
                raise HTTPException(status_code=400, detail="访客密钥不能是 admin 角色")
            role = "user"
            tenant_id = new_guest_tenant()
        if tenant_id:
            try:
                validate_tenant_id(tenant_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        row, plaintext = await key_store_provider().create_key(
            role=role,
            allowed_models=body.allowed_models,
            rpm_limit=body.rpm_limit,
            daily_budget_usd=body.daily_budget_usd,
            tenant_id=tenant_id,
        )
        result = {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "api_key": plaintext,  # 明文仅此一次返回，之后无法找回
            "role": row["role"],
            "allowed_models": row["allowed_models"],
            "rpm_limit": row["rpm_limit"],
            "daily_budget_usd": row["daily_budget_usd"],
            "created_at": row["created_at"],
        }
        if body.guest:
            result["notice"] = "演示输入仅保留 1 小时，请勿输入真实隐私信息。"
        return result

    @router.get("/keys")
    async def list_keys(x_admin_token: str | None = Header(default=None)) -> list[dict]:
        _check_admin(x_admin_token)
        rows = await key_store_provider().list_keys()
        rl = rate_limiter_provider()
        for r in rows:
            r["spent_today_usd"] = await rl.spent_today(r["id"])
        return rows

    @router.post("/keys/{key_id}/revoke")
    async def revoke_key(key_id: str, x_admin_token: str | None = Header(default=None)) -> dict:
        _check_admin(x_admin_token)
        if not await key_store_provider().revoke(key_id):
            raise HTTPException(status_code=404, detail=f"密钥不存在：{key_id}")
        return {"id": key_id, "revoked": True}

    return router
