"""结构化抽取工具：会议文本 → MeetingMinutes（architecture.md §6）。

三层实现，逐级降级：
1. LangExtract 抽取（显式开启且依赖/Key 就绪时优先）——输出带字符级原文溯源
   （char_interval），对齐失败的结果直接丢弃，零编造由库层面保证。
   见 app/tools/langextract_extract.py。
2. LLM 抽取（配置 API Key 时）——输出仍需经校验节点零编造检查。
3. 规则抽取（离线降级/无 Key 时）——确定性正则 + 关键词模式，
   保证无网络环境下 13 例样本可离线复现，且天然不编造。

三层都只填原文明确出现的字段，缺失留 "待确认"，由校验节点兜底。
"""

import logging
import re
from datetime import datetime
from typing import Any

from app.llm import get_llm
from app.schemas import PENDING, ActionItem, MeetingMinutes

logger = logging.getLogger("officeflow.extract")


def build_extract_prompt(current_year: int | None = None) -> str:
    """构造抽取提示词（字段清单=标准六段式模板，与 schemas.MeetingMinutes 对齐）。

    年份补全规则必须写死"用哪个年"，否则模型会自己猜：
    实测 B4（原文只有"10月14日"、全篇无年份）时模型返回了 **2024**-10-14，
    被零编造校验器拦下回退为"待确认"（护栏生效，但不符合
    guardrails §2"时间信息可按明确年月日标准化"的预期）。
    这里改为显式指定优先级：文档内已有年份 > 当前年份。
    """
    year = current_year or datetime.now().year
    return (
        "你是会议纪要抽取器。从用户提供的会议原始记录中抽取结构化字段。"
        "严格遵守零编造规则：只提取原文明确出现的信息；"
        "原文没有的字段一律填字符串 \"待确认\"（人员列表字段填空数组），"
        "不得推测或编造负责人、日期、人员、地点。"
        "日期统一标准化为 YYYY-MM-DD。原文只有月日、没有年份时，"
        f"按以下优先级补全年份：① 同一篇原文中已出现的年份；② 若全篇无任何年份，"
        f"则使用当前年份 {year}。（补全年份属于 guardrails §2 允许的"
        "「明确年月日标准化」，不算编造；但年份绝不可臆测为其它数字。）"
        "原文完全没有日期依据时，一律填 \"待确认\"。"
        "topic 用原文中出现的会议名称原样短语，不要增删词。"
        "每条待办必须附 evidence 字段：原文中支持该待办的简短原句片段。"
        '输出 JSON：{"date": "...", "location": "...", "host": "...", '
        '"attendees": ["..."], "observers": ["..."], "absentees": ["..."], '
        '"recorder": "...", "reviewer": "...", "topic": "...", '
        '"agenda": ["..."], "discussion": ["..."], "decisions": ["..."], '
        '"action_items": [{"task": "...", "owner": "...", "co_owner": "...", '
        '"deadline": "...", "deliverable": "...", "evidence": "..."}], '
        '"risks": ["..."], "next_meeting": {"time": "...", "location": "...", "agenda": "..."}}'
    )


# 向后兼容：默认提示词（使用当前年份）
EXTRACT_SYSTEM_PROMPT = build_extract_prompt()

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

# ---------- 标准模板元信息（标签优先，缺省"待确认"，不编造） ----------

_META_LABELS = {
    "location": re.compile(r"地点[：:]\s*([^\n，。；]+)"),
    "host": re.compile(r"主持人[：:]\s*([^\n，。；]+)"),
    "recorder": re.compile(r"记录人[：:]\s*([^\n，。；]+)"),
    "reviewer": re.compile(r"审核人[：:]\s*([^\n，。；]+)"),
}
_PEOPLE_LABELS = {
    "attendees": re.compile(r"出席(?:人员|人)?[：:]\s*([^\n。]+)"),
    "observers": re.compile(r"列席(?:人员|人)?[：:]\s*([^\n。]+)"),
    "absentees": re.compile(r"缺席(?:人员|人)?[：:]\s*([^\n。]+)"),
}
_AGENDA_LABEL = re.compile(r"议题[：:]\s*([^\n]+)")

# 隐式模式（原文常见表述）
_IMPLICIT_LOCATION_RE = re.compile(
    r"在([一-龥A-Za-z0-9]{2,12}(?:会议室|办公室|教室|实验室|礼堂|大厅|第一会议室|线上会议|线上))"
)
_IMPLICIT_HOST_RE = re.compile(r"由([一-龥]{2,4})(?:主持|担任主持)")
_IMPLICIT_ATTENDEES_RE = re.compile(
    r"([一-龥]{2,4}(?:[、，,][一-龥]{2,4}){1,11})(?:等?(?:共)?\d*人?)?(出席|参加)"
)
_IMPLICIT_RECORDER_RE = re.compile(r"由([一-龥]{2,4})(?:负责)?记录")

