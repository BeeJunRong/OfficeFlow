"""邮件草稿 Skill（FR-03）。

UI 规格（用户需求）：
    显示本人 → 协议 → 从会议纪要分析出的人数 → 名字 / 邮箱 / 内容 / 发送
    · 本人：发件人（默认"待确认"，由用户填写）
    · 协议：draft_only（默认，不发送）/ mailto / smtp
    · 人数：待办中出现的**去重负责人**数量
    · 邮箱：**用户手动填写，系统绝不编造**
    · 内容：由该负责人的待办自动生成（可编辑）
    · 发送：每封独立开关，默认 False

零编造约束（guardsrails §2）：
- 收件人姓名只取自已通过校验的结构化结果；
- 邮箱地址一律留空，不推测、不拼接（"张三"→"zhangsan@…" 属编造）；
- 正文中的事项/截止日期直接引用结构化字段，不新增事实。

三种进入方式：
1. 由会议纪要派生：状态含 `structured_result`（MeetingMinutes）→ 收件人取待办负责人；
2. 由周报派生：状态含 `weekly_report`（WeeklyReport）→ 收件人取下周计划负责人；
3. 直接输入：用户直接选"邮件草稿"并粘贴文本 → 先跑会议纪要抽取再派生。
"""

from datetime import datetime, timezone
from typing import Any

from app.schemas import (
    DEFAULT_EMAIL_PROTOCOL,
    PENDING,
    AgentState,
    EmailDraft,
    EmailRecipient,
    MeetingMinutes,
    WeeklyReport,
)
from app.skills.base import Skill
from app.tools.extract_fields import extract_meeting_minutes
from app.validate import validate_minutes

_CONFIRM_CLOSING = "如对以上安排有疑问，请回复本邮件确认。谢谢！"


def _build_content(owner: str, topic: str, date_str: str, lines: list[str]) -> str:
    """按收件人生成正文（纯模板 + 结构化字段，不含新事实）。"""
    return (
        f"{owner} 你好：\n\n"
        f"根据《{topic}》（{date_str}）的安排，以下事项由你负责，请留意时间节点：\n\n"
        + "\n".join(lines)
        + f"\n\n{_CONFIRM_CLOSING}"
    )


def recipients_from_minutes(minutes: MeetingMinutes) -> list[EmailRecipient]:
    """会议纪要 → 收件人列表（按负责人去重，保持出现顺序）。"""
    by_owner: dict[str, list[Any]] = {}
    for item in minutes.action_items:
        owner = (item.owner or PENDING).strip()
        if not owner or owner == PENDING:
            continue
        by_owner.setdefault(owner, []).append(item)

    recipients: list[EmailRecipient] = []
    for owner, items in by_owner.items():
        lines = [f"- {it.task}（截止：{it.deadline}）" for it in items]
        recipients.append(
            EmailRecipient(
                name=owner,
                email="",
                content=_build_content(owner, minutes.topic, minutes.date, lines),
                send=False,
            )
        )
    return recipients


def recipients_from_report(report: WeeklyReport) -> list[EmailRecipient]:
    """周报 → 收件人列表（按下周计划负责人去重）。"""
    by_owner: dict[str, list[Any]] = {}
    for item in report.next_plan:
        owner = (item.owner or PENDING).strip()
        if not owner or owner == PENDING:
            continue
        by_owner.setdefault(owner, []).append(item)

    recipients: list[EmailRecipient] = []
    for owner, items in by_owner.items():
        lines = [f"- {it.content}（计划完成：{it.deadline}）" for it in items]
        recipients.append(
            EmailRecipient(
                name=owner,
                email="",
                content=_build_content(owner, report.topic, report.source_date, lines),
                send=False,
            )
        )
    return recipients


def _pending_note_from_items(items: list[Any], attr: str) -> str:
    unknown = [it for it in items if (getattr(it, attr, None) or PENDING) == PENDING]
    if not unknown:
        return ""
    names = "、".join(getattr(it, "task", None) or getattr(it, "content", "") for it in unknown[:3])
    more = f" 等 {len(unknown)} 项" if len(unknown) > 3 else ""
    return (
        f"有 {len(unknown)} 项未提供负责人（{names}{more}），未生成收件人，请手动补充。"
    )


