"""派生建议（多 Skill 编排 · 理解 C）。

设计要点（见 docs/多Skill编排评估.md §1、§2理解C）：

1. **只提供有数据依据的派生项**。四个 Skill 不是线性链而是有向图：
   - 会议纪要 → 邮件草稿 ✅（收件人来自待办负责人）
   - 会议纪要 → 周报整理 ✅（决定→进展，待办→计划）
   - 周报整理 → 邮件草稿 ✅（周报可发邮件）
   - **网页整理不参与派生**：它需要 URL/网页文本作输入，会议记录里没有 ❌
   - 邮件草稿是终端产物，不再派生 ❌
2. **只给建议，不自动执行**。是否生成由用户点击决定（Q-02：不可逆行为人工确认）。
3. **复用已抽取结果**，派生时不重新抽取（省 token，且与源产物严格一致）。
"""

from app.schemas import (
    PENDING,
    DerivedTask,
    EmailDraft,
    MeetingMinutes,
    WeeklyReport,
)


def _unique_owners(minutes: MeetingMinutes) -> list[str]:
    """纪要中已核实的去重负责人（排除"待确认"）。"""
    seen: list[str] = []
    for item in minutes.action_items:
        owner = (item.owner or PENDING).strip()
        if owner and owner != PENDING and owner not in seen:
            seen.append(owner)
    return seen


def _owners_from_report(report: WeeklyReport) -> list[str]:
    seen: list[str] = []
    for item in report.next_plan:
        owner = (item.owner or PENDING).strip()
        if owner and owner != PENDING and owner not in seen:
            seen.append(owner)
    return seen


def derive_from_minutes(minutes: MeetingMinutes) -> list[DerivedTask]:
    """会议纪要 → 可派生任务。"""
    tasks: list[DerivedTask] = []
    owners = _unique_owners(minutes)

    if owners:
        tasks.append(
            DerivedTask(
                task_type="email_draft",
                label=f"给 {len(owners)} 位负责人发会后邮件",
                reason=f"纪要中分析出 {len(owners)} 位负责人：{'、'.join(owners)}",
            )
        )
    else:
        # 没有可确认的负责人时仍允许派生，但说明理由，避免用户困惑
        tasks.append(
            DerivedTask(
                task_type="email_draft",
                label="生成会后邮件草稿",
                reason="纪要中未出现可确认的负责人，将生成空白收件人列表供你手动补充",
            )
        )

    if minutes.decisions or minutes.action_items:
        tasks.append(
            DerivedTask(
                task_type="weekly_report",
                label="整理成本周周报素材",
                reason=f"含 {len(minutes.decisions)} 项决定、{len(minutes.action_items)} 项待办",
            )
        )
    return tasks


def derive_from_weekly_report(report: WeeklyReport) -> list[DerivedTask]:
    """周报 → 可派生任务（周报本身可作为邮件发出）。"""
    owners = _owners_from_report(report)
    if not report.next_plan:
        return []
    label = (
        f"把周报发给 {len(owners)} 位负责人"
        if owners
        else "把周报生成为邮件草稿"
    )
    return [
        DerivedTask(
            task_type="email_draft",
            label=label,
            reason=f"周报含 {len(report.next_plan)} 项计划"
            + (f"，涉及 {'、'.join(owners)}" if owners else ""),
        )
    ]


def compute_derived_tasks(
    task_type: str,
    *,
    minutes: MeetingMinutes | None = None,
    report: WeeklyReport | None = None,
    draft: EmailDraft | None = None,
) -> list[DerivedTask]:
    """按源任务类型计算可派生项。

    `draft`（邮件草稿）为终端产物，恒返回空列表 —— 显式写在签名里，
    以免后续误加派生规则造成循环（如 邮件→周报→邮件）。
    """
    if task_type == "meeting_minutes" and minutes is not None:
        return derive_from_minutes(minutes)
    if task_type == "weekly_report" and report is not None:
        return derive_from_weekly_report(report)
    if task_type in ("email_draft", "web_summary"):
        return []
    return []
