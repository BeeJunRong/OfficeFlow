## 1. 总体架构
┌─────────────────────────────────────────────┐
│ 前端工作台 │
│ Streamlit（原型）或 React + agentkit-ui │
│ · 统一搜索框 · 四个任务提示按钮 │
│ · 结果展示+编辑 · 工具调用状态面板 │
│ · 导出按钮 · 异常提示与重试 │
├─────────────────────────────────────────────┤
│ 后端 API │
│ FastAPI │
│ · POST /api/generate 触发纪要生成 │
│ · POST /api/export 导出 Markdown │
│ · POST /api/retry 重试失败操作 │
│ · GET /api/status/{id} 查询工具状态 │
├─────────────────────────────────────────────┤
│ Agent 编排层 │
│ LangGraph + LangChain │
│ · 状态图：route → parse → extract → │
│ validate → export │
│ · 工具：网页读取、结构化抽取、Markdown导出 │
│ · 结构化输出：Pydantic 校验 + 缺失值填充 │
├─────────────────────────────────────────────┤
│ LLM 服务 │
│ DeepSeek API / Qwen API / Gemini API │
│ （备选：Ollama + Qwen 本地部署） │
└─────────────────────────────────────────────┘

text

## 2. 前端设计

- **入口**：统一搜索框，下方四个提示按钮：会议纪要、周报整理、邮件草稿、网页整理。  
- **点击提示按钮**：输入框自动填入模板（如“请帮我整理会议纪要：\n[粘贴会议记录]”），并锁定 `task_type=meeting_minutes`。  
- **直接输入**：用户输入自然语言，由后端路由识别任务类型。  
- **结果展示**：结构化纪要可编辑；每项待办显示“事项/负责人/截止日期”；缺失字段显示“待确认”。  
- **工具状态面板**：展示 Agent 调用工具的顺序、状态、错误信息。  
- **导出**：用户点击“导出 .md”，触发后端导出，成功后下载。  
- **异常处理**：导出失败时明确提示“导出失败，可重试”，保留页面内容，提供“重试导出”按钮。

## 3. 后端设计

### 3.1 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/generate | 输入文本与任务类型，返回结构化纪要 |
| POST | /api/export | 接收编辑后的内容，生成 .md 文件并返回下载链接或文件流 |
| POST | /api/retry | 重试上一次失败的导出操作 |
| GET | /api/status/{session_id} | 查询当前会话的工具调用状态 |

### 3.2 Skill 路由

分层路由策略：

1. **显式任务类型优先**：前端点击提示按钮时传递 `task_type=meeting_minutes`，后端直接锁定。  
2. **指令区关键词快速匹配**：只检查输入开头或冒号前的短指令，如“帮我做会议纪要”“整理周报”。  
3. **LLM 意图分类兜底**：使用结构化输出识别 `task_type` 和 `confidence`，置信度低于 0.7 时追问用户。  
4. **正文关键词不触发路由**：正文中的“周报”“邮件”只作为内容，不作为任务类型依据。

### 3.3 Skill 注册表

```python
SKILLS = {
    "meeting_minutes": MeetingMinutesSkill(),   # 完整实现
    "weekly_report": PlaceholderSkill("周报整理"),
    "email_draft": PlaceholderSkill("邮件草稿"),
    "web_summary": PlaceholderSkill("网页整理"),
}
每个 Skill 定义 input_schema、output_schema、工具列表和验收样本。首版仅 meeting_minutes 完整实现。

## 4. Agent 工作流（LangGraph）
text
[入口] → [路由节点] → 若 meeting_minutes → [解析节点] → [抽取节点]
       → [校验节点] → [缺失值填充] → [生成 Markdown] → [导出节点]
       → 若导出失败 → [异常处理节点] → 可重试
解析节点：接收原始文本，调用 LLM 提取会议日期、主题、决定、待办。

抽取节点：使用 Pydantic 结构化输出，强制字段格式。

校验节点：检查是否编造负责人或日期；若原文无日期但输出具体日期，回退为“待确认”。

生成 Markdown：将结构化数据转为 Markdown 文本。

导出节点：调用导出工具，返回成功/失败状态。

## 5. 工具接口
工具	输入	输出	错误处理
网页读取	URL	正文、标题、来源 URL	抓取失败返回错误码
结构化抽取	会议文本	MeetingMinutes JSON	解析失败返回错误
Markdown 导出	文本内容	文件路径或文件流	保存失败返回错误码，支持重试
## 6. 数据模型（Pydantic 建议）
python
class ActionItem(BaseModel):
    task: str
    owner: str = "待确认"
    deadline: str = "待确认"
    evidence: str = ""

class MeetingMinutes(BaseModel):
    date: str = "待确认"
    topic: str = "待确认"
    decisions: list[str] = []
    action_items: list[ActionItem] = []
## 7. 数据库（可选）
首版不引入数据库。若需保存工具日志作为验收证据，可使用 SQLite 轻量记录：

sessions：session_id, task_type, input_text, created_at

results：session_id, structured_output, edited_output, export_status

tool_logs：session_id, tool_name, status, error_message, timestamp

## 8. 部署
开发环境：本地运行 FastAPI + Streamlit。

模型服务：云端 API（待确认）。

配置管理：环境变量存储 API Key。

日志：控制台 + 文件，记录工具调用与异常。

## 9. 待确认事项
前端最终选型（Streamlit 或 React）。

模型服务与经费。

是否引入 SQLite 日志表。

是否集成真实邮箱（首版不做）。
