"""安全中间件：鉴权(401) → 授权(403) → 限流(429) → 放行 → 记账。

- /health 全豁免；/admin/* 直通（由 admin 路由自行校验 X-Admin-Token）
- /v1/admin/* 与 /v1/rag/build 视为管理端点，仅 admin 角色
- /v1/chat/completions 会解析请求体中的 model/models（collab 时展开组），
  逐个做 user 角色的 allowed_models 授权检查
- 成本记账：端点把当次成本写入 request.state.cost_cny，中间件在响应后落 Redis
"""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Receive, Scope

from src.config_loader import AppConfig
from src.security.exceptions import ApiError
from src.security.keys import KeyStore, authenticate, authorize
from src.security.rate_limit import RateLimiter

_ADMIN_PREFIXES = ("/v1/admin/", "/v1/rag/build")
_EXEMPT_PATHS = ("/health", "/demo", "/demo/register", "/v1/models")


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        key_store_provider: Callable[[], KeyStore],
        rate_limiter_provider: Callable[[], RateLimiter],
        config_provider: Callable[[], AppConfig],
    ) -> None:
        super().__init__(app)
        # provider 延迟取实例：配置热加载或测试替换 state 上的对象后即时生效
        self._keys = key_store_provider
        self._rl = rate_limiter_provider
        self._config = config_provider

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 豁免：健康检查、演示页、公开注册、admin 平面均直通
        if path in _EXEMPT_PATHS or path.startswith("/admin/"):
            return await call_next(request)

        try:
            # ① 鉴权（401）
            ctx = await authenticate(request.headers.get("authorization"), self._keys())

            # ② 授权（403）：管理端点看角色；chat 端点逐模型看 allowed_models
            admin_path = any(path.startswith(p) for p in _ADMIN_PREFIXES)
            models: list[str] = []
            if path == "/v1/chat/completions" and request.method == "POST":
                models, request = await self._extract_models(request)
            authorize(ctx, models, admin_path=admin_path)

            # ③ 限流（429）
            await self._rl().enforce(ctx)

        except ApiError as exc:
            headers = {}
            if exc.retry_after is not None:
                headers["Retry-After"] = str(exc.retry_after)
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=headers
            )

        request.state.auth = ctx
        response = await call_next(request)

        # ⑤ 记账：端点把当次成本写入 request.state.cost_cny（人民币 CNY）
        cost = getattr(request.state, "cost_cny", 0.0)
        if cost > 0:
            await self._rl().record_cost(ctx, float(cost))
        return response

    # ---------- 请求体模型提取 ----------
    async def _extract_models(self, request: Request) -> tuple[list[str], Request]:
        """读取 JSON 体解析目标模型列表；读完后重建 body 供下游端点再次读取。"""
        body = await request.body()
        request = self._rebuild_request(request, body)
        try:
            # utf-8-sig 自动吞掉 PowerShell 等写文件时带的 UTF-8 BOM（\xef\xbb\xbf）
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ApiError(
                400,
                "请求体必须是 UTF-8 编码；Windows 终端建议把 JSON 存为 UTF-8 文件后用 curl -d @文件 发送",
            ) from exc
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            return [], request  # 体非法时跳过授权，由端点返回 400

        config = self._config()
        names = payload.get("models") or (
            [payload["model"]] if payload.get("model") and payload["model"] != "multi" else None
        ) or config.call.default_models
        if payload.get("collab"):
            # 协作模式按展开后的完整角色名单授权（顾问含组内全部模型）
            from src.collab_pipeline import expand_roster

            names = expand_roster(config, names)
        return list(names), request

    @staticmethod
    def _rebuild_request(request: Request, body: bytes) -> Request:
        """重新包装 receive 通道，让下游端点能再次读取请求体。"""
        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        scope: Scope = request.scope
        new_request = Request(scope, receive)  # type: ignore[arg-type]
        return new_request
