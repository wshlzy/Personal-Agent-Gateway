"""FastAPI 统一网关：OpenAI 兼容接口 + 完整个人 AI 链路。

POST /v1/chat/completions 内部链路：
    安全中间件(鉴权→授权→限流) → 个人画像注入 → 历史对话加载(滑动窗口+摘要)
    → RAG 相关内容检索 → 多模型并行调用 → 对话持久化 → OpenAI 格式统一返回 → 记账

安全模型（详见 design.md）：
    虚拟密钥 sk_live_*（库中只存 SHA-256）+ 角色 admin/user + RPM/日预算限流（Redis）
    /health 豁免；/admin/* 由环境变量 ADMIN_TOKEN 保护

启动：python -m src.api_gateway
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# 先加载 .env 再读下方环境变量（DATABASE_URL/REDIS_URL/KEYS_DB 支持写在 .env；
# dotenv 默认不覆盖已存在的 shell 环境变量）
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from src.config_loader import CONFIG_PATH, AppConfig, load_config
from src.collab_pipeline import expand_roster, run_collab_pipeline
from src.conversation_manager import (
    MAIN_TOPIC,
    ConversationManager,
)
from src.multi_model_client import MultiModelClient
from src.profile_manager import ProfileManager
from src import rag_engine as rag_engine_module
from src.rag_engine import get_rag_engine
from src.security.admin import create_admin_router
from src.security.exceptions import ApiError
from src.security.keys import open_key_store
from src.security.middleware import SecurityMiddleware
from src.security.rate_limit import RateLimiter
from src.security.tenant import guard_write, resolve_tenant
from src.storage import (
    ForbiddenWrite,
    build_data_store,
    is_guest,
    app_mode,
)
from src.profile_manager import SYSTEM_PREFIX

# 数据文件与外部服务地址（均可用环境变量覆盖）
KEYS_DB = Path(os.environ.get("KEYS_DB", "data/keys.db"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
# 密钥库后端：设置 DATABASE_URL（Neon: postgresql://...?sslmode=require）用 Postgres，
# 否则回退本地 SQLite（KEYS_DB）
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 演示页固定文案（访客数据保留时长，需与 base.GUEST_TTL_HOURS 一致）
GUEST_NOTICE = "⚠️ 演示数据 TTL = 1 小时：访客输入仅保留 1 小时后自动删除，请勿输入真实隐私信息。"


def _safe_service_addr(url: str) -> str:
    """去掉 URL 中的账号密码，日志只保留 host:port，避免密码落到终端或日志文件。"""
    return url.rsplit("@", 1)[-1]


class GatewayState:
    """运行期共享对象，支持配置热加载。"""

    def __init__(self) -> None:
        self.config: AppConfig = load_config()
        self.profile = ProfileManager(self.config.profile_file())
        self.conversations = ConversationManager(self.config)
        self.client = MultiModelClient(self.config)
        self._config_mtime: float = CONFIG_PATH.stat().st_mtime
        # 安全层：密钥库（SQLite 或 Neon Postgres）与限流器（Redis/Upstash）；客户端均惰性连接
        self.key_store = open_key_store(KEYS_DB, DATABASE_URL or None)
        # 数据层：按 APP_MODE 选择 FileDataStore / PostgresDataStore（租户隔离）
        self.store = build_data_store()
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        self.rate_limiter = RateLimiter(self.redis)

    def reload_if_changed(self) -> bool:
        """检测 config.yaml 是否变化，变化则热加载（enabled 开关即时生效）。"""
        mtime = CONFIG_PATH.stat().st_mtime
        if mtime == self._config_mtime:
            return False
        new_config = load_config()
        self.config = new_config
        self.profile = ProfileManager(new_config.profile_file())
        self.conversations = ConversationManager(new_config)
        self.client = MultiModelClient(new_config)
        self._config_mtime = mtime
        # RAG 引擎持有旧配置，重置单例，下次请求按新配置重建
        rag_engine_module._engine = None
        return True


state = GatewayState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"  默认对话：{MAIN_TOPIC}（请求不传 topic 时自动使用）")
    keys = await state.key_store.list_keys()
    backend = f"Postgres（{DATABASE_URL.split('@')[-1].split('/')[0]}）" if DATABASE_URL else f"SQLite（{KEYS_DB}）"
    print(f"  鉴权：虚拟密钥 {len(keys)} 个｜密钥库：{backend}｜限流：Redis {_safe_service_addr(REDIS_URL)}")
    mode = app_mode()
    print(f"  数据层：APP_MODE={mode}｜{type(state.store).__name__}")
    if not os.environ.get("ADMIN_TOKEN"):
        print("  提示：未配置 ADMIN_TOKEN，/admin/* 管理端点将返回 503")
    if mode == "private":
        await _import_legacy_private_data(keys)
    yield
    await state.client.aclose()
    await state.redis.aclose()
    await state.key_store.aclose()
    await state.store.aclose()


async def _import_legacy_private_data(keys: list[dict]) -> None:
    """private 模式一次性导入：把旧版全局画像 / 对话 MD 复制到各密钥租户。

    旧网关没有租户概念，历史数据对所有 key 共享；这里按 key 的 tenant 各存一份，
    已存在同名会话则跳过（幂等）。摘要与追问文件属于 CLI 特性，不在此迁移。
    """
    try:
        legacy_profile = state.profile.read()
        legacy_convs = [
            (c.topic, c.created_at, state.conversations.load_messages(c.topic))
            for c in state.conversations.list_conversations()
        ]
    except Exception as exc:  # 迁移失败不阻断启动
        print(f"  [数据迁移] 读取旧数据失败，跳过：{exc}")
        return
    imported = 0
    for key in keys:
        if key.get("revoked_at") is not None:
            continue  # 已吊销的 key 不再导入，避免重建已清理的租户目录
        tenant = key.get("tenant_id") or key.get("id")
        if not tenant or is_guest(tenant):
            continue
        if legacy_profile and await state.store.get_profile(tenant) is None:
            await state.store.save_profile(tenant, {"content": legacy_profile})
            imported += 1
        for topic, created_at, messages in legacy_convs:
            if not messages:
                continue
            if await state.store.get_conversation(tenant, topic) is None:
                await state.store.save_conversation(
                    tenant, topic, topic, messages, created_at=created_at
                )
                imported += 1
    if imported:
        print(f"  [数据迁移] 已向各租户导入 {imported} 份旧版数据")


app = FastAPI(title="Personal AI Assistant", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    SecurityMiddleware,
    key_store_provider=lambda: state.key_store,
    rate_limiter_provider=lambda: state.rate_limiter,
    config_provider=lambda: state.config,
)
app.include_router(
    create_admin_router(
        key_store_provider=lambda: state.key_store,
        rate_limiter_provider=lambda: state.rate_limiter,
    )
)


@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError):
    """安全层 / 租户层异常（401/403/404/429/503）统一输出。"""
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


@app.exception_handler(ForbiddenWrite)
async def _forbidden_write_handler(request: Request, exc: ForbiddenWrite):
    """访客写画像 / 知识：403。"""
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """未捕获异常统一落盘：终端窗口关闭后仍能从 logs/gateway_error.log 还原 500 现场。"""
    import traceback

    line = (
        f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {request.method} {request.url.path}\n"
        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    )
    try:
        log_path = Path("logs/gateway_error.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    if isinstance(exc, UnicodeDecodeError):
        # 非 chat 端点（中间件不预读 body）GBK 请求体会在 FastAPI 解析层炸到这里
        return JSONResponse(
            {"detail": "请求体必须是 UTF-8 编码；Windows 终端建议把 JSON 存为 UTF-8 文件后用 curl -d @文件 发送"},
            status_code=400,
        )
    # 公网演示环境不回显内部异常细节（可能含 SQL / 路径），完整堆栈已落盘日志
    if app_mode() == "demo":
        return JSONResponse({"detail": "内部错误"}, status_code=500)
    return JSONResponse({"detail": f"内部错误：{type(exc).__name__}: {exc}"}, status_code=500)


# ---------- 请求/响应模型 ----------
class ChatRequest(BaseModel):
    # 标准 OpenAI 字段
    model: str | None = None
    messages: list[dict] = Field(default_factory=list)
    stream: bool = False
    # 扩展字段
    models: list[str] | None = None       # 显式指定多个模型（优先于 model）
    topic: str | None = None              # 对话主题：留空默认使用「主对话」，自动持久化多轮记忆
    tags: list[str] | None = None
    rag: bool | None = None               # 临时覆盖配置中的 RAG 开关
    collab: bool = False                  # 四角色协作流水线（顾问→裁判→主笔→审计），默认关闭


class ConversationCreate(BaseModel):
    topic: str
    tags: list[str] | None = None


class KnowledgeCreate(BaseModel):
    id: str | None = None       # 留空自动生成
    title: str
    content: str = ""


class ProfileUpdate(BaseModel):
    content: str


def _resolve_model_names(req: ChatRequest) -> list[str]:
    if req.models:
        return req.models
    if req.model and req.model != "multi":
        return [req.model]
    return state.config.call.default_models


def _validate_model_names(names: list[str]) -> None:
    """校验模型名/组名，全部无效时返回 400。

    下游 MultiModelClient 对未知模型按降级语义跳过，若一个都没剩下会抛
    ValueError（没有启用的模型可调用），未捕获即变 500；此处提前拦截。
    """
    groups = state.config.call.model_groups
    expanded: list[str] = []
    for n in names:
        expanded.extend(groups.get(n, [n]))
    enabled = {m.name for m in state.config.models if m.enabled}
    if any(n in enabled for n in expanded):
        return
    unknown = "、".join(n for n in expanded if n not in enabled)
    raise HTTPException(
        status_code=400,
        detail=f"模型不可用：{unknown}；可用模型：{', '.join(sorted(enabled))}",
    )


def _cost_cny(model_name: str, tokens: int) -> float:
    """按模型单价折算当次成本（人民币 CNY）。

    上游响应只有 total_tokens（无输入/输出拆分），取输入输出单价的平均值近似；
    模型未配置单价（=0）时成本为 0，不影响日预算。
    价格字段 price_input_usd/price_output_usd 是模型实际计价单位（USD），
    此处按 USD_TO_CNY 汇率（环境变量，默认 7.2）换算为人民币，与预算单位一致。
    """
    if tokens <= 0:
        return 0.0
    try:
        m = state.config.get_model(model_name)
    except KeyError:
        return 0.0
    avg_price_usd = (m.price_input_usd + m.price_output_usd) / 2
    if avg_price_usd <= 0:
        return 0.0
    fx = float(os.environ.get("USD_TO_CNY", "7.2"))
    return tokens / 1_000_000 * avg_price_usd * fx


# ---------- 租户与上下文 ----------
async def _tenant_scope(request: Request, requested: str | None = None) -> str:
    """解析本次请求的 tenant_id；访客请求顺带做一次惰性过期清理。"""
    ctx = request.state.auth
    tenant = resolve_tenant(ctx, requested)
    if is_guest(tenant):
        try:
            await state.store.purge_expired_guests()
        except Exception as exc:  # 清理失败不影响本次请求
            print(f"  [访客清理失败，已忽略] {exc}")
    return tenant


def _est_tokens(text: str) -> int:
    return int(len(text) * 1.5)


def _split_turns(messages: list[dict]) -> list[list[dict]]:
    """按 user 消息切轮：一轮 = 一条 user + 其后所有 assistant。"""
    turns: list[list[dict]] = []
    current: list[dict] = []
    for m in messages:
        if m.get("role") == "user":
            if current:
                turns.append(current)
            current = [m]
        elif m.get("role") == "assistant" and current:
            current.append(m)
    if current:
        turns.append(current)
    return turns


def _window_messages(messages: list[dict]) -> list[dict]:
    """按 token 预算 + 轮数上限从最新一轮向前取上下文。"""
    conv = state.config.conversation
    budget = int(getattr(conv, "history_token_budget", 6000))
    max_turns = int(getattr(conv, "history_turns", 10))
    selected: list[list[dict]] = []
    used = 0
    for turn in reversed(_split_turns(messages)):
        cost = sum(_est_tokens(str(b.get("content", ""))) for b in turn)
        if selected and (used + cost > budget or len(selected) >= max_turns):
            break
        selected.insert(0, turn)
        used += cost
    return [
        {"role": b["role"], "content": b["content"]}
        for turn in selected
        for b in turn
    ]


def _profile_system_prompt(profile: dict | None) -> str:
    content = ((profile or {}).get("profile") or {}).get("content", "")
    return SYSTEM_PREFIX + (content or "（用户尚未填写个人画像）")


# ---------- 链路组装 ----------
async def _build_messages(
    req: ChatRequest, tenant: str, conversation_id: str
) -> tuple[list[dict], str, list[dict]]:
    """组装最终发给模型的 messages。

    返回 (messages, 最后一个用户问题, 含新用户消息的完整历史)。
    历史整篇存在 DataStore（按 tenant + conversation_id 隔离），写入即持久化。
    """
    # 1) 取用户最新问题
    user_text = ""
    for m in reversed(req.messages):
        if m.get("role") == "user":
            user_text = str(m.get("content", ""))
            break
    if not user_text:
        raise HTTPException(status_code=400, detail="messages 中缺少 user 消息")

    # 2) 读本租户会话历史，追加本轮用户消息并落库
    existing = await state.store.get_conversation(tenant, conversation_id)
    history: list[dict] = list((existing or {}).get("messages") or [])
    history.append({"role": "user", "content": user_text})
    await state.store.save_conversation(
        tenant,
        conversation_id,
        (existing or {}).get("title") or conversation_id,
        history,
    )

    profile = await state.store.get_profile(tenant)
    messages: list[dict] = [{"role": "system", "content": _profile_system_prompt(profile)}]
    messages.extend(_window_messages(history))

    # 3) RAG 检索：仅 private 且非访客（访客数据不进向量库，也不读知识库）
    use_rag = state.config.rag.enabled if req.rag is None else req.rag
    if use_rag and app_mode() == "private" and not is_guest(tenant):
        try:
            engine = get_rag_engine(state.config, force=True)
            if engine is not None:
                rag_text = engine.format_context(user_text)
                if rag_text:
                    messages.insert(1, {"role": "system", "content": rag_text})
        except Exception as exc:
            # RAG 失败不阻断主链路
            print(f"  [RAG 检索失败，已降级为不检索] {exc}")

    return messages, user_text, history


async def _append_assistant(
    tenant: str, conversation_id: str, history: list[dict], replies: list[tuple[str, str]]
) -> None:
    """追加助手回复并整篇写回（replies = [(model, content)]）。"""
    if not replies:
        return
    latest = await state.store.get_conversation(tenant, conversation_id)
    current = list((latest or {}).get("messages") or history)
    for model_name, content in replies:
        current.append({"role": "assistant", "content": content, "model": model_name})
    await state.store.save_conversation(
        tenant,
        conversation_id,
        (latest or {}).get("title") or conversation_id,
        current,
    )


# ---------- 端点 ----------
@app.get("/health")
async def health() -> dict:
    """健康检查：豁免鉴权与限流，只返回最小信息。"""
    return {"status": "ok"}


@app.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request) -> str:
    """演示页：说明访客策略与调用方式（豁免鉴权，仅静态页面）。"""
    notice = GUEST_NOTICE
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Personal AI Assistant · 演示</title>
<style>
 body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 760px;
        margin: 48px auto; padding: 0 20px; line-height: 1.7; color: #1f2328; }}
 .banner {{ background: #ffecec; border: 2px solid #e5534b; color: #b42318;
           padding: 14px 18px; border-radius: 8px; font-weight: 700;
           font-size: 1.08em; text-align: center; margin: 18px 0; }}
 code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; }}
 pre {{ background: #0d1117; color: #e6edf3; padding: 14px 16px; border-radius: 8px;
        overflow-x: auto; }}
</style></head>
<body>
<h1>Personal AI Assistant 演示环境</h1>
<p class="banner">{notice}</p>
<p>当前为访客模式：每个访客拥有独立租户（<code>guest-xxxxxxxx</code>），
互相不可见，也看不到演示预置数据。访客只能写入会话，不能写画像与知识库；
数据不参与画像提炼，也不进入向量库。<strong>所有访客会话在写入 1 小时后
被自动删除（TTL = 1 小时，惰性清理）</strong>，到期不可恢复。</p>
<h2>获取演示密钥（自助注册）</h2>
<p>无需任何 token，公开自助注册：</p>
<pre>curl -X POST {request.base_url}demo/register</pre>
<p>返回 <code>sk_live_...</code>（<strong>明文仅此一次</strong>），固定参数：rpm=3、预算 ¥0.02、
仅可调用 deepseek-v4.1-flash、glm-4-flash、24 小时后自动过期。防滥用：每 IP 每天 2 把、全局每天 50 把。</p>
<p>也可由管理员调用 <code>POST /admin/keys</code> 配 <code>{{"guest": true}}</code> 签发访客密钥。</p>
<h2>发起对话</h2>
<pre>curl -X POST {request.base_url}v1/chat/completions \\
  -H "Authorization: Bearer &lt;演示密钥&gt;" \\
  -H "Content-Type: application/json" \\
  -d '{{"model": "deepseek-v4.1-flash", "messages": [{{"role": "user", "content": "你好"}}]}}'</pre>
<p>支持 <code>"collab": true</code> 开启四角色协作流水线（自动展开 deepseek-v4.1-flash + glm-4-flash）。</p>
<p class="banner">{notice}</p>
<p>可用模型：<a href="/v1/models">/v1/models</a>（需密钥）。</p>
</body></html>"""


