"""个人 AI 助手 —— CLI 交互式对话入口。

用法：
    python main.py                 # 启动交互对话（默认模型组）
    python main.py deepseek-chat   # 指定本轮默认调用的模型（多个用逗号分隔）

启动行为：默认进入「主对话」（不存在则自动创建，不会每次启动新建临时对话）。
退出行为：不做自动整理，个人画像仅在手动执行 /profile 时更新。

内置命令：
    /new [主题]      创建新对话（不带主题则提示输入）
    /list            列出所有历史对话
    /switch [主题]   切换到指定对话（/switch <序号> 可按 /list 序号切换）
    /rag on|off      临时开关 RAG 检索
    /models          查看所有模型及启用状态
    /model add       添加任意 OpenAI 兼容模型（自动写 .env 与 config.yaml 并测试）
    /group           查看/切换/创建模型组（接下来的对话使用该组模型并行调用）
    /profile         让 AI 从当前对话提炼信息，与现有画像合并后重写个人画像
    /help            查看帮助
    /exit            退出（对话已实时保存）
"""

from __future__ import annotations

import asyncio
import getpass
import re
import sys
from datetime import datetime
from pathlib import Path

# Windows 终端 UTF-8（中文/特殊符号不乱码）dd
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

from src.collab_pipeline import expand_roster, resolve_roles, run_collab_pipeline
from src.config_loader import add_model, load_config, save_model_group
from src.conversation_manager import (
    MAIN_TOPIC,
    ConversationManager,
)
from src.multi_model_client import MultiModelClient
from src.profile_manager import ProfileManager
from src.rag_engine import get_rag_engine

HELP_TEXT = """\
可用命令：
  /new [主题]      创建新对话
  /list            列出所有历史对话
  /switch [主题]   切换到指定对话（/switch <序号> 可按 /list 序号切换）
  /rag on|off      临时开关 RAG 检索（默认跟随 config.yaml）
  /models          查看模型列表
  /model add       添加任意模型（向导式；只需提供名称/模型ID/base_url/Key）
  /group           查看模型组：/group
    切换组           /group <组名>          （接下来的对话改用该组模型）
    创建组           /group new <组名> [模型1,模型2,...]
                     模型列表缺省时交互式输入；创建后写回 config.yaml 持久化
                     例：/group new 国产组 deepseek-chat,kimi-k2.6,glm-4-flash
  /profile         AI 提炼当前对话，与现有画像比对后合并重写个人画像
                   （模型由 config.yaml 的 conversation.profile_model 指定，默认旗舰）
                   合并前自动备份旧版到 profile/backups/
  /insert <模型名> [对话主题]
                   把 insert_source.md 的内容作为指定模型的回复插入对话
                   （主题缺省=当前对话；来源文件路径在 config.yaml 的 insert_source 配置）
  /summarize       对当前对话最新一轮的多模型回复做决策总结（手动触发）
                   投票规则：少数服从多数 → 平票按 provider 权重排序
                   总结创建为附属对话（conversations/<主题>/summaries/<N>.md）
  /followup <主题> <序号> [追问内容]
                   基于附属对话所绑定的「该主题指定一轮对话」（一个用户提问 +
                   各智能体一次回复）追问细节，追问仅从该轮原始回复取事实
                   不带追问内容 → 进入追问模式；带内容 → 单次追问
  /back            退出附属对话追问模式，返回主对话
  /collab          查看/开关当前对话的四角色协作模式（顾问→裁判→主笔→审计）
    启用           /collab <组名>   仅当前对话生效（写入对话文件头持久化），
                   主笔唯一定稿，审计只挑错，阻断级问题回炉主笔修订
    关闭           /collab off      恢复默认多模型并行模式
                   角色默认定位见 config.yaml 的 call.collab；
                   组内启用模型不足 2 个时拒绝启用（防模型处理自己的语料）
  /help            显示本帮助
  /exit            退出
"""

# ---------- /summarize 使用的提示词 ----------
_SUMMARY_PROMPT = """你是多模型决策总结器。请对以下多个模型针对同一问题给出的回复进行综合总结。

## 总结原则

1. **少数服从多数**：把模型的回复按核心决策建议/立场聚类，相同或相近的归为一组，组内模型数即为"票数"。票数最高的组胜出。
2. **平票处理**：若两组或多组票数相同，按以下 provider 优先级排序（越靠前权重越高）：
{provider_priority}
   权重高的 provider 所在组优先。

## 输入

### 用户问题
{user_question}

### 各模型回复（按模型名 + provider 列出）
{replies_text}

## 输出格式

请严格按以下结构输出：

1. **各模型核心主张**：逐条列出每个模型的决策建议/立场（一句话提炼）
2. **投票统计**：把主张相似的归并后，列出每组的票数、包含的模型及 provider
3. **综合结论**：基于投票原则给出最终综合建议，说明依据（哪组胜出、票数或权重比较结果）
4. **分歧说明**（如有）：若各模型分歧较大，简要说明主要分歧点
"""

