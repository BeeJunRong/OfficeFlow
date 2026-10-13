"""Markdown 导出工具（architecture.md §6）。

- 导出以用户显式点击为触发（由 API 层保证，本工具不主动执行）。
- 保存失败返回错误信息，支持重试；不出现重复导出记录。
- inject_failure 用于测试环境受控错误注入（E1/E2 场景）。
"""

import os
import time
from pathlib import Path

from app.schemas import MeetingMinutes, PENDING

EXPORT_DIR = Path(os.getenv("OFFICEFLOW_EXPORT_DIR", "exports"))


def render_markdown(minutes: MeetingMinutes) -> str:
    """结构化结果 → Markdown 文本（缺失字段显示"待确认"）。"""
    lines = [
        f"# 会议纪要：{minutes.topic}",
        "",
        f"- **会议日期**：{minutes.date}",
        "",
    ]
    if minutes.decisions:
        lines.append("## 关键决定")
        lines.extend(f"- {d}" for d in minutes.decisions)
        lines.append("")
    if minutes.action_items:
        lines.append("## 待办事项")
        lines.append("")
        lines.append("| 事项 | 负责人 | 截止日期 | 原文依据 |")
        lines.append("|------|--------|----------|----------|")
        for item in minutes.action_items:
            evidence = item.evidence.replace("|", "\\|") if item.evidence else ""
            lines.append(
                f"| {item.task} | {item.owner} | {item.deadline} | {evidence} |"
            )
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
