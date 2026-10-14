"""基于 LangExtract 的会议纪要抽取（替代 extract_fields.py 的 LLM 分支）。

设计要点（对应 docs/开源选型.md 方案 A）：

1. **零编造由库保证**：每条抽取都由 LangExtract 对齐回原文的精确字符区间
   （`char_interval`）。对齐失败的结果 `char_interval is None`，本模块**直接丢弃**，
   因此"原文没有的负责人/日期"在结构上不可能进入输出。
2. **中文必须用 UnicodeTokenizer**：LangExtract 默认的 `RegexTokenizer` 把连续的
   汉字视为**一个** token（其词模式 `[^\\W\\d_]+`），中文对齐与分句都会失效。
   `UnicodeTokenizer` 对 CJK 逐字成 token 并把 `。！？` 视为句末，中文场景必须显式传入。
3. **LLM 只负责"定位"，代码负责"切分"**：模型输出抽取文本与属性，证据片段由本模块
   从原文按字符区间切出，模型无法伪造证据。
4. **失败不静默**：与 `extract_fields.llm_extract` 返回 None 不同，本模块抛出异常，
   由调用方决定是否降级到规则引擎。

环境变量：
- OFFICEFLOW_LANGEXTRACT_ENABLE      是否启用（默认 0，显式开启才走 LangExtract）
- OFFICEFLOW_LANGEXTRACT_BATCH_LENGTH 每批分块数（默认 10）
- OFFICEFLOW_LANGEXTRACT_MAX_WORKERS  并发数（默认 4）
- OFFICEFLOW_LANGEXTRACT_SCHEMA_CONSTRAINTS 是否用 json_schema 强约束（默认 0）
  说明：DeepSeek 兼容端点对 `response_format={"type":"json_schema"}` 支持不一，
  默认关闭（走 json_object + few-shot），对齐到原文的能力不受影响。
- OFFICEFLOW_LANGEXTRACT_PASSES       抽取遍数（默认 1；>1 提升召回但成倍计费）

复用 `app.llm` 的既有配置：OFFICEFLOW_MODEL_BASE_URL / API_KEY / NAME。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.schemas import PENDING, ActionItem, MeetingMinutes

logger = logging.getLogger("officeflow.langextract")

# ---------- 可选依赖：未安装时 is_available() 返回 False，不影响规则引擎离线路径 ----------

try:  # pragma: no cover - 取决于部署环境
    import langextract as lx
    from langextract.core.tokenizer import UnicodeTokenizer

    HAS_LANGEXTRACT = True
    IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - 任何导入失败都降级
    lx = None  # type: ignore[assignment]
    UnicodeTokenizer = None  # type: ignore[assignment]
    HAS_LANGEXTRACT = False
    IMPORT_ERROR = str(exc)


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL_NAME = "deepseek-chat"

PROMPT_DESCRIPTION = (
    "从中文会议原始记录中抽取会议要素。"
    "抽取文本必须逐字来自原文，不得改写、翻译、补全或概括。"
    "每条抽取的 attributes 值也必须是原文中明确出现的字面内容；"
    "原文未提供该信息时，省略该属性，不要臆造，也不要填写\"待确认\"之类的占位词。"
    "按原文出现顺序输出。"
)

_CLASS_ACTION = "action_item"
_CLASS_DECISION = "decision"
_CLASS_DATE = "meeting_date"
_CLASS_TOPIC = "topic"

# 无依据字段的统一占位符，与 app.schemas.PENDING 保持一致
_PENDING = PENDING


# ---------- few-shot 示例 ----------
# 要求：examples 的 text 需真实包含每条 extraction_text（LangExtract 会做
# prompt alignment 校验，不许出现示例里找不到的抽取）。
_EXAMPLE_TEXT = (
    "2026年10月9日项目例会，决定先上线纪要模块。"
    "待办：张三在10月12日前完成解析接口。"
)

_EXAMPLE_EXTRACTIONS = [
    # (抽取类别, 原文片段, 属性)
    (_CLASS_DATE, "2026年10月9日", None),
    (_CLASS_TOPIC, "项目例会", None),
    (_CLASS_DECISION, "先上线纪要模块", None),
    (
        _CLASS_ACTION,
        "待办：张三在10月12日前完成解析接口",
        {"owner": "张三", "deadline": "10月12日"},
    ),
]


def _build_examples() -> list[Any]:
    """构造 few-shot 示例（每次调用重建，避免跨调用共享可变状态）。"""
    return [
        lx.data.ExampleData(
            text=_EXAMPLE_TEXT,
            extractions=[
                lx.data.Extraction(
                    extraction_class=cls,
                    extraction_text=text,
                    attributes=attrs,
                )
                for cls, text, attrs in _EXAMPLE_EXTRACTIONS
            ],
        )
    ]


# ---------- 配置与环境判断 ----------


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _api_key() -> str:
    return os.getenv("OFFICEFLOW_MODEL_API_KEY", "").strip()


def is_available() -> bool:
    """三个条件同时满足才可用：库已安装、开关打开、配置了 API Key。"""
    return HAS_LANGEXTRACT and _env_flag("OFFICEFLOW_LANGEXTRACT_ENABLE") and bool(_api_key())


def unavailable_reason() -> str | None:
    """返回不可用原因；可用时返回 None。便于 /api/status 与启动自检展示。"""
    if not HAS_LANGEXTRACT:
        return f"未安装 langextract（{IMPORT_ERROR}）；pip install \"langextract[openai]\""
    if not _env_flag("OFFICEFLOW_LANGEXTRACT_ENABLE"):
        return "OFFICEFLOW_LANGEXTRACT_ENABLE 未开启（默认 0）"
    if not _api_key():
        return "OFFICEFLOW_MODEL_API_KEY 未配置"
    return None


def build_model_config() -> Any:
    """把 OfficeFlow 的既有 LLM 配置映射为 LangExtract 的 OpenAI 兼容配置。

    显式指定 provider="openai"：

    - 模型名（如 deepseek-chat）不在 LangExtract 的内置 OpenAI 匹配模式内，
      自动路由会失败，必须显式指定 provider。
    - `provider_kwargs` 里的 base_url/api_key 会透传给底层 openai SDK 客户端。
    """
    from langextract.factory import ModelConfig

    base_url = os.getenv("OFFICEFLOW_MODEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model_name = os.getenv("OFFICEFLOW_MODEL_NAME", DEFAULT_MODEL_NAME)

    return ModelConfig(
        model_id=model_name,
        provider="openai",
        provider_kwargs={
            "api_key": _api_key(),
            "base_url": base_url,
            "temperature": 0.0,
            "max_workers": _env_int("OFFICEFLOW_LANGEXTRACT_MAX_WORKERS", 4),
        },
    )


# ---------- 结果映射 ----------


def _slice_source(source: str, extraction: Any) -> str:
    """按 char_interval 从原文切出证据片段。

    模型不产出证据文本，证据由代码从原文切分，因此无法伪造。
    """
    interval = getattr(extraction, "char_interval", None)
    if interval is None:
        return ""
    return source[interval.start_pos : interval.end_pos]


def _plain(value: Any) -> str:
    """attributes 值可能是 str 或 list[str]，统一成去空白的字符串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "".join(str(v) for v in value)
    return str(value).strip()


