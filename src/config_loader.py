"""配置加载与校验模块。

职责：
1. 读取 config/config.yaml
2. 将 api_key 中的 ${ENV_VAR} 占位符替换为 .env / 系统环境变量中的真实值
3. 用 pydantic 校验配置完整性，缺失项给出明确报错
4. 解析并创建各数据目录
5. 支持 reload() 热加载（enabled 开关调整后无需重启进程）
"""

from __future__ import annotations

import os
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# 项目根目录（personal-ai-assistant/）
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _data_root() -> Path:
    """数据根目录：由 DATA_DIR 决定（默认 ./data/private）。

    延迟导入避免 config_loader ↔ storage.factory 循环依赖。
    """
    from src.storage.factory import data_dir

    return data_dir()


def _migrate_legacy_dirs(config: "AppConfig") -> None:
    """private 模式一次性把旧版项目根目录数据搬进 DATA_DIR（幂等）。

    旧版本把 conversations/profile/knowledge 直接放在项目根；升级后统一落
    DATA_DIR。目标已存在时跳过，避免覆盖新数据。
    """
    if os.environ.get("APP_MODE", "private").strip().lower() != "private":
        return
    root = _data_root()
    for key in ("profile", "conversations", "knowledge"):
        rel = Path(getattr(config.paths, key))
        if rel.is_absolute():
            continue
        legacy = (BASE_DIR / rel).resolve()
        target = (root / rel).resolve()
        if legacy == target or not legacy.exists() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(legacy), str(target))
        except OSError:
            shutil.copytree(legacy, target, dirs_exist_ok=True)
        print(f"  [数据迁移] {legacy} → {target}")


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8010
    # 出站代理（如 http://127.0.0.1:7890）；留空则跟随系统环境变量。
    # 启动时注入 HTTP_PROXY/HTTPS_PROXY，openai/httpx/requests 均读取环境变量，
    # 不读 WinHTTP/IE 系统代理。代理端口不可达时自动跳过，不影响国产模型直连。
    proxy: str = ""
    no_proxy: str = "127.0.0.1,localhost,::1"


class ModelConfig(BaseModel):
    """单个大模型的连接配置。"""

    name: str
    provider: str
    base_url: str
    api_key: str
    model_name: str
    enabled: bool = True
    tier: str = "light"
    # 模型级出站代理：留空=跟随全局（server.proxy / 环境变量）；
    # direct / off / none = 强制直连（绕过全局代理）；
    # http(s)://... = 仅该模型走指定代理，不影响其他模型
    proxy: str = ""
    # 模型级请求超时（秒）：留空=使用 call.timeout_seconds 全局值。
    # 旗舰推理模型长提示词可能生成数万 tokens，需单独放宽（如 kimi-k3 1800s），
    # 避免轻量模型跟着一起长时间等待失败请求。
    timeout_seconds: float | None = None
    # 计费单价（USD / 百万 tokens），用于密钥日预算记账；0=不计费（不阻塞）
    price_input_usd: float = 0.0
    price_output_usd: float = 0.0

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"base_url 必须以 http:// 或 https:// 开头，当前值：{v}")
        return v.rstrip("/")


class CollabConfig(BaseModel):
    """四角色协作流水线的默认角色定位（/collab <组名> 按主题启用后生效）。

    组内缺少某角色配置的模型时自动按组内顺序顺延（详见 collab_pipeline.resolve_roles）。
    """

    # 主笔：唯一负责定稿（长上下文理解与指令遵循最优的新架构旗舰）
    writer: str = "deepseek-v4.1-flash"
    # 裁判：聚合共识/冲突/硬约束（结构化输出稳定）
    aggregator: str = "glm-5.3"
    # 审计：只挑错不重写（长上下文强推理，适合挑错）
    auditor: str = "kimi-k3"
    # 审计发现阻断级问题后回炉主笔修订的最大轮数（0=只审计不修订）
    max_revision_rounds: int = 1


class CallConfig(BaseModel):
    default_models: list[str] = Field(default_factory=list)
    fallback_order: list[str] = Field(default_factory=list)
    timeout_seconds: float = 600.0
    # 自定义模型组：组名 -> 模型名列表（CLI /group 命令管理，API 可直接传组名）
    model_groups: dict[str, list[str]] = Field(default_factory=dict)
    # 四角色协作流水线默认定位（默认关闭，仅 /collab 启用的主题生效）
    collab: CollabConfig = CollabConfig()


