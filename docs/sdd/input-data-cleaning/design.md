# 输入数据清理模块技术设计文档

| 字段 | 值 |
|------|-----|
| 模块名称 | 输入数据清理模块（Input Data Cleaning Module） |
| 版本 | v1.0 |
| 创建日期 | 2026-10-14 |
| 状态 | 待评审 |
| 关联规格 | `spec.md` v1.0 |

---

## 1. 设计概述

### 1.1 设计目标

将 spec.md 中定义的 6 项功能需求（FR-DC-01 ~ FR-DC-06）转化为一个纯函数 `clean_input_text`，该函数在会议纪要提取流水线中 `strip_template_prefix` 之后、`extract_meeting_minutes` 之前执行，去除格式噪声同时严格保留原文实体字符串。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| 纯函数 | 输入 `str` → 输出 `str`，不修改输入，不依赖外部状态 |
| 确定性管道 | 清理步骤按固定顺序串联，每步只处理一类格式噪声 |
| 实体安全 | 每个清理步骤都保证不删除、不拆散人名/日期/地点等实体字符串 |
| 幂等性 | 管道中每一步对自身输出再次执行结果不变，整体组合自然幂等 |
| 最小依赖 | 仅使用 Python 标准库 `re`，不引入第三方依赖 |

### 1.3 技术栈对齐

- **语言**：Python 3.12（与项目一致）
- **类型标注**：完整类型标注，函数签名为 `def clean_input_text(text: str) -> str`
- **依赖**：仅 `re` 标准库，无外部服务依赖
- **测试框架**：`pytest`（与项目现有测试一致）

---

## 2. 架构设计

### 2.1 模块在数据流中的位置

```text
用户输入 input_text
       │
       ▼
┌──────────────────┐
│  POST /api/generate │  (main.py:generate)
└──────────────────┘
       │
       ▼  state["input_text"] = req.input_text
┌──────────────────┐
│   route_node     │  (graph.py)  路由识别 task_type
└──────────────────┘
       │
       ▼
┌──────────────────────────┐
│  execute_node            │  (graph.py)
│  ┌────────────────────┐  │
│  │ strip_template_prefix │  ← 剥离模板前缀（已有）
│  └────────────────────┘  │
│       │                  │
│       ▼                  │
│  ┌────────────────────┐  │
│  │ clean_input_text   │  │  ← 【新增】数据清理模块
│  └────────────────────┘  │
│       │                  │
│       ▼                  │
│  ┌────────────────────┐  │
│  │ MeetingMinutesSkill │  │  (meeting_minutes.py:run)
│  │  .run(state)        │  │
│  └────────────────────┘  │
└──────────────────────────┘
       │
       ▼
  extract_meeting_minutes(text)  → 三层降级抽取
       │
       ▼
  validate_minutes(minutes, text)  → 零编造校验
       │                              ↑
       │              text 为清理后文本，实体保留
       ▼
  落库（存原始 req.input_text，非清理后文本）
```

### 2.2 集成点

清理模块在 `graph.py:execute_node()` 中集成。当前 `execute_node` 的实现为：

```python
def execute_node(state: AgentState) -> AgentState:
    if state.get("needs_clarification"):
        return state
    skill = get_skill(state["task_type"])
    body = strip_template_prefix(state.get("input_text", ""))
    return skill.run({**state, "input_text": body})
```

集成后改为：

```python
def execute_node(state: AgentState) -> AgentState:
    if state.get("needs_clarification"):
        return state
    skill = get_skill(state["task_type"])
    body = strip_template_prefix(state.get("input_text", ""))
    cleaned = clean_input_text(body)          # ← 新增
    return skill.run({**state, "input_text": cleaned})
```

**设计决策说明**：

- **为何放在 `execute_node` 而非 `MeetingMinutesSkill.run` 内部**：清理是通用的文本预处理，未来周报、邮件草稿等 Skill 同样受益。放在 `execute_node` 层使所有 Skill 自动获得清理能力，无需各自重复调用。
- **为何放在 `strip_template_prefix` 之后**：模板前缀（如"请帮我整理会议纪要："）由 `strip_template_prefix` 专门处理，清理模块只处理正文部分的格式噪声，职责分离。
- **为何 `validate_minutes` 的 `source_text` 自动获得清理后文本**：`MeetingMinutesSkill.run` 中 `text = state.get("input_text", "").strip()`，此时 `state["input_text"]` 已是清理后的文本，后续 `validate_minutes(minutes, text)` 自然使用清理后文本做字符串包含检查，无需额外改动。

