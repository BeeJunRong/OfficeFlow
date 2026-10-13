"""校验节点：零编造检查（architecture.md §5.1、guardsrails.md §2）。

规则：
- 负责人：不在原文出现的负责人 → 回退 "待确认"。
- 日期：输出日期必须能在原文中找到依据——
  · 原文含完整 "2026年10月9日" / "2026-10-09" 形式；或
  · 原文含 "10月9日" 且年份与会议日期（或当前年）一致（明确年月日标准化）。
  找不到依据的具体日期 → 回退 "待确认"。
- "待确认" 本身视为合规缺失值，保持不变。
"""

import re
from datetime import datetime

from app.schemas import PENDING, ActionItem, MeetingMinutes

_YMD_RE = re.compile(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?")
_MD_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日(?!\d)")


def _norm(y: int, m: int, d: int) -> str | None:
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _dates_in_text(text: str, default_year: int) -> set[str]:
    """原文中可推出的全部合法标准化日期集合（含月日+补年）。"""
    found: set[str] = set()
    for m in _YMD_RE.finditer(text):
        d = _norm(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.add(d)
    for m in _MD_RE.finditer(text):
        d = _norm(default_year, int(m.group(1)), int(m.group(2)))
        if d:
            found.add(d)
    return found


def _validate_date(value: str, text: str, default_year: int) -> str:
    if value == PENDING or not value:
        return PENDING
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return PENDING  # 非标准日期一律视为无法核实
    if value in _dates_in_text(text, default_year):
        return value
    return PENDING  # 原文无依据 → 回退，不得编造


def validate_minutes(minutes: MeetingMinutes, source_text: str) -> tuple[MeetingMinutes, list[str]]:
    """校验并修正结构化结果。

    返回 (修正后结果, 修正说明列表)；修正说明进入工具日志作为验收证据。
    """
    corrections: list[str] = []
    default_year = datetime.now().year

    # 先校验会议日期，拿它的年份作为月日补全的默认年
    date = _validate_date(minutes.date, source_text, default_year)
    if date != minutes.date and minutes.date != PENDING:
        corrections.append(f"会议日期「{minutes.date}」原文无依据，回退为待确认")
    if date != PENDING:
        default_year = int(date[:4])

    items: list[ActionItem] = []
    for i, item in enumerate(minutes.action_items, start=1):
        owner = item.owner
        if owner != PENDING and owner and owner not in source_text:
            owner = PENDING
            corrections.append(f"待办{i}负责人「{item.owner}」原文未出现，回退为待确认")
        deadline = _validate_date(item.deadline, source_text, default_year)
        if deadline != item.deadline and item.deadline != PENDING:
            corrections.append(f"待办{i}截止日期「{item.deadline}」原文无依据，回退为待确认")
        items.append(item.model_copy(update={"owner": owner, "deadline": deadline}))

    fixed = minutes.model_copy(update={"date": date, "action_items": items})
    return fixed, corrections