class ConversationConfig(BaseModel):
    # 历史上下文最多保留的最近轮数（硬上限；预算可能进一步缩减实际轮数）
    history_turns: int = 10
    # 历史上下文 token 预算（粗估：字符数/1.5）。窗口按预算从最新轮向前累加，
    # 短对话自动多带轮、长回复自动少带轮（替代纯固定轮数）
    history_token_budget: int = 6000
    # 发给某模型时，历史中「其它模型的回复」只保留前 N 字要点；
    # 该模型自己的回复始终保留全文（模型最受益于自己的推理链）
    other_model_snippet_chars: int = 500
    # 每次上传前显示提示词 token 估算并等待用户确认（CLI 交互；网关 API 不受影响）
    confirm_before_send: bool = True
    summarize_model: str = ""
    # /insert 命令的固定来源文件：不再每次传路径，统一从该文件读取待插入的回复内容
    insert_source: str = "insert_source.md"
    # /summarize 使用的总结模型（deepseek 旗舰推理强）
    decision_summary_model: str = "deepseek-reasoner"
    # /profile 画像提炼 + 合并重写使用的模型（画像质量优先，用旗舰）
    profile_model: str = "deepseek-v4.1-flash"
    # /summarize 平票时的 provider 优先级（越靠前权重越高）
    provider_priority: list[str] = Field(
        default_factory=lambda: [
            "deepseek",
            "zhipu",
            "moonshot",
            "openai",
            "gemini-3-8-flash",
        ]
    )


class RagConfig(BaseModel):
    enabled: bool = False
    top_k: int = 3
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    hf_endpoint: str = "https://hf-mirror.com"
    chunk_size: int = 800
    chunk_overlap: int = 100


class LimitsConfig(BaseModel):
    token_warning_threshold: int = 20000


class PathsConfig(BaseModel):
    profile: str = "profile"
    conversations: str = "conversations"
    knowledge: str = "knowledge"
    chroma_db: str = "chroma_db"


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    models: list[ModelConfig]
    call: CallConfig = CallConfig()
    conversation: ConversationConfig = ConversationConfig()
    rag: RagConfig = RagConfig()
    limits: LimitsConfig = LimitsConfig()
    paths: PathsConfig = PathsConfig()

    # ---- 便捷查询方法 ----
    def get_model(self, name: str) -> ModelConfig:
        # 1) 精确匹配
        for m in self.models:
            if m.name == name:
                return m
        # 2) 宽松匹配：去空格、小写、子串包含（兼容调用方截断模型名，如 "Gemini" 匹配 "Gemini 3.8 Flash"）
        target = name.strip().lower().replace(" ", "")
        for m in self.models:
            normalized = m.name.strip().lower().replace(" ", "")
            if normalized == target or target in normalized:
                return m
        available = ", ".join(m.name for m in self.models)
        raise KeyError(f"模型 '{name}' 不存在，可用模型：{available}")

    def enabled_models(self, names: list[str] | None = None) -> list[ModelConfig]:
        """返回启用的模型；names 给定时按 names 顺序过滤。"""
        if names is None:
            return [m for m in self.models if m.enabled]
        result = []
        for n in names:
            m = self.get_model(n)
            if m.enabled:
                result.append(m)
        return result

    def dir_path(self, key: str) -> Path:
        """返回某个数据目录的绝对路径。

        相对路径统一解析到数据根目录（DATA_DIR，默认 ./data/private），
        不再直接落在项目根，避免本地与演示环境混用同一份数据。
        """
        rel = getattr(self.paths, key)
        p = Path(rel)
        if p.is_absolute():
            return p
        return _data_root() / p

    def profile_file(self) -> Path:
        return self.dir_path("profile") / "个人画像.md"


