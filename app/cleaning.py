"""输入数据清理模块。

在会议纪要提取之前对输入文本进行格式清理，去除格式噪声（多余空格、
多余空行、不可见字符、混合换行符、首尾空白），同时严格保留原文实体
字符串（人名、日期、地点、标点等），确保零编造校验（validate_minutes
的字符串包含检查）仍能正确匹配。

清理管道（固定顺序）：
  1. 统一换行符（\\r\\n / \\r → \\n）        FR-DC-04
  2. 去除不可见字符（零宽/BOM/控制字符）     FR-DC-03
  3. 合并行内连续空格                        FR-DC-01
  4. 合并连续空行                            FR-DC-02
  5. 去除首尾空白                            FR-DC-05

保证：
  - 纯函数，无副作用（NFR-DC-01）
  - 不依赖外部服务（NFR-DC-02）
  - 确定性：相同输入永远产生相同输出（NFR-DC-03）
  - 幂等性：clean_input_text(clean_input_text(x)) == clean_input_text(x)（NFR-DC-04）
  - 实体保留：人名、日期、地点等实体字符串原样保留（FR-DC-06）
"""

import re

__all__ = ["clean_input_text"]


# ── 预编译正则 ──────────────────────────────────────────────

# Step 1: 换行符统一
_CRLF_RE = re.compile(r"\r\n")
_CR_RE = re.compile(r"\r")

# Step 2: 不可见字符 —— 零宽字符、BOM、控制字符（保留 \t \n \r）
_INVISIBLE_RE = re.compile(
    r"[\u200B\u200C\u200D\uFEFF\x00-\x08\x0B\x0C\x0E-\x1F]"
)

# Step 3: 行内连续空格（2 个及以上）
_MULTI_SPACE_RE = re.compile(r" {2,}")

# Step 4: 连续空行（2 个及以上只含空白字符的行）
_BLANK_LINES_RE = re.compile(r"(\n[ \t]*){2,}")


# ── 私有清理函数 ────────────────────────────────────────────

def _normalize_newlines(text: str) -> str:
    """统一换行符：\\r\\n → \\n，单独 \\r → \\n。

    对应需求：FR-DC-04
    """
    text = _CRLF_RE.sub("\n", text)
    text = _CR_RE.sub("\n", text)
    return text


def _strip_invisible_chars(text: str) -> str:
    """去除不可见字符：零宽字符、BOM、控制字符。

    保留 \\t（U+0009）、\\n（U+000A）、\\r（U+000D，由 Step 1 处理）。

    对应需求：FR-DC-03
    """
    return _INVISIBLE_RE.sub("", text)


def _collapse_spaces(text: str) -> str:
    """合并行内连续空格为单个空格，并去除每行首尾空格。

    按行处理，避免跨行空格被错误合并。同时去除每行首尾空格，
    避免行尾/行首空格在后续空行合并中产生意外。

    对应需求：FR-DC-01
    """
    lines = text.split("\n")
    collapsed = [_MULTI_SPACE_RE.sub(" ", line).strip(" ") for line in lines]
    return "\n".join(collapsed)


def _collapse_blank_lines(text: str) -> str:
    """合并连续空行为单个空行。

    空行指只含空格/制表符或完全为空的行。连续 2 个及以上空行
    合并为 1 个空行（即两个换行符 \\n\\n）。

    对应需求：FR-DC-02
    """
    return _BLANK_LINES_RE.sub("\n\n", text)


def _strip_edges(text: str) -> str:
    """去除首尾空白字符。

    对应需求：FR-DC-05
    """
    return text.strip()


# ── 公开管道函数 ────────────────────────────────────────────

def clean_input_text(text: str) -> str:
    """对输入文本执行格式清理，去除格式噪声同时保留原文实体字符串。

    清理步骤（固定顺序）：
    1. 统一换行符（\\r\\n / \\r → \\n）
    2. 去除不可见字符（零宽字符、BOM、控制字符）
    3. 合并行内连续空格
    4. 合并连续空行
    5. 去除首尾空白

    保证：
    - 纯函数，无副作用
    - 幂等性：clean_input_text(clean_input_text(x)) == clean_input_text(x)
    - 实体保留：人名、日期、地点等实体字符串原样保留

    Args:
        text: 待清理的输入文本。

    Returns:
        清理后的文本。空输入返回空字符串。
    """
    if not text:
        return ""
    text = _normalize_newlines(text)
    text = _strip_invisible_chars(text)
    text = _collapse_spaces(text)
    text = _collapse_blank_lines(text)
    text = _strip_edges(text)
    return text
