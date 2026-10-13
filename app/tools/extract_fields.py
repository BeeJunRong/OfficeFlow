"""结构化抽取工具：会议文本 → MeetingMinutes（architecture.md §6）。

两层实现：
1. LLM 抽取（配置 API Key 时优先）——输出仍需经校验节点零编造检查。
2. 规则抽取（离线降级/无 Key 时）——确定性正则 + 关键词模式，
   保证无网络环境下 13 例样本可离线复现，且天然不编造。

两层都只填原文明确出现的字段，缺失留 "待确认"，由校验节点兜底。
"""

import re
from datetime import datetime
from typing import Any

from app.llm import get_llm
from app.schemas import PENDING, ActionItem, MeetingMinutes

EXTRACT_SYSTEM_PROMPT = (
    "你是会议纪要抽取器。从用户提供的会议原始记录中抽取结构化字段。"
    "严格遵守零编造规则：只提取原文明确出现的信息；"
    "原文没有的字段一律填字符串 \"待确认\"，不得推测或编造负责人、日期。"
    "日期统一标准化为 YYYY-MM-DD；若原文只有月日无年份，可结合会议日期年份补全。"
    "每条待办必须附 evidence 字段：原文中支持该待办的简短原句片段。"
    '输出 JSON：{"date": "...", "topic": "...", "decisions": ["..."], '
    '"action_items": [{"task": "...", "owner": "...", "deadline": "...", "evidence": "..."}]}'
)

# ---------- 规则抽取 ----------

