# 智能报表 AI

通过自然语言生成、修改、确认和预览结构化报表的 AI-first 原型。当前第一版聚焦“机房耗电量日报”，用于验证从用户需求到报表草稿、确定性校验和可视化预览的完整闭环。

> 当前处于本地原型和接口联调阶段。模型可以接入真实火山方舟服务，但业务数据、编译、发布和导出仍有 Mock 或未接入部分，不应直接用于生产环境。

## 项目简介

传统报表设计器依赖组件拖拽、字段配置和数据治理知识。本项目尝试将主要操作收敛为自然语言对话，让用户专注于“想看什么”，由 AI 生成结构化报表定义，再由确定性后端完成状态流转、权限与能力拦截、修改确认、校验和预览。

当前主流程：

```text
自然语言需求
  -> AI 分析与必要追问
  -> 生成结构化报表草稿
  -> 用户确认框架或提出修改
  -> 展示待确认修改计划
  -> 后端原子应用已接受的修改
  -> 保存、校验和生成预览
  -> 前端渲染报表
```

## 功能特性

- **自然语言生成**：根据报表目标生成标题、指标卡、趋势图、排名表、数据质量和分析说明等结构。
- **自然语言编辑**：支持改名称、增删区块、切换展示形式、数据源、指标、粒度、时间范围、TopN 和排序等。
- **修改前确认**：AI 先生成候选修改计划，用户明确接受后才修改草稿；组合修改按一次原子操作执行。
- **状态机编排**：覆盖分析、追问、拦截、草稿、确认、编辑、保存、校验、预览和完成等状态。
- **权限与能力拦截**：信息不足时追问，能力不支持或权限越界时返回结构化原因，不让模型绕过边界。
- **流式响应与过程追踪**：工作台通过 SSE 展示回复增量、执行阶段、路由决策和耗时。
- **会话管理**：支持会话创建、搜索、恢复、改名和删除，并持久化消息、草稿快照和待确认计划。
- **确定性校验与预览**：后端校验报表结构和字段，并使用 ECharts 渲染当前的模拟业务数据。
- **双 Agent 模式**：默认使用规则驱动的 `FakeReportAgent`，也可切换到 AgentScope 和火山方舟真实模型。

## 当前范围

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 机房能耗日报生成 | 已实现 | 支持草稿生成、确认、编辑、校验和预览主链路。 |
| 自然语言模型 | 可选接入 | `fake` 可离线运行；`agentscope` 需要火山方舟 API Key。 |
| 会话持久化 | 已实现 | 默认使用本地 SQLite。 |
| 前端联调工作台 | 已实现 | 由 FastAPI 同源提供静态页面和 API。 |
| v1-v3 静态原型 | 已实现 | 用于产品方案演进和页面演示，不等同于当前联调工作台。 |
| 真实业务取数与编译 | 未接入 | 当前预览使用确定性模拟数据。 |
| Java 保存、权限和指标协议 | 待联调 | Python 侧目前使用本地实现或 Mock。 |
| 发布与 PDF 导出 | 未接入 | 当前只显示未接入提示，不会假装执行成功。 |
| 生产级鉴权与部署 | 未完成 | 当前 CORS 面向本地联调开放，不可直接暴露公网。 |

## 快速开始

### 环境要求

- macOS 或 Linux
- Python 3.10+，推荐 Python 3.11+
- Node.js 20.19+（仅运行 Evalite 评测时需要）
- 可访问页面所使用的 CDN 资源
- 火山方舟 API Key（仅 `AGENT_IMPL=agentscope` 时需要）

### 安装

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

默认配置使用 `FakeReportAgent`，无需填写模型密钥即可运行主流程。

### 启动

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

启动后可访问：

- 工作台：<http://127.0.0.1:8000/>
- 工作台直接地址：<http://127.0.0.1:8000/playground/pages/workbench.html>
- OpenAPI 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

不要只用静态服务器打开 `frontend/pages/workbench.html`。该页面依赖 `/health`、`/api/sessions` 和其他后端接口，应通过 FastAPI 访问。

### 接入真实模型

编辑 `backend/.env`：

```dotenv
AGENT_IMPL=agentscope
VOLC_API_KEY=your-api-key-here
VOLC_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
VOLC_MODEL=doubao-seed-2-1-lite-260915
VOLC_TEMPERATURE=0.2
VOLC_TIMEOUT=30
```

`.env` 已被 Git 忽略。不要提交真实 API Key、Token 或其他密钥。

## 使用说明

一个典型操作流程如下：

1. 打开工作台并新建会话。
2. 输入“生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析”。
3. 检查 AI 生成的报表框架和右侧预览。
4. 用自然语言提出修改，例如“把趋势图改成最近 7 天”或“删除趋势图”。
5. 检查待确认修改计划，选择“接受修改”“继续调整”或“取消”。
6. 对整份草稿执行确认，触发保存、校验和预览流程。

注意：“接受修改”只更新当前草稿，“确认报表”才会推进整份报表的保存、校验和预览，两者不是同一个动作。