### 2.3 落库保留原文

`main.py:generate()` 中 `_persist(state, session_id, req.input_text)` 传入的是原始 `req.input_text`，不经过清理。清理后的文本仅存在于 `state["input_text"]` 中用于抽取和校验，不影响落库。**此行为无需改动**，天然满足 CON-DC-06。

---

## 3. 清理管道设计

### 3.1 管道步骤与顺序

清理函数采用固定顺序的管道，每一步处理一类格式噪声：

```text
输入 text
  │
  ├─ Step 1: _normalize_newlines    (FR-DC-04)  统一换行符 \r\n / \r → \n
  │
  ├─ Step 2: _strip_invisible_chars (FR-DC-03)  去除不可见字符
  │
  ├─ Step 3: _collapse_spaces      (FR-DC-01)  合并行内连续空格
  │
  ├─ Step 4: _collapse_blank_lines (FR-DC-02)  合并连续空行
  │
  ├─ Step 5: _strip_edges          (FR-DC-05)  去除首尾空白
  │
  ▼
输出 cleaned_text
```

**顺序设计理由**：

| 步骤 | 为何在此位置 |
|------|-------------|
| Step 1 换行符统一 | 必须最先执行。后续步骤依赖 `\n` 作为行分隔符，混合 `\r\n` / `\r` 会导致行分割不一致 |
| Step 2 不可见字符 | 在换行符统一后、空格处理前。零宽字符可能夹在空格之间影响空格合并的判断 |
| Step 3 空格合并 | 在不可见字符去除后。去除零宽字符后可能产生新的连续空格，需在此步合并 |
| Step 4 空行合并 | 在行内空格合并后。此时每行内容已规范，可安全地按 `\n` 分割判断空行 |
| Step 5 首尾空白 | 必须最后执行。前序步骤可能在首尾产生新的空白（如去除不可见字符后暴露出的空格），需最终清理 |

### 3.2 各步骤详细设计

#### Step 1: `_normalize_newlines(text: str) -> str`

**对应需求**：FR-DC-04

**实现方式**：使用正则表达式将 `\r\n` 和单独的 `\r` 统一为 `\n`。

```
正则: \r\n → \n  (先处理 Windows 换行)
正则: \r   → \n  (再处理剩余的旧 Mac 换行)
```

**幂等性证明**：处理后文本只含 `\n`，再次执行无 `\r` 可替换，结果不变。

**实体安全性**：换行符不是实体字符串的组成部分，替换不影响实体保留。

#### Step 2: `_strip_invisible_chars(text: str) -> str`

**对应需求**：FR-DC-03

**实现方式**：使用正则表达式字符类去除指定的不可见字符。

**需去除的字符集**：

| Unicode 码位 | 名称 | 说明 |
|-------------|------|------|
| U+200B | 零宽空格（ZWSP） | 常从网页粘贴带入 |
| U+200C | 零宽非连接符（ZWNJ） | — |
| U+200D | 零宽连接符（ZWJ） | — |
| U+FEFF | BOM / 零宽不换行空格 | 文件开头常见 |
| U+0000~U+0008 | 控制字符 | 排除 U+0009（`\t`） |
| U+000B | 垂直制表符 | — |
| U+000C | 换页符 | — |
| U+000E~U+001F | 控制字符 | 排除 U+000A（`\n`）和 U+000D（`\r`） |

**保留的字符**：`\t`（U+0009）、`\n`（U+000A）、`\r`（U+000D，由 Step 1 处理）

**正则模式**：`[\u200B\u200C\u200D\uFEFF\x00-\x08\x0B\x0C\x0E-\x1F]` → 替换为空字符串

**幂等性证明**：去除后文本不再含这些字符，再次执行无匹配，结果不变。

**实体安全性**：不可见字符不是实体字符串的可见组成部分，去除不影响实体保留。

#### Step 3: `_collapse_spaces(text: str) -> str`

**对应需求**：FR-DC-01

**实现方式**：按行处理，每行内将连续多个空格（U+0020）合并为单个空格。

