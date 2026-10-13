"""周报整理 Skill（FR-02，v0.3 从占位升级为完整实现）。

流程与会议纪要一致：抽取 → 校验（零编造）→ 缺失值填充 → Markdown。
- 规则引擎：按"本周工作 / 下周计划 / 风险"分节解析；无分节标题时按句子语义分类。
- LLM 抽取（配置 API Key 时优先），输出仍走零编造校验。
"""

import re
from datetime import datetime, timezone
from typing import Any

from app.llm import get_llm
from app.schemas import AgentState, PENDING, WeeklyReport, WorkItem
from app.skills.base import Skill

_LLM_PROMPT = (
    "你是周报整理器。从用户提供的工作记录中抽取结构化周报。"
    "严格遵守零编造规则：只整理原文明确出现的信息，缺失字段填 \"待确认\"。"
    "period 为统计周期，仅当原文有明确日期范围时填写（标准化为 YYYY-MM-DD ~ YYYY-MM-DD），否则填 \"待确认\"。"
    "work_done 为本周已完成/进行中的工作；plans 为下周计划；risks 为风险与问题。"
    "每项附 evidence（原文片段）。status 取 已完成/进行中/待确认。"
    '输出 JSON：{"period": "...", "work_done": [{"content": "...", "status": "...", "evidence": "..."}], '
    '"plans": [...], "risks": ["..."]}'
)

# 分节标题
_SECTION_DONE = re.compile(r"(本周|这周|本周工作|本周完成)[^：:，。\n]{0,6}[：:]")
_SECTION_PLAN = re.compile(r"(下周|下星期|未来一周|下周计划)[^：:，。\n]{0,6}[：:]")
_SECTION_RISK = re.compile(r"(风险|问题|困难|阻塞|隐患)[^：:，。\n]{0,6}[：:]")

_DONE_MARKERS = ("完成", "已上线", "上线", "修复", "通过", "提交", "合入", "发布", "已", "搞定")
_PLAN_MARKERS = ("计划", "将", "拟", "准备", "预计", "安排", "继续", "启动")
_RISK_MARKERS = ("风险", "问题", "困难", "阻塞", "担心", "延期", "不足")
_IN_PROGRESS = ("进行中", "正在", "开发中", "调试中", "编写中", "推进中")


def _split_items(text: str) -> list[str]:
    parts = re.split(r"[\n；;]+|(?:[一二三四五六七八九十\d]+[、.．])", text)
    return [p.strip(" ，。,") for p in parts if p.strip(" ，。,")]


def _classify(sentence: str) -> str:
    """无分节标题时按语义分类到 done / plan / risk。"""
    if any(m in sentence for m in _RISK_MARKERS):
        return "risk"
    if any(m in sentence for m in _PLAN_MARKERS):
        return "plan"
    if any(m in sentence for m in _DONE_MARKERS):
        return "done"
    return ""


def _status_of(sentence: str) -> str:
    if any(m in sentence for m in _IN_PROGRESS):
        return "进行中"
    if any(m in sentence for m in _DONE_MARKERS):
        return "已完成"
    return PENDING


def _find_period(text: str) -> str:
    """抽取统计周期：仅认原文明确的日期区间（零编造），否则"待确认"。

    形如"10月9日至10月13日""2026-10-06 ~ 2026-10-12"。
    """
    span = re.search(
        r"(\d{1,2}月\d{1,2}日|\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?)\s*[至到~\-—]\s*"
        r"(\d{1,2}月\d{1,2}日|\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?)",
        text,
    )
    if span:
        return f"{span.group(1)} ~ {span.group(2)}"
    return PENDING


