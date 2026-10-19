"""网页整理 Skill（FR-04，v1.3.0 从占位升级为完整实现）。

约束（guardsrails §7 / proposal §5）：
- 仅单页读取（一次一个 URL），不批量爬取，不登录。
- 要点摘自原文段落（截断保留原句），不生成评论性内容。

流程：URL 提取 → 网页读取工具 → 要点抽取（LLM 优先/规则降级）→ Markdown。
"""

import re
from datetime import datetime, timezone
from typing import Any

from app.llm import get_llm
from app.schemas import AgentState, PENDING, WebSummary
from app.skills.base import Skill
from app.tools.web_fetch import extract_url, fetch_page

_LLM_PROMPT = (
    "你是网页内容整理器。根据给出的网页标题与正文，提炼 3-6 条内容要点。"
    "要点必须忠于原文，使用原文表述，不得加入评论或原文没有的信息。"
    '输出 JSON：{"points": ["要点1", "要点2", ...]}'
)

_MAX_POINTS = 6
_MAX_POINT_CHARS = 120


def _rule_points(text: str) -> list[str]:
    """规则要点：取正文前几个实质段落，超长截断（保留原文表述）。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) >= 20]
    points: list[str] = []
    for para in paragraphs:
        if len(points) >= _MAX_POINTS:
            break
        point = para if len(para) <= _MAX_POINT_CHARS else para[: _MAX_POINT_CHARS - 1] + "…"
        points.append(point)
    return points


def _llm_points(title: str, text: str) -> list[str] | None:
    llm = get_llm()
    if not llm.is_available:
        return None
    raw: Any = llm.chat_json(_LLM_PROMPT, f"标题：{title}\n\n正文（截断）：\n{text[:6000]}")
    if raw is None:
        return None
    points = [str(p) for p in raw.get("points", []) if str(p).strip()]
    return points or None


def render_markdown(summary: WebSummary) -> str:
    lines = [
        f"# 网页整理：{summary.title}",
        "",
        f"- **来源**：{summary.url}",
        f"- **正文约字数**：{summary.word_count}",
        "",
        "## 内容要点",
    ]
    if summary.points:
        lines.extend(f"{i}. {p}" for i, p in enumerate(summary.points, start=1))
    else:
        lines.append(f"- {PENDING}")
    lines += [
        "",
        "> 说明：要点摘自网页原文段落；单页读取，不支持批量爬取与需登录页面。",
    ]
    return "\n".join(lines)


class WebSummarySkill(Skill):
    name = "web_summary"
    display_name = "网页整理"
    input_schema = {"type": "string", "description": "含单个 URL 的整理请求（无需登录的网页）"}
    output_schema = {"type": "object", "description": "WebSummary JSON"}
    tools = ["web_fetch", "extract_fields", "render_markdown"]
    acceptance_samples = "tests/test_web_summary.py 内联样本"

    def run(self, state: AgentState) -> AgentState:
        logs: list[dict[str, Any]] = list(state.get("tool_logs", []))

        def log(tool: str, status: str, error: str = "") -> None:
            logs.append(
                {
                    "tool_name": tool,
                    "status": status,
                    "error_message": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

        text = state.get("input_text", "").strip()
        if not text:
            log("web_fetch", "failed", "输入为空，已阻止生成")
            return {**state, "tool_logs": logs,
                    "error_message": "请输入包含网页 URL 的内容后再整理"}

        url = extract_url(text)
        if not url:
            log("web_fetch", "failed", "未从输入中找到 URL")
            return {
                **state,
                "tool_logs": logs,
                "error_message": "未在输入中找到网页 URL（http:// 或 https:// 开头），请补充后重试",
            }

        page, error = fetch_page(url)
        if page is None:
            log("web_fetch", "failed", error)
            return {**state, "tool_logs": logs, "error_message": error}
        log("web_fetch", "success", f"已读取 {page.url}")

        # 要点抽取：LLM 优先，规则降级
        points = _llm_points(page.title, page.text)
        engine = "llm" if points is not None else "rule"
        if points is None:
            points = _rule_points(page.text)
        log("extract_fields", "success", "" if engine == "llm" else "引擎=规则降级")

        summary = WebSummary(
            title=page.title,
            url=page.url,
            points=points,
            word_count=len(re.sub(r"\s", "", page.text)),
        )
        markdown = render_markdown(summary)
        log("render_markdown", "success")

        return {
            **state,
            "structured_result": summary.model_dump(),
            "markdown": markdown,
            "tool_logs": logs,
            "export_status": "pending",
            "error_message": "",
        }