```
正则: [ ]{2,} → " "  (行内连续 2+ 个空格合并为 1 个)
```

**处理策略**：按 `\n` 分割为行，每行内执行空格合并，再以 `\n` 拼接。这确保跨行的空格不被错误合并（避免行尾空格与下一行行首空格合并）。

**幂等性证明**：合并后每行内无连续空格，再次执行无匹配，结果不变。

**实体安全性**：人名、日期、地点等实体内部不含连续空格（如"张三"无空格、"2026年10月9日"无空格），行内连续空格合并不影响实体字符串的完整性。单个空格保持不变，如"王敏 负责"中的单个空格保留。

#### Step 4: `_collapse_blank_lines(text: str) -> str`

**对应需求**：FR-DC-02

**实现方式**：将连续多个空行（只含空白字符或完全为空的行）合并为单个空行。

```
正则: (\n[ \t]*){2,} → \n  (连续 2+ 个空行合并为 1 个换行)
```

**处理策略**：使用正则匹配连续的空白行（行内只含空格/制表符或为空），合并为单个 `\n`。

**幂等性证明**：合并后无连续空行，再次执行无匹配，结果不变。

**实体安全性**：空行不含实体内容，合并不影响实体保留。有内容的行不受影响。

#### Step 5: `_strip_edges(text: str) -> str`

**对应需求**：FR-DC-05

**实现方式**：调用 Python 内置 `str.strip()` 去除首尾空白字符。

**幂等性证明**：`strip()` 后首尾无空白，再次 `strip()` 结果不变。

**实体安全性**：首尾空白不是实体字符串的组成部分，去除不影响实体保留。

### 3.3 管道组合

```python
def clean_input_text(text: str) -> str:
    """对输入文本执行格式清理，去除格式噪声同时保留原文实体字符串。

    清理步骤（固定顺序）：
    1. 统一换行符（\r\n / \r → \n）
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

### 3.4 空输入处理

函数入口处 `if not text: return ""` 确保：
- 空字符串 `""` → 返回 `""`
- `None` 不会传入（类型标注为 `str`），但若传入空字符串则安全返回空字符串

这满足 AC-DC-06（空输入处理）。

---

## 4. 接口设计

### 4.1 公开接口

| 接口 | 签名 | 说明 |
|------|------|------|
| `clean_input_text` | `(text: str) -> str` | 主清理函数，管道入口 |

### 4.2 内部接口（私有函数）

| 接口 | 签名 | 对应需求 | 说明 |
|------|------|---------|------|
| `_normalize_newlines` | `(text: str) -> str` | FR-DC-04 | 统一换行符 |
| `_strip_invisible_chars` | `(text: str) -> str` | FR-DC-03 | 去除不可见字符 |
| `_collapse_spaces` | `(text: str) -> str` | FR-DC-01 | 合并行内连续空格 |
| `_collapse_blank_lines` | `(text: str) -> str` | FR-DC-02 | 合并连续空行 |
| `_strip_edges` | `(text: str) -> str` | FR-DC-05 | 去除首尾空白 |

所有函数均为纯函数，签名为 `(str) -> str`，无副作用。

### 4.3 类型定义

本模块不引入新的数据类型或类。所有接口均为 `str → str` 的纯函数，类型标注完整，不使用 `Any`。

---

## 5. 文件组织

### 5.1 新增文件

```text
app/
├── cleaning.py              ← 【新增】数据清理模块
└── ...