def to_meeting_minutes(source: str, extractions: list[Any]) -> MeetingMinutes:
    """把 LangExtract 的抽取列表映射为 MeetingMinutes。

    关键规则：`char_interval is None`（无法在原文定位）的抽取一律丢弃，
    这是零编造约束的落点。
    """
    grounded = [e for e in extractions if getattr(e, "char_interval", None) is not None]
    dropped = len(extractions) - len(grounded)
    if dropped:
        logger.warning("丢弃 %d 条未能在原文定位的抽取（零编造约束）", dropped)

    date = _PENDING
    topic = _PENDING
    decisions: list[str] = []
    action_items: list[ActionItem] = []

    for item in grounded:
        cls = getattr(item, "extraction_class", "")
        text = (getattr(item, "extraction_text", "") or "").strip()
        attrs = getattr(item, "attributes", None) or {}

        if cls == _CLASS_DATE and date == _PENDING and text:
            date = text
        elif cls == _CLASS_TOPIC and topic == _PENDING and text:
            topic = text
        elif cls == _CLASS_DECISION and text:
            decisions.append(text)
        elif cls == _CLASS_ACTION:
            owner = _plain(attrs.get("owner")) or _PENDING
            deadline = _plain(attrs.get("deadline")) or _PENDING
            action_items.append(
                ActionItem(
                    task=text,
                    owner=owner,
                    deadline=deadline,
                    evidence=_slice_source(source, item),
                )
            )

    return MeetingMinutes(
        date=date,
        topic=topic,
        decisions=decisions,
        action_items=action_items,
    )


# ---------- 主入口 ----------


def langextract_extract(text: str) -> MeetingMinutes:
    """用 LangExtract 抽取会议纪要。

    Raises:
        RuntimeError: 环境不可用（未安装 / 未开启 / 无 Key）。
        Exception: 模型调用或解析失败，由调用方决定是否降级到规则引擎。
    """
    reason = unavailable_reason()
    if reason:
        raise RuntimeError(f"LangExtract 不可用：{reason}")

    use_schema = _env_flag("OFFICEFLOW_LANGEXTRACT_SCHEMA_CONSTRAINTS")
    result = lx.extract(
        text_or_documents=text,
        prompt_description=PROMPT_DESCRIPTION,
        examples=_build_examples(),
        config=build_model_config(),
        tokenizer=UnicodeTokenizer(),  # 中文必需，否则汉字被合并成单个 token
        use_schema_constraints=use_schema,
        max_char_buffer=_env_int("OFFICEFLOW_LANGEXTRACT_MAX_CHAR_BUFFER", 1000),
        batch_length=_env_int("OFFICEFLOW_LANGEXTRACT_BATCH_LENGTH", 10),
        extraction_passes=_env_int("OFFICEFLOW_LANGEXTRACT_PASSES", 1),
        show_progress=False,
    )

    extractions = list(getattr(result, "extractions", None) or [])
    return to_meeting_minutes(text, extractions)


def extract_meeting_minutes(text: str) -> tuple[MeetingMinutes, str]:
    """抽取入口，接口与 extract_fields.extract_meeting_minutes 保持一致。

    返回 (结构化结果, 引擎标记)。引擎标记为 "langextract"；LangExtract 调用失败时
    **抛出异常而非静默降级**，由调用方显式决定回退到规则引擎（避免掩盖配置错误）。
    """
    return langextract_extract(text), "langextract"