## 原型版本

| 版本 | 定位 | 当前用途 |
| --- | --- | --- |
| v1 | 传统拖拽设计器 + AI 辅助面板 | 保留早期探索记录。 |
| v2 | AI-first 轻量生成闭环 | 作为上一版方案对照。 |
| v3 | 用户页面隐藏数据治理入口 | 当前静态原型方向。 |

只查看 v3 静态原型时，可以单独启动静态服务：

```bash
python3 -m http.server 8001 --directory "v3/report-ai-workspace v3"
```

页面地址：

- <http://127.0.0.1:8001/home.html>
- <http://127.0.0.1:8001/workbench.html>
- <http://127.0.0.1:8001/validation.html>
- <http://127.0.0.1:8001/result.html>

## 项目结构

```text
ai功能/
├── frontend/pages/            # 当前前后端联调工作台
├── backend/
│   ├── app/
│   │   ├── agents/            # FakeAgent、AgentScope Agent 和 Prompt
│   │   ├── routers/           # Agent、报表预览、会话和 Mock 能力 API
│   │   ├── schemas/           # Agent、报表、编辑计划和会话协议
│   │   ├── services/          # 编排、状态机、编辑、校验、预览和会话服务
│   │   ├── storage/           # SQLite Repository 与存储模型
│   │   ├── tools/             # Agent 可调用的受约束工具
│   │   └── main.py            # FastAPI 应用入口
│   ├── scripts/               # HTTP 和真实模型冒烟脚本
│   ├── tests/                 # pytest 测试
│   └── requirements.txt
├── v1/、v2/、v3/              # 三代静态原型和产品说明
├── flows/                     # 前端交互冒烟流程
├── checks.json                # v3 四页静态自检配置
├── evals/                     # Evalite 接口契约评测
├── bench/                     # LLM TTFT/TPOT 性能脚本
├── docs/                      # ADR 与 Agent 协作文档
├── GLOSSARY.md                # 领域术语
└── README.md
```

## 配置说明

运行配置从 `backend/.env` 和环境变量读取。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AGENT_IMPL` | `fake` | Agent 实现，可选 `fake` 或 `agentscope`。 |
| `VOLC_API_KEY` | 空 | 火山方舟 API Key。 |
| `VOLC_BASE_URL` | 方舟 OpenAI 兼容地址 | 模型接口地址。 |
| `VOLC_MODEL` | 见 `config.py` | 业务 Agent 模型或 endpoint ID。 |
| `VOLC_TEMPERATURE` | `0.2` | 模型温度。 |
| `VOLC_TIMEOUT` | `30` | 模型调用超时，单位为秒。 |
| `VOLC_CONTEXT_CONCIERGE_MODEL` | 跟随 `VOLC_MODEL` | ContextEngine 接待模型。 |
| `VOLC_CONTEXT_ROUTER_MODEL` | 跟随 `VOLC_MODEL` | ContextEngine 深度路由模型。 |
| `VOLC_CONTEXT_CONCIERGE_TIMEOUT` | `30` | 接待模型超时，单位为秒。 |
| `VOLC_CONTEXT_ROUTER_TIMEOUT` | `30` | 路由模型超时，单位为秒。 |
| `LLM_REPAIR_RETRIES` | `1` | 结构校验失败后的修复重试次数。 |
| `REACT_MAX_ITERS` | `6` | 生成流程的 ReAct 最大步数。 |
| `REACT_MAX_ITERS_EDIT` | `4` | 编辑流程的 ReAct 最大步数。 |
| `REACT_MAX_ITERS_FIX` | `5` | 修复流程的 ReAct 最大步数。 |
| `AI_SESSION_DB_PATH` | `backend/data/haewu.db` | 会话 SQLite 文件路径。 |
| `AI_BACKEND_LOG_PATH` | 系统日志目录 | 后端滚动日志路径。 |

`AI_ACCESS_TOKEN` 虽然已预留配置位，但当前路由尚未完成鉴权接入，不能将它视为已生效的公网保护。

## 主要接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 健康检查。 |
| `POST /api/agent/chat` | 同步 AI 对话。 |
| `POST /api/agent/chat/stream` | SSE 流式 AI 对话。 |
| `GET /api/report/preview/{report_run_id}` | 获取报表预览结果。 |
| `/api/sessions` | 会话创建、查询、更新和删除。 |
| `/api/workspaces` | 工作空间创建、查询、更新和删除。 |
| `GET /api/mock/catalogs` | 查询本地 Mock 目录能力。 |
| `GET /api/mock/metrics` | 查询本地 Mock 指标能力。 |

完整请求和响应模型以启动后的 `/docs` 及 `backend/01_接口协议.md` 为准。

## 测试与验证

### 后端测试

```bash
cd backend
source .venv/bin/activate
pytest -q
```

### HTTP 冒烟

先启动后端，再执行：

```bash
cd backend
python3 scripts/smoke_e2e.py
```

真实模型冒烟需要正确配置火山方舟：

```bash
cd backend
AGENT_IMPL=agentscope python3 scripts/smoke_llm.py
```

### Evalite 评测

先保持后端运行，再打开另一个终端：

```bash
cd evals
npm ci
npm run eval:run
```

开发监听模式：

```bash
npm run eval:dev
```

### 前端静态自检

从包含 `.trae/` 的工作区根目录执行：

```bash
node .trae/skills/frontend-self-check/scripts/selfcheck.mjs \
  --config "20261012 智能报表功能/ai功能/checks.json"