# 协办 / 输出物（待办句内）
# 两种语序都要认：主语后置（"王敏协办"）与前置（"协办：王敏"）。
_CO_OWNER_RE = re.compile(r"(?:协办|配合|协助)[：:]?\s*([一-龥]{2,4})")
_CO_OWNER_PREFIX_RE = re.compile(r"([一-龥]{2,4})(?:负责)?(?:协办|配合|协助)")
# 规则抽取的降级路径要挡掉最常见的非人名误命中
_NOT_A_NAME = {"共同", "大家", "我们", "全体", "全员", "部门", "团队", "双方", "各方"}
_DELIVERABLE_RE = re.compile(r"(?:输出|交付|提交|产出)[：:]?\s*([一-龥A-Za-z0-9]{2,16})")

# 风险 / 下次会议
_RISK_HINTS_RE = re.compile(r"风险|遗留问题|隐患|存在.{0,6}问题|担心")
_NEXT_TIME_RE = re.compile(
    r"下次会议[^。；]{0,12}?(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?|\d{1,2}月\d{1,2}日|下周[一二三四五六日末])"
)
_NEXT_LOCATION_RE = re.compile(
    r"下次会议[^。；]{0,20}?(?:在|于)([一-龥A-Za-z0-9]{2,12}(?:会议室|办公室|教室|实验室|线上))"
)
_NEXT_AGENDA_RE = re.compile(r"下次会议[^。；]{0,12}(?:议题|讨论)(?:为|是|：|:)?\s*([^。；]+)")


