"""邮件草稿 Skill（FR-03，v0.3 从占位升级为完整实现）。

安全约束（Q-02，100% 满足）：
- 只生成邮件草稿，不集成真实邮箱，不出现任何自动发送行为。
- 主题/收件人/落款/要点只来自用户输入，缺失标"待确认"，不编造。
"""

import re
from datetime import datetime, timezone
from typing import Any

from app.llm import get_llm
from app.schemas import AgentState, EmailDraft, PENDING
from app.skills.base import Skill

_LLM_PROMPT = (
    "你是邮件草稿生成器。根据用户提供的背景与要点生成一封得体的中文邮件草稿。"
    "严格遵守：主题、收件人、落款、要点只使用用户明确给出的信息，"
    "用户未提供的字段一律填 \"待确认\"，不得编造收件人、日期或承诺。"
    "输出 JSON：{\"subject\": \"...\", \"recipient\": \"...\", \"sender\": \"...\", "
    "\"key_points\": [\"...\"]}，正文内容由系统按要点组装，你不负责写完整正文。"
)

_FIELD_PATTERNS = {
    "subject": re.compile(r"(主题|标题|关于)[：:]\s*([^\n，。；]+)"),
    "recipient": re.compile(r"(收件人|发给|致|给|称呼)[：:]\s*([^\n，。；]+)"),
    "sender": re.compile(r"(落款|署名|签名|此致)[：:]\s*([^\n，。；]+)"),
}
_POINTS_HEADER = re.compile(r"(要点|内容|正文|说明)[：:]")

_GREETINGS = ("您好", "老师您好", "同学你好", "各位好")


def rule_based_extract(text: str) -> EmailDraft:
    """从输入中抽取邮件字段：主题/收件人/落款 + 要点列表。"""
    fields: dict[str, str] = {}
    for key, pattern in _FIELD_PATTERNS.items():
        m = pattern.search(text)
        if m:
            fields[key] = m.group(2).strip()

    # 要点：优先"要点："之后的条目；否则去掉字段行后的剩余句子
    points: list[str] = []
    m = _POINTS_HEADER.search(text)
    if m:
        body = text[m.end():]
        points = [
            p.strip(" ，。,；;")
            for p in re.split(r"[\n；;]+|(?:[一二三四五六七八九十\d]+[、.．])", body)
            if p.strip(" ，。,；;")
        ]
    else:
        remainder = text
        for pattern in _FIELD_PATTERNS.values():
            remainder = pattern.sub("", remainder)
        remainder = re.sub(r"请帮我写邮件草稿[：:]?", "", remainder)
        points = [
            p.strip(" ，。,；;")
            for p in re.split(r"[。；;\n]+", remainder)
            if p.strip(" ，。,；;")
        ]

    return EmailDraft(
        subject=fields.get("subject", PENDING),
        recipient=fields.get("recipient", PENDING),
        sender=fields.get("sender", PENDING),
        key_points=points,
    )


def llm_extract(text: str) -> EmailDraft | None:
    llm = get_llm()
    if not llm.is_available:
        return None
    raw: Any = llm.chat_json(_LLM_PROMPT, text)
    if raw is None:
        return None
    try:
        return EmailDraft(
            subject=raw.get("subject", PENDING),
            recipient=raw.get("recipient", PENDING),
            sender=raw.get("sender", PENDING),
            key_points=[str(p) for p in raw.get("key_points", [])],
        )
    except Exception:  # noqa: BLE001
        return None


def render_markdown(draft: EmailDraft) -> str:
    """组装邮件草稿正文（仅草稿，导出/复制由用户显式操作）。"""
    lines = [f"**主题**：{draft.subject}", "", f"{draft.recipient}：", ""]
    if any(g in draft.recipient for g in _GREETINGS):
        pass  # 称呼已含问候语
    else:
        lines.append("您好！")
        lines.append("")
    if draft.key_points:
        lines.append("现将相关事项说明如下：")
        lines.append("")
        for i, point in enumerate(draft.key_points, start=1):
            lines.append(f"{i}. {point}")
    else:
        lines.append(PENDING)
    lines += [
        "",
        "以上，如有疑问请随时联系。",
        "",
        "此致",
        "敬礼",
        "",
        draft.sender,
        "",
        "> 说明：本邮件为系统生成的草稿，仅供编辑与参考，不会自动发送。",
    ]
    return "\n".join(lines)


class EmailDraftSkill(Skill):
    name = "email_draft"
    display_name = "邮件草稿"
    input_schema = {"type": "string", "description": "邮件背景（可含 主题：/收件人：/落款：/要点：）"}
    output_schema = {"type": "object", "description": "EmailDraft JSON"}
    tools = ["extract_fields", "validate", "render_markdown"]
    acceptance_samples = "tests/test_email_draft.py 内联样本"

    def run(self, state: AgentState) -> AgentState:
        logs: list[dict[str, Any]] = list(state.get("tool_logs", []))

        def log(tool: str, status: str, error: str = "") -> None:
            logs.append(
                {
                    "tool_name": tool,
                    "status": status,
                    "error_message": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

        text = state.get("input_text", "").strip()
        if not text:
            log("extract_fields", "failed", "输入为空，已阻止生成")
            return {**state, "tool_logs": logs,
                    "error_message": "请输入非空邮件背景后再生成草稿"}

        draft = llm_extract(text)
        engine = "llm" if draft is not None else "rule"
        if draft is None:
            draft = rule_based_extract(text)
        log("extract_fields", "success", "" if engine == "llm" else "引擎=规则降级")

        # 零编造校验：字段值必须出现在原文中
        corrections = 0
        updates: dict[str, str] = {}
        for key in ("subject", "recipient", "sender"):
            value = getattr(draft, key)
            if value != PENDING and value and value not in text:
                updates[key] = PENDING
                corrections += 1
        if updates:
            draft = draft.model_copy(update=updates)
        log("validate", "success", f"回退{corrections}处" if corrections else "")

        markdown = render_markdown(draft)
        log("render_markdown", "success")

        return {
            **state,
            "structured_result": draft.model_dump(),
            "markdown": markdown,
            "tool_logs": logs,
            "export_status": "pending",
            "error_message": "",
        }
