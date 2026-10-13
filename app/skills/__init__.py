"""Skill 注册表（architecture.md §4.3）。

首版仅 meeting_minutes 完整实现；其余三个为占位 Skill，
返回"P1 开发中"。
"""

from app.skills.base import Skill
from app.skills.meeting_minutes import MeetingMinutesSkill
from app.skills.placeholders import PlaceholderSkill

SKILLS: dict[str, Skill] = {
    "meeting_minutes": MeetingMinutesSkill(),
    "weekly_report": PlaceholderSkill("周报整理"),
    "email_draft": PlaceholderSkill("邮件草稿"),
    "web_summary": PlaceholderSkill("网页整理"),
}


def get_skill(task_type: str) -> Skill:
    skill = SKILLS.get(task_type)
    if skill is None:
        raise ValueError(f"未知任务类型: {task_type}")
    return skill


__all__ = ["SKILLS", "get_skill", "Skill", "MeetingMinutesSkill", "PlaceholderSkill"]