tests/
├── test_cleaning.py         ← 【新增】数据清理单元测试
└── ...
```

### 5.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `app/graph.py` | `execute_node` 中新增 `clean_input_text` 调用（1 行新增 + 1 行 import） |

### 5.3 不修改的文件

| 文件 | 原因 |
|------|------|
| `app/main.py` | 落库仍存原始 `req.input_text`，无需改动 |
| `app/skills/meeting_minutes.py` | 从 `state["input_text"]` 取已清理文本，无需改动 |
| `app/tools/extract_fields.py` | `_normalize_date_breaks` 职责不变，无需改动 |
| `app/validate.py` | `validate_minutes` 接口不变，`source_text` 自动获得清理后文本 |
| `app/router.py` | `strip_template_prefix` 职责不变 |

---

## 6. 与 `_normalize_date_breaks` 的关系

### 6.1 职责划分

| 模块 | 职责 | 处理对象 |
|------|------|---------|
| `clean_input_text` | 格式噪声清理 | 空格、空行、不可见字符、换行符、首尾空白 |
| `_normalize_date_breaks` | 日期跨行修复 | "10月9\n日" → "10月9日" |

### 6.2 不冲突证明

- `clean_input_text` **不处理**日期跨行修复：清理管道中没有任何步骤会合并被换行符断开的日期字符串。
- `_normalize_date_breaks` **不处理**格式噪声：它只修复 `\d{1,2}月\d{1,2}\s+日` 模式中的空白，不处理空格合并、空行合并、不可见字符等。
- **执行顺序**：`clean_input_text` 在 `execute_node` 中执行（抽取之前），`_normalize_date_breaks` 在 `rule_based_extract` 内部执行（抽取过程中）。两者串联执行，`clean_input_text` 先清理格式噪声，`_normalize_date_breaks` 后修复日期跨行，互不干扰。
- **幂等性兼容**：`clean_input_text` 的输出中换行符已统一为 `\n`，`_normalize_date_breaks` 的正则 `\d{1,2}月\d{1,2}\s+日` 在 `\n` 环境下正常工作。

---

## 7. 实体保留保证

### 7.1 保证机制

清理管道中每一步都只操作格式字符（空格、空行、不可见字符、换行符），不操作语义字符（汉字、字母、数字、标点）。因此：

- **人名**（如"张三"、"王敏"）：由汉字组成，不含格式字符，不受任何清理步骤影响。
- **日期**（如"2026年10月9日"、"10月14日"）：由数字和汉字"年""月""日"组成，不含格式字符，不受影响。
- **地点**（如"第一会议室"、"线上"）：由汉字组成，不受影响。
- **标点符号**（如"："、"，"、"。"、"、"）：不是格式字符，不受影响。

### 7.2 与 `validate_minutes` 的兼容性

`validate_minutes` 使用 `value in source_text` 做字符串包含检查。清理后：

- 原文中的实体字符串 `value` 在清理后文本中仍然完整存在（7.1 节保证）。
- `value in cleaned_text` 为 `True`，校验通过。
- 清理不会产生新的"编造"实体，因此 `corrections` 列表不会因清理而增加。

### 7.3 边界情况分析

| 边界情况 | 分析 | 结论 |
|---------|------|------|
| 实体前后有多余空格 | "张三　　负责" → "张三 负责" | "张三"仍完整保留 |
| 实体跨行 | "张\n三" | 清理不合并跨行汉字，"张\n三"保持不变（这不是清理职责） |
| 实体含零宽字符 | "张\u200B三" → "张三" | 零宽字符去除后"张三"更规范，有利于校验匹配 |
| 日期跨行断开 | "10月9\n日" | 清理不处理，由 `_normalize_date_breaks` 修复 |

---

## 8. 测试设计

### 8.1 单元测试（`tests/test_cleaning.py`）

| 测试函数 | 覆盖需求 | 说明 |
|---------|---------|------|
| `test_collapse_multiple_spaces` | FR-DC-01 | 连续空格合并为单个 |
| `test_single_space_preserved` | FR-DC-01 | 单个空格保持不变 |
| `test_collapse_blank_lines` | FR-DC-02 | 连续空行合并为单个 |
| `test_single_blank_line_preserved` | FR-DC-02 | 单个空行保持不变 |
| `test_strip_zero_width_chars` | FR-DC-03 | 零宽字符 U+200B/C/D 去除 |
| `test_strip_bom` | FR-DC-03 | BOM U+FEFF 去除 |
| `test_strip_control_chars` | FR-DC-03 | 控制字符去除 |
| `test_preserve_tab` | FR-DC-03 | 制表符 `\t` 保留 |
| `test_normalize_crlf` | FR-DC-04 | `\r\n` → `\n` |
| `test_normalize_cr` | FR-DC-04 | 单独 `\r` → `\n` |
| `test_preserve_lf` | FR-DC-04 | `\n` 保持不变 |
| `test_strip_edges` | FR-DC-05 | 首尾空白去除 |
| `test_preserve_entities` | FR-DC-06 | 人名/日期/地点/标点保留 |
| `test_idempotent` | NFR-DC-04 | 幂等性：f(f(x)) == f(x) |
| `test_deterministic` | NFR-DC-03 | 确定性：多次调用结果一致 |
| `test_empty_input` | AC-DC-06 | 空输入返回空字符串 |
| `test_pure_function` | NFR-DC-01 | 输入参数不被修改 |
| `test_performance_100kb` | NFR-DC-05 | 100KB 文本 ≤100ms |

### 8.2 集成测试

| 测试函数 | 覆盖需求 | 说明 |
|---------|---------|------|
| `test_cleaning_with_validate_minutes` | AC-DC-02 | 清洗后实体仍能被 `validate_minutes` 正确匹配 |
| `test_existing_samples_no_regression` | AC-DC-05 | 13 例合成样本清洗后校验回退项不增加 |
| `test_existing_tests_pass` | AC-DC-05 | 现有测试套件无回归 |

### 8.3 性能测试策略

- 构造 100KB 文本（约 10 万字符，含混合格式噪声）
- 使用 `time.perf_counter()` 测量 `clean_input_text` 执行时间
- 断言耗时 ≤ 100ms
- 正则操作在 100KB 文本上的时间复杂度为 O(n)，满足性能要求

---

## 9. 设计决策记录

### DR-01: 清理模块放在 `execute_node` 而非 Skill 内部

**决策**：在 `graph.py:execute_node()` 中调用 `clean_input_text`，而非在 `MeetingMinutesSkill.run()` 内部调用。

**理由**：清理是通用文本预处理，所有 Skill 均可受益。放在 `execute_node` 层避免每个 Skill 重复调用，且未来新增 Skill 自动获得清理能力。

**影响**：`graph.py` 新增 1 行 import + 1 行调用，改动最小。

### DR-02: 不引入新的数据类型或类

**决策**：清理模块仅提供 `str → str` 的纯函数，不定义新的 Pydantic 模型或 dataclass。

**理由**：清理的输入和输出都是纯字符串，无需结构化数据模型。保持接口简单，降低耦合。

### DR-03: 管道步骤顺序固定且不可配置

**决策**：5 个清理步骤按固定顺序执行，不提供配置参数跳过某步骤。

**理由**：步骤顺序经过仔细设计（见 3.1 节），可配置会破坏幂等性保证和实体安全性。固定顺序降低出错风险。

### DR-04: 不处理日期跨行修复

**决策**：清理模块不包含日期跨行修复功能（如 "10月9\n日" → "10月9日"），该职责保留给 `_normalize_date_breaks`。

**理由**：`_normalize_date_breaks` 已在 `rule_based_extract` 内部实现且经过测试验证。在清理模块中重复实现会导致职责重叠和潜在冲突。清理模块只处理格式噪声，不处理语义修复。

### DR-05: 仅使用 `re` 标准库

**决策**：清理模块仅依赖 Python 标准库 `re`，不引入第三方依赖（如 `unicodedata`）。

**理由**：需去除的不可见字符集是固定且有限的（见 3.2 节 Step 2），可用正则字符类直接枚举，无需 `unicodedata` 的分类查询。降低依赖复杂度。

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 清理意外删除有意义的空格 | 实体字符串被破坏 | Step 3 只合并连续 2+ 个空格，单个空格保留；测试覆盖实体保留 |
| 清理意外合并有意义的空行 | 段落结构被破坏 | Step 4 只合并连续 2+ 个空行，单个空行保留；测试覆盖段落结构 |
| 正则性能在极端输入下退化 | 性能不达标 | 使用简单正则（无回溯风险），100KB 性能测试验证 |
| 现有测试因清理引入而失败 | 回归 | 集成测试验证 13 例样本无回归；现有测试套件全量运行 |

---

## 11. 术语表

| 术语 | 定义 |
|------|------|
| 清理管道 | 按固定顺序串联的多个纯函数，每个处理一类格式噪声 |
| 幂等性 | f(f(x)) == f(x)，对已清理文本再次清理结果不变 |
| 实体字符串 | 原文中具有语义意义的人名、日期、地点等字符串片段 |
| 格式噪声 | 文本中不影响语义但影响程序处理的字符（多余空格、空行、不可见字符等） |
| 零编造校验 | `validate_minutes` 中通过 `value in source_text` 检查抽取值是否在原文中出现 |