# ---------- 公开自助注册（演示环境） ----------
async def _demo_register_rate_limit(redis_client, ip: str) -> None:
    """每 IP 每天最多 2 把；全局每天最多 50 把。fail-closed。"""
    now_utc = datetime.now(timezone.utc)
    day = now_utc.strftime("%Y%m%d")
    seconds_to_midnight = int(
        (
            (now_utc + timedelta(days=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            - now_utc
        ).total_seconds()
    ) + 60  # 多 60s 余量避免边界竞态

    async def _check(key_label: str, key_val: str, limit: int) -> None:
        key = f"register:{key_label}:{key_val}:{day}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, seconds_to_midnight)
        if count > limit:
            raise ApiError(
                429,
                "register_rate_limited",
                retry_after=seconds_to_midnight,
            )

    await _check("ip", ip, 2)
    await _check("total", "", 50)


def _extract_client_ip(request: Request) -> str:
    """取客户端 IP：优先 X-Forwarded-For 第一个，fallback client.host。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


class DemoRegisterRequest(BaseModel):
    name: str | None = None


@app.post("/demo/register", status_code=201)
async def demo_register(body: DemoRegisterRequest | None = None, request: Request = None):
    """公开自助注册端点：无需鉴权。

    防滥用：每 IP 每天 2 把、全局每天 50 把（Redis INCR+EXPIRE 原子操作）。
    返回的 key 固定：role=user、allowed_models=["deepseek-v4.1-flash","glm-4-flash"]、
    rpm=3、daily_budget_cny=0.02、expires_at=now()+24h。
    """
    # 惰性清理已过期的 keys（含上一批 demo register 创建的）
    try:
        await state.key_store.purge_expired()
    except Exception:
        pass  # 清理失败不阻断注册

    # Redis 限流（fail-closed）
    try:
        await _demo_register_rate_limit(state.redis, _extract_client_ip(request))
    except ApiError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": exc.detail,
                "message": "演示注册配额用尽，请 24 小时后重试",
            },
            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None,
        )
    except Exception as exc:
        # Redis 不可达：fail-closed，返回 503
        raise HTTPException(status_code=503, detail=f"注册服务暂时不可用：{exc}")

    expires_at = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
    row, plaintext = await state.key_store.create_key(
        role="user",
        allowed_models="deepseek-v4.1-flash,glm-4-flash",
        rpm_limit=3,
        daily_budget_cny=0.02,
        expires_at=expires_at,
    )

    result = {
        "api_key": plaintext,
        "rpm_limit": row["rpm_limit"],
        "daily_budget_cny": row["daily_budget_cny"],
        "allowed_models": ["deepseek-v4.1-flash", "glm-4-flash"],
        "expires_at": row["expires_at"],
        "notice": (
            "演示密钥 24 小时后自动失效；仅可调用 deepseek-v4.1-flash、glm-4-flash；"
            "演示输入仅保留 1 小时，请勿输入真实隐私信息。"
        ),
    }
    if body and body.name:
        result["name"] = body.name
    return JSONResponse(result, status_code=201)


@app.get("/v1/models")
async def list_models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": m.name,
                "object": "model",
                "owned_by": m.provider,
                "tier": m.tier,
                "enabled": m.enabled,
            }
            for m in state.config.models
        ],
    }


# ---------- 会话 / 知识 / 画像（全部按 tenant 隔离） ----------
@app.get("/v1/conversations")
async def list_conversations(request: Request) -> list[dict]:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    items = await state.store.list_conversations(tenant)
    return [
        {"topic": c["id"], "title": c["title"], "created_at": c["created_at"],
         "message_count": c["message_count"]}
        for c in items
    ]


@app.post("/v1/conversations")
async def create_conversation(body: ConversationCreate, request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    guard_write(tenant, "conversation")  # 访客可写会话，其余角色按原逻辑
    existing = await state.store.get_conversation(tenant, body.topic)
    if existing is None:
        existing = await state.store.save_conversation(
            tenant, body.topic, body.topic, []
        )
    return {"topic": existing["id"], "title": existing["title"],
            "created_at": existing["created_at"]}


@app.get("/v1/conversations/{topic}")
async def get_conversation(topic: str, request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    conv = await state.store.get_conversation(tenant, topic)
    if conv is None:
        raise ApiError(404, "资源不存在")  # 越权与不存在同码，不泄露存在性
    return conv


@app.delete("/v1/conversations/{topic}")
async def delete_conversation(topic: str, request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    if not await state.store.delete_conversation(tenant, topic):
        raise ApiError(404, "资源不存在")
    return {"deleted": True, "topic": topic}


@app.get("/v1/knowledge")
async def list_knowledge(request: Request) -> list[dict]:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    return await state.store.list_knowledge(tenant)


@app.post("/v1/knowledge")
async def save_knowledge(body: KnowledgeCreate, request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    guard_write(tenant, "knowledge")  # 访客 403
    item_id = body.id or f"k-{uuid.uuid4().hex[:12]}"
    return await state.store.save_knowledge(tenant, item_id, body.title, body.content)


@app.get("/v1/knowledge/{item_id}")
async def get_knowledge(item_id: str, request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    item = await state.store.get_knowledge(tenant, item_id)
    if item is None:
        raise ApiError(404, "资源不存在")
    return item


@app.get("/v1/profile")
async def get_profile(request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    return await state.store.get_profile(tenant) or {
        "tenant_id": tenant, "profile": {}, "updated_at": ""
    }


@app.put("/v1/profile")
async def save_profile(body: ProfileUpdate, request: Request) -> dict:
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    guard_write(tenant, "profile")  # 访客 403
    return await state.store.save_profile(tenant, {"content": body.content})


@app.post("/v1/rag/build")
async def rag_build() -> dict:
    # 建库允许在配置关闭时执行；检索时仍需显式 rag:true 或配置开启
    engine = get_rag_engine(state.config, force=True)
    if engine is None:
        raise HTTPException(status_code=500, detail="RAG 依赖不可用，请先安装 RAG 依赖")
    stats = engine.build_vector_db()
    return {"indexed_files": len(stats), "chunks": stats}


@app.post("/v1/admin/reload")
async def reload_config() -> dict:
    changed = state.reload_if_changed()
    return {"reloaded": changed}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    state.reload_if_changed()

    # 租户解析（admin 可带 ?tenant= 跨租户；user 越权 → 404）
    tenant = await _tenant_scope(request, request.query_params.get("tenant"))
    # 未传 topic 时默认使用主对话（带持久化多轮记忆）
    conversation_id = req.topic or MAIN_TOPIC

    model_names = _resolve_model_names(req)
    _validate_model_names(model_names)
    messages, user_text, history = await _build_messages(req, tenant, conversation_id)

    # ---------- 协作模式（四角色流水线，不支持流式） ----------
    if req.collab:
        if req.stream:
            raise HTTPException(status_code=400, detail="协作模式不支持流式，请 stream=false")
        roster = expand_roster(state.config, model_names)
        if len(roster) < 2:
            raise HTTPException(
                status_code=400,
                detail=f"协作模式至少需要 2 个启用模型，当前可用：{roster or '无'}",
            )
        outcome = await run_collab_pipeline(
            state.client, state.config, messages, user_text, roster,
        )
        if not outcome.ok:
            raise HTTPException(status_code=502, detail=f"协作流水线失败：{outcome.error}")
        await _append_assistant(
            tenant, conversation_id, history,
            [(outcome.writer, outcome.final_content)],
        )
        # 成本记账（中间件在响应后读取并落 Redis）
        request.state.cost_cny = _cost_cny(outcome.writer, outcome.tokens)
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": outcome.writer,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": outcome.final_content,
                            "model": outcome.writer,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": outcome.tokens},
                "collab": {
                    "writer": outcome.writer,
                    "aggregator": outcome.aggregator,
                    "auditor": outcome.auditor,
                    "advisors": outcome.advisors,
                    "revisions": outcome.revisions,
                    "audit_passed": outcome.audit_passed,
                },
            }
        )

    # ---------- 流式（单模型，SSE 透传） ----------
    if req.stream:
        if len(model_names) != 1:
            raise HTTPException(
                status_code=400, detail="流式模式仅支持单个模型，请在 model 或 models 中指定一个"
            )

        async def event_generator():
            import json

            full_text_parts: list[str] = []
            try:
                async for chunk_json in state.client.stream(messages, model_names[0]):
                    # 累计 delta.content，用于流式结束后持久化完整回复
                    try:
                        delta = json.loads(chunk_json)["choices"][0].get("delta", {})
                        if delta.get("content"):
                            full_text_parts.append(delta["content"])
                    except (KeyError, IndexError, json.JSONDecodeError):
                        pass
                    yield f"data: {chunk_json}\n\n"
            except Exception as exc:
                import traceback

                traceback.print_exc()
                err = {"error": {"message": str(exc), "type": "stream_error"}}
                yield f"data: {err}\n\n"
            yield "data: [DONE]\n\n"

            # 持久化：流式结束后把完整回复写入本租户会话
            if full_text_parts:
                await _append_assistant(
                    tenant, conversation_id, history,
                    [(model_names[0], "".join(full_text_parts))],
                )
            # 成本记账：流式响应无 usage 字段，按字符×1.5 估算 tokens。
            # 注意：中间件在 StreamingResponse 返回后即执行记账，晚于生成器收尾，
            # 因此这里直接由生成器自行记账（不经过 request.state）。
            text_chars = sum(len(p) for p in full_text_parts)
            est_tokens = int(text_chars * 1.5)
            cost = _cost_cny(model_names[0], est_tokens)
            if cost > 0:
                auth = getattr(request.state, "auth", None)
                if auth is not None:
                    await state.rate_limiter.record_cost(auth, cost)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------- 非流式：多模型并行 ----------
    results = await state.client.chat(messages, models=model_names, stream=False)

    # 持久化助手回复（每个成功模型一块，同一轮内写入）
    await _append_assistant(
        tenant, conversation_id, history,
        [(r.model, r.content) for r in results if r.ok and r.content],
    )

    # 组装 OpenAI 兼容响应：每个模型一个 choice
    choices = []
    errors = []
    total_tokens = 0
    total_cost = 0.0
    for i, r in enumerate(results):
        if r.ok:
            total_tokens += r.tokens
            total_cost += _cost_cny(r.model, r.tokens)
            choices.append(
                {
                    "index": i,
                    "message": {"role": "assistant", "content": r.content, "model": r.model},
                    "finish_reason": "stop",
                }
            )
        else:
            errors.append({"model": r.model, "error": r.error})

    # 成本记账（中间件在响应后读取并落 Redis）
    request.state.cost_cny = total_cost

    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model or "multi",
            "choices": choices,
            "usage": {"total_tokens": total_tokens},
            "errors": errors,
        }
    )


def run() -> None:
    import uvicorn

    cfg = state.config.server
    # BIND_HOST 环境变量优先：本地 127.0.0.1，Render 等平台设 0.0.0.0
    host = os.environ.get("BIND_HOST", cfg.host)
    port = int(os.environ.get("PORT", cfg.port))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()