```

该配置检查 v3 的 `home`、`workbench`、`validation` 和 `result` 四个静态页面。

### 前端交互冒烟

```bash
node .trae/skills/frontend-interaction-check/scripts/interact.mjs \
  --config "20261012 智能报表功能/ai功能/flows/workbench.flow.json"
```

`flows/` 还包含会话切换、请求期间锁定、刷新恢复、待确认编辑计划和预览编辑等专项流程。依赖真实 API 的流程需要先启动 FastAPI 后端。

## 部署说明

当前推荐部署方式仅限本机开发：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

仓库暂未提供 Docker、Kubernetes、Nginx 或 CI/CD 配置。正式部署前至少需要完成：

- 接入真实鉴权，并验证 Token 在所有 API 和前端请求中生效。
- 限制 CORS 来源，不能继续使用 `allow_origins=["*"]`。
- 通过密钥管理服务保存模型 API Key。
- 配置 HTTPS、反向代理、限流、审计和异常监控。
- 为 SQLite 或替代数据库配置持久化、备份和恢复方案。
- 验证多进程并发下的会话锁和状态一致性。

在这些事项完成前，不要通过 Tunnel 或公网服务器直接暴露当前服务。

## 常见问题

### 第一版是否已经接通真实业务数据？

没有。自然语言生成和编辑可以接真实模型，但当前预览仍使用确定性模拟数据，真实 Java 取数、编译和正式预览尚未完成。

### `fake` 和 `agentscope` 有什么区别？

`fake` 使用关键词和确定性规则，适合本地开发、测试和演示；`agentscope` 调用火山方舟真实模型，适合验证自然语言理解与 ReAct 工具调用。

### 接受修改后为什么还要确认报表？

接受修改只把候选计划应用到草稿；确认报表才推进整份草稿的保存、校验和预览。这种分层确认用于避免局部修改被误当成最终发布动作。

### 模型失败或用户停止请求会破坏草稿吗？

不会。失败、超时或停止不会用半截结果覆盖已确认草稿；“继续工作”会重新执行原请求，而不是从模型中途续算。

### 为什么不能直接把本地服务临时挂到公网？

当前 CORS 对本地联调开放，访问令牌尚未真正接入路由，且 SQLite 数据位于运行机器上。Tunnel 只改变访问通道，不会自动补齐鉴权、备份和安全隔离。

### 日志和本地数据在哪里？

- 会话数据库默认位于 `backend/data/haewu.db`。
- 后端日志默认位于 `~/Library/Logs/haewu-ai-report/backend.log`。
- 日志单文件最大 10 MB，默认保留 5 份轮转文件。

## 路线图

- 接入真实 Java 保存、权限、指标、取数和编译协议。
- 用真实业务数据替换 Mock 预览并完成结果一致性验收。
- 完成生产级鉴权、CORS、密钥管理和部署配置。
- 补齐发布、导出和失败后自动修复能力。
- 验证多 worker 会话一致性和数据库迁移方案。
- 收敛 v3 产品待确认项及 Skill/MCP 能力发现机制。

## 相关文档

- `backend/20261022_第一版闭环范围_访谈记录.md`：第一版验收范围和产品口径。
- `backend/01_接口协议.md`：Agent 与报表协议。
- `backend/05_功能盘点_按状态机.md`：当前功能和状态机盘点。
- `backend/06_ReAct改造方案.md`：ReAct Agent 架构方案。
- `backend/07_会话管理系统方案.md`：会话持久化和恢复方案。
- `backend/20261022 template-editing react架构改进方案.md`：自然语言编辑与确认方案。
- `backend/20261022 交付报告.md`：最近一次集成验证记录。
- `GLOSSARY.md`：项目领域术语。
- `工具栈选择与讨论.md`：技术栈选择背景。

## 贡献

提交改动前请至少运行受影响范围的测试。修改前端页面或交互后，还应依次运行静态自检和相应交互冒烟，并检查生成的截图。

建议在 Pull Request 中说明：

- 改动解决的问题和影响范围。
- 接口、状态机或领域模型是否变化。
- 已执行的测试命令和结果。
- 尚未覆盖的风险、Mock 边界和后续事项。

## License

当前仓库未声明开源许可证，默认按内部项目管理。未经项目所有者明确授权，不得视为可自由复制、修改或分发的开源软件。

## 联系方式

维护负责人、反馈渠道和线上演示地址尚未在仓库中统一记录，建议由项目团队补充。