class EmailDraftSkill(Skill):
    name = "email_draft"
    display_name = "邮件草稿"
    input_schema = {
        "type": "string",
        "description": "会议原始文字记录；或由会议纪要 / 周报派生（复用其结构化结果）",
    }
    output_schema = {
        "type": "object",
        "properties": {
            "sender": {"type": "string"},
            "protocol": {"type": "string", "enum": ["draft_only", "mailto", "smtp"]},
            "subject": {"type": "string"},
            "recipients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string", "description": "用户手填"},
                        "content": {"type": "string"},
                        "send": {"type": "boolean"},
                    },
                },
            },
        },
    }
    tools = ["extract_fields", "validate", "render_email"]
    acceptance_samples = "由 FR-01 样本派生（不单独纳入 13 例验收）"

    def run(self, state: AgentState) -> AgentState:
        from app.tools.export_markdown import render_email_markdown

        logs: list[dict[str, Any]] = list(state.get("tool_logs", []))
        now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

        def log(tool: str, status: str, error: str = "") -> None:
            logs.append(
                {
                    "tool_name": tool,
                    "status": status,
                    "error_message": error,
                    "timestamp": now(),
                }
            )

        text = state.get("input_text", "").strip()
        minutes_raw = state.get("structured_result")
        report_raw = state.get("weekly_report")

        if not text and not minutes_raw and not report_raw:
            log("extract_fields", "failed", "输入为空，已阻止生成")
            return {
                **state,
                "tool_logs": logs,
                "error_message": "请输入非空会议文字记录后再生成邮件草稿",
            }

        topic = PENDING
        date_str = PENDING
        recipients: list[EmailRecipient] = []
        note = ""
        source_desc = "从原文抽取"

        try:
            if report_raw:
                report = WeeklyReport(**report_raw)
                topic, date_str = report.topic, report.source_date
                recipients = recipients_from_report(report)
                note = _pending_note_from_items(report.next_plan, "owner")
                source_desc = "复用周报结构化结果（派生）"
            elif minutes_raw:
                minutes = MeetingMinutes(**minutes_raw)
                topic, date_str = minutes.topic, minutes.date
                recipients = recipients_from_minutes(minutes)
                note = _pending_note_from_items(minutes.action_items, "owner")
                source_desc = "复用会议纪要结构化结果（派生）"
            else:
                minutes, _engine = extract_meeting_minutes(text)
                minutes, _corrections = validate_minutes(
                    minutes, text, line_source_text=state.get("raw_input_text")
                )
                topic, date_str = minutes.topic, minutes.date
                recipients = recipients_from_minutes(minutes)
                note = _pending_note_from_items(minutes.action_items, "owner")
        except Exception as exc:  # noqa: BLE001
            log("extract_fields", "failed", str(exc))
            return {
                **state,
                "tool_logs": logs,
                "error_message": f"抽取工具失败：{exc}，内容已保留，可重试",
            }

        log("extract_fields", "success", source_desc)
        log(
            "validate",
            "success",
            note or "收件人均来自已核实的负责人；邮箱留空待你填写",
        )

        draft = EmailDraft(
            sender=PENDING,
            protocol=DEFAULT_EMAIL_PROTOCOL,
            subject=f"【会议待办确认】{topic}（{date_str}）",
            recipients=recipients,
            source_topic=topic,
            source_date=date_str,
        )
        markdown = render_email_markdown(draft)
        log(
            "render_email",
            "success",
            f"共分析出 {len(recipients)} 位收件人；协议={draft.protocol}（不会自动发送）",
        )

        return {
            **state,
            "email_draft": draft.model_dump(),
            "markdown": markdown,
            "tool_logs": logs,
            "export_status": "pending",
            "error_message": "",
        }
