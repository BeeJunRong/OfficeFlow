# 输入数据清理模块编码任务规划

| 字段 | 值 |
|------|-----|
| 模块名称 | 输入数据清理模块（Input Data Cleaning Module） |
| 版本 | v1.0 |
| 创建日期 | 2026-10-14 |
| 状态 | 待评审 |
| 关联规格 | `spec.md` v1.0 |
| 关联设计 | `design.md` v1.0 |

---

## 任务概览

| 指标 | 值 |
|------|-----|
| 主任务数 | 4 |
| 子任务数 | 11 |
| 覆盖需求数 | 17（6 功能需求 + 5 非功能需求 + 6 约束/验收标准） |

### 任务依赖图

```text
Task 1: 创建清理模块 app/cleaning.py
  ├── 1.1 实现 _normalize_newlines     (FR-DC-04)
  ├── 1.2 实现 _strip_invisible_chars  (FR-DC-03)
  ├── 1.3 实现 _collapse_spaces        (FR-DC-01)
  ├── 1.4 实现 _collapse_blank_lines   (FR-DC-02)
  ├── 1.5 实现 _strip_edges            (FR-DC-05)
  └── 1.6 组合 clean_input_text 管道   (FR-DC-06, NFR-01~04, AC-06)
         │
         ▼
Task 2: 编写单元测试 tests/test_cleaning.py
  ├── 2.1 功能需求测试                 (FR-DC-01~06, AC-DC-01)
  ├── 2.2 非功能需求测试               (NFR-DC-01~05, AC-DC-03/04/06)
  └── 2.3 实体保留测试                 (FR-DC-06, CON-DC-01/02/03)
         │
         ▼
Task 3: 集成到工作流 app/graph.py
  └── 3.1 修改 execute_node 调用清理    (CON-DC-05, CON-DC-06)
         │
         ▼
Task 4: 集成测试与回归验证
  ├── 4.1 validate_minutes 兼容性测试   (AC-DC-02)
  └── 4.2 现有测试套件回归              (AC-DC-05)
```

---

## Task 1: 创建清理模块 `app/cleaning.py`

**目标**：实现数据清理模块的核心代码，包含 5 个私有清理函数和 1 个公开管道函数。

**输入**：design.md 第 3 节（清理管道设计）和第 4 节（接口设计）。

**输出**：`app/cleaning.py` 文件，包含完整的清理管道实现。

**验收标准**：模块可被 `from app.cleaning import clean_input_text` 导入，所有函数类型标注完整，无 `Any` 类型。

---

### 1.1 实现 `_normalize_newlines`（统一换行符）

**描述**：实现私有函数 `_normalize_newlines(text: str) -> str`，将 `\r\n` 替换为 `\n`，再将剩余单独的 `\r` 替换为 `\n`。使用 `re` 标准库的正则替换，先处理 Windows 换行符 `\r\n`，再处理旧 Mac 换行符 `\r`，确保两种情况都正确统一为 `\n`。

**输入**：可能包含 `\r\n`、`\r`、`\n` 混合换行符的字符串。

**输出**：仅包含 `\n` 换行符的字符串。

**验收标准**：
- `\r\n` 被替换为单个 `\n`
- 单独的 `\r` 被替换为单个 `\n`
- 原有的 `\n` 保持不变
- 函数为纯函数，不修改输入参数

**覆盖需求**：FR-DC-04

**代码生成提示**：
```python
import re

def _normalize_newlines(text: str) -> str:
    """统一换行符：\\r\\n / \\r → \\n。"""
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\r", "\n", text)
    return text
```

---

### 1.2 实现 `_strip_invisible_chars`（去除不可见字符）

**描述**：实现私有函数 `_strip_invisible_chars(text: str) -> str`，使用正则表达式字符类去除零宽字符（U+200B、U+200C、U+200D）、BOM（U+FEFF）和控制字符（U+0000~U+0008、U+000B、U+000C、U+000E~U+001F）。保留制表符 `\t`（U+0009）、换行符 `\n`（U+000A）和回车符 `\r`（U+000D，由 Step 1 处理）。

