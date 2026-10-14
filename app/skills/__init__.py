"""Skill 注册表（architecture.md §4.3）。

v0.6 起四个 Skill 均为完整实现，不再有占位：
- FR-01 会议纪要：完整实现（唯一纳入 13 例验收的项）
- FR-02 周报整理：由会议纪要派生（v0.4.0 起实现）
- FR-03 邮件草稿：由会议纪要派生（v0.4.0 起实现）
- FR-04 网页整理：单页读取实现（v0.3 线完成，v0.6 合并引入；不批量爬取、不登录）
"""

from app.skills.base import Skill
from app.skills.email_draft import EmailDraftSkill
from app.skills.meeting_minutes import MeetingMinutesSkill
from app.skills.web_summary import WebSummarySkill
from app.skills.weekly_report import WeeklyReportSkill

SKILLS: dict[str, Skill] = {
    "meeting_minutes": MeetingMinutesSkill(),
    "weekly_report": WeeklyReportSkill(),
    "email_draft": EmailDraftSkill(),
    "web_summary": WebSummarySkill(),
}


def get_skill(task_type: str) -> Skill:
    skill = SKILLS.get(task_type)
    if skill is None:
        raise ValueError(f"未知任务类型: {task_type}")
    return skill


__all__ = [
    "SKILLS",
    "get_skill",
    "Skill",
    "MeetingMinutesSkill",
    "WeeklyReportSkill",
    "EmailDraftSkill",
    "WebSummarySkill",
]