def rule_based_extract(text: str) -> WeeklyReport:
    work_done: list[WorkItem] = []
    plans: list[WorkItem] = []
    risks: list[str] = []

    # 分节解析：按分节标题切块
    sections = re.split(r"((?:本周|这周|下周|下星期|未来一周|风险|问题|困难|阻塞)[^：:，。\n]{0,6}[：:])", text)
    current: dict[str, str] = {}
    pending_key: str | None = None
    for part in sections:
        header = re.match(r"^((?:本周|这周|下周|下星期|未来一周|风险|问题|困难|阻塞)[^：:，。\n]{0,6})[：:]", part)
        if header:
            label = header.group(1)
            if any(k in label for k in ("本周", "这周")):
                pending_key = "done"
            elif any(k in label for k in ("下周", "下星期", "未来一周")):
                pending_key = "plan"
            else:
                pending_key = "risk"
            body = part[header.end():]
            if body.strip():
                current.setdefault(pending_key, "")
                current[pending_key] += "\n" + body
        elif pending_key:
            current[pending_key] = current.get(pending_key, "") + "\n" + part

    def _push(bucket: str, body: str) -> None:
        for item in _split_items(body):
            if bucket == "risk":
                risks.append(item)
            elif bucket == "done":
                work_done.append(WorkItem(content=item, status=_status_of(item), evidence=item))
            else:
                plans.append(WorkItem(content=item, status=_status_of(item), evidence=item))

    if current:
        for bucket, body in current.items():
            _push(bucket, body)
    else:
        # 无分节标题：逐句语义分类
        for sentence in _split_items(text):
            kind = _classify(sentence)
            if kind == "risk":
                risks.append(sentence)
            elif kind == "done":
                work_done.append(WorkItem(content=sentence, status=_status_of(sentence), evidence=sentence))
            elif kind == "plan":
                plans.append(WorkItem(content=sentence, status=_status_of(sentence), evidence=sentence))

    return WeeklyReport(period=_find_period(text), work_done=work_done, plans=plans, risks=risks)


def llm_extract(text: str) -> WeeklyReport | None:
    llm = get_llm()
    if not llm.is_available:
        return None
    raw: Any = llm.chat_json(_LLM_PROMPT, text)
    if raw is None:
        return None
    try:
        def _items(key: str) -> list[WorkItem]:
            return [
                WorkItem(**item)
                for item in raw.get(key, [])
                if isinstance(item, dict) and item.get("content")
            ]
        return WeeklyReport(
            period=raw.get("period", PENDING),
            work_done=_items("work_done"),
            plans=_items("plans"),
            risks=[str(r) for r in raw.get("risks", [])],
        )
    except Exception:  # noqa: BLE001
        return None


def render_markdown(report: WeeklyReport) -> str:
    lines = [
        f"# 周报（{report.period}）",
        "",
        "## 本周工作",
    ]
    if report.work_done:
        for item in report.work_done:
            lines.append(f"- [{item.status}] {item.content}")
    else:
        lines.append(f"- {PENDING}")
    lines += ["", "## 下周计划"]
    if report.plans:
        for item in report.plans:
            lines.append(f"- [{item.status}] {item.content}")
    else:
        lines.append(f"- {PENDING}")
    lines += ["", "## 风险与问题"]
    if report.risks:
        lines.extend(f"- {r}" for r in report.risks)
    else:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


class WeeklyReportSkill(Skill):
    name = "weekly_report"
    display_name = "周报整理"
    input_schema = {"type": "string", "description": "本周工作记录（建议分节：本周工作/下周计划/风险）"}
    output_schema = {"type": "object", "description": "WeeklyReport JSON"}
    tools = ["extract_fields", "validate", "render_markdown"]
    acceptance_samples = "tests/test_weekly_report.py 内联样本"

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
            log("extract_fields", "failed", "输入为空，已阻止生成")
            return {**state, "tool_logs": logs,
                    "error_message": "请输入非空工作记录后再生成周报"}

        report = llm_extract(text)
        engine = "llm" if report is not None else "rule"
        if report is None:
            report = rule_based_extract(text)
        log("extract_fields", "success", "" if engine == "llm" else "引擎=规则降级")

        # 零编造校验：period 必须有原文日期依据
        if report.period != PENDING:
            dates_in_text = _find_period(text)
            if dates_in_text == PENDING:
                report = report.model_copy(update={"period": PENDING})
                log("validate", "success", "统计周期原文无依据，回退为待确认")
            else:
                log("validate", "success")
        else:
            log("validate", "success")

        markdown = render_markdown(report)
        log("render_markdown", "success")

        return {
            **state,
            "structured_result": report.model_dump(),
            "markdown": markdown,
            "tool_logs": logs,
            "export_status": "pending",
            "error_message": "",
        }