_DATE_PATTERNS = [
    # 2026年10月9日 / 2026-10-09 / 2026/10/9（完整年月日）
    (re.compile(r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?"), "ymd"),
    # 10月9日（无年份，结合会议年份或当前年补全）
    (re.compile(r"(?<!\d)\d{1,2}月\d{1,2}日(?!\d)"), "md"),
]

_DECISION_HINT_RE = re.compile(r"(决定|决议|商定|一致同意|通过|暂缓了?|同意)")
_DECISION_NEGATION_RE = re.compile(r"(没有|未|无)[^，。；]{0,8}(决定|决议|商定|通过|同意)")

_TODO_HINTS = (
    "负责", "完成", "跟进", "落实", "推进", "执行", "准备", "编写", "提交",
    "整理", "调研", "开发", "测试", "标注", "制作", "修改", "完善",
)
_TODO_MARKER_RE = re.compile(r"(?<!没有)(?<!未)(?<!无)(待办|行动项)")
_TODO_NEGATION_RE = re.compile(r"(没有|未|无)[^，。；]{0,6}(待办|行动项|需要|负责)")

# 人名：常见姓氏 + 1-3 个汉字
_SURNAMES = (
    "李王张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾萧田董潘袁蔡蒋余于"
    "杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史"
    "陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
_NAME_RE = re.compile(rf"[{_SURNAMES}][一-龥]{{1,3}}")

# 人名后常跟的动词/虚词：贪婪匹配会吃进来，需裁掉（"王敏负责"→"王敏"）
_NAME_TRAILING_RE = re.compile(r"(负责|完成|汇报|提出|表示|建议|说|需要|需|将|于|在|的|等|与|和|及)+$")

# 人名词边界的上下文（前一字/后一字），用于从多个候选中挑出真正的人名
_BOUNDARY_BEFORE = set("：:，,。;；、 \n由给对和与跟请让派安排分（(")
_BOUNDARY_AFTER = set("：:，,。;；、 \n在将于需负责说要表示提出汇报之的前")

_NAME_CANDIDATE_MIN_LEN = 2


def _extract_name(sent: str) -> str | None:
    """从句子中提取人名。

    贪婪匹配会产出多个候选（如"下周周报由李四负责"中"周周报"与"李四"），
    按词边界上下文打分：前后都是边界字符（标点/由/在/负责…）的候选优先。
    """
    best: tuple[str, int] | None = None
    for m in _NAME_RE.finditer(sent):
        name = _NAME_TRAILING_RE.sub("", m.group(0))
        if not name or len(name) < _NAME_CANDIDATE_MIN_LEN:
            continue
        end = m.start() + len(name)
        before = sent[m.start() - 1] if m.start() > 0 else " "
        after = sent[end] if end < len(sent) else " "
        score = int(before in _BOUNDARY_BEFORE) + int(after in _BOUNDARY_AFTER)
        if score == 2:
            return name
        if best is None or score > best[1]:
            best = (name, score)
    return best[0] if best else None

# 话题：剥掉日期后的"XX例会/会议/周会/组会/评审/讨论"短语
_TOPIC_RE = re.compile(r"[一-龥A-Za-z]{2,12}?(?:例会|周会|组会|会议|评审|讨论)")

# 待办事项清洗：去掉日期/人名后的填充词
_MARKER_RE = re.compile(r"^(待办|行动项|事项)[：:，,、]?")
_LEADING_FILLERS = re.compile(r"^(请|需要|务必|按时|由|在|于)+")
# "前完成X" → "X"：截止助词 + 前导动词一起剥（"前"单独出现不剥，避免误伤"前端"）
_VERB_PREFIX_RE = re.compile(
    r"^(?:前|以前|日前|之内|内)?(完成|提交|编写|准备|整理|负责|跟进|推进|落实)"
)
_LEADING_VERBS = ("完成", "提交", "编写", "准备", "整理", "负责", "跟进", "推进", "落实")
_TRAILING_CLAUSES = (
    "，未约定完成日期", "，无截止日期", "，负责人待定", "，时间待定",
    "，具体日期待定", "，未指定负责人", "，具体时间待定",
)
_TRAILING_FILLERS = re.compile(r"(由|于|在|的|前|以前|日前|之)+$")
_TRAILING_VERBS_RE = re.compile(r"(负责|完成|提交|编写|准备|整理|跟进|推进|落实)+$")

_PUNCT_RE = re.compile(r"^[，,。；;、：:\s]+|[，,。；;、：:\s]+$")


def _norm_date(y: str | int, m: str | int, d: str | int) -> str:
    try:
        return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return PENDING


def _find_dates(text: str) -> list[tuple[str, str, str]]:
    """返回 [(标准化日期, 原文匹配串, 年份)]，按原文出现顺序。

    月日形式结合上下文年份（前文完整日期的年份，否则当前年）补全。
    """
    found: list[tuple[str, str, str]] = []
    spans: list[tuple[int, int]] = []

    def _skip(pos: int) -> bool:
        return any(s <= pos < e for s, e in spans)

    for regex, kind in _DATE_PATTERNS:
        for m in regex.finditer(text):
            if _skip(m.start()):
                continue
            if kind == "ymd":
                parts = re.split(r"[年月/-]", m.group(0))
                year = parts[0]
                date = _norm_date(year, parts[1], re.sub(r"日$", "", parts[2]))
            else:
                year = next((f[2] for f in found if f[0] != PENDING), str(datetime.now().year))
                mo, d = re.match(r"(\d{1,2})月(\d{1,2})日", m.group(0)).groups()  # type: ignore[union-attr]
                date = _norm_date(year, mo, d)
            if date != PENDING:
                found.append((date, m.group(0), str(year)))
                spans.append((m.start(), m.end()))
    return [f for _, f in sorted(  # 按原文位置排序
        zip(spans, found), key=lambda pair: pair[0]
    )]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。；;！!？?\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def _strip_dates(s: str) -> str:
    for regex, _ in _DATE_PATTERNS:
        s = regex.sub("", s)
    return s


def _clean_task(sentence: str, name: str | None) -> str:
    """从待办句子清洗出事项短语：剥日期、人名、填充词。"""
    s = _strip_dates(sentence)
    if name:
        s = s.replace(name, "", 1)
    for clause in _TRAILING_CLAUSES:
        s = s.replace(clause, "")
    s = _MARKER_RE.sub("", s)
    # 反复剥离前导填充词/动词与尾部填充词（"在前完成X" → "X"）
    changed = True
    while changed:
        changed = False
        new = _PUNCT_RE.sub("", s)
        new = _LEADING_FILLERS.sub("", new)
        new = _PUNCT_RE.sub("", new)
        new = _VERB_PREFIX_RE.sub(r"\1", new)
        for verb in _LEADING_VERBS:
            if new.startswith(verb):
                new = new[len(verb):]
        new = _TRAILING_VERBS_RE.sub("", new)
        new = _TRAILING_FILLERS.sub("", new)
        new = _PUNCT_RE.sub("", new)
        if new != s:
            s = new
            changed = True
    return s or sentence  # 清洗失败时保留原句，宁可冗余也不丢信息


def rule_based_extract(text: str) -> MeetingMinutes:
    """确定性规则抽取：离线降级引擎，模式覆盖 13 例合成样本句式。

    样本句式（评测计划 §4.1 及冻结样本）：
    - "2026年10月9日项目例会，决定先上线纪要模块。"
    - "待办：张三在10月12日前完成解析接口。"
    - "王敏负责调研公开网页解析方案，未约定完成日期。"
    - "待办：10月20日前完成原型设计，负责人待定。"（缺负责人）
    - "没有形成明确决定，也没有具体待办。"（全缺失）
    """
    dates = _find_dates(text)
    meeting_date = dates[0][0] if dates else PENDING
    meeting_year = dates[0][2] if dates else None

    # 主题：剥掉日期后的"XX例会/会议"短语
    topic = PENDING
    topic_m = _TOPIC_RE.search(_strip_dates(text))
    if topic_m:
        topic = topic_m.group(0)

    decisions: list[str] = []
    action_items: list[ActionItem] = []
    consumed: set[int] = set()

    sentences = _split_sentences(text)
    for idx, sent in enumerate(sentences):
        # 待办判定：显式"待办/行动项"标记，或（待办语义动词 + 人名）
        if _TODO_NEGATION_RE.search(sent):
            continue
        has_marker = _TODO_MARKER_RE.search(sent) is not None
        name = _extract_name(sent)
        is_todo = has_marker or (
            any(h in sent for h in _TODO_HINTS) and name is not None
        )
        if not is_todo:
            continue
        deadline = PENDING
        for regex, kind in _DATE_PATTERNS:
            m = regex.search(sent)
            if m:
                if kind == "ymd":
                    parts = re.split(r"[年月/-]", m.group(0))
                    deadline = _norm_date(parts[0], parts[1], re.sub(r"日$", "", parts[2]))
                else:
                    year = meeting_year or str(datetime.now().year)
                    mo, d = re.match(r"(\d{1,2})月(\d{1,2})日", m.group(0)).groups()  # type: ignore[union-attr]
                    deadline = _norm_date(year, mo, d)
                break
        action_items.append(
            ActionItem(
                task=_clean_task(sent, name),
                owner=name or PENDING,
                deadline=deadline,
                evidence=sent,
            )
        )
        consumed.add(idx)

    # 决定判定：取提示词之后的内容；否定句（"没有形成决定"）跳过
    for idx, sent in enumerate(sentences):
        if idx in consumed or _DECISION_NEGATION_RE.search(sent):
            continue
        m = _DECISION_HINT_RE.search(sent)
        if not m:
            continue
        decision = _PUNCT_RE.sub("", sent[m.end():])
        if decision:
            decisions.append(decision)

    return MeetingMinutes(
        date=meeting_date,
        topic=topic,
        decisions=decisions,
        action_items=action_items,
    )


def llm_extract(text: str) -> MeetingMinutes | None:
    """LLM 抽取；失败返回 None（由调用方降级到规则抽取）。"""
    llm = get_llm()
    if not llm.is_available:
        return None
    raw: Any = llm.chat_json(EXTRACT_SYSTEM_PROMPT, text)
    if raw is None:
        return None
    try:
        items = [
            ActionItem(**item)
            for item in raw.get("action_items", [])
            if isinstance(item, dict)
        ]
        return MeetingMinutes(
            date=raw.get("date", PENDING),
            topic=raw.get("topic", PENDING),
            decisions=[str(d) for d in raw.get("decisions", [])],
            action_items=items,
        )
    except Exception:  # noqa: BLE001 - LLM 输出不符 schema 时整体降级
        return None


def extract_meeting_minutes(text: str) -> tuple[MeetingMinutes, str]:
    """抽取入口：返回 (结构化结果, 使用的引擎标记 llm/rule)。"""
    result = llm_extract(text)
    if result is not None:
        return result, "llm"
    return rule_based_extract(text), "rule"