**输入**：可能包含不可见字符的字符串。

**输出**：不含上述不可见字符的字符串。

**验收标准**：
- U+200B、U+200C、U+200D 被去除
- U+FEFF（BOM）被去除
- U+0000~U+0008、U+000B、U+000C、U+000E~U+001F 被去除
- `\t`（U+0009）保留
- `\n`（U+000A）保留
- `\r`（U+000D）保留

**覆盖需求**：FR-DC-03

**代码生成提示**：
```python
_INVISIBLE_RE = re.compile(r"[\u200B\u200C\u200D\uFEFF\x00-\x08\x0B\x0C\x0E-\x1F]")

def _strip_invisible_chars(text: str) -> str:
    """去除不可见字符：零宽字符、BOM、控制字符。"""
    return _INVISIBLE_RE.sub("", text)
```

---

### 1.3 实现 `_collapse_spaces`（合并行内连续空格）

**描述**：实现私有函数 `_collapse_spaces(text: str) -> str`，按 `\n` 分割为行，每行内将连续 2 个或更多空格（U+0020）合并为单个空格，再以 `\n` 拼接。按行处理确保跨行的空格不被错误合并。

**输入**：可能包含行内连续多空格的字符串。

**输出**：每行内无连续多空格的字符串。

**验收标准**：
- 行内连续 2+ 个空格合并为 1 个
- 单个空格保持不变
- 跨行空格不合并（行尾空格与下一行行首空格不合并）

**覆盖需求**：FR-DC-01

**代码生成提示**：
```python
_MULTI_SPACE_RE = re.compile(r" {2,}")

def _collapse_spaces(text: str) -> str:
    """合并行内连续空格为单个空格。"""
    lines = text.split("\n")
    collapsed = [_MULTI_SPACE_RE.sub(" ", line) for line in lines]
    return "\n".join(collapsed)
```

---

### 1.4 实现 `_collapse_blank_lines`（合并连续空行）

**描述**：实现私有函数 `_collapse_blank_lines(text: str) -> str`，将连续 2 个或更多空行（行内只含空格/制表符或完全为空）合并为单个空行。使用正则匹配连续的空白行并替换为单个 `\n`。

**输入**：可能包含连续空行的字符串。

**输出**：无连续空行的字符串。

**验收标准**：
- 连续 2+ 个空行合并为 1 个空行
- 单个空行保持不变
- 有内容的行不受影响

**覆盖需求**：FR-DC-02

**代码生成提示**：
```python
_BLANK_LINES_RE = re.compile(r"(\n[ \t]*){2,}")

def _collapse_blank_lines(text: str) -> str:
    """合并连续空行为单个空行。"""
    return _BLANK_LINES_RE.sub("\n", text)
```

---

### 1.5 实现 `_strip_edges`（去除首尾空白）

**描述**：实现私有函数 `_strip_edges(text: str) -> str`，调用 Python 内置 `str.strip()` 去除首尾空白字符（空格、制表符、换行符等）。

**输入**：首尾可能含空白字符的字符串。

**输出**：首尾无空白字符的字符串。

**验收标准**：
- 首部空白字符被去除
- 尾部空白字符被去除
- 首尾无空白时保持不变

**覆盖需求**：FR-DC-05

**代码生成提示**：
```python
def _strip_edges(text: str) -> str:
    """去除首尾空白字符。"""
    return text.strip()
```

---

### 1.6 组合 `clean_input_text` 管道函数

**描述**：实现公开函数 `clean_input_text(text: str) -> str`，按固定顺序串联上述 5 个私有函数，并在入口处处理空输入（`if not text: return ""`）。添加完整的 docstring 说明清理步骤、保证条件和对应需求。

