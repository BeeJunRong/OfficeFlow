"""Markdown 导出工具（architecture.md §6）。

- 导出以用户显式点击为触发（由 API 层保证，本工具不主动执行）。
- 保存失败返回错误信息，支持重试；不出现重复导出记录。
- inject_failure 用于测试环境受控错误注入（E1/E2 场景）。
"""

import os
import time
from pathlib import Path

from app.schemas import (
    PROTOCOL_LABELS,
    PENDING,
    EmailDraft,
    MeetingMinutes,
    WeeklyReport,
)

EXPORT_DIR = Path(os.getenv("OFFICEFLOW_EXPORT_DIR", "exports"))


def _evidence_cell(evidence: str, line: int | None) -> str:
    """原文依据单元格：优先显示"第N行：原文片段"。"""
    text = (evidence or "").replace("|", "\\|")
    if line:
        return f"第{line}行：{text}" if text else f"第{line}行"
    return text


def render_markdown(minutes: MeetingMinutes) -> str:
    """结构化结果 → 标准会议纪要模板（用户指定结构）。

    六段式：会议议题 / 内容与讨论 / 决议 / 待办事项（表格）/ 风险与遗留问题 /
    下次会议安排；头部为元信息栏。缺失字段统一"待确认"，不编造。
    """
    title = f"{minutes.topic}会议纪要" if minutes.topic != PENDING else "会议纪要"

    def _people(names: list[str]) -> str:
        return "、".join(names) if names else ""

    def _numbered(items: list[str]) -> list[str]:
        if not items:
            return [f"1. {PENDING}"]
        return [f"{i}. {item}" for i, item in enumerate(items, start=1)]

    discussion_lines = (
        _numbered(minutes.discussion)
        if minutes.discussion
        else ["（按议题记录汇报要点、讨论意见和共识）"]
    )

    lines = [
        f"# {title}",
        "",
        f"时间：{minutes.date}",
        f"地点：{minutes.location}",
        f"主持人：{minutes.host}",
        f"出席人员：{_people(minutes.attendees)}",
        f"列席人员：{_people(minutes.observers)}",
        f"缺席人员：{_people(minutes.absentees)}",
        f"记录人：{minutes.recorder}",
        f"审核人：{minutes.reviewer}",
        "",
        "## 一、会议议题",
        *_numbered(minutes.agenda),
        "",
        "## 二、会议内容与讨论",
        *discussion_lines,
        "",
        "## 三、会议决议",
        *_numbered(minutes.decisions),
        "",
        "## 四、待办事项",
        "",
        "| 序号 | 事项 | 负责人 | 协办 | 截止时间 | 输出物 | 原文依据 |",
        "|------|------|--------|------|----------|--------|----------|",
    ]
    if minutes.action_items:
        for i, item in enumerate(minutes.action_items, start=1):
            evidence = _evidence_cell(item.evidence, item.evidence_line)
            lines.append(
                f"| {i} | {item.task} | {item.owner} | {item.co_owner} "
                f"| {item.deadline} | {item.deliverable} | {evidence} |"
            )
    lines += [
        "",
        "## 五、风险与遗留问题",
        *_numbered(minutes.risks),
        "",
        "## 六、下次会议安排",
        f"时间：{minutes.next_meeting.time}",
        f"地点：{minutes.next_meeting.location}",
        f"议题：{minutes.next_meeting.agenda}",
        "",
        f"附件：{_people(minutes.attachments)}",
        f"记录人：{minutes.recorder}",
        f"审核人：{minutes.reviewer}",
        f"日期：{minutes.date}",
        "",
    ]
    return "\n".join(lines)


