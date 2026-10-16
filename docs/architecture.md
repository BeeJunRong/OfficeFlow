> 前端交互与状态管理已于 2026-10-16 更新为当前实现（§3）；其余标为“首版”的内容保留原始设计背景，后续业务演进见[版本记录](版本.md)。

## 1. 架构目标与约束

- 采用 **单 Agent + 多工具** 架构，避免多智能体带来的开发与验收复杂度。
- 首版入口为 **统一搜索框**，下方提供四个任务提示按钮：会议纪要、周报整理、邮件草稿、网页整理。
- 首版 **只完整实现会议纪要 Skill（FR-01）**，其余三个 Skill 仅提供提示按钮与模板填充，生成结果返回“P1 开发中”。
- 支持 **显式点击优先 + 指令区关键词 + LLM 意图分类兜底** 的分层路由；正文关键词不触发路由。
- 所有生成结果可编辑，缺失字段统一显示“待确认”，不得编造。
- 导出以用户显式点击为触发；邮件仅生成草稿，不自动发送。
- 工具失败时明确报错、保留内容、支持重试。
- 首版不引入数据库；可选 SQLite 仅记录工具日志，作为验收证据。

---

## 2. 总体架构

```text
┌─────────────────────────────────────────────┐
│              前端工作台                        │
│   Streamlit（原型）或 React + agentkit-ui     │
│   · 统一搜索框  · 四个任务提示按钮             │
│   · 结果展示 + 编辑  · 工具调用状态面板        │
│   · 导出按钮  · 异常提示与重试                │
├─────────────────────────────────────────────┤
│              后端 API                         │
│   FastAPI                                    │
│   · POST /api/generate  触发纪要生成          │
│   · POST /api/export    导出 Markdown        │
│   · POST /api/retry     重试失败操作          │
│   · GET  /api/status/{id}  查询工具状态       │
├─────────────────────────────────────────────┤
│            Agent 编排层                       │
│   LangGraph + LangChain                       │
│   · 状态图：route → parse → extract →         │
│     validate → export                         │
│   · 工具：网页读取、结构化抽取、Markdown导出   │
│   · 结构化输出：Pydantic 校验 + 缺失值填充    │
├─────────────────────────────────────────────┤
│              LLM 服务                         │
│   DeepSeek API / Qwen API / Gemini API       │
│   （备选：Ollama + Qwen 本地部署）            │
└─────────────────────────────────────────────┘
```

---

## 3. 前端设计

### 3.1 入口与交互

- **统一输入框**：使用 Streamlit Components v2 实现一个持续挂载的组件（`frontend/composer.py`、`frontend/composer_assets/`）。圆角矩形、多行文本自适应高度，超出上限后在框内滚动，沿用现有主题色、边框和阴影。
- **底部操作栏**：左侧依次为“会议纪要 / 周报整理 / 邮件草稿 / 网页整理 / 自动识别”五个胶囊标签；右侧始终为圆形“↑”发送按钮。窄屏时标签横向滚动，发送按钮不收缩。
- **点击标签**：选择对应任务类型；仅在输入内容为空时填入提示词，不覆盖已有材料。例如会议纪要标签填入：

  ```text
  请将以下会议记录整理成会议纪要：
  ```

  发送时前端传递 `task_type=meeting_minutes`，锁定任务类型。“自动识别”清除显式任务选择，由后端路由判断，不主动替换已有文字。

- **直接输入**：用户输入自然语言，例如“帮我做会议纪要：2026年10月9日项目例会……”，由后端路由识别任务类型。
- **发送操作**：Enter 发送，Shift + Enter 换行；中文输入法组词期间回车不发送。空白内容禁用发送，有内容时高亮。发送后立即清空输入；处理中禁用输入、标签和发送按钮，提示“正在整理，请稍候…”，避免重复提交。目前为同步生成请求，未提供“停止生成”。
- **页面状态**：无对话时输入框居中；首条消息发送后，同一个组件通过 `.docked` class 平滑移到底部。后续发送保持底部位置。消息区滚动，底部按输入框实际高度预留空间；“新建对话”清空历史与任务选择，让同一组件回到初始位置。
- **结果展示**：结构化纪要可编辑；每项待办显示“事项 / 负责人 / 截止日期”；缺失字段显示“待确认”。
- **工具状态面板**：展示 Agent 调用工具的顺序、状态、错误信息。
- **导出**：用户点击“导出 .md”，触发后端导出，成功后下载。
- **异常处理**：导出失败时明确提示“导出失败，可重试”，保留页面内容，提供“重试导出”按钮。

### 3.2 前端状态管理