**输入**：任意字符串（包括空字符串）。

**输出**：清理后的字符串。

**验收标准**：
- 空字符串输入返回空字符串，不抛出异常（AC-DC-06）
- 5 个清理步骤按固定顺序执行：换行符统一 → 不可见字符去除 → 空格合并 → 空行合并 → 首尾空白去除
- 函数为纯函数，不修改输入参数（NFR-DC-01）
- 幂等性：`clean_input_text(clean_input_text(x)) == clean_input_text(x)`（NFR-DC-04）
- 实体保留：人名、日期、地点、标点等实体字符串原样保留（FR-DC-06）

**覆盖需求**：FR-DC-06, NFR-DC-01, NFR-DC-02, NFR-DC-03, NFR-DC-04, AC-DC-06

**代码生成提示**：
```python
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
    """
    if not text:
        return ""
    text = _normalize_newlines(text)
    text = _strip_invisible_chars(text)
    text = _collapse_spaces(text)
    text = _collapse_blank_lines(text)
    text = _strip_edges(text)
    return text
```

---

## Task 2: 编写单元测试 `tests/test_cleaning.py`

**目标**：为清理模块编写全面的单元测试，覆盖所有功能需求、非功能需求和实体保留约束。

**输入**：`app/cleaning.py` 模块（Task 1 产出）、spec.md 验收标准。

**输出**：`tests/test_cleaning.py` 文件，所有测试通过。

**验收标准**：`pytest tests/test_cleaning.py -v` 全部通过，覆盖 AC-DC-01。

---

### 2.1 功能需求测试

**描述**：为 FR-DC-01 ~ FR-DC-05 各编写测试用例，验证每项功能需求的验收标准。包括：连续空格合并、单个空格保留、连续空行合并、单个空行保留、零宽字符去除、BOM 去除、控制字符去除、制表符保留、`\r\n` 转换、`\r` 转换、`\n` 保留、首尾空白去除。

**输入**：`clean_input_text` 函数。

**输出**：测试函数 `test_collapse_multiple_spaces`、`test_single_space_preserved`、`test_collapse_blank_lines`、`test_single_blank_line_preserved`、`test_strip_zero_width_chars`、`test_strip_bom`、`test_strip_control_chars`、`test_preserve_tab`、`test_normalize_crlf`、`test_normalize_cr`、`test_preserve_lf`、`test_strip_edges`。

**验收标准**：所有功能需求测试用例通过。

**覆盖需求**：FR-DC-01, FR-DC-02, FR-DC-03, FR-DC-04, FR-DC-05, AC-DC-01

**代码生成提示**：
```python
import pytest
from app.cleaning import clean_input_text

def test_collapse_multiple_spaces():
    assert clean_input_text("张三    负责") == "张三 负责"

def test_single_space_preserved():
    assert clean_input_text("王敏 负责") == "王敏 负责"

def test_collapse_blank_lines():
    assert clean_input_text("第一行\n\n\n\n第二行") == "第一行\n\n第二行"

def test_single_blank_line_preserved():
    assert clean_input_text("第一行\n\n第二行") == "第一行\n\n第二行"

def test_strip_zero_width_chars():
    assert clean_input_text("张\u200B三\u200C\u200D") == "张三"

def test_strip_bom():
    assert clean_input_text("\uFEFF会议纪要") == "会议纪要"

def test_strip_control_chars():
    assert clean_input_text("文本\x00\x01\x02内容") == "文本内容"

def test_preserve_tab():
    assert clean_input_text("缩进\t内容") == "缩进\t内容"

def test_normalize_crlf():
    assert clean_input_text("第一行\r\n第二行") == "第一行\n第二行"

def test_normalize_cr():
    assert clean_input_text("第一行\r第二行") == "第一行\n第二行"

def test_preserve_lf():
    assert clean_input_text("第一行\n第二行") == "第一行\n第二行"

def test_strip_edges():
    assert clean_input_text("  \n  会议内容  \n  ") == "会议内容"
```