# ---------- 附属对话（决策总结追问）的约束 system prompt ----------
# 写入附属对话文件末尾，追问时自动注入到 system 消息
_FOLLOWUP_SYSTEM_PROMPT = """你正在回答用户对"某一轮多模型决策"的追问。
该轮的原始对话（一个用户提问 + 各模型的一次回复）已随本消息一并提供，追问必须严格基于它作答。

你的角色和回答有明确边界：

1. **基于指定的原始对话作答**：所有细节补充必须严格源自消息中提供的该轮「原始对话」（各模型的原始回复）；「决策总结」仅供参考，不得当作细节来源，更不得凭记忆或推断脑补。
2. **只作细节补充，不延伸新决策**：回复可以是对该轮某个建议、观点或事实提供更具体的细节、数据或解释，但不要提出新的决策方向或新的分析角度。
3. **简短回复原则**：每条回复控制在 150-300 字以内，直奔要点，避免客套和冗余展开。
4. **信息不足就明说**：如果该轮各模型原始回复中没有足够信息支撑用户追问的细节，请直说"各模型在这一轮没有展开该点，无法给出更具体细节"，不要猜测或编造。"""


class CliApp:
    def __init__(self, model_names: list[str] | None) -> None:
        self.config = load_config()
        self.profile = ProfileManager(self.config.profile_file())
        self.conversations = ConversationManager(self.config)
        self.client = MultiModelClient(self.config)
        self.model_names = model_names  # None = 配置文件默认
        self.group_name: str | None = "默认组" if model_names is None else "自定义选择"
        self.rag_enabled = self.config.rag.enabled
        # 默认进入主对话：已存在则直接复用，不再每次启动生成"默认对话-时间戳"
        self.topic = MAIN_TOPIC
        self.conversations.create_conversation(MAIN_TOPIC, tags=["主对话"])
        # 附属对话（决策总结追问）状态：None=主对话模式，(topic, index)=追问模式
        self.followup_ctx: tuple[str, int] | None = None

    @property
    def active_models(self) -> list[str]:
        return self.model_names or self.config.call.default_models

    # ---------- 命令 ----------
    def cmd_new(self, arg: str) -> None:
        topic = arg.strip() or input("请输入新对话主题：").strip()
        if not topic:
            print("  主题不能为空")
            return
        self.conversations.create_conversation(topic)
        self.topic = topic
        print(f"  已创建并切换到新对话：{topic}")

    def cmd_list(self) -> None:
        items = self.conversations.list_conversations()
        if not items:
            print("  （暂无历史对话）")
            return
        print(f"  共 {len(items)} 个对话：")
        for i, c in enumerate(items, 1):
            mark = " *" if c.topic == self.topic else ""
            print(f"  {i:>2}. {c.topic}  [{c.created_at}] {c.tags}{mark}")

    def cmd_switch(self, arg: str) -> None:
        target = arg.strip()
        items = self.conversations.list_conversations()
        if not target:
            self.cmd_list()
            target = input("输入序号或主题名切换：").strip()
            if not target:
                return
        # /switch <序号>：按 /list 显示的顺序（从 1 开始）切换
        if target.isdigit():
            idx = int(target) - 1
            if not (0 <= idx < len(items)):
                print(f"  序号超出范围（当前共 {len(items)} 个对话）")
                return
            self.topic = items[idx].topic
            print(f"  已切换到：{self.topic}")
            return
        # /switch <主题名>
        if not self.conversations._main_path(target).exists():
            print(f"  对话不存在：{target}（用 /new 先创建）")
            return
        self.topic = target
        print(f"  已切换到：{target}")

    def cmd_rag(self, arg: str) -> None:
        v = arg.strip().lower()
        if v in ("on", "off"):
            self.rag_enabled = v == "on"
            print(f"  RAG 已{'开启' if self.rag_enabled else '关闭'}")
        else:
            print(f"  当前 RAG：{'开启' if self.rag_enabled else '关闭'}（用法：/rag on 或 /rag off）")

    def cmd_models(self) -> None:
        for m in self.config.models:
            flag = "启用" if m.enabled else "禁用"
            default = "（默认组）" if m.name in self.config.call.default_models else ""
            proxy_tag = ""
            if m.proxy:
                p = m.proxy.strip().lower()
                proxy_tag = "  代理:直连" if p in ("direct", "off", "none") else f"  代理:{m.proxy}"
            print(f"  - {m.name:<18} [{m.tier:<8}] {flag} {default}{proxy_tag}")
        print(f"  本次会话调用：{', '.join(self.active_models)}")

    # ---------- 添加任意模型 ----------
    @staticmethod
    def _ask(prompt: str, default: str = "", required: bool = True) -> str:
        suffix = f"（默认 {default}）" if default else ""
        while True:
            value = input(f"  {prompt}{suffix}：").strip() or default
            if value or not required:
                return value
            print("    该项不能为空")

    @staticmethod
    def _ask_secret(prompt: str) -> str:
        while True:
            try:
                value = getpass.getpass(f"  {prompt}（输入不回显）：").strip()
            except (EOFError, OSError):
                value = input(f"  {prompt}：").strip()
            if value:
                return value
            print("    API Key 不能为空")

    async def cmd_model(self, arg: str) -> None:
        """/model add —— 添加任意 OpenAI 兼容模型。"""
        parts = arg.split()
        if not parts or parts[0] not in ("add", "new"):
            print("  用法：")
            print("    /model add                                          交互式向导")
            print("    /model add <名称> <模型ID> <base_url> [档位] [key] [代理]  一行参数式")
            print("  代理：留空=跟随全局；direct=强制直连；http://127.0.0.1:7890=仅该模型走此代理")
            print("  示例：/model add qwen-plus qwen-plus https://dashscope.aliyuncs.com/compatible-mode/v1 light")
            print("  同提供方（同 base_url 域名）再加模型时自动复用已有 Key，无需重复输入")
            return

        rest = parts[1:]
        try:
            if rest:
                # 参数式：名称 模型ID base_url [tier] [api_key]
                if len(rest) < 3:
                    print("  参数不足：至少需要 名称、模型ID、base_url")
                    return
                name, model_id, base_url = rest[0], rest[1], rest[2]
                tier = rest[3] if len(rest) > 3 else "light"
                api_key = rest[4] if len(rest) > 4 else None
                proxy = rest[5] if len(rest) > 5 else ""
            else:
                # 交互式向导
                print("  添加新模型（OpenAI 兼容接口，Enter 使用默认值）：")
                name = self._ask("本地名称（唯一，如 qwen-plus）")
                model_id = self._ask("上游模型 ID", default=name)
                print("  常见 base_url：")
                print("    阿里通义：https://dashscope.aliyuncs.com/compatible-mode/v1")
                print("    字节豆包：https://ark.cn-beijing.volces.com/api/v3")
                print("    DeepSeek：https://api.deepseek.com/v1")
                base_url = self._ask("base_url")
                tier = self._ask("档位 light/flagship", default="light")
                proxy = self._ask(
                    "代理（留空=跟随全局；direct=强制直连；或 http://IP:端口）",
                    required=False,
                )
                # 同源判断：决定要不要问 Key（与 add_model 内逻辑保持一致）
                from urllib.parse import urlparse

                host = urlparse(base_url.strip().rstrip("/")).hostname or ""
                same = next(
                    (
                        m
                        for m in self.config.models
                        if urlparse(m.base_url).hostname == host
                    ),
                    None,
                )
                if same is not None:
                    print(f"  检测到同提供方模型「{same.name}」，将自动复用其 Key")
                    api_key = None
                else:
                    api_key = self._ask_secret("API Key")

            info = add_model(
                name=name,
                model_name=model_id,
                base_url=base_url,
                api_key=api_key,
                tier=tier,
                proxy=proxy,
            )
        except ValueError as exc:
            print(f"  [添加失败] {exc}")
            return

        print(f"  模型「{name}」已写入 config.yaml（provider={info['provider']}）")
        if info["reused_key"]:
            print(f"  已复用同提供方密钥变量：{info['env_var'] or '（明文配置）'}")
        elif info["env_written"]:
            print(f"  密钥已写入 .env：{info['env_var']}（config.yaml 仅保留引用）")
        if proxy:
            mode = "强制直连（绕过全局代理）" if proxy.lower() in ("direct", "off", "none") else f"仅该模型走代理 {proxy}"
            print(f"  独立代理：{mode}（其他模型不受影响）")

        # 热加载：无需重启即可使用
        old_client = self.client
        self.config = load_config()
        self.client = MultiModelClient(self.config)
        await old_client.aclose()

        # 自动连通性测试
        print(f"  正在调用 {name} 做连通性测试...")
        results = await self.client.chat(
            [{"role": "user", "content": "只回复两个字：收到"}], models=[name]
        )
        r = results[0]
        if r.ok:
            print(f"  [测试成功] {name} 回复正常（{r.tokens} tokens），可直接使用")
        else:
            print(f"  [测试失败] {r.error[:200]}")
            print("  配置已保存；修正密钥/模型 ID 后重启程序再试，或编辑 config.yaml")
            return
        print(f"  对话时指定该模型：直接输入模型名调用，或 /group new 建组后并行调用")

    # ---------- 模型组 ----------
    def _list_groups(self) -> None:
        print(f"  当前使用：{self.group_name or '自定义选择'}")
        print(f"  - 默认组    {self.config.call.default_models}")
        for gname, members in self.config.call.model_groups.items():
            print(f"  - {gname}    {members}")
        print("  切换：/group <组名>    创建：/group new <组名> [模型1,模型2,...]")

    def cmd_group(self, arg: str) -> None:
        arg = arg.strip()
        if not arg:
            self._list_groups()
            return
        parts = arg.split(maxsplit=1)
        if parts[0] in ("new", "add", "create"):
            self._group_new(parts[1].strip() if len(parts) > 1 else "")
        else:
            self._group_switch(arg)

    def _group_switch(self, name: str) -> None:
        if name in ("默认组", "default"):
            self.model_names = None
            self.group_name = "默认组"
            print(f"  已切换到默认组：{', '.join(self.config.call.default_models)}")
            return
        members = self.config.call.model_groups.get(name)
        if members is None:
            print(f"  模型组不存在：{name}")
            self._list_groups()
            return
        self.model_names = list(members)
        self.group_name = name
        print(f"  已切换到模型组「{name}」：{', '.join(members)}")

    def _group_new(self, rest: str) -> None:
        """创建模型组：rest = "组名 [模型1,模型2,...]"，模型列表缺省时交互式输入。"""
        parts = rest.split(maxsplit=1)
        if not parts:
            print("  用法：/group new <组名> [模型1,模型2,...]")
            return
        name = parts[0]
        if name in ("默认组", "default", "new"):
            print(f"  组名不能为保留字：{name}")
            return
        raw = parts[1] if len(parts) > 1 else ""
        if not raw:
            print(f"  可用模型：{', '.join(m.name for m in self.config.models)}")
            raw = input("  输入要归入该组的模型（逗号分隔）：").strip()
        models = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        if not models:
            print("  模型列表不能为空")
            return
        try:
            for n in models:
                self.config.get_model(n)
        except KeyError as e:
            print(f"  {e}")
            return
        save_model_group(name, models)
        self.config.call.model_groups[name] = models  # 同步内存，无需重启
        print(f"  模型组「{name}」已创建并写回 config.yaml：{', '.join(models)}")
        print(f"  输入 /group {name} 使用该组")

    # ---------- 四角色协作模式 ----------
    def _print_collab_status(self) -> None:
        group = self.conversations.get_topic_collab(self.topic)
        if not group:
            print("  当前对话未启用协作模式（默认多模型并行）")
            return
        roster = expand_roster(self.config, [group])
        try:
            r = resolve_roles(roster, self.config.call.collab)
            print(
                f"  当前对话已启用协作模式（组：{group}）\n"
                f"    顾问：{', '.join(r['advisors'])}\n"
                f"    裁判：{r['aggregator']}    主笔：{r['writer']}    审计：{r['auditor']}"
                f"{'（复用裁判，不看共识）' if not r['auditor_sees_consensus'] else ''}"
            )
        except ValueError as e:
            print(f"  当前对话协作模式（组：{group}）当前不可用：{e}")

    def cmd_collab(self, arg: str) -> None:
        arg = arg.strip()
        if not arg or arg in ("status", "状态"):
            self._print_collab_status()
            if not arg:
                groups = ", ".join(self.config.call.model_groups) or "（无）"
                print(f"  可用组：{groups}    启用：/collab <组名>    关闭：/collab off")
            return
        if arg in ("off", "关闭"):
            if self.conversations.get_topic_collab(self.topic):
                self.conversations.set_topic_collab(self.topic, None)
                print("  已关闭当前对话的协作模式，恢复多模型并行")
            else:
                print("  当前对话本就未启用协作模式")
            return
        if self.config.call.model_groups.get(arg) is None:
            print(f"  模型组不存在：{arg}")
            print(f"  可用组：{', '.join(self.config.call.model_groups) or '（无）'}")
            return
        roster = expand_roster(self.config, [arg])
        try:
            r = resolve_roles(roster, self.config.call.collab)
        except ValueError as e:
            print(f"  无法启用：{e}")
            return
        self.conversations.set_topic_collab(self.topic, arg)
        print(
            f"  已为对话「{self.topic}」启用协作模式（组：{arg}），仅该对话生效：\n"
            f"    顾问：{', '.join(r['advisors'])}（多源发散，输出结构化分析）\n"
            f"    裁判：{r['aggregator']}（聚合共识/冲突/硬约束，不聚合自己的分析）\n"
            f"    主笔：{r['writer']}（唯一定稿）\n"
            f"    审计：{r['auditor']}（只挑错；阻断级问题回炉主笔修订，最多 "
            f"{self.config.call.collab.max_revision_rounds} 轮）"
        )

    async def cmd_profile(self) -> None:
        messages = await self.conversations.build_context(self.topic, self.client)
        if not any(x["role"] == "user" for x in messages):
            print("  当前对话还没有内容，暂无可提炼的信息")
            return
        model = self.config.conversation.profile_model
        print(f"  正在用 {model} 提炼画像并与现有画像合并...")
        new_content = await self.profile.update_from_dialogue(
            self.client, messages, model
        )
        if new_content.strip() == "无新增":
            print("  没有发现需要新增的画像信息")
        else:
            print(f"  画像已更新：{self.config.profile_file()}")

    # /insert <模型名> [对话主题]：从固定来源文件读取内容（config.yaml: conversation.insert_source）
    _INSERT_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
    _PLACEHOLDER_MARKERS = ("# 在这里写入要插入的回复内容",)

    def _resolve_insert_source(self) -> Path | None:
        """按优先级查找来源文件；找不到有实质内容的就返回 None（让调用方走自动创建）。"""
        cfg_path = self.config.conversation.insert_source
        cfg_p = Path(cfg_path)

        # 构建候选路径（绝对路径就只一个，相对路径则覆盖多个常见位置）
        candidates: list[Path] = []
        if cfg_p.is_absolute():
            candidates.append(cfg_p)
        else:
            project_root = Path(__file__).resolve().parent
            candidates.extend([
                project_root / cfg_p,              # 项目根目录
                project_root.parent / cfg_p,      # 工作区根目录（D:\AI integrate）
                Path.cwd() / cfg_p,                # 当前工作目录
                Path.home() / cfg_p,               # 用户主目录（兜底）
            ])

        for p in candidates:
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8").strip()
            except UnicodeDecodeError:
                text = p.read_text(encoding="gbk").strip()
            if not text:
                continue
            # 跳过程序自动创建的占位文件（用户没写过内容）
            if any(text.startswith(m) for m in self._PLACEHOLDER_MARKERS):
                continue
            return p
        return None

    def cmd_insert(self, arg: str) -> None:
        """把固定来源文件的内容作为指定模型的回复，插入到指定对话（缺省为当前对话）。"""
        parts = [
            m.group(1) if m.group(1) is not None else m.group(2)
            for m in self._INSERT_TOKEN_RE.finditer(arg)
        ]
        if not parts:
            print("  用法：/insert <模型名> [对话主题]")
            print("  内容来源：编辑 config.yaml 的 conversation.insert_source 指定的文件")
            return
        model_name = parts[0]
        topic = parts[1] if len(parts) >= 2 else self.topic

        path = self._resolve_insert_source()
        if path is None:
            # 所有候选都不存在或只有占位 → 按现有流程在项目根自动创建
            path = Path(__file__).resolve().parent / self.config.conversation.insert_source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# 在这里写入要插入的回复内容\n", encoding="utf-8")
            print(f"  已创建来源文件：{path}")
            print("  请把要插入的内容写入该文件后，重新执行 /insert")
            return

        try:
            content = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            content = path.read_text(encoding="gbk").strip()
        if not content:
            print(f"  来源文件为空：{path}")
            return

        topics = {c.topic for c in self.conversations.list_conversations()}
        if topic not in topics:
            print(f"  对话不存在：{topic}（用 /list 查看现有对话，或先用 /new 创建）")
            return

        self.conversations.add_message(topic, "assistant", content, model_name=model_name)
        print(f"  已从 {path} 读取 {len(content)} 字符 → 对话「{topic}」（标注模型：{model_name}）")

    async def cmd_summarize(self) -> None:
        """手动触发：对当前对话最新一轮的多模型回复做投票总结。"""
        latest = self.conversations.get_latest_round(self.topic)
        if latest is None or not latest:
            print("  当前对话还没有任何内容，无法总结")
            return
        user_block = latest[0]
        # 过滤掉之前的决策总结块（model == "决策总结"），只保留真实模型回复
        assistant_blocks = [
            b for b in latest[1:]
            if (b.get("model") or "") != "决策总结"
        ]
        if user_block["role"] != "user":
            print("  最新一轮异常：没有用户提问，无法总结")
            return
        if len(assistant_blocks) < 2:
            print(f"  最新一轮只有 {len(assistant_blocks)} 个模型回复，至少需要 2 个才能做投票总结")
            return

        # 收集各模型回复，标注 provider
        def _provider_of(model_name: str | None) -> str:
            if not model_name:
                return "未知"
            try:
                return self.config.get_model(model_name).provider
            except (KeyError, StopIteration):
                return "未知"  # /insert 进来的标注模型可能不在配置中，或模型名被截断

        replies_lines: list[str] = []
        for b in assistant_blocks:
            mn = b.get("model") or "（未标注）"
            pv = _provider_of(b.get("model"))
            replies_lines.append(f"### {mn}（provider: {pv}）\n{b['content'].strip()}")
        replies_text = "\n\n".join(replies_lines)

        priority_text = " → ".join(
            f"{i + 1}. {p}" for i, p in enumerate(self.config.conversation.provider_priority)
        )

        prompt = _SUMMARY_PROMPT.format(
            provider_priority=priority_text,
            user_question=user_block["content"].strip(),
            replies_text=replies_text,
        )

        model = self.config.conversation.decision_summary_model
        print(f"  正在用 {model} 总结最近一轮（{len(assistant_blocks)} 个模型回复）...")
        try:
            results = await self.client.chat(
                [{"role": "user", "content": prompt}], models=[model]
            )
        except Exception as exc:
            print(f"  总结调用失败：{exc}")
            return
        res = results[0]
        if not res.ok:
            print(f"  总结模型返回错误：{res.error[:200]}")
            return

        # 创建附属对话：绑定本轮原始对话（1 个用户提问 + 各智能体一次回复）
        existing_summaries = self.conversations.list_summaries(self.topic)
        summary_index = len(existing_summaries) + 1
        source_round = self.conversations.render_round([user_block, *assistant_blocks])
        summary_path = self.conversations.create_summary_conversation(
            parent_topic=self.topic,
            summary_index=summary_index,
            summary_content=res.content.strip(),
            source_round=source_round,
            followup_system_prompt=_FOLLOWUP_SYSTEM_PROMPT,
        )
        print(
            f"  总结完成 → 附属对话 #{summary_index}（{summary_path}），共 {len(res.content)} 字符"
        )

    # ---------- 附属对话（决策总结追问） ----------
    async def cmd_followup(self, arg: str) -> None:
        """进入或使用附属对话追问模式。

        格式：/followup <主题> <序号> [追问内容]
        - 不带追问内容：进入该附属对话的追问模式
        - 带追问内容：发送单次追问
        """
        parts = arg.strip().split(maxsplit=2)
        if len(parts) < 2:
            print("  用法：/followup <主题> <序号> [追问内容]")
            print("  示例：/followup 个人情况 1 那 deepseek 推荐的具体路径是什么？")
            return

        follow_topic = parts[0]
        try:
            follow_index = int(parts[1])
        except ValueError:
            print(f"  序号必须是整数，收到：{parts[1]}")
            return

        summaries = self.conversations.list_summaries(follow_topic)
        if not summaries or follow_index < 1 or follow_index > len(summaries):
            print(
                f"  附属对话不存在：「{follow_topic}」共有 {len(summaries)} 个（序号 1~{len(summaries)}）"
            )
            return

        follow_question = parts[2] if len(parts) > 2 else ""

        if not follow_question:
            # 进入追问模式
            self.followup_ctx = (follow_topic, follow_index)
            print(
                f"  已进入「{follow_topic}」附属对话 #{follow_index} 追问模式"
            )
            print("  输入追问内容 → 回车发送；/back 返回主对话模式")
            return

        # 单次追问
        await self._send_followup(follow_topic, follow_index, follow_question)

    async def _send_followup(
        self, parent_topic: str, summary_index: int, question: str
    ) -> None:
        """执行一次附属对话追问：加载历史 + 约束 system prompt + 发送 + 持久化。"""
        # 1) 持久化用户问题
        self.conversations.add_summary_followup(
            parent_topic, summary_index, "user", question
        )

        # 2) 加载：追问历史 + 约束 prompt + 决策总结原文 + 该轮原始对话原文
        followup_msgs, followup_prompt, summary_text, source_round_text = (
            self.conversations.load_summary_followup_messages(parent_topic, summary_index)
        )

        # 3) 组装 messages：约束 prompt + 该轮原始对话（追问依据）+ 决策总结（参考）
        messages: list[dict] = []
        if followup_prompt:
            parts = [followup_prompt]
            if source_round_text:
                parts.append(
                    "以下是本次追问所基于的「原始对话」——该主题指定一轮：一个用户提问 + 各模型的一次回复。\n"
                    "你的所有细节补充必须严格来自这些原始内容，不得依据决策总结或自身记忆脑补：\n\n"
                    f"{source_round_text}"
                )
            else:
                parts.append(
                    "注意：本附属对话未保存该轮原始对话原文（旧版文件），"
                    "若追问的细节无法从下方决策总结中得到支撑，请如实说明原始回复不可用。"
                )
            if summary_text:
                parts.append(
                    f"以下是该轮的「决策总结」原文（仅供参考，不作为细节来源）：\n\n{summary_text}"
                )
            messages.append({"role": "system", "content": "\n\n---\n\n".join(parts)})
        messages.extend(followup_msgs)

        # 4) 发送前确认（附属对话追问同样是上传），确认后用主对话当前模型组调用
        models = self.active_models
        est = self.client.estimate_tokens(messages)
        if not self._confirm_send([est], len(models), "；附属对话追问"):
            self._cancel_send(
                self.conversations.remove_last_followup_message(
                    parent_topic, summary_index
                )
            )
            return

        print(
            f"  [附属对话 #{summary_index}] 追问中（模型：{', '.join(models)}）..."
        )
        try:
            results = await self.client.chat(messages, models=models)
        except Exception as exc:
            print(f"  追问调用失败：{exc}")
            return

        for r in results:
            if r.ok and r.content:
                print(f"\n  【{r.model}】\n{r.content.strip()}\n")
                self.conversations.add_summary_followup(
                    parent_topic, summary_index, "assistant", r.content, model_name=r.model
                )
            elif not r.ok:
                print(f"\n  【{r.model}】调用失败：{r.error[:200]}\n")

    # ---------- 正常对话 ----------
    # ---------- 发送前确认 ----------
    def _confirm_send(
        self, estimates: list[int], n_models: int, extra: str = ""
    ) -> bool:
        """显示本次上传提示词的 token 估算，用户确认后才真正上传。

        estimates：各模型实际上下文的 token 估算列表（差异化上下文时互不相同）。
        返回 False 表示用户取消，调用方需回滚刚持久化的用户消息。
        """
        if not self.config.conversation.confirm_before_send:
            return True
        lo, hi = min(estimates), max(estimates)
        if lo == hi:
            print(f"  [发送确认] 本次上传提示词约 {lo:,} tokens（{n_models} 个模型{extra}）")
        else:
            print(
                f"  [发送确认] 本次上传提示词约 {lo:,}~{hi:,} tokens"
                f"（{n_models} 个模型按各自上下文计{extra}）"
            )
        try:
            ans = input("  确认上传？(回车=确认 / n=取消): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
            print()
        return ans not in ("n", "no", "否", "取消", "0")

    def _cancel_send(self, rollback: bool) -> None:
        if rollback:
            print("  已取消发送：本次输入未上传，并已从对话中移除")
        else:
            print("  已取消发送：本次输入未上传")

    async def _contexts_by_model(
        self, messages: list[dict], model_names: list[str]
    ) -> dict[str, list[dict]]:
        """为每个模型构造差异化上下文：统一 system 前缀（画像/RAG）+ 该模型视角的
        历史（自己的回复保留全文，其它模型回复截断为要点）。"""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        result: dict[str, list[dict]] = {}
        for name in model_names:
            ctx = await self.conversations.build_context_for_model(
                self.topic, name, self.client
            )
            result[name] = system_msgs + ctx
        return result

    async def handle_chat(self, user_text: str) -> None:
        # 1) 先持久化用户消息，再读回含滑动窗口/摘要的完整上下文
        self.conversations.add_message(self.topic, "user", user_text)
        context = await self.conversations.build_context(self.topic, self.client)

        messages = [{"role": "system", "content": self.profile.system_prompt}]

        # 2) RAG 检索（本地知识库 + 历史对话）
        if self.rag_enabled:
            try:
                engine = get_rag_engine(self.config, force=self.rag_enabled)
                if engine is not None:
                    rag_text = engine.format_context(user_text)
                    if rag_text:
                        messages.append({"role": "system", "content": rag_text})
                        print("  [已注入 RAG 检索上下文]")
            except Exception as exc:
                print(f"  [RAG 不可用，本次降级为普通对话] {exc}")

        messages.extend(context)

        # 3) 当前对话启用协作模式时走四角色流水线，否则多模型并行
        collab_group = self.conversations.get_topic_collab(self.topic)
        if collab_group:
            await self._handle_collab_chat(user_text, messages, collab_group)
            return

        # 4) 多模型并行调用；结果不在终端展示，只持久化到对话 MD，
        #    保持终端只有清晰的用户指令链条（需要查看回复时打开对应 MD 文件）
        #    按模型差异化上下文（B 方案）：每个模型自己的历史回复保留全文，
        #    其它模型的回复截断为要点，降低多模型场景的 token 膨胀
        model_names = expand_roster(self.config, self.active_models)
        messages_by_model = await self._contexts_by_model(messages, model_names)

        # 5) 发送前确认：显示各模型上下文的 token 估算，确认后才上传
        estimates = [self.client.estimate_tokens(messages_by_model[n]) for n in model_names]
        if not self._confirm_send(estimates, len(model_names)):
            self._cancel_send(self.conversations.remove_last_user_message(self.topic))
            return

        results = await self.client.chat(
            messages, models=self.active_models, messages_by_model=messages_by_model
        )
        ok_models: list[str] = []
        for r in results:
            if r.ok:
                # 5) 实时持久化每个模型的回复
                self.conversations.add_message(
                    self.topic, "assistant", r.content, model_name=r.model
                )
                ok_models.append(r.model)
            else:
                # 失败属于异常状态，保留一行简短提示（不输出任何模型正文）
                print(f"  [调用失败] {r.model}：{r.error[:80]}")
        if ok_models:
            print(f"  已收到 {len(ok_models)} 个模型回复并保存：{', '.join(ok_models)}")

    async def _handle_collab_chat(
        self, user_text: str, messages: list[dict], group_name: str
    ) -> None:
        """四角色协作流水线：顾问发散 → 裁判聚合 → 主笔定稿 → 审计回炉。

        主对话只落主笔的唯一最终回复（保持主题整洁）；
        顾问分析/共识/草稿/审计清单等过程产物写入 collab/<N>.md 流水线日志。
        """
        roster = expand_roster(self.config, [group_name])
        if len(roster) < 2:
            print(
                f"  [协作失败] 组「{group_name}」当前可用模型不足 2 个"
                f"（现有：{', '.join(roster) or '无'}），本次未调用。"
                "请检查模型启用状态，或 /collab off 后改用并行模式"
            )
            return

        # 发送前确认：协作流水线是多阶段调用，一次确认后整条流水线执行
        messages_by_model = await self._contexts_by_model(messages, roster)
        estimates = [self.client.estimate_tokens(messages_by_model[n]) for n in roster]
        max_calls = len(roster) + 3 + self.config.call.collab.max_revision_rounds * 2
        if not self._confirm_send(
            estimates, len(roster), f"；协作流水线最多发起 {max_calls} 次调用"
        ):
            self._cancel_send(self.conversations.remove_last_user_message(self.topic))
            return

        outcome = await run_collab_pipeline(
            self.client,
            self.config,
            messages,
            user_text,
            roster,
            messages_by_model=messages_by_model,
        )
        if not outcome.ok:
            print(f"  [协作失败] {outcome.error}")
            return

        # 主笔的唯一最终回复持久化为主对话本轮的助手块
        self.conversations.add_message(
            self.topic, "assistant", outcome.final_content, model_name=outcome.writer
        )

        # 过程产物落流水线日志
        audit_state = {True: "通过", False: "仍有未解决问题（已达修订上限）", None: "未执行"}[
            outcome.audit_passed
        ]
        header = (
            f"# 协作流水线日志（来自「{self.topic}」）\n"
            f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- 模型组：{group_name}（{', '.join(roster)}）\n"
            f"- 分工：主笔 {outcome.writer} / 裁判 {outcome.aggregator} / "
            f"审计 {outcome.auditor} / 顾问=全体\n"
            f"- 用户问题：{user_text[:200]}\n"
            f"- 修订轮数：{outcome.revisions}；审计结论：{audit_state}；"
            f"总 tokens：{outcome.tokens}\n\n---\n\n"
        )
        log_path = self.conversations.add_collab_log(
            self.topic, header + "\n\n".join(outcome.log_parts)
        )
        print(
            f"  [协作完成] 主笔 {outcome.writer} 已定稿并保存"
            f"（审计：{audit_state}；共 {outcome.tokens} tokens）；"
            f"流水线日志：{log_path}"
        )

    # ---------- 主循环 ----------
    async def run(self) -> None:
        print("=" * 60)
        print("  个人 AI 助手（数据 100% 本地存储 · 多模型并行对比）")
        print("=" * 60)
        print(f"  当前对话：{self.topic}")
        print(f"  调用模型：{', '.join(self.active_models)}  （组：{self.group_name}）")
        print(f"  RAG：{'开启' if self.rag_enabled else '关闭'}    画像：{self.config.profile_file().name}")
        print("  输入 /help 查看命令，/exit 退出")
        print("=" * 60)

        while True:
            try:
                # lstrip("\ufeff")：兼容 PowerShell 管道输入首行携带的 UTF-8 BOM
                text = input("\n你: ").strip().lstrip("\ufeff").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == "/exit":
                break
            if text == "/help":
                print(HELP_TEXT)
                continue
            if text == "/list":
                self.cmd_list()
                continue
            if text == "/models":
                self.cmd_models()
                continue
            if text == "/model" or text.startswith("/model "):
                await self.cmd_model(text[6:])
                continue
            if text == "/group" or text.startswith("/group "):
                self.cmd_group(text[6:])
                continue
            if text == "/collab" or text.startswith("/collab "):
                self.cmd_collab(text[7:])
                continue
            if text.startswith("/new"):
                self.cmd_new(text[4:])
                continue
            if text.startswith("/switch"):
                self.cmd_switch(text[7:])
                continue
            if text.startswith("/rag"):
                self.cmd_rag(text[4:])
                continue
            if text == "/profile":
                await self.cmd_profile()
                continue
            if text == "/insert" or text.startswith("/insert "):
                self.cmd_insert(text[7:])
                continue
            if text == "/summarize":
                await self.cmd_summarize()
                continue
            if text == "/followup" or text.startswith("/followup "):
                await self.cmd_followup(text[9:])
                continue
            if text == "/back":
                if self.followup_ctx:
                    self.followup_ctx = None
                    print("  已返回主对话模式")
                else:
                    print("  当前不在追问模式")
                continue
            if text.startswith("/"):
                print(f"  未知命令：{text}，输入 /help 查看可用命令")
                continue

            # 非命令：根据上下文决定发送到哪
            if self.followup_ctx:
                f_topic, f_idx = self.followup_ctx
                await self._send_followup(f_topic, f_idx, text)
            else:
                await self.handle_chat(text)

        await self.client.aclose()
        if self.followup_ctx:
            f_topic, f_idx = self.followup_ctx
            print(f"\n附属对话已保存到：{self.conversations._summaries_dir(f_topic) / f'{f_idx}.md'}")
        print(f"对话已保存到：{self.conversations._main_path(self.topic)}")
        print("再见。")


def main() -> None:
    # 命令行参数解析：python main.py model1,model2
    names = None
    if len(sys.argv) > 1:
        names = [x.strip() for x in sys.argv[1].split(",") if x.strip()]
    asyncio.run(CliApp(names).run())


if __name__ == "__main__":
    main()
