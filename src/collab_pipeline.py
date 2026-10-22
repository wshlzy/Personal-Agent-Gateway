"""四角色协作流水线：顾问 → 裁判 → 主笔 → 审计（可选回炉修订）。

角色分工（对应用户约定的多模型协作模式）：
1. 顾问（组内全部模型）：多源发散，输出结构化 JSON
   （目标理解/关键约束/风险清单/可选路径/个人偏好匹配点/资源需求），不写最终计划
2. 裁判（1 个模型）：汇总共识、冲突点及适用条件、硬约束、裁决建议，不写最终计划
3. 主笔（1 个模型）：唯一负责定稿，基于共识约束 + 对话上下文输出唯一最终回复
4. 审计（1 个模型）：只挑错不重写，输出问题清单（阻断/建议）；
   发现阻断级问题时回到主笔修订，而不是让审计另起炉灶

防「同一模型生成的语料直接由该模型自己处理」：
- 裁判的输入剔除其本人署名的顾问分析
- 主笔 ≠ 裁判（不基于自己聚合的共识定稿，由角色解析保证）
- 审计 ≠ 主笔（不审自己的稿，由角色解析保证）
- 仅 2 模型组时审计复用裁判，此时审计输入不含共识（共识出自裁判本人）
- 回炉修订仍由主笔执行：作者改自己的稿属同角色延续，不视为跨角色自处理

启用方式：CLI /collab <组名>（按主题持久化）或 API 请求 collab=true；
未启用时保持原有多模型并行模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_loader import AppConfig, CollabConfig
    from src.multi_model_client import MultiModelClient


# ---------- 各角色提示词 ----------
_ADVISOR_PROMPT = """【协作指令｜顾问】你是多模型协作中的顾问。请只针对本次对话最后的用户问题，结合对话历史与用户画像独立分析。

只输出一个 JSON 对象，不要输出任何其它文字或代码块标记，字段固定为：
{"目标理解": "…", "关键约束": ["…"], "风险清单": ["…"], "可选路径": ["…"], "个人偏好匹配点": ["…"], "资源需求": ["…"]}

不要给出最终计划或定稿——定稿由主笔模型负责。"""

_AGGREGATOR_PROMPT = """【协作指令｜裁判】以下是多位顾问针对用户问题的独立分析。请汇总形成共识约束，供主笔定稿使用。

## 用户问题
{question}

## 顾问分析（已剔除你本人署名的分析）
{advisors_text}

请严格按以下四节输出（Markdown）：
1. **共识**：多数顾问一致的建议与判断
2. **冲突点**：顾问间的分歧及各观点适用的条件
3. **硬约束**：最终回复必须满足的硬性约束（源自用户画像、关键约束与风险清单）
4. **裁决建议**：对每个冲突点的取舍与优先级

不要写最终计划——定稿由主笔负责。"""

_WRITER_PROMPT = """【协作指令｜主笔】你是多模型协作中唯一负责定稿的主笔。请基于以下裁判共识约束，对上面的用户问题给出唯一的最终回复。

## 共识约束（裁判汇总）
{consensus}

要求：
- 必须满足「硬约束」全部条目；冲突取舍遵循「裁决建议」
- 直接输出最终回复正文（用户将直接阅读），不要输出角色说明、协作过程或 JSON
- 与对话历史及用户画像保持一致"""

_REVISE_PROMPT = """【协作指令｜审计回炉】审计发现以下问题，请修订你的最终回复：

{audit_text}

要求：所有「阻断」级问题必须修正；「建议」级可自行判断。直接输出修订后的完整最终回复正文，不要输出修订说明。"""

_AUDITOR_PROMPT = """【协作指令｜审计】请只挑错，不要重写回复，不要另起炉灶。

## 用户问题
{question}

## 待审草稿（主笔产出）
{draft}

{consensus_block}
## 审计维度
1. 时间是否冲突
2. 资源是否超限（金钱/时间/精力）
3. 是否违背用户约束或画像
4. 是否有遗漏的重要风险
5. 是否与用户历史信息矛盾