---

### 2.2 非功能需求测试

**描述**：为 NFR-DC-01 ~ NFR-DC-05 和 AC-DC-03/04/06 编写测试用例，验证纯函数无副作用、确定性、幂等性、性能和空输入处理。性能测试需构造 100KB 文本并使用 `time.perf_counter()` 测量耗时。

**输入**：`clean_input_text` 函数。

**输出**：测试函数 `test_idempotent`、`test_deterministic`、`test_empty_input`、`test_pure_function`、`test_performance_100kb`。

**验收标准**：
- 幂等性：对多种输入连续两次清理结果相同
- 确定性：多次调用结果一致
- 空输入返回空字符串
- 纯函数：输入参数不被修改
- 性能：100KB 文本清理耗时 ≤ 100ms

**覆盖需求**：NFR-DC-01, NFR-DC-03, NFR-DC-04, NFR-DC-05, AC-DC-03, AC-DC-04, AC-DC-06

**代码生成提示**：
```python
import time

def test_idempotent():
    inputs = ["张三    负责\n\n\n\n李四", "\r\n文本\r\n", "\u200B\uFEFF内容"]
    for text in inputs:
        once = clean_input_text(text)
        twice = clean_input_text(once)
        assert once == twice, f"幂等性失败: {repr(text)}"

def test_deterministic():
    text = "测试  文本\n\n\n内容"
    results = [clean_input_text(text) for _ in range(10)]
    assert all(r == results[0] for r in results)

def test_empty_input():
    assert clean_input_text("") == ""
    assert clean_input_text("   ") == ""
    assert clean_input_text("\n\n\n") == ""

def test_pure_function():
    original = "张三    负责\n\n\n李四"
    snapshot = original[:]
    clean_input_text(original)
    assert original == snapshot, "输入参数被修改"

def test_performance_100kb():
    large_text = "会议内容  多空格  \n\n\n\n" * 5000  # ~100KB
    start = time.perf_counter()
    clean_input_text(large_text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms <= 100, f"性能不达标: {elapsed_ms:.1f}ms"
```

---

### 2.3 实体保留测试

**描述**：为 FR-DC-06 和 CON-DC-01/02/03 编写测试用例，验证清理后人名、日期、地点、标点等实体字符串完整保留。构造包含各类实体的测试文本，清理后逐一断言实体字符串仍在结果中。

**输入**：`clean_input_text` 函数。

**输出**：测试函数 `test_preserve_entities`。

**验收标准**：
- 人名实体（"张三"、"王敏"、"马浚荣"）清理后仍完整存在
- 日期实体（"2026年10月9日"、"10月14日"）清理后仍完整存在
- 地点实体（"第一会议室"、"线上"）清理后仍完整存在
- 标点符号（"："、"，"、"。"、"、"）清理后仍存在

**覆盖需求**：FR-DC-06, CON-DC-01, CON-DC-02, CON-DC-03

**代码生成提示**：
```python
def test_preserve_entities():
    text = (
        "2026年10月9日项目例会在第一会议室举行，由张三主持。"
        "出席人员：张三、李四、王敏。"
        "待办：马浚荣在10月14日前完成讲稿初稿。"
    )
    cleaned = clean_input_text(text)
    # 人名保留
    for name in ("张三", "李四", "王敏", "马浚荣"):
        assert name in cleaned, f"人名丢失: {name}"
    # 日期保留
    for date in ("2026年10月9日", "10月14日"):
        assert date in cleaned, f"日期丢失: {date}"
    # 地点保留
    for loc in ("第一会议室",):
        assert loc in cleaned, f"地点丢失: {loc}"
    # 标点保留
    for punct in ("：", "，", "。", "、"):
        assert punct in cleaned, f"标点丢失: {punct}"
```