def render_email_markdown(draft: EmailDraft) -> str:
    """邮件草稿 → Markdown（用于预览与导出 .md）。

    明确标注协议与"未发送"状态，避免用户误以为邮件已发出（Q-02 可核对）。
    """
    protocol_label = PROTOCOL_LABELS.get(draft.protocol, draft.protocol)
    lines = [
        "# 邮件草稿（未发送）",
        "",
        f"- **发件人（本人）**：{draft.sender}",
        f"- **发送协议**：{protocol_label}",
        f"- **来源会议**：{draft.source_topic}（{draft.source_date}）",
        f"- **收件人**：{draft.recipient_count} 位",
        "",
        f"**主题**：{draft.subject}",
        "",
        "---",
        "",
    ]
    if not draft.recipients:
        lines.append(
            "_未能从会议纪要中分析出负责人，无法生成收件人。"
            "请手动补充负责人后重试。_"
        )
        lines.append("")
        return "\n".join(lines)

    for i, r in enumerate(draft.recipients, start=1):
        addr = r.email or "（待你填写）"
        lines.append(f"## {i}. {r.name} &lt;{addr}&gt;")
        lines.append("")
        lines.append(f"- 邮箱：{addr}")
        lines.append(f"- 发送：{'是' if r.send else '否'}")
        lines.append("")
        lines.append("```text")
        lines.append(r.content or "（内容为空）")
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "> 本文件仅为草稿。邮箱地址需由你手动填写；"
        "是否发送由你在界面中逐封确认，本工具不会自动发送。"
    )
    lines.append("")
    return "\n".join(lines)


def render_weekly_markdown(report: WeeklyReport) -> str:
    """周报 → Markdown（缺失字段显示"待确认"）。"""
    lines = [
        f"# 周报：{report.topic}",
        "",
        f"- **报告周期**：{report.period}",
        f"- **来源会议日期**：{report.source_date}",
        "",
        "## 本周进展",
    ]
    if report.progress:
        for p in report.progress:
            if p.evidence_line:
                detail = f"第{p.evidence_line}行" + (f"：{p.evidence}" if p.evidence else "")
                lines.append(f"- {p.content}（原文{detail}）")
            else:
                lines.append(f"- {p.content}")
    else:
        lines.append(f"- {PENDING}")
    lines.append("")

    lines.append("## 下周计划")
    if report.next_plan:
        lines.append("")
        lines.append("| 事项 | 负责人 | 计划完成 | 原文依据 |")
        lines.append("|------|--------|----------|----------|")
        for it in report.next_plan:
            evidence = _evidence_cell(it.evidence, it.evidence_line)
            lines.append(
                f"| {it.content} | {it.owner} | {it.deadline} | {evidence} |"
            )
    else:
        lines.append(f"- {PENDING}")
    lines.append("")

    if report.risks:
        lines.append("## 风险与待确认")
        lines.extend(f"- {r}" for r in report.risks)
        lines.append("")

    return "\n".join(lines)


def export_markdown(
    content: str,
    filename: str = "meeting_minutes",
    inject_failure: bool = False,
) -> tuple[bool, str, str]:
    """导出 Markdown 文件。

    返回 (success, file_path, error_message)。
    - 文件名做安全清洗，防止路径穿越。
    - 同一 session 重试成功后覆盖同名文件，不产生重复导出记录。
    """
    if inject_failure:
        return False, "", "导出失败：保存失败/服务不可用（受控注入），可重试"

    safe = "".join(c for c in filename if c.isalnum() or c in "-_") or "meeting_minutes"
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"{safe}.md"
    try:
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)  # 原子替换，重试成功不产生重复文件
    except OSError as exc:
        return False, "", f"导出失败：{exc}，可重试"
    return True, str(path), ""


def missing_field_summary(minutes: MeetingMinutes) -> list[str]:
    """统计缺失字段，供校验节点与前端提示使用。"""
    missing = []
    if minutes.date == PENDING:
        missing.append("会议日期")
    if minutes.topic == PENDING:
        missing.append("会议主题")
    for i, item in enumerate(minutes.action_items, start=1):
        if item.owner == PENDING:
            missing.append(f"待办{i}负责人")
        if item.deadline == PENDING:
            missing.append(f"待办{i}截止日期")
    return missing


if __name__ == "__main__":  # 手动冒烟
    demo = MeetingMinutes(
        date=PENDING,
        topic="项目例会",
        decisions=["先上线纪要模块"],
        action_items=[],
    )
    ok, fp, err = export_markdown(render_markdown(demo), f"smoke_{int(time.time())}")
    print(ok, fp, err)
