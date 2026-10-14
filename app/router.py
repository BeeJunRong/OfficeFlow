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

# 指令意图标记：**无冒号**时必须出现在开头，短文本才被当作"指令"。
# 否则像"下周周报由李四负责。"这种 10 字的会议正文会被当成短指令，
# 正文里的"周报"落进指令区 → 误路由（guardrails §3 明令禁止的场景）。
# 限制"开头"是必要的：描述句里也会出现这些词
# （如"会议记录已经整理完了。"含"整理"，但它是叙述而非指令）。
INSTRUCTION_MARKERS = (
    "帮我", "给我", "请你", "麻烦", "请",
    "整理", "做", "写", "生成", "总结", "处理", "来一份", "一份",
)
_INSTRUCTION_MARKER_RE = re.compile("|".join(map(re.escape, INSTRUCTION_MARKERS)))

# 允许标记前出现的最多字符数（覆盖"帮我整理…""请你写…"这类前缀）
_INSTRUCTION_MARKER_LEAD = 2


def has_instruction_marker(text: str) -> bool:
    """指令区**开头**是否含"请求意图"标记（帮我/请/整理/做/写…）。

    用于区分"用户在下指令"与"正文恰好含这些词"，见 INSTRUCTION_MARKERS 注释。
    要求标记落在前 _INSTRUCTION_MARKER_LEAD 个字符之内：
    "整理周报…""帮我整理周报…"通过，"会议记录已经整理完了。"不通过。
    """
    return bool(_INSTRUCTION_MARKER_RE.search((text or "")[:_INSTRUCTION_MARKER_LEAD]))


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
    - 关键词只匹配指令区（≤16 字符的短指令）；**无冒号**时还要求指令区含
      请求意图标记（帮我/请/整理/做…），否则短正文会因含关键词被误判为指令。
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
    # 有冒号 → 冒号前的短头部即为用户写的指令，可直接匹配；
    # 无冒号 → 前 16 字可能整段都是正文（如"下周周报由李四负责。"），
    #          必须含请求意图标记才认定为"指令"，否则交给 LLM 兜底/追问。
    instruction, _ = split_instruction(text)
    has_colon = ("：" in text) or (":" in text)
    if instruction and (has_colon or has_instruction_marker(instruction)):
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