---

## Task 3: 集成到工作流 `app/graph.py`

**目标**：在 `graph.py:execute_node()` 中集成 `clean_input_text` 调用，使清理模块在模板前缀剥离之后、Skill 执行之前生效。

**输入**：`app/cleaning.py` 模块（Task 1 产出）、`app/graph.py` 现有代码。

**输出**：修改后的 `app/graph.py`，`execute_node` 中新增 import 和清理调用。

**验收标准**：
- `execute_node` 中 `strip_template_prefix` 之后调用 `clean_input_text`
- 清理后文本传入 `skill.run()` 的 `state["input_text"]`
- 落库仍存原始 `req.input_text`（`main.py` 不改动，天然满足）

**覆盖需求**：CON-DC-05, CON-DC-06

---

### 3.1 修改 `execute_node` 调用清理

**描述**：在 `app/graph.py` 文件顶部新增 `from app.cleaning import clean_input_text` 导入语句。在 `execute_node` 函数中，`body = strip_template_prefix(...)` 之后新增 `cleaned = clean_input_text(body)`，并将 `skill.run({**state, "input_text": body})` 改为 `skill.run({**state, "input_text": cleaned})`。

**输入**：`app/graph.py` 当前 `execute_node` 实现。

**输出**：修改后的 `execute_node`，包含清理调用。

**验收标准**：
- import 语句位于文件顶部 import 区
- `execute_node` 中清理在 `strip_template_prefix` 之后执行
- 清理后文本传入 Skill
- 不影响 `needs_clarification` 分支（该分支直接返回 state，不经过清理）

**覆盖需求**：CON-DC-05, CON-DC-06

**代码生成提示**：
```python
# 在 app/graph.py 顶部 import 区新增：
from app.cleaning import clean_input_text

# execute_node 修改为：
def execute_node(state: AgentState) -> AgentState:
    """执行节点：分发到 Skill；会议纪要走完整抽取流程。"""
    if state.get("needs_clarification"):
        return state
    skill = get_skill(state["task_type"])
    body = strip_template_prefix(state.get("input_text", ""))
    cleaned = clean_input_text(body)
    return skill.run({**state, "input_text": cleaned})
```

---

## Task 4: 集成测试与回归验证

**目标**：验证清理模块集成后不破坏现有功能，特别是 `validate_minutes` 的零编造校验兼容性和 13 例合成样本的回归。

**输入**：集成后的 `app/graph.py`（Task 3 产出）、`tests/samples/samples.py` 中的 13 例样本。

**输出**：在 `tests/test_cleaning.py` 中新增集成测试函数，全部通过。

**验收标准**：`pytest tests/test_cleaning.py tests/test_fr01.py tests/test_router.py -v` 全部通过。

---

### 4.1 `validate_minutes` 兼容性测试

**描述**：编写集成测试，验证清理后文本传入 `validate_minutes` 时，原文中存在的实体值仍能通过字符串包含检查。使用 13 例合成样本，对每个样本执行清理 → 抽取 → 校验完整流程，断言校验结果中的回退项（corrections）不因清理而增加。

**输入**：`tests/samples/samples.py` 中的 `SAMPLES` 列表、`clean_input_text`、`extract_meeting_minutes`、`validate_minutes`。

**输出**：测试函数 `test_cleaning_with_validate_minutes`、`test_existing_samples_no_regression`。

**验收标准**：
- 对每个合成样本，清理后文本中实体值仍能被 `validate_minutes` 匹配
- 清理后校验回退项数量 ≤ 未清理时回退项数量（不增加）

**覆盖需求**：AC-DC-02, CON-DC-01, CON-DC-04

