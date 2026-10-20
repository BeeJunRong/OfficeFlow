"""ReAct 工具注册表：把原子 tool 包装为 OpenAI function calling 协议。

每个工具包含两部分：
1. spec：OpenAI tools 声明（JSON Schema），传给 LLM 让其知道何时调用。
2. handler：实际执行函数，接收 arguments dict 与上下文，返回
   {"summary": str, "state_updates": dict}。
   - summary：回填给 LLM 的 tool 角色消息（让 LLM 知道工具结果以决定下一步）。
   - state_updates：要合并进 AgentState 的字段（如 structured_result / markdown）。

设计原则：
- 工具是原子粒度（extract_fields / validate / render_markdown / web_fetch /
  render_email / render_weekly），不暴露 Skill 流水线。
- 零编造约束由工具内部保证（extract_fields 三层降级、validate 回退待确认）。
- handler 不直接写 state，只返回 state_updates，由 ReAct 循环统一合并，
  避免工具间隐式状态依赖。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.schemas import (
    EmailDraft,
    MeetingMinutes,
    PENDING,
    WeeklyReport,
)
from app.skills.email_draft import (
    recipients_from_minutes,
    recipients_from_report,
)
from app.skills.weekly_report import build_weekly_report
from app.tools.export_markdown import (
    render_email_markdown,
    render_markdown,
    render_weekly_markdown,
)
from app.tools.extract_fields import extract_meeting_minutes
from app.tools.web_fetch import extract_url, fetch_page
from app.validate import validate_minutes

logger = logging.getLogger("officeflow.react_tools")


# ---------------------------------------------------------------------------
# 工具 spec（OpenAI function calling 协议）
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "extract_fields",
            "description": (
                "从会议原始文字记录中抽取结构化字段（议题/决定/待办/出席人等）。"
                "三层降级：LangExtract → LLM → 规则；缺失字段填'待确认'，零编造。"
                "输入应为会议原文，不要传 URL 或非会议文本。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "会议原始文字记录",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate",
            "description": (
                "对抽取出的会议纪要做零编造校验：日期/人员类字段若原文无依据则回退为'待确认'。"
                "返回修正后的结构化结果与修正说明。"
                "调用前需先调用 extract_fields 得到 minutes。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "object",
                        "description": "extract_fields 返回的 MeetingMinutes JSON",
                    },
                    "source_text": {
                        "type": "string",
                        "description": "原始会议文字（用于核对字段是否有原文依据）",
                    },
                },
                "required": ["minutes", "source_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_markdown",
            "description": (
                "把校验后的会议纪要结构化结果渲染为标准六段式 Markdown。"
                "调用前需已有 minutes（来自 extract_fields + validate）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "object",
                        "description": "校验后的 MeetingMinutes JSON",
                    }
                },
                "required": ["minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_weekly_report",
            "description": (
                "由会议纪要派生周报：关键决定→本周进展，待办→下周计划，"
                "缺失负责人/截止日期的待办→风险与待确认。不新增原文没有的事实。"
                "调用前需已有校验后的 minutes。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "object",
                        "description": "校验后的 MeetingMinutes JSON",
                    }
                },
                "required": ["minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_weekly",
            "description": "把周报结构化结果渲染为 Markdown。调用前需已有 weekly_report。",
            "parameters": {
                "type": "object",
                "properties": {
                    "weekly_report": {
                        "type": "object",
                        "description": "build_weekly_report 返回的 WeeklyReport JSON",
                    }
                },
                "required": ["weekly_report"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_email_draft",
            "description": (
                "由会议纪要或周报派生邮件草稿：按待办负责人去重生成收件人，"
                "邮箱留空待用户填写，不自动发送。"
                "需提供 minutes 或 weekly_report 之一。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "object",
                        "description": "校验后的 MeetingMinutes JSON（与 weekly_report 二选一）",
                    },
                    "weekly_report": {
                        "type": "object",
                        "description": "WeeklyReport JSON（与 minutes 二选一）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_email",
            "description": "把邮件草稿渲染为 Markdown 预览。调用前需已有 email_draft。",
            "parameters": {
                "type": "object",
                "properties": {
                    "email_draft": {
                        "type": "object",
                        "description": "build_email_draft 返回的 EmailDraft JSON",
                    }
                },
                "required": ["email_draft"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "读取单个网页（http/https URL），返回标题与去标签正文。"
                "仅单页，不批量爬取，不处理需登录页面。"
                "若输入文本含 URL 但 arguments 未显式给出，可从输入文本提取。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "网页 URL（http:// 或 https:// 开头）",
                    },
                    "text": {
                        "type": "string",
                        "description": "用户输入文本（当 url 未给出时从中提取 URL）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_summarize",
            "description": (
                "对网页正文提炼 3-6 条内容要点（LLM 优先，规则降级）。"
                "要点摘自原文段落，不生成评论性内容。"
                "调用前需先调用 web_fetch 得到 page。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "网页标题"},
                    "text": {"type": "string", "description": "网页正文"},
                    "url": {"type": "string", "description": "网页 URL"},
                },
                "required": ["title", "text", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_web_summary",
            "description": "把网页整理结果渲染为 Markdown。调用前需已有 web_summary。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "points": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "word_count": {"type": "integer"},
                },
                "required": ["title", "url", "points", "word_count"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具 handler
# ---------------------------------------------------------------------------

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _handle_extract_fields(args: dict[str, Any]) -> dict[str, Any]:
    text = (args.get("text") or "").strip()
    if not text:
        return {"summary": "错误：text 为空", "state_updates": {}}
    minutes, engine = extract_meeting_minutes(text)
    return {
        "summary": (
            f"抽取成功（引擎={engine}）。字段：topic={minutes.topic!r}, "
            f"date={minutes.date!r}, decisions={len(minutes.decisions)} 条, "
            f"action_items={len(minutes.action_items)} 条。"
            "请接着调用 validate 做零编造校验。"
        ),
        "state_updates": {
            "structured_result": minutes.model_dump(),
        },
    }


def _handle_validate(args: dict[str, Any]) -> dict[str, Any]:
    minutes_raw = args.get("minutes") or {}
    source_text = args.get("source_text") or ""
    try:
        minutes = MeetingMinutes(**minutes_raw)
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"错误：minutes 反序列化失败：{exc}", "state_updates": {}}
    minutes, corrections = validate_minutes(minutes, source_text)
    # 缺失值兜底
    minutes = minutes.model_copy(
        update={"date": minutes.date or PENDING, "topic": minutes.topic or PENDING}
    )
    note = f"校验完成，回退 {len(corrections)} 处。" if corrections else "校验通过，无回退。"
    return {
        "summary": (
            f"{note} 现在可以调用 render_markdown 生成纪要，"
            "或 build_weekly_report 派生周报，或 build_email_draft 派生邮件草稿。"
        ),
        "state_updates": {
            "structured_result": minutes.model_dump(),
        },
    }


def _handle_render_markdown(args: dict[str, Any]) -> dict[str, Any]:
    minutes_raw = args.get("minutes") or {}
    try:
        minutes = MeetingMinutes(**minutes_raw)
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"错误：minutes 反序列化失败：{exc}", "state_updates": {}}
    markdown = render_markdown(minutes)
    return {
        "summary": "会议纪要 Markdown 已生成，可输出给用户。",
        "state_updates": {
            "markdown": markdown,
            "structured_result": minutes.model_dump(),
            "export_status": "pending",
        },
    }


def _handle_build_weekly_report(args: dict[str, Any]) -> dict[str, Any]:
    minutes_raw = args.get("minutes") or {}
    try:
        minutes = MeetingMinutes(**minutes_raw)
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"错误：minutes 反序列化失败：{exc}", "state_updates": {}}
    report, notes = build_weekly_report(minutes)
    return {
        "summary": (
            f"周报已派生：进展 {len(report.progress)} 项 / 计划 {len(report.next_plan)} 项 / "
            f"风险 {len(report.risks)} 条。请接着调用 render_weekly 生成 Markdown。"
        ),
        "state_updates": {
            "weekly_report": report.model_dump(),
        },
    }


def _handle_render_weekly(args: dict[str, Any]) -> dict[str, Any]:
    report_raw = args.get("weekly_report") or {}
    try:
        report = WeeklyReport(**report_raw)
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"错误：weekly_report 反序列化失败：{exc}", "state_updates": {}}
    markdown = render_weekly_markdown(report)
    return {
        "summary": "周报 Markdown 已生成，可输出给用户。",
        "state_updates": {
            "markdown": markdown,
            "weekly_report": report.model_dump(),
            "export_status": "pending",
        },
    }


def _handle_build_email_draft(args: dict[str, Any]) -> dict[str, Any]:
    minutes_raw = args.get("minutes")
    report_raw = args.get("weekly_report")
    if minutes_raw:
        try:
            minutes = MeetingMinutes(**minutes_raw)
        except Exception as exc:  # noqa: BLE001
            return {"summary": f"错误：minutes 反序列化失败：{exc}", "state_updates": {}}
        topic, date_str = minutes.topic, minutes.date
        recipients = recipients_from_minutes(minutes)
    elif report_raw:
        try:
            report = WeeklyReport(**report_raw)
        except Exception as exc:  # noqa: BLE001
            return {"summary": f"错误：weekly_report 反序列化失败：{exc}", "state_updates": {}}
        topic, date_str = report.topic, report.source_date
        recipients = recipients_from_report(report)
    else:
        return {"summary": "错误：需提供 minutes 或 weekly_report 之一", "state_updates": {}}

    from app.schemas import EmailRecipient  # 局部导入避免循环依赖
    from app.skills.email_draft import DEFAULT_EMAIL_PROTOCOL

    draft = EmailDraft(
        sender=PENDING,
        protocol=DEFAULT_EMAIL_PROTOCOL,
        subject=f"【会议待办确认】{topic}（{date_str}）",
        recipients=recipients,
        source_topic=topic,
        source_date=date_str,
    )
    return {
        "summary": (
            f"邮件草稿已生成：收件人 {len(recipients)} 位，邮箱留空待用户填写，不自动发送。"
            "请接着调用 render_email 生成 Markdown 预览。"
        ),
        "state_updates": {
            "email_draft": draft.model_dump(),
        },
    }


def _handle_render_email(args: dict[str, Any]) -> dict[str, Any]:
    draft_raw = args.get("email_draft") or {}
    try:
        draft = EmailDraft(**draft_raw)
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"错误：email_draft 反序列化失败：{exc}", "state_updates": {}}
    markdown = render_email_markdown(draft)
    return {
        "summary": "邮件草稿 Markdown 已生成，可输出给用户。",
        "state_updates": {
            "markdown": markdown,
            "email_draft": draft.model_dump(),
            "export_status": "pending",
        },
    }


def _handle_web_fetch(args: dict[str, Any]) -> dict[str, Any]:
    url = args.get("url")
    text = args.get("text") or ""
    if not url:
        url = extract_url(text)
    if not url:
        return {"summary": "错误：未找到 URL（需 http:// 或 https:// 开头）", "state_updates": {}}
    page, error = fetch_page(url)
    if page is None:
        return {"summary": f"错误：网页读取失败：{error}", "state_updates": {}}
    return {
        "summary": (
            f"已读取网页：title={page.title!r}, 正文约 {len(page.text)} 字符。"
            "请接着调用 web_summarize 提炼要点。"
        ),
        "state_updates": {
            "_web_page": {"title": page.title, "text": page.text, "url": page.url},
        },
    }


def _handle_web_summarize(args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title") or ""
    text = args.get("text") or ""
    url = args.get("url") or ""

    # LLM 优先，规则降级
    from app.skills.web_summary import _llm_points, _rule_points
    points = _llm_points(title, text)
    engine = "llm" if points is not None else "rule"
    if points is None:
        points = _rule_points(text)

    import re
    word_count = len(re.sub(r"\s", "", text))
    return {
        "summary": (
            f"要点已提炼（引擎={engine}），共 {len(points)} 条。"
            "请接着调用 render_web_summary 生成 Markdown。"
        ),
        "state_updates": {
            "structured_result": {
                "title": title,
                "url": url,
                "points": points,
                "word_count": word_count,
            },
        },
    }


def _handle_render_web_summary(args: dict[str, Any]) -> dict[str, Any]:
    from app.schemas import WebSummary
    summary_raw = {
        "title": args.get("title", ""),
        "url": args.get("url", ""),
        "points": args.get("points", []),
        "word_count": args.get("word_count", 0),
    }
    try:
        summary = WebSummary(**summary_raw)
    except Exception as exc:  # noqa: BLE001
        return {"summary": f"错误：WebSummary 反序列化失败：{exc}", "state_updates": {}}
    from app.skills.web_summary import render_markdown as render_web_md
    markdown = render_web_md(summary)
    return {
        "summary": "网页整理 Markdown 已生成，可输出给用户。",
        "state_updates": {
            "markdown": markdown,
            "structured_result": summary.model_dump(),
            "export_status": "pending",
        },
    }


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "extract_fields": _handle_extract_fields,
    "validate": _handle_validate,
    "render_markdown": _handle_render_markdown,
    "build_weekly_report": _handle_build_weekly_report,
    "render_weekly": _handle_render_weekly,
    "build_email_draft": _handle_build_email_draft,
    "render_email": _handle_render_email,
    "web_fetch": _handle_web_fetch,
    "web_summarize": _handle_web_summarize,
    "render_web_summary": _handle_render_web_summary,
}


def get_tool_spec(name: str) -> dict[str, Any] | None:
    """按名取 spec；不存在返回 None。"""
    for spec in TOOL_SPECS:
        if spec["function"]["name"] == name:
            return spec
    return None


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """统一调用入口；未知工具返回错误 summary 而非抛异常（让 ReAct 循环继续）。"""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"summary": f"错误：未知工具 {name!r}", "state_updates": {}}
    try:
        return handler(arguments)
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具 %s 执行失败：%s", name, exc)
        return {"summary": f"错误：工具 {name} 执行失败：{exc}", "state_updates": {}}