- `st.session_state` 保存 `turns`（生成结果、编辑内容、工具状态）、`locked_task_type`（显式任务类型）、`pending_message`（待处理消息）及 `composer_epoch`（仅在新建对话时递增）。未发送草稿保存在持续挂载的 textarea 中，普通重跑不清空。
- 输入组件始终使用固定的 `key="message_composer"`，并在配置入口、条件分支和动态消息列表之前渲染，保持 Streamlit delta path 不变。不能把输入组件放到会增长的消息列表后面；仅固定 key 不能避免位置变化引起的卸载重建。
- 发送时序：本地清空内容并更新禁用状态 → 下一动画帧添加 `.docked` → 再下一帧发送 `submitted` 事件 → Python 回调写入任务类型和待处理消息 → 渲染用户消息并调用原有生成逻辑 → 追加结果、清除待处理状态并重跑。前端更新先于后端重跑。
- 组件始终采用 `position: fixed; bottom: 0` 和相同内边距；只对 `transform` 做 420 ms 过渡，不切换定位方式或动画 `bottom`。遵循 `prefers-reduced-motion`。普通状态回传不移除 `.docked`；仅新对话 epoch 变化时允许复位。
- 状态更新复用 textarea 和标签 DOM，清理 ResizeObserver 与未执行的动画帧；按实际组件高度更新 `--of-composer-height`，防止消息末尾被底栏遮挡。
- 刷新页面不要求恢复历史会话；首版不引入跨会话持久化。

