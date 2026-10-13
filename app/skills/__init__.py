"""Skill 注册表（architecture.md §4.3）。

v0.3 起四个 Skill 均为完整实现（meeting_minutes 为首版验收项，
weekly_report / email_draft / web_summary 于 v0.3 从占位升级）。
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