## 输出格式（严格）
- 逐条列出问题，每条一行，格式：- [阻断] 或 - [建议] + 问题描述
- 「阻断」= 违背硬约束/事实错误/资源明显超限；「建议」= 可选优化
- 最后一行必须是：审计结论：需修订 或 审计结论：通过"""


def expand_roster(config: "AppConfig", names: list[str] | None) -> list[str]:
    """把模型名/组名列表展开为去重后的启用模型名列表（组只展开一层）。"""
    ordered: list[str] = []
    for n in names or config.call.default_models:
        for m in config.call.model_groups.get(n, [n]):
            try:
                if config.get_model(m).enabled and m not in ordered:
                    ordered.append(m)
            except KeyError:
                print(f"  [协作] 跳过未配置的模型：{m}")
    return ordered


def resolve_roles(roster: list[str], collab: "CollabConfig") -> dict:
    """解析四角色分工；不足 2 个模型时抛 ValueError（无法避免跨角色自处理）。"""
    if len(roster) < 2:
        raise ValueError(
            f"协作模式至少需要 2 个启用模型（否则无法避免模型处理自己的语料），当前仅：{roster}"
        )
    writer = collab.writer if collab.writer in roster else roster[0]
    # 裁判必须 ≠ 主笔
    if collab.aggregator in roster and collab.aggregator != writer:
        aggregator = collab.aggregator
    else:
        aggregator = next(m for m in roster if m != writer)
    if len(roster) >= 3:
        # 审计必须 ∉ {主笔, 裁判}
        candidates = [m for m in roster if m not in (writer, aggregator)]
        auditor = collab.auditor if collab.auditor in candidates else candidates[0]
        sees_consensus = True
    else:
        # 2 模型组：审计复用裁判，审计输入不含共识（共识出自裁判本人）
        auditor = aggregator
        sees_consensus = False
    return {
        "writer": writer,
        "aggregator": aggregator,
        "auditor": auditor,
        "advisors": list(roster),
        "auditor_sees_consensus": sees_consensus,
    }


def _parse_audit(text: str) -> bool:
    """解析审计结论；返回是否需要修订。缺少结论行时视为通过（防死循环），并提示。"""
    for line in reversed(text.strip().splitlines()):
        s = line.strip()
        if "审计结论" in s:
            return "需修订" in s
    print("  [协作] 审计输出缺少结论行，视为通过")
    return False


@dataclass
class CollabOutcome:
    """协作流水线结果。"""

    ok: bool = False
    error: str = ""
    final_content: str = ""
    writer: str = ""
    aggregator: str = ""
    auditor: str = ""
    advisors: list[str] = field(default_factory=list)
    revisions: int = 0
    audit_passed: bool | None = None  # None=审计未执行（调用失败被跳过）
    tokens: int = 0
    log_parts: list[str] = field(default_factory=list)  # 附属日志的各节（Markdown）


async def run_collab_pipeline(
    client: "MultiModelClient",
    config: "AppConfig",
    base_messages: list[dict],
    user_question: str,
    roster: list[str],
    messages_by_model: dict[str, list[dict]] | None = None,
) -> CollabOutcome:
    """执行一次四角色协作流水线。

    base_messages：已组装好的上下文（画像 system + 摘要/历史，末条为用户问题）。
    messages_by_model：可选，按模型名提供差异化上下文（历史中该模型自己的回复
    保留全文、其它模型回复截断为要点）；未提供的模型回退到 base_messages。
    返回 CollabOutcome；final_content 为主笔的唯一定稿，由调用方持久化与展示。
    """
    outcome = CollabOutcome()
    roles = resolve_roles(roster, config.call.collab)
    outcome.writer = roles["writer"]
    outcome.aggregator = roles["aggregator"]
    outcome.auditor = roles["auditor"]
    outcome.advisors = roles["advisors"]

    def _ctx(model: str) -> tuple[list[dict], list[dict]]:
        """该模型自己的上下文，拆成 (system 消息, 对话历史) 两部分。"""
        base = (messages_by_model or {}).get(model, base_messages)
        return (
            [m for m in base if m.get("role") == "system"],
            [m for m in base if m.get("role") != "system"],
        )

    system_msgs = [m for m in base_messages if m.get("role") == "system"]
    history = [m for m in base_messages if m.get("role") != "system"]

    # ---------- 1) 顾问：组内全部模型并行发散 ----------
    print(f"  [协作 1/4] 顾问发散（{len(roles['advisors'])} 个模型并行：{', '.join(roles['advisors'])}）")
    adv_msgs_by_model: dict[str, list[dict]] = {}
    for name in roles["advisors"]:
        sys_m, hist_m = _ctx(name)
        adv_msgs_by_model[name] = sys_m + hist_m + [
            {"role": "user", "content": _ADVISOR_PROMPT}
        ]
    results = await client.chat(
        adv_msgs_by_model.get(roles["advisors"][0], system_msgs + history + [
            {"role": "user", "content": _ADVISOR_PROMPT}
        ]),
        models=roles["advisors"],
        messages_by_model=adv_msgs_by_model,
    )
    advisor_parts: list[str] = []
    ok_advisors = 0
    for r in results:
        outcome.tokens += r.tokens
        if r.ok and r.content.strip():
            ok_advisors += 1
            advisor_parts.append(f"### 顾问：{r.model}\n{r.content.strip()}")
        else:
            err = r.error or "空回复"
            print(f"  [协作] 顾问 {r.model} 失败：{err[:80]}")
            advisor_parts.append(f"### 顾问：{r.model}\n（调用失败：{err[:200]}）")
    outcome.log_parts.append("## 顾问分析\n" + "\n\n".join(advisor_parts))
    if not ok_advisors:
        outcome.error = "所有顾问模型均调用失败，无法进入聚合阶段"
        return outcome

    advisors_text = "\n\n".join(advisor_parts)
    # 裁判输入剔除其本人署名的分析（防自处理）
    own_header = f"### 顾问：{roles['aggregator']}\n"
    fed_parts = [p for p in advisor_parts if not p.startswith(own_header)]

    # ---------- 2) 裁判：聚合共识约束 ----------
    print(f"  [协作 2/4] 裁判聚合（{roles['aggregator']}，已剔除其自身分析）")
    agg_sys, _ = _ctx(roles["aggregator"])
    agg_msgs = agg_sys + [
        {
            "role": "user",
            "content": _AGGREGATOR_PROMPT.format(
                question=user_question, advisors_text="\n\n".join(fed_parts)
            ),
        }
    ]
    agg_res = (await client.chat(agg_msgs, models=[roles["aggregator"]]))[0]
    outcome.tokens += agg_res.tokens
    if agg_res.ok and agg_res.content.strip():
        consensus = agg_res.content.strip()
    else:
        print(f"  [协作] 裁判失败：{(agg_res.error or '空回复')[:80]}，主笔将直接基于顾问原始分析")
        consensus = (
            "（裁判聚合失败，以下为顾问原始分析，请主笔自行提炼共识与硬约束）\n\n"
            + advisors_text
        )
    outcome.log_parts.append(f"## 共识约束（裁判：{roles['aggregator']}）\n{consensus}")

    # ---------- 3) 主笔：唯一定稿 ----------
    print(f"  [协作 3/4] 主笔定稿（{roles['writer']}）")
    w_sys, w_hist = _ctx(roles["writer"])
    writer_msgs = w_sys + w_hist + [
        {"role": "user", "content": _WRITER_PROMPT.format(consensus=consensus)}
    ]
    w_res = (await client.chat(writer_msgs, models=[roles["writer"]]))[0]
    outcome.tokens += w_res.tokens
    if not (w_res.ok and w_res.content.strip()):
        outcome.error = f"主笔模型 {roles['writer']} 调用失败：{w_res.error or '空回复'}"
        return outcome
    draft = w_res.content.strip()
    outcome.log_parts.append(f"## 初稿（主笔：{roles['writer']}）\n{draft}")

    # ---------- 4) 审计：只挑错；阻断级回炉主笔修订 ----------
    async def _audit(content: str) -> tuple[str, bool, bool]:
        """返回 (审计原文, 是否需修订, 是否执行成功)。"""
        consensus_block = (
            f"## 裁判共识约束（供对照）\n{consensus}\n\n"
            if roles["auditor_sees_consensus"]
            else ""
        )
        a_sys, _ = _ctx(roles["auditor"])
        audit_msgs = a_sys + [
            {
                "role": "user",
                "content": _AUDITOR_PROMPT.format(
                    question=user_question,
                    draft=content,
                    consensus_block=consensus_block,
                ),
            }
        ]
        a_res = (await client.chat(audit_msgs, models=[roles["auditor"]]))[0]
        outcome.tokens += a_res.tokens
        if not (a_res.ok and a_res.content.strip()):
            print(f"  [协作] 审计失败：{(a_res.error or '空回复')[:80]}，跳过本次审计")
            return "", False, False
        text = a_res.content.strip()
        return text, _parse_audit(text), True

    print(f"  [协作 4/4] 审计（{roles['auditor']}，只挑错不重写）")
    audit_text, need_revise, audit_ok = await _audit(draft)
    if audit_ok:
        outcome.log_parts.append(f"## 审计 #1（审计：{roles['auditor']}）\n{audit_text}")

    rounds = 0
    while audit_ok and need_revise and rounds < config.call.collab.max_revision_rounds:
        rounds += 1
        print(f"  [协作] 审计发现需修订问题，回炉主笔修订（第 {rounds} 轮）")
        revise_msgs = writer_msgs + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content": _REVISE_PROMPT.format(audit_text=audit_text)},
        ]
        r_res = (await client.chat(revise_msgs, models=[roles["writer"]]))[0]
        outcome.tokens += r_res.tokens
        if not (r_res.ok and r_res.content.strip()):
            print(f"  [协作] 修订失败：{(r_res.error or '空回复')[:80]}，保留上一版草稿")
            break
        draft = r_res.content.strip()
        outcome.log_parts.append(f"## 修订稿 #{rounds}（主笔）\n{draft}")
        audit_text, need_revise, audit_ok = await _audit(draft)
        if audit_ok:
            outcome.log_parts.append(
                f"## 审计 #{rounds + 1}（审计：{roles['auditor']}）\n{audit_text}"
            )

    outcome.final_content = draft
    outcome.revisions = rounds
    outcome.audit_passed = (not need_revise) if audit_ok else None
    outcome.ok = True
    return outcome