回归测试见 `tests/test_composer.py`（挂载位置与组件 ID）和 `tests/frontend/composer.test.mjs`（连续发送时序、DOM 复用及草稿保留）。视觉验收见[评测计划 §12](评测计划.md#12-输入框交互与过渡回归fr-05)。

---

## 4. 后端设计

### 4.1 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/generate` | 输入文本与任务类型，返回结构化纪要 |
| POST | `/api/export` | 接收编辑后的内容，生成 `.md` 文件并返回下载链接或文件流 |
| POST | `/api/retry` | 重试上一次失败的导出操作 |
| GET | `/api/status/{session_id}` | 查询当前会话的工具调用状态 |

### 4.2 Skill 路由分层

1. **显式任务类型优先**  
   前端点击提示按钮时传递 `task_type=meeting_minutes`，后端直接锁定，不再看正文关键词。

2. **指令区关键词快速匹配**  
   只检查输入开头或冒号前的短指令，例如“帮我做会议纪要”“整理周报”“写邮件草稿”。

3. **LLM 意图分类兜底**  
   使用结构化输出识别 `task_type` 和 `confidence`，置信度低于 `0.7` 时追问用户，不静默路由。

4. **正文关键词不触发路由**  
   正文中的“周报”“邮件”只作为内容，不作为任务类型依据。

5. **降级方案**  
   若首版 LLM 意图分类不稳定，降级为“直接输入路径提示用户选择任务类型”，显式点击路径保证 100% 准确。

### 4.3 Skill 注册表

```python
SKILLS = {
    "meeting_minutes": MeetingMinutesSkill(),   # 完整实现
    "weekly_report": PlaceholderSkill("周报整理"),
    "email_draft": PlaceholderSkill("邮件草稿"),
    "web_summary": PlaceholderSkill("网页整理"),
}
```

每个 Skill 定义：

- `input_schema`
- `output_schema`
- 工具列表
- 验收样本

首版仅 `meeting_minutes` 完整实现。

---

## 5. Agent 工作流（LangGraph）

```text
[入口]
  ↓
[路由节点]
  ↓
若 task_type = meeting_minutes
  ↓
[解析节点] → [抽取节点] → [校验节点]
  ↓
[缺失值填充] → [生成 Markdown] → [导出节点]
  ↓
若导出失败 → [异常处理节点] → 可重试
```

### 5.1 节点职责

| 节点 | 职责 |
|------|------|
| 路由节点 | 根据显式任务类型、指令区关键词、LLM 分类确定 `task_type` |
| 解析节点 | 接收原始文本，调用 LLM 提取会议日期、主题、决定、待办 |
| 抽取节点 | 使用 Pydantic 结构化输出，强制字段格式 |
| 校验节点 | 检查是否编造负责人或日期；若原文无日期但输出具体日期，回退为“待确认” |
| 缺失值填充 | 对未提供字段统一填“待确认” |
| 生成 Markdown | 将结构化数据转为 Markdown 文本 |
| 导出节点 | 调用导出工具，返回成功/失败状态 |
| 异常处理节点 | 记录失败步骤、保留内容、提供重试入口 |

### 5.2 状态字段（建议）

```python
class AgentState(TypedDict):
    session_id: str
    task_type: str
    input_text: str
    structured_result: dict
    edited_content: str
    tool_logs: list
    export_status: str
    error_message: str
```

---

## 6. 工具接口

| 工具 | 输入 | 输出 | 错误处理 |
|------|------|------|----------|
| 网页读取 | URL | 正文、标题、来源 URL | 抓取失败返回错误码 |
| 结构化抽取 | 会议文本 | `MeetingMinutes` JSON | 解析失败返回错误 |
| Markdown 导出 | 文本内容 | 文件路径或文件流 | 保存失败返回错误码，支持重试 |

### 6.1 工具调用日志

每次工具调用记录：

- `tool_name`
- `status`：`success` / `failed`
- `error_message`
- `timestamp`

日志用于：

- 前端工具状态面板展示
- 验收证据保存
- 异常场景复现

---

## 7. 数据模型（Pydantic 建议）

```python
from pydantic import BaseModel, Field

class ActionItem(BaseModel):
    task: str = Field(description="待办事项")
    owner: str = Field(default="待确认", description="负责人")
    deadline: str = Field(default="待确认", description="截止日期")
    evidence: str = Field(default="", description="原文证据片段")

class MeetingMinutes(BaseModel):
    date: str = Field(default="待确认", description="会议日期")
    topic: str = Field(default="待确认", description="会议主题")
    decisions: list[str] = Field(default_factory=list, description="关键决定")
    action_items: list[ActionItem] = Field(default_factory=list, description="待办列表")
```

### 7.1 缺失值与零编造规则

- 只提取原文明确出现的决定、待办、负责人和日期。
- 原文未提供的字段统一显示“待确认”。
- 时间信息可按明确年月日标准化。
- 每条关键待办附可回看原文的简短证据片段或定位信息。
- 若原文无日期但输出具体日期，系统必须回退为“待确认”。

---

## 8. 数据库（可选）

首版不引入数据库。若需保存工具日志作为验收证据，可使用 SQLite 轻量记录：

| 表 | 字段 |
|----|------|
| `sessions` | `session_id`, `task_type`, `input_text`, `created_at` |
| `results` | `session_id`, `structured_output`, `edited_output`, `export_status` |
| `tool_logs` | `session_id`, `tool_name`, `status`, `error_message`, `timestamp` |

数据库仅用于日志和证据，不参与核心生成流程。

---

## 9. 异常处理与安全

### 9.1 异常处理

- **空输入**：阻止生成，提示用户输入非空会议文字记录。
- **工具失败**：明确报错，说明失败步骤，保留已输入/已生成内容，支持重试。
- **导出失败**：提示“导出失败，可重试”，不得声称导出成功；页面内容仍在；恢复后可成功导出，且不出现重复导出记录。
- **模型/网络不可用**：不计入正常性能样本，单独进行故障测试；向用户展示可恢复操作。

### 9.2 安全与可控

- 只生成邮件草稿，不自动发送。
- 导出以用户显式点击为触发。
- 发送、对外分享等不可逆行为不由 Agent 擅自执行。
- 工具调用日志可查，未经用户操作不得出现发送邮件或自动导出行为。
- 此约束必须 100% 满足。

---

## 10. 部署与配置

### 10.1 开发环境

- 本地运行 FastAPI + Streamlit（或 React）。
- 模型服务：云端 API（待确认）。
- 配置管理：环境变量存储 API Key。

### 10.2 启动命令（计划）

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
export MODEL_API_KEY="your_key"

# 启动后端
uvicorn app.main:app --reload

# 启动前端（Streamlit）
streamlit run frontend/app.py
```

### 10.3 目录结构（建议）

```text
officeflow/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── graph.py             # LangGraph 工作流
│   ├── skills/
│   │   ├── meeting_minutes.py
│   │   └── placeholders.py  # P1 占位 Skill
│   ├── tools/
│   │   ├── extract_fields.py
│   │   └── export_markdown.py
│   ├── schemas.py           # Pydantic 模型
│   └── router.py            # Skill 路由
├── frontend/
│   ├── app.py               # Streamlit 页面与业务调用
│   ├── composer.py          # Components v2 输入组件注册
│   └── composer_assets/    # 输入组件 HTML / CSS / JavaScript
├── tests/
│   ├── samples/             # 13 例合成样本
│   └── test_fr01.py
├── docs/
│   ├── architecture.md
│   ├── api.yaml
│   ├── guardsrails.md
│   ├── evaluation_plan.md
│   └── rtm.md
├── requirements.txt
└── README.md
```

---

## 11. 性能与路由约束

### 11.1 Q-01 响应时间

- 对 ≤4,000 汉字会议输入，从点击“↑”发送按钮或按 Enter 提交到页面完整显示可编辑结果，测量 20 次，P95 ≤20 秒。
- 测试条件：同一台开发测试电脑、同一模型与版本、稳定网络、无其他并发任务。
- 记录浏览器时间戳和原始耗时。
- 该目标为团队拟定，待原型测试后确认。

### 11.2 Q-03 路由准确性

- 显式点击路径：任务类型识别准确率 100%。
- 直接输入路径：会议纪要意图识别 ≥90%（10 例中 ≥9 例正确路由或追问）。
- 若 LLM 意图分类不稳定，降级为“直接输入路径提示用户选择任务类型”。

---

## 12. 待确认事项

- 前端最终选型：Streamlit 或 React + agentkit-ui。
- 模型服务与经费：DeepSeek / Qwen / Gemini / Ollama。
- 是否引入 SQLite 日志表。
- 是否集成真实邮箱（首版不做）。
- 13 例基准样本和通过阈值。
- P95 目标与 Q-03 路由准确率目标。
- 实际分工中的交叉复核安排。
- 成员姓名以小组实际为准（PDF OCR 存在不一致）。

---

## 13. 版本记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.1 | 2026-10-09 | 统一入口交互修订，新增 FR-00、T4、Q-03；FR-02～FR-04 降为 P1 占位 |
| v2 | 待定 | 根据邻组互评修订 |