def _proxy_reachable(proxy_url: str, timeout: float = 1.0) -> bool:
    """TCP 探测代理端口是否在监听。"""
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _apply_proxy(proxy_url: str, no_proxy: str) -> None:
    """启动时把 config.yaml 的代理写入环境变量。

    必须在创建任何 httpx/openai 客户端之前执行（httpx 在客户端构造时
    读取环境变量）。规则：
    - shell / .env 已显式设置 HTTPS_PROXY 时优先外部值，不覆盖；
    - 配置了代理但端口不通时跳过并告警，避免代理软件未开时所有请求失败。
    """
    if os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        return
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return
    if not _proxy_reachable(proxy_url):
        print(f"  [代理] {proxy_url} 端口不可达，本次跳过代理（国产模型直连不受影响）")
        return
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ[key] = proxy_url
    if no_proxy.strip() and not (os.getenv("NO_PROXY") or os.getenv("no_proxy")):
        os.environ["NO_PROXY"] = no_proxy.strip()
        os.environ["no_proxy"] = no_proxy.strip()
    print(f"  [代理] 已启用出站代理：{proxy_url}")


def _expand_env(value: str) -> str:
    """把 ${VAR} 替换成环境变量值；变量不存在时原样保留（由校验阶段报错）。"""

    def _repl(match: re.Match) -> str:
        return os.getenv(match.group(1), match.group(0))

    return _ENV_PATTERN.sub(_repl, value)


def load_config(path: Path | None = None) -> AppConfig:
    """加载并校验配置。配置非法时抛 ValueError / pydantic.ValidationError。"""
    # 自动加载项目根目录下的 .env（无需用户手动配置系统环境变量）
    load_dotenv(BASE_DIR / ".env")

    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    raw: dict = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    # 展开所有模型 api_key 中的 ${VAR}
    for m in raw.get("models", []):
        if isinstance(m.get("api_key"), str):
            m["api_key"] = _expand_env(m["api_key"])

    config = AppConfig(**raw)

    # 启动阶段注入代理（在任何模型 HTTP 客户端创建之前）
    _apply_proxy(config.server.proxy, config.server.no_proxy)

    # ---- 业务校验 ----
    if not config.models:
        raise ValueError("config.yaml 中未配置任何模型（models 不能为空）")

    enabled = [m for m in config.models if m.enabled]
    if not enabled:
        raise ValueError("没有任何 enabled: true 的模型，至少需要启用一个模型")

    for m in enabled:
        if not m.api_key or _ENV_PATTERN.search(m.api_key):
            raise ValueError(
                f"模型 '{m.name}' 的 api_key 为空或环境变量未设置，"
                f"请检查 .env 文件或 config.yaml 中的 api_key 配置"
            )
        if m.timeout_seconds is not None and m.timeout_seconds <= 0:
            raise ValueError(f"模型 '{m.name}' 的 timeout_seconds 必须为正数")

    for group_name in ("default_models", "fallback_order"):
        for n in getattr(config.call, group_name):
            try:
                config.get_model(n)
            except KeyError as e:
                raise ValueError(f"call.{group_name} 配置错误：{e}") from e

    for gname, members in config.call.model_groups.items():
        if not members:
            raise ValueError(f"call.model_groups.{gname} 的模型列表不能为空")
        for n in members:
            try:
                config.get_model(n)
            except KeyError as e:
                raise ValueError(f"call.model_groups.{gname} 配置错误：{e}") from e

    for role in ("writer", "aggregator", "auditor"):
        name = getattr(config.call.collab, role)
        try:
            config.get_model(name)
        except KeyError as e:
            raise ValueError(f"call.collab.{role} 配置错误：{e}") from e
    if config.call.collab.max_revision_rounds < 0:
        raise ValueError("call.collab.max_revision_rounds 不能为负数")

    if config.conversation.summarize_model:
        try:
            config.get_model(config.conversation.summarize_model)
        except KeyError as e:
            raise ValueError(f"conversation.summarize_model 配置错误：{e}") from e
    if config.conversation.history_turns < 0:
        raise ValueError("conversation.history_turns 不能为负数")
    if config.conversation.history_token_budget <= 0:
        raise ValueError("conversation.history_token_budget 必须为正数")
    if config.conversation.other_model_snippet_chars < 0:
        raise ValueError("conversation.other_model_snippet_chars 不能为负数")

    if config.rag.chunk_overlap >= config.rag.chunk_size:
        raise ValueError("rag.chunk_overlap 必须小于 rag.chunk_size")

    # ---- 确保数据目录存在 ----
    _migrate_legacy_dirs(config)  # 必须在 mkdir 之前：目标目录已存在会跳过迁移
    for key in ("profile", "conversations", "knowledge", "chroma_db"):
        config.dir_path(key).mkdir(parents=True, exist_ok=True)

    return config


