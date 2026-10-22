"""个人画像管理模块。

职责：
1. 读取 profile/个人画像.md（自动剔除 HTML 注释与占位符，注释只给人看、不发给模型）
2. 组装为固定格式的 system 级提示词
3. 两阶段更新画像：先从对话提炼增量 → 再与现有画像合并重写（去重/去矛盾/更新过时项）
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.multi_model_client import MultiModelClient

_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
# 占位符行（如 "- 所在地：（待填写）"、"（待补充）"）：仅模板提示，注入模型前剔除
_PLACEHOLDER_LINE = re.compile(r"^\s*[-*]?\s*(?:[^：:]*[：:]\s*)?（待(填写|补充)）\s*$")

# ---------- 画像框架（6 小节，顺序固定）----------
PROFILE_TITLE = "# 个人画像"
SECTIONS = [
    "基本背景",        # 身份、所在地、职业、技能栈、健康状况等
    "核心目标",        # 短期 / 中期 / 长期目标
    "决策偏好与习惯",  # 回复风格、决策逻辑、工作习惯、禁忌
    "已沉淀经验",      # 已掌握技能、踩坑记录、领域认知
    "关注领域与兴趣",  # 长期关注的话题与兴趣方向
    "重要关系与协作",  # 常合作的人/角色、协作偏好
]
# 供提示词列举小节名
SECTION_NAMES = " / ".join(SECTIONS)

# 画像注入的固定提示词格式
SYSTEM_PREFIX = "你是我的私人助理，请基于以下我的个人背景给出贴合的建议：\n"

# 阶段一：让 AI 从对话中提炼画像"增量"（与现有画像比对，只输出新增）
ENRICH_PROMPT = """任务：从【最近对话】中提炼用户个人画像的**增量**信息。

严格输出规则（违反即作废）：
1. 只输出"新增要点"，每条一行，格式为：- 小节名：事实内容
2. 小节名只能是：{sections}
3. 只写对话中明确体现的事实；禁止猜测、建议、客套、解释
4. 绝对不要输出"当前画像""最近对话"的原文，不要重复已有信息，不要输出标题"# 个人画像"
5. 若没有任何新增信息，只输出四个字：无新增

输出示例：
- 基本背景：所在地为杭州
- 核心目标：今年读完 20 本书

## 当前画像（仅供你判断哪些已存在，禁止原样输出）
{profile}

## 最近对话
{dialogue}

请现在只输出增量要点："""

# 阶段二：把增量合并进现有画像，重写一份完整、干净的画像全文
MERGE_PROMPT = """任务：把【新增要点】合并进【现有画像】，重新输出一份**完整、干净**的个人画像全文。

框架（严格保持以下 6 个小节及其顺序）：
1. 基本背景 —— 身份、所在地、职业、技能栈、健康状况等
2. 核心目标 —— 短期 / 中期 / 长期目标
3. 决策偏好与习惯 —— 回复风格、决策逻辑、工作习惯、禁忌
4. 已沉淀经验 —— 已掌握技能、踩坑记录、领域认知
5. 关注领域与兴趣 —— 长期关注的话题与兴趣方向
6. 重要关系与协作 —— 常合作的人/角色、协作偏好

输出规则（违反即作废）：
1. 只输出画像正文，第一行必须是：{title}
2. 严格按上述 6 个小节顺序，每节以"## 小节名"作标题
3. 每条要点一行，格式为：- 要点内容
4. 合并同类项、去重；新旧信息矛盾时以【新增要点】为准更新旧信息
5. 删除（待填写）（待补充）等占位内容；某节确无内容时保留标题、不写条目
6. 不要输出任何解释、"增量更新"、时间戳，也不要回显本提示词

## 现有画像
{profile}

## 新增要点
{increment}