def _split_people(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[、，,\s]+", raw) if p.strip()]

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


_TODO_CLAUSE_SEPARATOR_RE = re.compile(
    r"[，,]\s*(?=[一-龥]{2,4}在"
    r"(?:\d{1,4}[年/-])?\d{1,2}[月/-]\d{1,2}日?前"
    r"(?:完成|提交|编写|准备|整理|负责|跟进|推进|落实|开发|测试|制作|修改|完善))"
)


def _split_todo_clauses(sentence: str) -> list[str]:
    """Split one todo sentence into clauses, one owner assignment per clause."""
    return [part.strip() for part in _TODO_CLAUSE_SEPARATOR_RE.split(sentence) if part.strip()]


def _clean_evidence(sentence: str) -> str:
    """Keep the source clause while removing the routing marker from its start."""
    return _MARKER_RE.sub("", sentence, count=1).strip(" ，,")


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

    sentences = [
        clause
        for sentence in _split_sentences(text)
        for clause in _split_todo_clauses(sentence)
    ]
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
        # 协办 / 输出物：待办句内的明确表述，缺省"待确认"
        co_owner = PENDING
        m = _CO_OWNER_RE.search(sent)
        if m and m.group(1) in sent:
            co_owner = m.group(1)
        else:
            m = _CO_OWNER_PREFIX_RE.search(sent)
            if m and m.group(1) not in _NOT_A_NAME:
                co_owner = m.group(1)
        deliverable = PENDING
        m = _DELIVERABLE_RE.search(sent)
        if m:
            deliverable = m.group(1)
        action_items.append(
            ActionItem(
                task=_clean_task(sent, name),
                owner=name or PENDING,
                co_owner=co_owner,
                deadline=deadline,
                deliverable=deliverable,
                evidence=_clean_evidence(sent),
            )
        )
        consumed.add(idx)

    # 决定判定：取提示词之后的内容；否定句（"没有形成决定"）跳过
    decision_indexes: set[int] = set()
    for idx, sent in enumerate(sentences):
        if idx in consumed or _DECISION_NEGATION_RE.search(sent):
            continue
        m = _DECISION_HINT_RE.search(sent)
        if not m:
            continue
        decision = _PUNCT_RE.sub("", sent[m.end():])
        if decision:
            decisions.append(decision)
            decision_indexes.add(idx)

    # ---- 标准模板元信息：标签优先 → 隐式模式，缺省"待确认" ----
    meta: dict[str, str] = {}
    for key, pattern in _META_LABELS.items():
        m = pattern.search(text)
        if m:
            meta[key] = m.group(1).strip()
    if "location" not in meta:
        m = _IMPLICIT_LOCATION_RE.search(text)
        if m:
            meta["location"] = m.group(1)
    if "host" not in meta:
        m = _IMPLICIT_HOST_RE.search(text)
        if m:
            meta["host"] = m.group(1)
    if "recorder" not in meta:
        m = _IMPLICIT_RECORDER_RE.search(text)
        if m:
            meta["recorder"] = m.group(1)

    people: dict[str, list[str]] = {}
    for key, pattern in _PEOPLE_LABELS.items():
        m = pattern.search(text)
        if m:
            people[key] = _split_people(m.group(1))
    if "attendees" not in people:
        m = _IMPLICIT_ATTENDEES_RE.search(text)
        if m:
            people["attendees"] = _split_people(m.group(1))

    # 议题：标签列表
    agenda: list[str] = []
    m = _AGENDA_LABEL.search(text)
    if m:
        agenda = [
            a.strip(" ，。,；;")
            for a in re.split(r"[\n；;]+|(?:[一二三四五六七八九十\d]+[、.．])", m.group(1))
            if a.strip(" ，。,；;")
        ]

    # 风险与遗留问题
    risks: list[str] = []
    risk_indexes: set[int] = set()
    for idx, sent in enumerate(sentences):
        if idx in consumed or idx in decision_indexes:
            continue
        if _RISK_HINTS_RE.search(sent):
            risks.append(_PUNCT_RE.sub("", sent))
            risk_indexes.add(idx)

    # 会议内容与讨论：剩余句子（排除元信息行、纯"日期+会议名"开头句）
    discussion: list[str] = []
    meta_label_re = re.compile(r"(地点|主持人|记录人|审核人|出席|列席|缺席|议题)[：:]")
    for idx, sent in enumerate(sentences):
        if idx in consumed or idx in decision_indexes or idx in risk_indexes:
            continue
        if meta_label_re.search(sent):
            continue
        stripped = _strip_dates(sent)
        # 纯"日期+XX例会/会议"开头的句子是标题句，不进讨论区
        if _TOPIC_RE.search(stripped) and len(stripped) <= 20:
            continue
        cleaned = _PUNCT_RE.sub("", sent)
        if cleaned:
            discussion.append(cleaned)

    # 下次会议安排
    next_meeting: dict[str, str] = {}
    for key, pattern in (
        ("time", _NEXT_TIME_RE),
        ("location", _NEXT_LOCATION_RE),
        ("agenda", _NEXT_AGENDA_RE),
    ):
        m = pattern.search(text)
        if m:
            next_meeting[key] = m.group(1).strip("，。；、")

    return MeetingMinutes(
        date=meeting_date,
        location=meta.get("location", PENDING),
        host=meta.get("host", PENDING),
        attendees=people.get("attendees", []),
        observers=people.get("observers", []),
        absentees=people.get("absentees", []),
        recorder=meta.get("recorder", PENDING),
        reviewer=meta.get("reviewer", PENDING),
        topic=topic,
        agenda=agenda,
        discussion=discussion,
        decisions=decisions,
        action_items=action_items,
        risks=risks,
        next_meeting=next_meeting,  # Pydantic 会转成 NextMeeting，缺失字段为"待确认"
    )


def llm_extract(text: str) -> MeetingMinutes | None:
    """LLM 抽取；失败返回 None（由调用方降级到规则抽取）。"""
    llm = get_llm()
    if not llm.is_available:
        return None
    raw: Any = llm.chat_json(build_extract_prompt(), text)
    if raw is None:
        return None
    try:
        items = [
            ActionItem(**item)
            for item in raw.get("action_items", [])
            if isinstance(item, dict)
        ]
        nm = raw.get("next_meeting") or {}
        if not isinstance(nm, dict):
            nm = {}
        return MeetingMinutes(
            date=raw.get("date", PENDING),
            location=raw.get("location", PENDING),
            host=raw.get("host", PENDING),
            attendees=[str(p) for p in raw.get("attendees", [])],
            observers=[str(p) for p in raw.get("observers", [])],
            absentees=[str(p) for p in raw.get("absentees", [])],
            recorder=raw.get("recorder", PENDING),
            reviewer=raw.get("reviewer", PENDING),
            topic=raw.get("topic", PENDING),
            agenda=[str(a) for a in raw.get("agenda", [])],
            discussion=[str(d) for d in raw.get("discussion", [])],
            decisions=[str(d) for d in raw.get("decisions", [])],
            action_items=items,
            risks=[str(r) for r in raw.get("risks", [])],
            next_meeting={
                "time": nm.get("time", PENDING),
                "location": nm.get("location", PENDING),
                "agenda": nm.get("agenda", PENDING),
            },
        )
    except Exception:  # noqa: BLE001 - LLM 输出不符 schema 时整体降级
        return None


def extract_meeting_minutes(text: str) -> tuple[MeetingMinutes, str]:
    """抽取入口：返回 (结构化结果, 使用的引擎标记)。

    引擎优先级（逐级降级，保证任何环境都能出结果）：
    1. ``langextract`` —— 需显式开启且装好依赖且配置 Key；带字符级原文溯源。
    2. ``llm``         —— OfficeFlow 自带的 OpenAI 兼容 JSON 抽取。
    3. ``rule``        —— 确定性规则引擎，离线可用（无 Key 时的默认路径）。

    LangExtract 失败时记 warning 并降级，不向用户暴露异常；
    是否真正启用由 ``OFFICEFLOW_LANGEXTRACT_ENABLE`` 决定（默认关闭）。
    """
    try:
        from app.tools.langextract_extract import (
            extract_meeting_minutes as langextract_extract,
        )
        from app.tools.langextract_extract import is_available as langextract_available

        if langextract_available():
            return langextract_extract(text)
    except Exception as exc:  # noqa: BLE001 - 任何失败都降级到既有路径
        logger.warning("LangExtract 抽取失败，降级到 LLM/规则抽取：%s", exc)

    result = llm_extract(text)
    if result is not None:
        return result, "llm"
    return rule_based_extract(text), "rule"