def save_model_group(name: str, models: list[str]) -> None:
    """把模型组写回 config.yaml 的 call.model_groups 段（按行编辑，保留注释）。

    - 已有 model_groups 段：同名组就地更新，否则段内追加
    - 没有 model_groups 段：在 call 段末尾插入该段
    """
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    entry = f"    {name}: [{', '.join(models)}]"

    gi = next(
        (i for i, l in enumerate(lines) if re.match(r"^  model_groups:(\s|#|$)", l)),
        None,
    )
    if gi is not None:
        # 段内范围：model_groups: 之后直到第一个非空且缩进不足 4 的行
        end = gi + 1
        while end < len(lines) and (lines[end].strip() == "" or lines[end].startswith("    ")):
            end += 1
        pat = re.compile(rf"^    {re.escape(name)}:")
        for i in range(gi + 1, end):
            if pat.match(lines[i]):
                lines[i] = entry  # 同名组就地更新
                break
        else:
            end = min(end, _last_nonblank(lines, gi + 1, end) + 1)
            lines.insert(end, entry)  # 段内追加
    else:
        # 无 model_groups 段：定位 call: 段末插入
        ci = next(i for i, l in enumerate(lines) if re.match(r"^call:", l))
        end = ci + 1
        while end < len(lines) and (lines[end].strip() == "" or lines[end].startswith("  ")):
            end += 1
        end = min(end, _last_nonblank(lines, ci + 1, end) + 1)
        lines[end:end] = [
            "  # 自定义模型组：CLI 用 /group <组名> 一键切换，API 的 models 字段也可直接传组名",
            "  model_groups:",
            entry,
        ]

    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _last_nonblank(lines: list[str], start: int, stop: int | None = None) -> int:
    """返回 [start, stop) 范围内最后一个非空行的下标；范围内全空则返回 start-1。"""
    stop = len(lines) if stop is None else stop
    idx = start - 1
    for i in range(start, stop):
        if lines[i].strip():
            idx = i
    return idx


# ---------- 添加任意模型（CLI /model add 的通用流程）----------

def _existing_env_names() -> set[str]:
    """收集 .env 已定义 + config.yaml 已通过 ${VAR} 引用的全部变量名。"""
    names: set[str] = set()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and not line.lstrip().startswith("#"):
                names.add(m.group(1))
    if CONFIG_PATH.exists():
        names.update(_ENV_PATTERN.findall(CONFIG_PATH.read_text(encoding="utf-8")))
    return names


def _append_env_var(var_name: str, value: str) -> bool:
    """向 .env 追加 KEY=VALUE；变量已存在则不覆盖。返回是否发生写入。"""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if re.search(rf"^\s*{re.escape(var_name)}\s*=", text, re.MULTILINE):
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    text += f"{var_name}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    return True


def _gen_env_var(provider: str, taken: set[str]) -> str:
    """根据提供方名生成形如 DASHSCOPE_API_KEY 的环境变量名，冲突时追加序号。"""
    base = re.sub(r"[^A-Za-z0-9]+", "_", provider).strip("_").upper() or "CUSTOM"
    candidate = f"{base}_API_KEY"
    i = 2
    while candidate in taken:
        candidate = f"{base}_API_KEY_{i}"
        i += 1
    return candidate


def _normalize_provider(name: str) -> str:
    """从本地模型名派生 provider 标识：取字母数字、小写；纯非 ASCII 名回退 custom。"""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "custom"