请现在只输出合并后的完整画像："""

# 模型若回显了 prompt 模板，说明未遵循指令，判为无效
_ENRICH_INVALID_MARKERS = ("## 当前画像", "## 最近对话", "# 个人画像", "当前画像（仅供")
_MERGE_INVALID_MARKERS = ("## 现有画像", "## 新增要点", "请现在只输出", "任务：把")


class ProfileManager:
    def __init__(self, profile_path: Path) -> None:
        self.profile_path = profile_path

    # ---------- 读取 ----------
    def read(self) -> str:
        """读取画像纯文本（剔除 HTML 注释、占位符行）。文件不存在时返回空串。"""
        if not self.profile_path.exists():
            return ""
        text = self.profile_path.read_text(encoding="utf-8")
        text = _COMMENT_PATTERN.sub("", text)
        cleaned: list[str] = []
        blank = False
        for ln in text.splitlines():
            ln = ln.rstrip()
            if _PLACEHOLDER_LINE.match(ln):
                continue
            if not ln.strip():
                if blank:
                    continue  # 去掉因删注释/占位符产生的连续空行（最多保留一个）
                blank = True
            else:
                blank = False
            cleaned.append(ln)
        return "\n".join(cleaned).strip()

    @property
    def system_prompt(self) -> str:
        """每次对话前置注入到 messages 最顶端的 system 消息内容。"""
        content = self.read() or "（用户尚未填写个人画像）"
        return SYSTEM_PREFIX + content

    # ---------- 更新（两阶段）----------
    async def update_from_dialogue(
        self,
        client: "MultiModelClient",
        messages: list[dict],
        model_name: str | None = None,
    ) -> str:
        """两阶段更新画像：提炼增量 → 与现有画像合并重写。

        返回更新所用的增量要点文本；无新增时返回"无新增"。
        """
        candidates = self._candidates(client, model_name)
        profile_before = self.read()

        # 阶段一：提炼增量（把现有画像拼进提示词，让模型只输出新增）
        increment = await self._extract_increment(
            client, messages, profile_before, candidates
        )
        if increment == "无新增":
            return "无新增"

        # 阶段二：与现有画像比对后合并重写全文；失败则退回追加，保证信息不丢
        merged = await self._merge_profile(
            client, profile_before, increment, candidates
        )
        if merged:
            self._write_with_backup(merged)
        else:
            print("  [画像合并] 合并重写未成功，退回追加模式（下次 /profile 会再次合并）")
            self._append_increment(increment)
        return increment

    async def _extract_increment(
        self,
        client: "MultiModelClient",
        messages: list[dict],
        profile: str,
        candidates: list[str],
    ) -> str:
        prompt = ENRICH_PROMPT.format(
            sections=SECTION_NAMES,
            profile=profile or "（暂无）",
            dialogue=self.format_dialogue(messages),
        )
        request = [{"role": "user", "content": prompt}]
        for candidate in candidates:
            out = self._strip_fences(
                (await client.chat_text(request, candidate)).strip()
            )
            if self._is_valid_increment(out):
                return out
            print(f"  [画像提炼] {candidate} 输出未遵循格式，尝试下一个模型...")
        print("  [画像提炼] 各模型均未产出有效增量，本次未更新画像")
        return "无新增"

    async def _merge_profile(
        self,
        client: "MultiModelClient",
        profile: str,
        increment: str,
        candidates: list[str],
    ) -> str:
        prompt = MERGE_PROMPT.format(
            title=PROFILE_TITLE,
            profile=profile or "（暂无）",
            increment=increment,
        )
        request = [{"role": "user", "content": prompt}]
        for candidate in candidates:
            out = self._strip_fences(
                (await client.chat_text(request, candidate)).strip()
            )
            if self._is_valid_merge(out):
                return out
            print(f"  [画像合并] {candidate} 输出未遵循格式，尝试下一个模型...")
        return ""

    def _write_with_backup(self, content: str) -> None:
        """覆盖写回合并后的画像；旧版先备份到 profile/backups/ 以便回滚。"""
        if self.profile_path.exists():
            backup_dir = self.profile_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(
                self.profile_path, backup_dir / f"{self.profile_path.stem}_{stamp}.md"
            )
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(content.strip() + "\n", encoding="utf-8")

    def _append_increment(self, new_content: str) -> None:
        """兜底追加：合并重写失败时，把增量要点规范化去重后追加到文件末尾。

        与画像中已有要点做规范化去重；同一对话可能多次执行 /profile，
        没有这层保护会累积重复条目。
        """
        new_content = new_content.strip()
        if not new_content or new_content == "无新增":
            return

        existing = self.read() if self.profile_path.exists() else ""
        known = {self._norm_line(ln) for ln in existing.splitlines()}

        kept: list[str] = []
        for ln in new_content.splitlines():
            if not ln.strip().startswith("-"):
                continue  # 模型偶发输出的标题/解释行不入文件
            key = self._norm_line(ln)
            if key and key not in known:
                kept.append(ln.strip())
                known.add(key)
        if not kept:
            return

        block = (
            f"\n\n<!-- 增量更新 {datetime.now():%Y-%m-%d %H:%M}（AI 从对话中提炼） -->\n"
            f"{chr(10).join(kept)}\n"
        )
        with self.profile_path.open("a", encoding="utf-8") as f:
            f.write(block)

    # ---------- 校验 ----------
    @staticmethod
    def _strip_fences(text: str) -> str:
        """剥掉模型可能包裹的 ```markdown 代码围栏。"""
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _is_valid_increment(text: str) -> bool:
        """校验提炼结果：回显 prompt 模板或缺少要点行均视为无效。"""
        text = text.strip()
        if not text or text == "无新增":
            return False
        if any(marker in text for marker in _ENRICH_INVALID_MARKERS):
            return False
        # 至少要有一行 "- xxx：yyy" 形式的要点
        return any(
            line.strip().startswith("-") and "：" in line
            for line in text.splitlines()
        )

    @staticmethod
    def _is_valid_merge(text: str) -> bool:
        """校验合并结果：必须以画像标题开头，且 6 个小节齐全。"""
        text = text.strip()
        if not text or any(marker in text for marker in _MERGE_INVALID_MARKERS):
            return False
        if not text.startswith(PROFILE_TITLE):
            return False
        return all(f"## {name}" in text for name in SECTIONS)

    # ---------- 工具 ----------
    @staticmethod
    def _candidates(client: "MultiModelClient", model_name: str | None) -> list[str]:
        """首选模型 + 降级顺序，去重。"""
        names = ([model_name] if model_name else []) + list(client.fallback_order)
        seen: set[str] = set()
        return [n for n in names if n and not (n in seen or seen.add(n))]

    @staticmethod
    def _norm_line(line: str) -> str:
        """规范化一条画像要点用于去重：去前导符号/空白、去标点、转小写。

        保守策略：只合并"写法几乎一致"的重复，不做语义模糊匹配，避免误删。
        """
        s = line.strip().lstrip("-－•·").strip()
        s = re.sub(r"[\s，。、；：:,.!?！？\"'“”‘’（）()【】\[\]]+", "", s)
        return s.lower()

    @staticmethod
    def format_dialogue(messages: list[dict], max_chars: int = 3000) -> str:
        """把 OpenAI 格式消息数组转成给提炼模型看的对话文本。"""
        parts = []
        for m in messages:
            role = "用户" if m["role"] == "user" else "助手"
            content = str(m["content"])[:300]
            parts.append(f"- {role}：{content}")
        text = "\n".join(parts)
        return text[:max_chars]
