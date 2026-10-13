"""Skill 路由（FR-00）：显式点击优先 → 指令区关键词 → LLM 意图分类兜底。

依据 docs/architecture.md §4.2、docs/guardrails.md §3：
1. 前端显式传入 task_type 时直接锁定，不再看正文关键词。
2. 只检查输入开头或首个冒号前的"指令区"短指令做关键词匹配，
   正文中的"周报""邮件"不作为任务类型依据（T4 场景）。
3. LLM 意图分类兜底：置信度 < 0.7 时追问用户，不静默路由。
4. LLM 不可用时降级为"提示用户选择任务类型"（needs_clarification）。
"""

import re
from dataclasses import dataclass

from app.llm import get_llm
from app.schemas import VALID_TASK_TYPES

CONFIDENCE_THRESHOLD = 0.7

# 指令区关键词表：短语 → 任务类型（只作用于指令区，不作用于正文）
KEYWORD_TABLE: list[tuple[str, str]] = [
    ("会议纪要", "meeting_minutes"),
    ("整理会议", "meeting_minutes"),
    ("会议记录", "meeting_minutes"),
    ("会议总结", "meeting_minutes"),
    ("周报", "weekly_report"),
    ("邮件", "email_draft"),
    ("网页", "web_summary"),
    ("网站", "web_summary"),
]

INTENT_SYSTEM_PROMPT = (
    "你是任务型助手的意图分类器。根据用户输入判断其想要的办公任务类型。"
    "可选类型：meeting_minutes（会议纪要）、weekly_report（周报整理）、"
    "email_draft（邮件草稿）、web_summary（网页整理）、unknown（无法判断）。"
    "注意：只依据用户指令意图判断，正文内容中出现的词汇不代表任务类型。"
    '仅输出 JSON：{"task_type": "...", "confidence": 0.0~1.0}'
)


@dataclass
class RouteResult:
    task_type: str
    routed_by: str  # explicit / keyword / llm / none
    needs_clarification: bool = False


INSTRUCTION_MAX_LEN = 16  # 指令区最长字符数；超长视为正文而非短指令


def split_instruction(text: str) -> tuple[str, str]:
    """把输入拆为（指令区, 正文）。

    指令区 = 首个中文/英文冒号之前的部分；无冒号时取前 16 个字符。
    指令区超长（>16 字符）说明冒号属于正文（如"待办："），
    不再按指令区处理，正文关键词不参与路由（FR-00/T4）。
    """
    if not text:
        return "", ""
    for sep in ("：", ":"):
        if sep in text:
            head, _, body = text.partition(sep)
            head = head.strip()
            if len(head) <= INSTRUCTION_MAX_LEN:
                return head, body.strip()
            return "", text.strip()  # 超长头部不是指令区
    return text[:INSTRUCTION_MAX_LEN].strip(), text.strip()


def route_task(input_text: str, explicit_task_type: str | None = None) -> RouteResult:
    """分层路由入口。

    - explicit_task_type 来自前端提示按钮，优先级最高且锁定。
    - 关键词只匹配指令区（≤16 字符的短指令）。
    - LLM 兜底；不可用或低置信度 → needs_clarification=True。
    """
    if explicit_task_type:
        if explicit_task_type not in VALID_TASK_TYPES:
            raise ValueError(f"未知任务类型: {explicit_task_type}")
        return RouteResult(task_type=explicit_task_type, routed_by="explicit")

    text = (input_text or "").strip()
    if not text:
        return RouteResult(task_type="", routed_by="none", needs_clarification=True)

    # 第二层：指令区关键词（仅短指令区；正文关键词不触发）
    instruction, _ = split_instruction(text)
    if instruction:
        for keyword, task_type in KEYWORD_TABLE:
            if keyword in instruction:
                return RouteResult(task_type=task_type, routed_by="keyword")

    # 第三层：LLM 意图分类兜底
    llm = get_llm()
    if llm.is_available:
        result = llm.chat_json(INTENT_SYSTEM_PROMPT, text)
        if result is not None:
            task_type = result.get("task_type", "unknown")
            try:
                confidence = float(result.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if task_type in VALID_TASK_TYPES and confidence >= CONFIDENCE_THRESHOLD:
                return RouteResult(task_type=task_type, routed_by="llm")
            # 低置信度或未知类型：追问，不静默路由
            return RouteResult(task_type="", routed_by="llm", needs_clarification=True)

    # 降级方案：提示用户选择任务类型
    return RouteResult(task_type="", routed_by="none", needs_clarification=True)


# 模板指令前缀：仅匹配开头的祈使短句（如"请帮我整理会议纪要："），
# 不误伤正文中的冒号（如"待办：……"）。
_TEMPLATE_PREFIX_RE = re.compile(
    r"^\s*[请帮麻烦了的了\s]{0,4}"
    r"(?:帮我|给我|麻烦你?|请你?)?\s*"
    r"(?:整理|做|写|生成|总结|处理)?\s*"
    r"(?:一份|一个|下)?\s*"
    r"(会议纪要|会议记录|会议总结|周报|邮件草稿|邮件|网页内容|网页|网站内容)"
    r"\s*[:：]\s*",
    re.DOTALL,
)

# 前端模板占位符（用户可能忘记替换，生成前剔除）
_PLACEHOLDERS = (
    "[粘贴会议记录]", "[粘贴本周工作记录]", "[说明邮件背景与要点]", "[粘贴网页文字]",
)


def strip_template_prefix(text: str) -> str:
    """去掉前端模板填充的指令前缀（如"请帮我整理会议纪要："），保留正文。

    前端点击提示按钮会把模板整体填入输入框，用户在冒号后粘贴记录；
    生成时只把正文交给抽取工具。只有匹配祈使句模板时才剥离，
    普通正文中的冒号（"待办：""决定："）不受影响。
    """
    m = _TEMPLATE_PREFIX_RE.match(text)
    body = text[m.end():] if m else text
    for ph in _PLACEHOLDERS:
        body = body.replace(ph, "")
    return body.strip()