**代码生成提示**：
```python
from app.cleaning import clean_input_text
from app.tools.extract_fields import extract_meeting_minutes
from app.validate import validate_minutes
from tests.samples.samples import SAMPLES

def test_cleaning_with_validate_minutes():
    """清洗后实体仍能被 validate_minutes 正确匹配。"""
    for sample in SAMPLES:
        if sample["category"] == "exception":
            continue
        text = sample["input"]
        cleaned = clean_input_text(text)
        minutes, _ = extract_meeting_minutes(cleaned)
        fixed, corrections = validate_minutes(minutes, cleaned)
        # 清理后校验不应产生额外回退
        _, raw_corrections = validate_minutes(minutes, text)
        assert len(corrections) <= len(raw_corrections), (
            f"{sample['id']}: 清理导致回退项增加"
        )

def test_existing_samples_no_regression():
    """13 例合成样本清洗后抽取+校验流程正常。"""
    for sample in SAMPLES:
        if sample["category"] == "exception":
            continue
        cleaned = clean_input_text(sample["input"])
        minutes, engine = extract_meeting_minutes(cleaned)
        fixed, corrections = validate_minutes(minutes, cleaned)
        assert fixed is not None, f"{sample['id']}: 清洗后抽取失败"
```

---

### 4.2 现有测试套件回归

**描述**：运行现有测试套件（`test_fr01.py`、`test_router.py`、`test_config.py`、`test_export_retry.py`、`test_multiskill.py`、`test_web_summary.py`），确认引入清理模块后无回归。此任务为验证性任务，不编写新测试代码，而是执行现有测试并确认全部通过。

**输入**：集成后的完整代码。

**输出**：现有测试全部通过的确认。

**验收标准**：
- `pytest tests/ -v` 全部通过
- 无新增失败用例

**覆盖需求**：AC-DC-05

**代码生成提示**：
```bash
# 执行回归测试
cd /home/junrong/OfficeFlow
python -m pytest tests/ -v --tb=short
```

---

## 需求覆盖矩阵

| 需求 ID | 任务 ID | 覆盖方式 |
|---------|---------|---------|
| FR-DC-01 | 1.3, 2.1 | 实现 + 单元测试 |
| FR-DC-02 | 1.4, 2.1 | 实现 + 单元测试 |
| FR-DC-03 | 1.2, 2.1 | 实现 + 单元测试 |
| FR-DC-04 | 1.1, 2.1 | 实现 + 单元测试 |
| FR-DC-05 | 1.5, 2.1 | 实现 + 单元测试 |
| FR-DC-06 | 1.6, 2.3, 4.1 | 管道组合 + 实体测试 + 兼容性测试 |
| NFR-DC-01 | 1.6, 2.2 | 纯函数设计 + 纯函数测试 |
| NFR-DC-02 | 1.6 | 仅依赖 re 标准库（设计保证） |
| NFR-DC-03 | 1.6, 2.2 | 确定性设计 + 确定性测试 |
| NFR-DC-04 | 1.6, 2.2 | 幂等性设计 + 幂等性测试 |
| NFR-DC-05 | 2.2 | 性能测试 |
| CON-DC-01 | 2.3, 4.1 | 实体保留测试 + 兼容性测试 |
| CON-DC-02 | 2.3 | 标点保留测试 |
| CON-DC-03 | 2.3 | 语义不变测试（实体保留即语义不变） |
| CON-DC-04 | 4.1 | 兼容性测试（不与 _normalize_date_breaks 冲突） |
| CON-DC-05 | 3.1 | 集成位置正确（strip_template_prefix 之后） |
| CON-DC-06 | 3.1 | 落库不改动（main.py 不修改） |
| AC-DC-01 | 2.1 | 功能需求测试全覆盖 |
| AC-DC-02 | 4.1 | validate_minutes 兼容性测试 |
| AC-DC-03 | 2.2 | 幂等性测试 |
| AC-DC-04 | 2.2 | 性能测试 |
| AC-DC-05 | 4.2 | 现有测试回归 |
| AC-DC-06 | 1.6, 2.2 | 空输入处理 + 空输入测试 |
