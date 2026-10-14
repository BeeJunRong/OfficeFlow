# OfficeFlow

单 Agent + 多工具的办公信息整理应用。以**统一搜索框**为入口，通过 Skill 路由识别用户意图，调用工具完成办公信息整理。

**已实现**：

- **FR-01 会议纪要**（唯一纳入 13 例验收的项）：粘贴会议文字记录 → 抽取会议日期/主题/关键决定/待办 → 可编辑 → 导出 `.md`。
- **FR-02 周报整理**：由会议纪要派生（决定 → 本周进展，待办 → 下周计划，缺失项进风险清单）。
- **FR-03 邮件草稿**：由会议纪要派生；发件人（本人）+ 发送协议 + **按纪要中负责人去重生成的收件人**（姓名/邮箱手填/内容生成/逐封发送开关）。**默认只出草稿，绝不自动发送**。
- **FR-04 网页整理**：粘贴含单个 URL 的请求 → 单页读取（**不批量爬取、不登录**，检出登录表单直接拒绝）→ 提炼 3-6 条**摘自原文段落**的要点 → 可编辑 → 导出 `.md`。
- **FR-06 多 Skill 派生编排**：会议纪要完成后给出"可继续生成"建议，**用户点击才执行**，且复用源会话结果、不重新抽取。
- **Web 配置面板**：页面最顶部右对齐的「⚙️」按钮——**选服务商 → 填 API Key → 测试连接 → 保存**，全程不用碰命令行；另含抽取引擎、邮件发送、文件路径等进阶项。

**四个 Skill（FR-01/02/03/04）均为完整实现，已无 P1 占位**；FR-04 由 v0.6.0 合并引入（见 `docs/版本.md`）。

## 架构

```text
前端工作台（Streamlit）  统一搜索框 · 四个提示按钮 · 结果编辑 · 工具状态面板 · 导出/重试
      ↓
后端 API（FastAPI）      POST /api/generate · /api/derive · /api/email/send · /api/config
                         POST /api/export · /api/retry · GET /api/status/{id}
      ↓
Agent 编排（LangGraph）  route → parse/extract → validate（零编造）→ 缺失值填充 → Markdown
      ↓
LLM 服务                 OpenAI 兼容协议（DeepSeek/Qwen/Ollama）；无 Key 时规则引擎离线降级
```

设计文档见 `docs/`（proposal、architecture、guardrails、评测计划、需求追溯矩阵、版本记录）。各版本交付明细见 `docs/版本.md`。

**开源复用选型**见 `docs/开源选型.md`：提取项目的 7 项可替换技术能力，并给出经核验的 GitHub 开源项目建议（推荐用 `google/langextract` 的字符级溯源替换手写抽取规则，以满足"零编造 + 原文可回看"）。该文档仅为选型论证，尚未改动运行代码。

## 快速开始

> **本机（Windows）请以 [`docs/部署与启动.md`](docs/部署与启动.md) 为准。**
> 本机**没有安装 conda**，且系统 Python 是 3.14（过新），下述 `conda` 命令**不可用**；
> 实际环境是项目自带的 `.venv`（Python 3.12）。一键启停：`.\scripts\start.ps1 start`。

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
| `OFFICEFLOW_EMAIL_ENABLE_SEND` | **邮件真实发送总开关**；`0` 时即使选 smtp 也不会发送 | `0` |
| `OFFICEFLOW_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_TLS` | 仅 smtp 协议需要；不配置则 smtp 不可用 | 空 |

## 目录结构

```text
app/
├── main.py              # FastAPI 入口（generate/derive/email·send/config/export/retry/status/download）
├── env.py               # .env 加载器（必须在业务 import 前调用）
├── config_store.py      # Web 配置面板后端：字段白名单、掩码回显、写 .env、连通性测试
├── graph.py             # LangGraph 工作流（route → execute）
├── router.py            # FR-00 分层路由 + 模板前缀剥离
├── derive.py            # FR-06 派生规则（只给有依据的方向；网页整理不参与、邮件为终端）
├── schemas.py           # Pydantic 模型（MeetingMinutes/WeeklyReport/EmailDraft/WebSummary/AgentState）
├── validate.py          # 零编造校验（无依据日期/负责人回退"待确认"）
├── llm.py               # OpenAI 兼容 LLM 客户端
├── storage.py           # SQLite 工具日志（验收证据）+ 派生时取回源产物
├── skills/
│   ├── meeting_minutes.py   # FR-01 会议纪要
│   ├── weekly_report.py     # FR-02 周报整理
│   ├── email_draft.py       # FR-03 邮件草稿（收件人来自负责人，邮箱手填）
│   └── web_summary.py       # FR-04 网页整理（单页读取，要点摘自原文）
└── tools/
    ├── extract_fields.py       # 抽取入口：langextract → llm → rule 三级降级
    ├── langextract_extract.py  # LangExtract 抽取 + char_interval 溯源（中文用 UnicodeTokenizer）
    ├── web_fetch.py            # 网页单页抓取 + 正文抽取 + 登录页拒绝（FR-04）
    ├── email_send.py           # 发送协议 draft_only/mailto/smtp + 四重闸门（Q-02）
    └── export_markdown.py      # 纪要/邮件/周报的 Markdown 渲染与导出（可注入失败用于测试）
frontend/app.py          # Streamlit 工作台（顶部右对齐「⚙️」配置面板 + 派生按钮 + 邮件编辑器 + 周报/网页渲染）
frontend/env_loader.py   # 前端专用 .env 加载器（纯标准库；前端入口禁止导入 app 包，见 docs/部署与启动.md §8）
scripts/
├── start.ps1                  # 一键启停/状态/自检/测试（UTF-8 with BOM）
├── check_langextract.py       # LangExtract 四步环境自检
└── check_acceptance_live.py   # 真实 LLM 下的 13 例验收检查
tests/                   # 13 例冻结合成样本 + pytest（test_multiskill.py / test_web_summary.py / test_config.py）
docs/                    # 架构/护栏/评测/追溯/版本记录/开源选型/部署与启动/多Skill编排评估
.env.example             # 环境变量模板（复制为 .env 使用）
```

## 关键约束（见 docs/guardrails.md）

- **零编造**：缺失字段统一显示"待确认"；原文无依据的日期/负责人由校验节点强制回退。
  真实 LLM 下已验证生效（曾拦下模型把 `10月14日` 猜成 2024 年的幻觉）。
- **导出显式触发**：未经用户点击不得自动导出；导出失败保留内容、可重试、无重复记录。
- **邮件默认不发送**（Q-02）：生成/派生流程**绝不**发送；默认协议 `draft_only`；
  真实发送需四重闸门（显式点击 + `confirm=true` + SMTP 配置齐全 + `.env` 总开关 `=1`）。
  **邮箱地址一律由用户手填，系统绝不推测或拼接。**
- **路由边界**（FR-00）：显式点击优先 → 指令区（≤16 字符短指令）关键词 → LLM 意图分类兜底（置信度 <0.7 追问）；正文关键词不触发路由。
- **派生边界**（FR-06）：只派生有数据依据的方向；网页整理不参与；邮件草稿为终端产物。
- **网页整理边界**（FR-04）：仅单页读取（一次一个 URL），不批量爬取；检出登录表单/登录页直接拒绝，**不做任何登录**。
