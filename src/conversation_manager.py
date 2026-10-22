"""对话管理模块：对话 MD 文件的读写、上下文组装、滑动窗口 + 自动摘要。

MD 文件格式（强制规范，见项目说明）：

    # 对话主题：xxx
    - 创建时间：2026-10-10 17:00
    - 标签：[标签1, 标签2]

    ---

    ## 早期对话摘要（自动生成，已覆盖前 K 轮）
    ...

    ## 历史对话
    ### 用户（2026-10-10 17:01）
    ...
    ### 助手（2026-10-10 17:01）【deepseek-chat】
    ...

滑动窗口策略：
- 最近 history_turns 轮（1 轮 = 1 次用户输入 + 其后的助手回复）原文保留
- 更早的轮次交给摘要模型压缩，沉淀为"早期对话摘要"
- 原始 MD 永远完整保留（数据不丢），摘要只用于控制发给 API 的 token
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.config_loader import AppConfig

if TYPE_CHECKING:
    from src.multi_model_client import MultiModelClient
    from src.profile_manager import ProfileManager

# 默认主对话：启动时自动进入，退出时与其它对话一起整理
MAIN_TOPIC = "主对话"

# 文件名非法字符
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')
# 消息块：### 用户（时间） 或 ### 助手（时间）【模型名】
# 用户标题行可能带 <span class="user-msg"> 包装（预览端颜色区分），解析需兼容
_BLOCK_PATTERN = re.compile(
    r'^### (?:<span class="user-msg">)?(用户|助手)（([^）]+)）(?:</span>)?'
    r"(?:【([^】]+)】)?[ \t]*\n(.*?)(?=^### |\Z)",
    re.MULTILINE | re.DOTALL,
)
# 用户/助手块头（用于发送确认取消后的回滚删除）
_USER_HEAD_RE = re.compile(
    r'\n### (?:<span class="user-msg">)?用户（[^）]+）(?:</span>)?[^\n]*\n'
)
_ASSISTANT_HEAD_RE = re.compile(
    r'^### (?:<span class="user-msg">)?助手（', re.MULTILINE
)
# 用户消息正文包装标签（写入时加，解析时剥掉，不进入模型上下文）
_USER_SPAN_OPEN = '<span class="user-msg">'
_USER_SPAN_CLOSE = "</span>"
_SUMMARY_HEADER = "## 早期对话摘要（自动生成，已覆盖前 {k} 轮）"
_SUMMARY_PATTERN = re.compile(
    r"## 早期对话摘要（自动生成，已覆盖前 (\d+) 轮）\n(.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)

SUMMARY_PROMPT = """请把更早的对话历史压缩为累计摘要，供后续对话参考。
要求：保留事实、决定、偏好、待办、关键结论；用简洁中文；不要客套；不要超过 400 字。

## 已有摘要
{old_summary}

