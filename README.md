# OfficeFlow

单 Agent + 多工具的办公信息整理应用。以**统一搜索框**为入口，通过 Skill 路由识别用户意图，调用工具完成办公信息整理。首版完整实现**会议纪要 Skill（FR-01）**：粘贴会议文字记录 → 抽取会议日期/主题/关键决定/待办 → 可编辑 → 导出 `.md`；其余三个 Skill（周报整理/邮件草稿/网页整理）为 P1 占位。

## 架构

```text
前端工作台（Streamlit）  统一搜索框 · 四个提示按钮 · 结果编辑 · 工具状态面板 · 导出/重试
      ↓
后端 API（FastAPI）      POST /api/generate · /api/export · /api/retry · GET /api/status/{id}
      ↓
Agent 编排（LangGraph）  route → parse/extract → validate（零编造）→ 缺失值填充 → Markdown
      ↓
LLM 服务                 OpenAI 兼容协议（DeepSeek/Qwen/Ollama）；无 Key 时规则引擎离线降级
```

设计文档见 `docs/`（proposal、architecture、guardrails、评测计划、需求追溯矩阵、版本记录）。各版本交付明细见 `docs/版本.md`。

## 快速开始

```bash
# 1. 创建环境并安装依赖
conda create -n officeflow python=3.12 -y
conda run -n officeflow pip install -r requirements.txt

# 2. 配置模型服务（可选；不配置则使用内置规则抽取引擎，离线可用）
export OFFICEFLOW_MODEL_API_KEY="your_key"          # 必填才启用 LLM
export OFFICEFLOW_MODEL_BASE_URL="https://api.deepseek.com/v1"   # 默认 DeepSeek
export OFFICEFLOW_MODEL_NAME="deepseek-chat"

# 3. 启动后端（端口 8000）
conda activate officeflow && uvicorn app.main:app --reload

# 4. 启动前端（另开终端）
conda activate officeflow && streamlit run frontend/app.py
```

## 测试

```bash
# 13 例冻结合成样本（N1-N6 / B1-B4 / E1-E2 / T4）+ 路由/导出/零编造测试
conda run -n officeflow python -m pytest tests/ -v
```

覆盖：T1 正常抽取、T2 缺失字段标"待确认"、T3/E1 导出失败提示与重试（无重复记录）、
T4 正文关键词不误路由、E2 空输入阻止、Q-02 导出显式触发、零编造校验回退。

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OFFICEFLOW_MODEL_API_KEY` | LLM API Key；为空时走规则引擎 | 空（规则引擎） |
| `OFFICEFLOW_MODEL_BASE_URL` | OpenAI 兼容 API 地址 | `https://api.deepseek.com/v1` |
| `OFFICEFLOW_MODEL_NAME` | 模型名 | `deepseek-chat` |
| `OFFICEFLOW_EXPORT_DIR` | 导出目录 | `exports/` |
| `OFFICEFLOW_DB_PATH` | 工具日志 SQLite 路径 | `officeflow_logs.db` |
| `OFFICEFLOW_ENABLE_DB` | 是否启用日志库（`0` 关闭） | `1` |
| `OFFICEFLOW_BACKEND` | 前端连接的后端地址 | `http://localhost:8000` |

## 目录结构

```text
app/
├── main.py              # FastAPI 入口（generate/export/retry/status/download）
├── graph.py             # LangGraph 工作流（route → execute）
├── router.py            # FR-00 分层路由 + 模板前缀剥离
├── schemas.py           # Pydantic 模型（ActionItem/MeetingMinutes/AgentState）
├── validate.py          # 零编造校验（无依据日期/负责人回退"待确认"）
├── llm.py               # OpenAI 兼容 LLM 客户端
├── storage.py           # SQLite 工具日志（验收证据）
├── skills/
│   ├── meeting_minutes.py   # FR-01 完整实现
│   └── placeholders.py      # P1 占位 Skill
└── tools/
    ├── extract_fields.py    # 结构化抽取（LLM + 规则降级）
    └── export_markdown.py   # Markdown 渲染与导出（可注入失败用于测试）
frontend/app.py          # Streamlit 对话式工作台（ChatGPT 风格：空状态居中搜索框，提交后沉底，逐步展示工具调用过程）
tests/                   # 13 例冻结合成样本 + pytest
docs/                    # 架构/护栏/评测/追溯/版本记录文档
```

## 关键约束（见 docs/guardrails.md）

- **零编造**：缺失字段统一显示"待确认"；原文无依据的日期/负责人由校验节点强制回退。
- **导出显式触发**（Q-02）：未经用户点击不得自动导出；导出失败保留内容、可重试、无重复记录。
- **路由边界**（FR-00）：显式点击优先 → 指令区（≤16 字符短指令）关键词 → LLM 意图分类兜底（置信度 <0.7 追问）；正文关键词不触发路由。
