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
    校验范围：日期类（会议日期/待办截止/下次会议时间）与人员类
    （待办负责人/协办/主持人/记录人/审核人/出席人员）字段。
    """
    corrections: list[str] = []
    default_year = datetime.now().year
    updates: dict[str, object] = {}

    # 先校验会议日期，拿它的年份作为月日补全的默认年
    date = _validate_date(minutes.date, source_text, default_year)
    if date != minutes.date and minutes.date != PENDING:
        corrections.append(f"会议日期「{minutes.date}」原文无依据，回退为待确认")
    if date != PENDING:
        default_year = int(date[:4])
    updates["date"] = date

    # 人员类元信息：不在原文出现的名字回退"待确认"
    for field, label in (
        ("host", "主持人"), ("recorder", "记录人"), ("reviewer", "审核人"),
    ):
        value = getattr(minutes, field)
        if value != PENDING and value and value not in source_text:
            updates[field] = PENDING
            corrections.append(f"{label}「{value}」原文未出现，回退为待确认")

    # 人员列表：过滤原文未出现的名字
    for field, label in (
        ("attendees", "出席人员"), ("observers", "列席人员"), ("absentees", "缺席人员"),
    ):
        names = getattr(minutes, field)
        kept = [n for n in names if n in source_text]
        if len(kept) != len(names):
            dropped = [n for n in names if n not in source_text]
            corrections.append(f"{label}「{'、'.join(dropped)}」原文未出现，已移除")
            updates[field] = kept
        elif names:
            updates[field] = kept

    items: list[ActionItem] = []
    for i, item in enumerate(minutes.action_items, start=1):
        owner = item.owner
        if owner != PENDING and owner and owner not in source_text:
            owner = PENDING
            corrections.append(f"待办{i}负责人「{item.owner}」原文未出现，回退为待确认")
        co_owner = item.co_owner
        if co_owner != PENDING and co_owner and co_owner not in source_text:
            co_owner = PENDING
            corrections.append(f"待办{i}协办「{item.co_owner}」原文未出现，回退为待确认")
        deadline = _validate_date(item.deadline, source_text, default_year)
        if deadline != item.deadline and item.deadline != PENDING:
            corrections.append(f"待办{i}截止日期「{item.deadline}」原文无依据，回退为待确认")
        items.append(
            item.model_copy(update={"owner": owner, "co_owner": co_owner, "deadline": deadline})
        )
    updates["action_items"] = items

    # 下次会议时间：日期需有原文依据
    nm = minutes.next_meeting
    nm_time = _validate_date(nm.time, source_text, default_year)
    if nm_time != nm.time and nm.time != PENDING:
        # 下次会议允许"下周三"等相对表述（非标准日期保持原样仅当在原文出现）
        if nm.time not in source_text:
            corrections.append(f"下次会议时间「{nm.time}」原文无依据，回退为待确认")
            nm_time = PENDING
        else:
            nm_time = nm.time
    updates["next_meeting"] = nm.model_copy(update={"time": nm_time})

    fixed = minutes.model_copy(update=updates)
    return fixed, corrections