## 需要并入的更早对话
{old_dialogue}
"""

# 对话文件头部的显示样式块：字号与用户消息颜色集中在此，用户可直接编辑调整。
# 仅对 Markdown 预览生效（VS Code / Trae 预览等），不影响源码与模型输入。
_STYLE_BLOCK = """<style>
/* 对话显示样式（仅 Markdown 预览生效）。可自行修改： */
body { font-size: 1.5em; }        /* 整体字号：默认放大 50%，改数字即可 */
.user-msg { color: #4FC3F7; }     /* 用户消息颜色：亮蓝；助手回复保持默认白色 */
</style>"""


class ConversationMeta:
    def __init__(self, topic: str, created_at: str, tags: list[str], path: Path) -> None:
        self.topic = topic
        self.created_at = created_at
        self.tags = tags
        self.path = path


class ConversationManager:
    def __init__(self, config: AppConfig) -> None:
        self.dir = config.dir_path("conversations")
        self.history_turns = config.conversation.history_turns
        self.history_token_budget = config.conversation.history_token_budget
        self.other_model_snippet_chars = config.conversation.other_model_snippet_chars
        self.summarize_model = config.conversation.summarize_model
        self.dir.mkdir(parents=True, exist_ok=True)
        # 存量迁移：旧版本是扁平 md 文件，自动移入各自主题文件夹
        self._migrate_flat_files()
        # 所有 main.md 自动补样式块（幂等）
        for main_md in self.dir.glob("*/main.md"):
            self._ensure_style_block(main_md)

    # ---------- 迁移（旧版扁平 md → 每主题文件夹） ----------
    def _migrate_flat_files(self) -> None:
        """启动时把根目录下遗留的 *.md 移入同名子文件夹作为 main.md。"""
        for flat in list(self.dir.glob("*.md")):
            topic_dir = self.dir / flat.stem
            topic_dir.mkdir(exist_ok=True)
            target = topic_dir / "main.md"
            if target.exists():
                # 已存在同名 main.md，避免覆盖；重命名旧文件
                target = topic_dir / f"main_migrated_{flat.stat().st_size}.md"
            flat.rename(target)
            print(f"  [迁移] {flat.name} → {topic_dir.name}/main.md")

    @staticmethod
    def _ensure_style_block(path: Path) -> None:
        """给对话文件头部插入显示样式块（字号/用户消息颜色，可手动调整）。"""
        text = path.read_text(encoding="utf-8")
        if "<style>" in text:
            return
        marker = "\n---\n"
        if marker in text:
            text = text.replace(marker, f"\n{_STYLE_BLOCK}\n{marker}", 1)
            path.write_text(text, encoding="utf-8")

    # ---------- 路径工具 ----------
    @staticmethod
    def _safe_filename(topic: str) -> str:
        name = _UNSAFE_FILENAME.sub("_", topic.strip())
        return (name or "未命名对话").strip().rstrip(".")

    def _topic_dir(self, topic: str) -> Path:
        return self.dir / self._safe_filename(topic)

    def _main_path(self, topic: str) -> Path:
        return self._topic_dir(topic) / "main.md"

    def _summaries_dir(self, topic: str) -> Path:
        return self._topic_dir(topic) / "summaries"

    # ---------- 协作模式（按主题持久化于 main.md 头部元信息） ----------
    def get_topic_collab(self, topic: str) -> str | None:
        """读取该主题启用的协作模型组名；未启用返回 None（保持并行模式）。"""
        path = self._main_path(topic)
        if not path.exists():
            return None
        head = path.read_text(encoding="utf-8").partition("\n---\n")[0]
        m = re.search(r"^- 协作：(.+)$", head, re.MULTILINE)
        return m.group(1).strip() if m else None

    def set_topic_collab(self, topic: str, group: str | None) -> None:
        """设置/清除主题的协作模式（group=None 关闭）。写入 main.md 头部「- 协作：」行。"""
        path = self._main_path(topic)
        if not path.exists():
            raise FileNotFoundError(f"对话不存在：{topic}")
        text = path.read_text(encoding="utf-8")
        head, sep, tail = text.partition("\n---\n")
        head = re.sub(r"^- 协作：[^\n]*\n", "", head, flags=re.MULTILINE)
        if group:
            line = f"- 协作：{group}"
            anchor = next(
                (
                    pat
                    for pat in (r"^- 标签：[^\n]*$", r"^- 创建时间：[^\n]*$")
                    if re.search(pat, head, re.MULTILINE)
                ),
                None,
            )
            if anchor:
                head = re.sub(
                    anchor,
                    lambda m: m.group(0) + "\n" + line,
                    head,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                head = head.rstrip("\n") + "\n" + line
        path.write_text(head + sep + tail, encoding="utf-8")

    def add_collab_log(self, topic: str, content: str) -> Path:
        """保存一轮协作流水线的完整日志（顾问分析/共识/草稿/审计/修订）到 collab/<N>.md。"""
        cdir = self._topic_dir(topic) / "collab"
        cdir.mkdir(parents=True, exist_ok=True)
        idx = len(list(cdir.glob("*.md"))) + 1
        path = cdir / f"{idx}.md"
        path.write_text(content, encoding="utf-8")
        return path

    # ---------- 创建 ----------
    def create_conversation(self, topic: str, tags: list[str] | None = None) -> Path:
        """按主题创建对话文件夹 + main.md；已存在则直接返回 main.md 路径。"""
        path = self._main_path(topic)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        tag_text = "[" + ", ".join(tags or []) + "]"
        content = (
            f"# 对话主题：{topic}\n"
            f"- 创建时间：{now}\n"
            f"- 标签：{tag_text}\n\n"
            f"{_STYLE_BLOCK}\n\n"
            f"---\n\n"
            f"{_SUMMARY_HEADER.format(k=0)}\n（暂无）\n\n"
            f"## 历史对话\n"
        )
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def render_round(blocks: list[dict]) -> str:
        """把一轮对话块（1 个用户提问 + 各模型各一次回复）渲染为 Markdown 文本。

        用于附属对话文件中固化「原始对话（本轮）」，保证追问时模型能拿到该轮原文。
        """
        lines: list[str] = []
        for b in blocks:
            ts = b.get("time") or ""
            if b["role"] == "user":
                lines.append(f"### 用户（{ts}）\n{b['content'].strip()}\n")
            else:
                model_tag = f"【{b['model']}】" if b.get("model") else ""
                lines.append(f"### 助手（{ts}）{model_tag}\n{b['content'].strip()}\n")
        return "\n".join(lines).strip()

    def create_summary_conversation(
        self,
        parent_topic: str,
        summary_index: int,
        summary_content: str,
        source_round: str,
        followup_system_prompt: str,
    ) -> Path:
        """创建附属对话（决策总结文件）：独立 md 文件，绑定主题中的指定一轮对话。

        source_round 为该轮原始对话原文（1 个用户提问 + 各智能体的一次回复），
        固化写入文件，追问时随总结一并注入模型上下文，保证追问基于指定对话作答。
        followup_system_prompt 用于附属对话的约束 system prompt（只作细节追问、简短回复）。
        """
        # summary_index 由调用方决定，这里直接用
        path = self._summaries_dir(parent_topic) / f"{summary_index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = (
            f"# 决策总结 #{summary_index}（来自「{parent_topic}」）\n"
            f"- 创建时间：{now}\n"
            f"- 类型：附属对话（基于该主题指定一轮对话的决策总结，用于追问细节）\n\n"
            f"{_STYLE_BLOCK}\n\n"
            f"---\n\n"
            f"## 决策总结\n"
            f"{summary_content.strip()}\n\n"
            f"---\n\n"
            f"## 原始对话（本轮）\n"
            f"{source_round.strip() or '（暂无）'}\n\n"
            f"---\n\n"
            f"## 追问历史\n"
        )
        path.write_text(content, encoding="utf-8")
        # 把 followup_system_prompt 临时存到文件首块的注释区域，或直接作为首条 user 块前的 system 消息？
        # 简洁做法：放在文件末尾注释，供后续追问时读取
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- 追问时使用的 system prompt:\n{followup_system_prompt}\n-->\n")
        return path

    # ---------- 列表 ----------
    def list_conversations(self) -> list[ConversationMeta]:
        result: list[ConversationMeta] = []
        for topic_dir in sorted(self.dir.iterdir()):
            if not topic_dir.is_dir():
                continue
            main_md = topic_dir / "main.md"
            if not main_md.exists():
                continue
            text = main_md.read_text(encoding="utf-8")
            m_topic = re.search(r"^# 对话主题：(.+)$", text, re.MULTILINE)
            m_time = re.search(r"^- 创建时间：(.+)$", text, re.MULTILINE)
            m_tags = re.search(r"^- 标签：\[(.*?)\]$", text, re.MULTILINE)
            topic = m_topic.group(1).strip() if m_topic else topic_dir.name
            created = m_time.group(1).strip() if m_time else "未知时间"
            tags = [t.strip() for t in m_tags.group(1).split(",") if t.strip()] if m_tags else []
            result.append(ConversationMeta(topic, created, tags, main_md))
        return result

    # ---------- 追加消息（只能写到主对话 main.md） ----------
    def add_message(
        self, topic: str, role: str, content: str, model_name: str | None = None
    ) -> None:
        """追加一条消息到主对话 main.md 的末尾（历史对话节内），自动带时间戳。"""
        path = self._main_path(topic)
        if not path.exists():
            raise FileNotFoundError(f"对话不存在，请先 create_conversation：{topic}")
        self._append_block(path, role, content, model_name, target_section="历史对话")

    def add_summary_followup(
        self,
        parent_topic: str,
        summary_index: int,
        role: str,
        content: str,
        model_name: str | None = None,
    ) -> Path:
        """追加一条消息到附属对话（决策总结文件）的末尾。"""
        path = self._summaries_dir(parent_topic) / f"{summary_index}.md"
        if not path.exists():
            raise FileNotFoundError(
                f"附属对话不存在：{parent_topic}/summaries/{summary_index}.md"
            )
        self._append_block(path, role, content, model_name, target_section="追问历史")
        return path

    # ---------- 发送确认取消后的回滚 ----------
    def _remove_last_user_block(self, path: Path) -> bool:
        """删除文件末尾最后一条"刚写入、尚未获得回复"的用户块。

        安全约束：该用户块之后若已存在任何助手块（说明已回复），一律不删除。
        返回是否实际删除。
        """
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8")
        heads = list(_USER_HEAD_RE.finditer(text))
        if not heads:
            return False
        last = heads[-1]
        tail = text[last.start():]
        if _ASSISTANT_HEAD_RE.search(tail):
            return False  # 已有回复，不回滚
        # 用户块之后可能有 HTML 注释（_append_block 插入点），保留之
        comment_rel = tail.find("<!--")
        if comment_rel != -1:
            new_text = text[: last.start()] + tail[comment_rel:]
        else:
            new_text = text[: last.start()]
        path.write_text(new_text, encoding="utf-8")
        return True

    def remove_last_user_message(self, topic: str) -> bool:
        """回滚主对话中刚写入、尚未回复的最后一条用户消息。"""
        return self._remove_last_user_block(self._main_path(topic))

    def remove_last_followup_message(self, parent_topic: str, summary_index: int) -> bool:
        """回滚附属对话中刚写入、尚未回复的最后一条用户追问。"""
        path = self._summaries_dir(parent_topic) / f"{summary_index}.md"
        return self._remove_last_user_block(path)

    def _append_block(
        self,
        path: Path,
        role: str,
        content: str,
        model_name: str | None,
        target_section: str,
    ) -> None:
        """内部：往指定文件的 ## {target_section} 节末尾追加消息块（在 HTML 注释之前）。"""
        if role not in ("user", "assistant"):
            raise ValueError(f"role 只能是 user / assistant，收到：{role}")

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if role == "user":
            lines = content.strip().splitlines() or [""]
            wrapped = "\n".join(
                f"{_USER_SPAN_OPEN}{ln}{_USER_SPAN_CLOSE}" if ln.strip() else ln
                for ln in lines
            )
            block = f"\n### {_USER_SPAN_OPEN}用户（{now}）{_USER_SPAN_CLOSE}\n{wrapped}\n"
        else:
            tag = f"【{model_name}】" if model_name else ""
            block = f"\n### 助手（{now}）{tag}\n{content.strip()}\n"

        text = path.read_text(encoding="utf-8")
        marker = f"## {target_section}"
        idx = text.find(marker)
        if idx == -1:
            # 兜底：直接追加到文件末尾
            with path.open("a", encoding="utf-8") as f:
                f.write(block)
            return

        # 找到 marker 后的 HTML 注释位置（如果有），在它之前插入
        section_start = idx + len(marker)
        html_comment_idx = text.find("<!--", section_start)
        if html_comment_idx == -1:
            # 没有 HTML 注释，追加到文件末尾
            new_text = text + block
        else:
            # 在 HTML 注释之前插入
            new_text = text[:html_comment_idx] + block + "\n" + text[html_comment_idx:]
        path.write_text(new_text, encoding="utf-8")

    # ---------- 解析 ----------
    def _parse_blocks(self, text: str) -> list[dict]:
        """从 MD 解析出消息块（含时间戳与模型名）。"""
        # 只解析"历史对话"节，避免摘要节被误读
        hist = text.split("## 历史对话", 1)[-1]
        blocks: list[dict] = []
        for m in _BLOCK_PATTERN.finditer(hist):
            kind, ts, model, content = m.groups()
            if kind == "用户":
                # 剥掉显示用包装标签，保证进入模型上下文的是纯文本
                content = content.replace(_USER_SPAN_OPEN, "").replace(
                    _USER_SPAN_CLOSE, ""
                )
            blocks.append(
                {
                    "role": "user" if kind == "用户" else "assistant",
                    "content": content.strip(),
                    "time": ts.strip(),
                    "model": model,
                }
            )
        return blocks

    @staticmethod
    def _to_openai_messages(blocks: list[dict]) -> list[dict]:
        """把原始块转成严格的 OpenAI messages 数组。

        多模型并行时同一轮会有连续多个"助手"块；组装上下文时每组只保留
        第一个助手回复（代表本轮已作答），避免冗余并保持 user/assistant 交替。
        """
        messages: list[dict] = []
        for b in blocks:
            if b["role"] == "user":
                messages.append({"role": "user", "content": b["content"]})
            else:
                if messages and messages[-1]["role"] == "assistant":
                    continue  # 同轮并行模型的其它回复，不进上下文
                messages.append({"role": "assistant", "content": b["content"]})
        return messages

    def load_messages(self, topic: str) -> list[dict]:
        """解析对话 MD，输出 OpenAI 格式 messages（不含画像，不含滑动窗口摘要）。"""
        path = self._main_path(topic)
        if not path.exists():
            raise FileNotFoundError(f"对话不存在：{topic}")
        blocks = self._parse_blocks(path.read_text(encoding="utf-8"))
        return self._to_openai_messages(blocks)

    def get_latest_round(self, topic: str) -> list[dict] | None:
        """返回该对话最近一轮的消息列表：[user_block, assistant_block*, ...]

        一轮 = 一个 user 块 + 紧随其后的所有 assistant 块（直到下一个 user 出现）。
        若无任何 user 块（对话为空），返回 None。
        """
        path = self._main_path(topic)
        if not path.exists():
            return None
        blocks = self._parse_blocks(path.read_text(encoding="utf-8"))
        turns: list[list[dict]] = []
        current: list[dict] = []
        for b in blocks:
            if b["role"] == "user":
                if current:
                    turns.append(current)
                current = [b]
            elif current:
                current.append(b)
        if current:
            turns.append(current)
        return turns[-1] if turns else None

    # ---------- 摘要读写 ----------
    def _read_summary(self, text: str) -> tuple[str, int]:
        m = _SUMMARY_PATTERN.search(text)
        if not m:
            return "", 0
        return m.group(2).strip(), int(m.group(1))

    def _write_summary(self, text: str, summary: str, covered_turns: int) -> str:
        header = _SUMMARY_HEADER.format(k=covered_turns)
        new_section = f"{header}\n{summary.strip() or '（暂无）'}\n"
        if _SUMMARY_PATTERN.search(text):
            # 用函数式替换，避免摘要文本中的反斜杠被当作正则反向引用
            return _SUMMARY_PATTERN.sub(
                lambda _m: new_section + "\n", text, count=1
            )
        # 首次生成：插在 "## 历史对话" 之前
        return text.replace("## 历史对话", new_section + "\n## 历史对话", 1)

    @staticmethod
    def _split_turns(blocks: list[dict]) -> list[list[dict]]:
        """按 user 块把消息切分成"轮"（1 轮 = 1 个 user 块 + 紧随的 assistant 块）。"""
        turns: list[list[dict]] = []
        current: list[dict] = []
        for b in blocks:
            if b["role"] == "user":
                if current:
                    turns.append(current)
                current = [b]
            elif current:
                current.append(b)
        if current:
            turns.append(current)
        return turns

    @staticmethod
    def _est_tokens(text: str) -> int:
        """粗略 token 估算（中文混合文本约 1.5 字符/token），用于窗口预算。"""
        return max(1, int(len(text) / 1.5))

    def _turn_cost(self, turn: list[dict], target_model: str | None = None) -> int:
        """一轮的发送成本（token 估算）：user 全计；assistant 与实际发送口径一致——
        统一口径只计第一个块；target 口径计该模型自己的块（无则计截断要点）。"""
        users = [b for b in turn if b["role"] == "user"]
        assts = [b for b in turn if b["role"] == "assistant"]
        cost = sum(self._est_tokens(b["content"]) for b in users)
        if not assts:
            return cost
        if target_model:
            own = next((b for b in assts if b.get("model") == target_model), None)
            if own is not None:
                return cost + self._est_tokens(own["content"])
            return cost + self._est_tokens(
                assts[0]["content"][: self.other_model_snippet_chars]
            )
        return cost + self._est_tokens(assts[0]["content"])

    def _select_recent_turns(
        self, turns: list[list[dict]], target_model: str | None = None
    ) -> list[list[dict]]:
        """A 方案：预算制窗口。从最新轮向前累加，超出 history_token_budget 即止，
        同时不超过 history_turns 硬上限；至少保留最近 1 轮（保证当前问题在上下文中）。
        target_model 决定成本口径：自己的长回复会收窄自己的窗口（体积被预算压住）。"""
        budget = self.history_token_budget
        max_turns = self.history_turns
        selected: list[list[dict]] = []
        used = 0
        for turn in reversed(turns):
            cost = self._turn_cost(turn, target_model)
            if selected and (used + cost > budget or len(selected) >= max_turns):
                break
            selected.insert(0, turn)
            used += cost
        return selected

    def _to_messages_for_model(
        self, blocks: list[dict], target_model: str
    ) -> list[dict]:
        """B 方案：按目标模型差异化转换。

        每轮优先保留目标模型自己的回复（全文，模型最受益于自己的推理链）；
        该轮它没有回复时，保留第一个 assistant 块但截断为前 N 字要点——
        其它模型的长篇回复对它参考价值有限，只占体积。
        """
        snippet = self.other_model_snippet_chars
        messages: list[dict] = []
        for turn in self._split_turns(blocks):
            users = [b for b in turn if b["role"] == "user"]
            assts = [b for b in turn if b["role"] == "assistant"]
            for b in users:
                messages.append({"role": "user", "content": b["content"]})
            if not assts:
                continue
            own = next((b for b in assts if b.get("model") == target_model), None)
            if own is not None:
                messages.append({"role": "assistant", "content": own["content"]})
            else:
                content = assts[0]["content"]
                if snippet >= 0 and len(content) > snippet:
                    content = (
                        content[:snippet]
                        + f"\n…（其余略；本条为 {assts[0].get('model', '其它模型')} 的回复，仅供要点参考）"
                    )
                messages.append({"role": "assistant", "content": content})
        return messages

    # ---------- 滑动窗口组装 ----------
    async def _build(
        self,
        topic: str,
        client: "MultiModelClient | None",
        target_model: str | None,
    ) -> list[dict]:
        """核心组装：早期摘要(system) + 预算窗口内最近轮原文（可选按目标模型差异化）。

        摘要覆盖区间按统一口径（原文成本）推进，保证各模型前缀一致（缓存友好）；
        target_model 只影响窗口内 assistant 回复的取舍与截断（B 方案）。
        """
        path = self._main_path(topic)
        text = path.read_text(encoding="utf-8")
        blocks = self._parse_blocks(text)
        old_summary, covered = self._read_summary(text)
        turns = self._split_turns(blocks)
        # 摘要覆盖区间按统一口径（原文成本）推进：各模型共享同一份摘要，
        # 且多模型循环调用时 pending_end 一致，不会重复触发摘要生成
        unified_selected = self._select_recent_turns(turns)
        # 发送窗口按目标模型的实际成本选择（自己的长回复会收窄窗口）
        selected = (
            self._select_recent_turns(turns, target_model)
            if target_model
            else unified_selected
        )

        # 需要新纳入摘要的轮次：[已覆盖, 总轮数-统一窗口)
        pending_end = max(0, len(turns) - len(unified_selected))
        pending = turns[covered:pending_end]

        summary = old_summary
        if pending and client is not None and self.summarize_model:
            dialogue_lines = []
            for turn in pending:
                for b in turn:
                    who = "用户" if b["role"] == "user" else "助手"
                    model_tag = f"（{b['model']}）" if b.get("model") else ""
                    dialogue_lines.append(f"- {who}{model_tag}：{b['content'][:500]}")
            try:
                summary = await client.chat_text(
                    [
                        {
                            "role": "user",
                            "content": SUMMARY_PROMPT.format(
                                old_summary=old_summary or "（暂无）",
                                old_dialogue="\n".join(dialogue_lines),
                            ),
                        }
                    ],
                    model_name=self.summarize_model,
                )
                covered = pending_end
                path.write_text(
                    self._write_summary(text, summary, covered), encoding="utf-8"
                )
            except Exception as exc:
                # 摘要失败不阻断对话：退回全量原文（受模型自身上下文限制）
                print(f"  [摘要生成失败，本次使用原文兜底] {exc}")
                selected = turns
                summary = old_summary

        messages: list[dict] = []
        if summary and summary != "（暂无）":
            messages.append(
                {
                    "role": "system",
                    "content": f"以下是你与用户更早对话的累计摘要，请在回答时参考：\n{summary}",
                }
            )
        recent_blocks = [b for turn in selected for b in turn]
        if target_model:
            messages.extend(self._to_messages_for_model(recent_blocks, target_model))
        else:
            messages.extend(self._to_openai_messages(recent_blocks))
        return messages

    async def build_context(
        self, topic: str, client: "MultiModelClient | None" = None
    ) -> list[dict]:
        """组装发给模型的上下文：早期摘要(system) + 预算窗口内最近轮原文。"""
        return await self._build(topic, client, None)

    async def build_context_for_model(
        self, topic: str, target_model: str, client: "MultiModelClient | None" = None
    ) -> list[dict]:
        """按目标模型差异化组装：该模型自己的历史回复保留全文，
        其它模型的回复截断为要点（other_model_snippet_chars）。"""
        return await self._build(topic, client, target_model)

    # ---------- 附属对话（决策总结）辅助 ----------
    def list_summaries(self, parent_topic: str) -> list[Path]:
        """列出指定主题的所有附属对话文件（按序号升序）。"""
        sdir = self._summaries_dir(parent_topic)
        if not sdir.exists():
            return []
        return sorted(sdir.glob("*.md"))

    @staticmethod
    def _extract_section(text: str, heading: str) -> str:
        """提取 '## {heading}' 节的内容。

        终止符是"下一个已知节标题"（决策总结 / 原始对话（本轮）/ 追问历史），
        而不是 '---'：因为总结正文与模型回复内部都可能自带 '---' 分隔线，
        用它当终止符会把本节内容提前截断。
        """
        siblings = ("决策总结", "原始对话（本轮）", "追问历史")
        m = re.search(rf"^## {re.escape(heading)}[ \t]*\n", text, re.MULTILINE)
        if not m:
            return ""
        rest = text[m.end():]
        stop: int | None = None
        for h in siblings:
            if h == heading:
                continue
            mm = re.search(rf"^## {re.escape(h)}[ \t]*$", rest, re.MULTILINE)
            if mm and (stop is None or mm.start() < stop):
                stop = mm.start()
        if stop is not None:
            rest = rest[:stop]
        return rest.strip()

    def load_summary_followup_messages(
        self, parent_topic: str, summary_index: int
    ) -> tuple[list[dict], str, str, str]:
        """加载附属对话的完整上下文。

        返回 (messages, followup_system_prompt, decision_summary_text, source_round_text)：
        - messages：## 追问历史 节里已有的 user/assistant 块（空数组表示首次追问）
        - followup_system_prompt：文件末尾 HTML 注释中的追问约束 prompt
        - decision_summary_text：## 决策总结 节的原文，供模型参考
        - source_round_text：## 原始对话（本轮）节的原文（该轮用户提问 + 各智能体回复），
          追问必须基于它作答
        """
        path = self._summaries_dir(parent_topic) / f"{summary_index}.md"
        if not path.exists():
            raise FileNotFoundError(
                f"附属对话不存在：{parent_topic}/summaries/{summary_index}.md"
            )
        text = path.read_text(encoding="utf-8")

        # 取 ## 追问历史 节（不含决策总结节）
        if "## 追问历史" in text:
            hist = text.split("## 追问历史", 1)[-1]
            hist = hist.split("<!--", 1)[0]
        else:
            hist = ""
        blocks = self._parse_blocks(hist) if hist.strip() else []
        messages = self._to_openai_messages(blocks)

        # 提取 ## 决策总结 / ## 原始对话（本轮）两节原文
        decision_summary_text = self._extract_section(text, "决策总结")
        source_round_text = self._extract_section(text, "原始对话（本轮）")

        # 提取 followup_system_prompt
        prompt = ""
        m = re.search(
            r"<!-- 追问时使用的 system prompt:\s*\n(.*?)-->",
            text,
            re.DOTALL,
        )
        if m:
            prompt = m.group(1).strip()

        return messages, prompt, decision_summary_text, source_round_text
