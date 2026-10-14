"""周报整理 Skill（FR-02）。

由会议纪要派生，字段映射保持"可核对"（guardsrails §2 零编造）：

| 会议纪要字段 | 周报字段 | 理由 |
|---|---|---|
| 关键决定 decisions | 本周进展 progress | 已决定的事项即本周推进结果 |
| 待办 action_items | 下周计划 next_plan | 带负责人/截止日期，天然是计划 |
| 负责人/截止日期为"待确认"的待办 | 风险与待确认 risks | 无法排期的条目要显式提示 |

不新增任何原文没有的事实：所有条目都直接引用纪要字段，并保留 evidence。
周期（period）由会议日期推算其所在自然周；会议日期为"待确认"时周期同样标注待确认。
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.schemas import PENDING, AgentState, MeetingMinutes, WeeklyReport, WeeklyReportItem
from app.skills.base import Skill
from app.tools.extract_fields import extract_meeting_minutes
from app.validate import validate_minutes


def _week_period(meeting_date: str) -> str:
    """由会议日期推算所在自然周（周一~周日）；无法解析则返回待确认。"""
    if not meeting_date or meeting_date == PENDING:
        return PENDING
    try:
        d = datetime.strptime(meeting_date, "%Y-%m-%d").date()
    except ValueError:
        return PENDING
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.isoformat()} ~ {sunday.isoformat()}"


def _minutes_from_state(state: AgentState, text: str) -> MeetingMinutes:
    """取会议纪要：优先复用状态中已校验的结果（派生路径），否则现抽一次。"""
    raw = state.get("structured_result")
    if raw:
        try:
            return MeetingMinutes(**raw)
        except Exception:  # noqa: BLE001
            pass
    minutes, _engine = extract_meeting_minutes(text)
    minutes, _corrections = validate_minutes(minutes, text)
    return minutes


def build_weekly_report(minutes: MeetingMinutes) -> tuple[WeeklyReport, list[str]]:
    """会议纪要 → 周报。返回 (周报, 校验说明列表)。"""
    notes: list[str] = []

    progress = [
        WeeklyReportItem(content=d, owner=PENDING, deadline=PENDING, evidence=d)
        for d in minutes.decisions
        if d and d.strip()
    ]

    next_plan: list[WeeklyReportItem] = []
    risks: list[str] = []
    for item in minutes.action_items:
        entry = WeeklyReportItem(
            content=item.task,
            owner=item.owner,
            deadline=item.deadline,
            evidence=item.evidence,
        )
        next_plan.append(entry)
        missing = []
        if (item.owner or PENDING) == PENDING:
            missing.append("负责人")
        if (item.deadline or PENDING) == PENDING:
            missing.append("截止日期")
        if missing:
            risks.append(f"「{item.task}」的{'、'.join(missing)}原文未提供，需人工确认")

    if not progress:
        notes.append("本次会议未抽取到关键决定，本周进展为空")
    if not next_plan:
        notes.append("本次会议未抽取到待办，下周计划为空")

    report = WeeklyReport(
        period=_week_period(minutes.date),
        topic=minutes.topic,
        progress=progress,
        next_plan=next_plan,
        risks=risks,
        source_date=minutes.date,
    )
    return report, notes


class WeeklyReportSkill(Skill):
    name = "weekly_report"
    display_name = "周报整理"
    input_schema = {
        "type": "string",
        "description": "会议原始文字记录；或由会议纪要派生（复用其结构化结果）",
    }
    output_schema = {
        "type": "object",
        "properties": {
            "period": {"type": "string"},
            "topic": {"type": "string"},
            "progress": {"type": "array"},
            "next_plan": {"type": "array"},
            "risks": {"type": "array", "items": {"type": "string"}},
        },
    }
    tools = ["extract_fields", "validate", "render_weekly"]
    acceptance_samples = "由 FR-01 样本派生（不单独纳入 13 例验收）"

    def run(self, state: AgentState) -> AgentState:
        from app.tools.export_markdown import render_weekly_markdown

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
        derived = bool(state.get("structured_result"))

        if not text and not derived:
            log("extract_fields", "failed", "输入为空，已阻止生成")
            return {
                **state,
                "tool_logs": logs,
                "error_message": "请输入非空会议文字记录后再生成周报",
            }

        try:
            minutes = _minutes_from_state(state, text)
        except Exception as exc:  # noqa: BLE001
            log("extract_fields", "failed", str(exc))
            return {
                **state,
                "tool_logs": logs,
                "error_message": f"抽取工具失败：{exc}，内容已保留，可重试",
            }

        log(
            "extract_fields",
            "success",
            "复用会议纪要结构化结果（派生）" if derived else "从原文抽取",
        )

        report, notes = build_weekly_report(minutes)
        log(
            "validate",
            "success",
            f"进展 {len(report.progress)} 项 / 计划 {len(report.next_plan)} 项"
            + (f"；{'；'.join(notes)}" if notes else ""),
        )

        markdown = render_weekly_markdown(report)
        log("render_weekly", "success", f"待确认项 {len(report.risks)} 条")

        return {
            **state,
            "weekly_report": report.model_dump(),
            "markdown": markdown,
            "tool_logs": logs,
            "export_status": "pending",
            "error_message": "",
        }