def _insert_model_block(block: list[str]) -> None:
    """把模型 YAML 块插入 models 段末尾（按行编辑，保留注释与其它配置）。"""
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    mi = next((i for i, l in enumerate(lines) if re.match(r"^models:", l)), None)
    if mi is None:
        raise ValueError("config.yaml 中找不到 models: 段")
    # models 段结束：其后第一个列 0 非空白行（顶层键或顶层注释）
    end = mi + 1
    while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
        end += 1
    # 插到段内最后一个非空行之后，并与上文隔开一行
    insert_at = _last_nonblank(lines, mi + 1, end) + 1
    lines[insert_at:insert_at] = [""] + block
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_model(
    name: str,
    model_name: str,
    base_url: str,
    api_key: str | None = None,
    tier: str = "light",
    provider: str | None = None,
    proxy: str = "",
) -> dict:
    """向系统添加任意 OpenAI 兼容模型的通用流程：

    1. 校验名称 / base_url / tier / proxy；
    2. 与已有模型同源（同 host）时自动复用其 provider 与密钥变量；
    3. 全新提供方：密钥写入 .env（自动生成环境变量名，不硬编码进 yaml），
       config.yaml 只保留 ${变量名} 引用；
    4. 按行编辑把模型块追加到 config.yaml 的 models 段（保留全部注释）。

    proxy：留空=跟随全局代理；direct=强制直连；http(s)://...=仅该模型走此代理。

    返回 {provider, env_var, reused_key, env_written}；失败抛 ValueError。
    """
    name = name.strip()
    model_name = model_name.strip()
    base_url = base_url.strip().rstrip("/")
    tier = (tier or "light").strip().lower()
    proxy = (proxy or "").strip()
    if not name or not model_name or not base_url:
        raise ValueError("模型名称、上游模型 ID、base_url 均不能为空")
    if not re.match(r"^https?://", base_url):
        raise ValueError(f"base_url 必须以 http:// 或 https:// 开头：{base_url}")
    if tier not in ("light", "flagship"):
        raise ValueError(f"tier 只能是 light 或 flagship，当前：{tier}")
    if proxy and not (
        re.match(r"^https?://", proxy) or proxy.lower() in ("direct", "off", "none")
    ):
        raise ValueError(
            f"proxy 需为 http(s):// 地址、direct（强制直连）或留空，当前：{proxy}"
        )

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    existing = raw.get("models", []) or []
    if any(m.get("name") == name for m in existing):
        raise ValueError(f"模型名 '{name}' 已存在，请换一个本地名称")

    host = urlparse(base_url).hostname or ""
    same_origin = next(
        (
            m
            for m in existing
            if (urlparse(str(m.get("base_url", ""))).hostname or "") == host
        ),
        None,
    )

    if same_origin is not None:
        # 同源复用：沿用 provider 与 api_key 原文（${VAR} 或明文），无需再提供密钥
        provider = str(same_origin.get("provider") or provider or _normalize_provider(name))
        key_yaml = str(same_origin.get("api_key") or "")
        env_var = ""
        mobj = _ENV_PATTERN.search(key_yaml)
        if mobj:
            env_var = mobj.group(1)
        reused, env_written = True, False
    else:
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("这是新提供方，必须提供 API Key")
        provider = (provider or _normalize_provider(name)).strip()
        taken = _existing_env_names()
        env_var = _gen_env_var(provider, taken)
        env_written = _append_env_var(env_var, api_key)
        os.environ[env_var] = api_key  # 让本进程随后的 load_config 立即生效
        key_yaml = "${" + env_var + "}"
        reused = False

    block = [
        f"  - name: {name}",
        f"    provider: {provider}",
        f"    base_url: {base_url}",
        f"    api_key: {key_yaml}",
        f"    model_name: {model_name}",
    ]
    if proxy:
        block.append(f"    proxy: {proxy}     # 模型级代理（direct=强制直连）")
    block += [
        "    enabled: true",
        f"    tier: {tier}            # 通过 /model add 添加",
    ]
    _insert_model_block(block)
    return {
        "provider": provider,
        "env_var": env_var,
        "reused_key": reused,
        "env_written": env_written,
    }


if __name__ == "__main__":
    # 自检：python -m src.config_loader
    c = load_config()
    print(f"启用模型（{len(c.enabled_models())} 个）：")
    for m in c.enabled_models():
        key_preview = m.api_key[:6] + "***" if m.api_key else "(空)"
        print(f"  - {m.name:<18} [{m.tier:<8}] {m.base_url} key={key_preview}")
    print(f"RAG：{'开启' if c.rag.enabled else '关闭'}")
    print(f"对话目录：{c.dir_path('conversations')}")
