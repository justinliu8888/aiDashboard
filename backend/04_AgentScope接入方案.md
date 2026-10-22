# AgentScope 接入方案（按状态机讨论稿）

> 目标：沿 `00_统筹.md` 的 Agent 链路和 12 个状态，从头到尾盘清楚 AgentScope 在每一步负责什么、调用什么受控能力、产出什么协议对象，再决定具体实现方式。
>
> 本文档先讨论边界和流程，不急着写代码。定稿后再实现 `agentscope_report_agent.py`。

---

## 0. 当前基线

已经完成：

| 模块 | 状态 |
| --- | --- |
| `00_统筹.md` | 已定义首功能、状态机、验收样例和扩展原则 |
| `01_接口协议.md` | 已定义 `AgentRequest` / `AgentResponse` / `ReportDefinitionDraft` / `ChangeSet` |
| FakeAgent | 已覆盖 6 个验收样例 |
| FastAPI | 已提供 `POST /api/agent/chat` |
| pytest / smoke | 已验证接口契约和 6 个样例 |
| workbench 页面 | 已能真实调用 FakeAgent 并展示草稿 / 追问 / 拦截 / ChangeSet |

AgentScope 接入原则：**只换大脑，不改协议和前端。**

---

## 1. 总体链路对齐 `00_统筹.md`

`00_统筹.md` 里的首功能链路是：

```text
用户输入
  -> 语义分析 Module
  -> 报表框架生成 Module
  -> 用户确认框架
  -> 取数 Skill
  -> Agent 检查取数结果
  -> 渲染 Module
  -> 前端渲染完整报表
```

当前 FakeAgent 只覆盖前半段：

```text
用户输入
  -> 语义分析（Fake）
  -> 报表框架生成（Fake）
  -> 用户确认 / 修改 / 拦截
```

AgentScope 第一阶段也先覆盖前半段，不直接做真实取数、预览、发布：

```text
用户输入
  -> AgentScope 语义理解
  -> 受控工具查询目录 / 指标 / 能力
  -> 生成 ReportPolicyDraft / ReportDefinitionDraft / ChangeSet
  -> 返回 AgentResponse
  -> 前端展示并等待用户确认
```

---

## 2. 状态机逐段接入

### 2.1 `ANALYZING`：理解用户意图

| 项 | 内容 |
| --- | --- |
| 输入 | 用户自然语言、当前 `state`、`context`、可选当前草稿快照 |
| AgentScope 任务 | 识别用户想生成、修改、删除、解释还是越权查询 |
| 可用工具 | `search_catalog`、`search_metric`、`check_capability`、`check_permission` |
| 输出分支 | `NEEDS_CLARIFICATION` / `BLOCKED_OR_UNSUPPORTED` / `DEFINITION_DRAFT_READY` |

这里是 AgentScope 最核心的入口。FakeAgent 现在靠关键词判断，AgentScope 要换成真实语义理解。

关键问题：AgentScope 是直接输出完整 `AgentResponse`，还是先输出一个中间 `IntentPlan`，再由后端组装响应？

---

### 2.2 `NEEDS_CLARIFICATION`：信息不足时追问

| 项 | 内容 |
| --- | --- |
| 触发 | 缺日期、对象、异常口径、范围、展示方式等关键信息 |
| AgentScope 任务 | 生成明确、可回答的追问 |
| 输出对象 | `questions[]` |
| 不做什么 | 不胡乱假设，不直接生成无法校验的定义 |

示例：用户说“帮我看异常设备”，AgentScope 应该追问：异常类型、统计范围、时间窗口。

---

### 2.3 `BLOCKED_OR_UNSUPPORTED`：权限或能力拦截

| 项 | 内容 |
| --- | --- |
| 触发 | 跨项目、超权限、分钟级能力不存在、危险动作等 |
| AgentScope 任务 | 解释为什么不能做，并给替代方案 |
| 输出对象 | `blocks[]` |
| 必须依赖 | 后端权限上下文 / 数据目录能力，不能靠模型猜 |

原则：AgentScope 可以解释和建议，但不能自己决定“放行”。最终边界来自 Java 后端或受控工具。

---

### 2.4 `DEFINITION_DRAFT_READY`：生成草稿或 ChangeSet

这个状态有两种情况。

| 场景 | 输入 | AgentScope 输出 |
| --- | --- | --- |
| 首次生成 | 用户需求 + 目录 / 指标摘要 | `ReportPolicyDraft` + `ReportDefinitionDraft` |
| 修改已有草稿 | 当前定义 + `baseDefinitionRevision` + 用户修改要求 | `ChangeSet` |

这里要特别控制输出可靠性：

- 数据源、指标、单位不能编造。
- 修改必须按稳定区域 ID 定位，不能按数组下标。
- `ChangeSet` 必须带 `baseDefinitionRevision`。
- 删除区域用 `after: null` 表达。

---

### 2.5 `WAITING_USER_CONFIRM`：等待用户确认

| 项 | 内容 |
| --- | --- |
| 触发 | 草稿或 ChangeSet 已展示给用户 |
| AgentScope 任务 | 解释草稿、解释变更、响应用户的确认 / 拒绝 / 修改 |
| 输出分支 | 用户确认后进入保存；用户修改则回到 `ANALYZING`；用户拒绝则重新生成或给 ChangeSet |

第一阶段可以不让 AgentScope 真正保存，只返回 `uiActions`，仍由现有流程展示。

---

### 2.6 `SAVE_DEFINITION_DRAFT`：保存草稿

| 项 | 内容 |
| --- | --- |
| 真实职责 | Java 后端保存定义草稿版本 |
| AgentScope 职责 | 不直接保存，只生成保存请求意图或解释 |
| 第一阶段 | 暂不实现 |

---

### 2.7 `VALIDATING` / `VALIDATION_FAILED`：校验与修正

| 状态 | AgentScope 职责 | 第一阶段 |
| --- | --- | --- |
| `VALIDATING` | 调校验工具，解释校验中状态 | 暂不实现 |
| `VALIDATION_FAILED` | 根据校验结果生成修正建议或 `ChangeSet` | 暂不实现 |

这部分依赖 Java 后端真实校验接口，等草稿生成稳定后再接。

---

### 2.8 `PREVIEWING` / `PREVIEW_READY` / `RENDERING` / `READY`

| 状态 | AgentScope 职责 | 第一阶段 |
| --- | --- | --- |
| `PREVIEWING` | 发起预览运行或解释预览中 | 暂不实现 |
| `PREVIEW_READY` | 解释预览结果、质量提示 | 暂不实现 |
| `RENDERING` | 不参与前端渲染，只可解释上下文 | 暂不实现 |
| `READY` | 支持继续修改、解释结果 | 暂不实现 |

这些属于后半段链路，等 Java 后端预览能力接入后再扩展。

---

## 3. 第一阶段建议范围

第一阶段只替换 FakeAgent 的前半段能力：

| 验收样例 | 是否接 AgentScope | 原因 |
| --- | --- | --- |
| 样例 1：正常生成 | 是 | 验证真实 LLM 能否产出合法草稿 |
| 样例 2：信息不足 | 可选 | 追问相对简单，可以第二步接 |
| 样例 3：权限越界 | 暂不接 | 权限边界应依赖后端，不先交给模型 |
| 样例 4：能力不支持 | 暂不接 | 依赖目录能力校验，先保留 FakeAgent |
| 样例 5：改 7 天 | 暂不接 | ChangeSet 稳定性要求高 |
| 样例 6：删除趋势图 | 暂不接 | 删除语义和版本并发需要更稳 |

推荐第一阶段目标：**只让 AgentScope 生成样例 1 的 `ReportPolicyDraft` + `ReportDefinitionDraft`，其他样例继续走 FakeAgent。**

---

## 4. AgentScope 与现有代码边界

接入点仍然是 `agent_service.py`，不改路由和协议。

```text
POST /api/agent/chat
  -> agent_service.py
  -> 根据配置选择 agent
     -> fake_report_agent.py
     -> agentscope_report_agent.py
  -> 返回 AgentResponse
```

实际落地结构：

```text
app/
  agents/
    agentscope_report_agent.py
    agent_router.py
    fake_report_agent.py
    prompts/
      report_generation.md
  tools/                      ← 13 个工具，统一收在这里，一个工具一个文件
    base.py                   ← 统一契约 + 节点白名单常量
    registry.py               ← 注册 / Toolkit 构建 / 契约校验 / 中台清单导出
    context.py                ← ToolContext（授权范围 + 草稿状态）
    submission.py             ← 终结工具提交槽
    schemas.py                ← 终结工具载荷模型
  services/
    scope_service.py          ← 项目 / 实例可见性规则
  config.py
scripts/
  smoke_llm.py
```

FakeAgent 必须保留：

- 作为 pytest 的稳定对象。
- 作为 LLM 失败时的降级。
- 作为协议变更时的基准样例。

---

## 5. 工具边界

AgentScope 不应该直接编造数据源、指标、单位和能力，必须通过工具拿到候选项。

所有工具统一收在 [app/tools/](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools) 目录，**一个工具一个文件**：

| 工具 | 分类 | 用途 | 来源 |
| --- | --- | --- | --- |
| `search_catalog` | FACT | 查可用数据源、字段、粒度、权限标签 | `catalog_mock_service`，后续换 Java Catalog |
| `search_metric` | FACT | 查指标口径、单位、维度 | `metric_mock_service`，后续换 Java Metric |
| `check_capability` | FACT | 判断小时级 / 日级 / 分钟级是否支持 | 数据目录能力 |
| `get_current_draft` | FACT | 读当前草稿概览 | 请求快照 |
| `get_current_region` | FACT | 读单个 region（编辑语境） | 请求快照 |
| `get_validation_result` | FACT | 读结构化校验问题 | 校验服务 |
| `build_definition_draft` | BUILDER | 根据 Agent 的结构化计划组装完整定义 | `report_draft_service` |
| `submit_*`（6 个） | TERMINAL | 提交本轮结论 | 各工具 + 确定性组装层 |

关键选择：LLM 是直接产完整 `ReportDefinitionDraft`，还是只产“报表计划”，由后端工具组装完整定义。

候选建议：**LLM 只产计划，后端组装完整定义。** 这样更稳定，也更符合“AI 负责理解，系统负责确定规则”。

**工具还必须过一层数据范围授权**：能调用工具 ≠ 能看所有数据。目录 / 指标 / 能力查询都要按 `projectId` / `subjectId` 过滤，范围由后端从 `ToolContext` 取，不暴露给模型。详见 `06_ReAct改造方案.md` 的 [6.3.1](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/06_ReAct改造方案.md)。

---

## 6. 输出可靠性

所有 AgentScope 输出必须过协议校验。

```text
LLM 输出
  -> JSON 解析
  -> Pydantic 校验
  -> 业务规则校验（数据源/指标/单位/状态流转）
  -> 失败则修正重试
  -> 仍失败则降级 FakeAgent 或返回 BLOCKED_OR_UNSUPPORTED
```

需要先决定：

| 问题 | 候选 |
| --- | --- |
| 重试次数 | 1 / 2 / 3 |
| 失败兜底 | 降级 FakeAgent / 返回错误 |
| pytest 是否触发 LLM | 不触发 |
| LLM 冒烟测试 | 单独用 `scripts/smoke_llm.py` |

---

## 7. 需要逐项讨论的 5 个关键问题

后续只讨论这 5 个问题：

| # | 问题 | 要决定什么 |
| --- | --- | --- |
| Q1 | 到底用不用 AgentScope？ | ✅ 用 AgentScope，不直接裸调 LLM SDK |
| Q2 | 选哪个 LLM？ | ✅ 优先用火山引擎，适合国内访问和已有预算 |
| Q3 | Agent 架构怎么做？ | ✅ 单 Agent + Skills/Tools，优先沉淀为 Agent 中台能力 |
| Q4 | 第一阶段只做什么？ | ✅ 只替换样例 1 的正常生成流程 |
| Q5 | LLM 输出不合法时怎么办？ | ✅ 修复重试 1 次，仍失败降级 FakeAgent，再失败返回拦截 |

讨论顺序：Q1 / Q2 / Q3 / Q4 / Q5 均已确认。

### 7.1 已确认：Q1 用 AgentScope

结论：接入真实大脑时使用 AgentScope，不直接裸调 LLM SDK。

理由：

- 和最初技术方向一致，后续扩展工具调用、多 Agent、状态编排更自然。
- 可以把语义理解、工具调用、草稿生成和修正建议收束到 Agent 编排里。
- 仍然保留 `FakeAgent`，用于 pytest、降级和协议基准。

工程边界：业务代码不要直接依赖具体模型 SDK，而是通过 `AgentScopeReportAgent` 封装。

### 7.2 已确认：Q2 用火山引擎

结论：第一版 LLM 供应商优先使用火山引擎。

理由：

- 国内访问更方便，避免国外模型网络和账号问题。
- 已经充值，开发验证成本更可控。
- 火山引擎模型能力足够先验证“自然语言 -> 报表草稿 JSON”的闭环。

接入方式候选：

| 方式 | 说明 | 倾向 |
| --- | --- | --- |
| OpenAI-compatible API | 如果火山当前模型接口兼容 OpenAI Chat Completions，可让 AgentScope 走 OpenAI 兼容配置 | 优先 |
| AgentScope 自定义模型封装 | 如果 AgentScope 没有现成火山适配，就封一层 `VolcengineModel` | 备选 |

待实现时确认的配置项：

- `VOLCENGINE_API_KEY`
- `VOLCENGINE_BASE_URL`
- `VOLCENGINE_MODEL` 或火山上的 endpoint / model id
- 是否支持结构化输出 / JSON mode / tool calling

### 7.3 已确认：Q3 单 Agent + Skills/Tools

结论：采用单 Agent + Skills/Tools，不做单 Agent 大 Prompt，也不在第一阶段拆多 Agent。

推荐运行方式：

```text
AgentScopeReportAgent
  -> 理解用户输入
  -> 决定需要哪些事实或能力
  -> 调用受控 Skill / Tool
  -> 输出中间计划
  -> 后端确定性函数组装 definition / changeSet
  -> Pydantic 校验
  -> 返回 AgentResponse
```

分工：

| 步骤 | 谁负责 | 说明 |
| --- | --- | --- |
| 理解用户输入 | Agent | 判断生成、修改、追问、拦截等意图 |
| 选择要查的事实 | Agent | 决定是否需要查目录、指标、能力、权限 |
| 执行事实查询 | Skill / Tool | 查询数据源、指标、能力、权限等确定信息 |
| 输出中间计划 | Agent | 例如要生成哪些区域、每个区域用什么指标和窗口 |
| 组装 `ReportDefinitionDraft` | 后端确定性函数 / Skill | 按 schema 和规则生成稳定结构 |
| 组装 `ChangeSet` | 后端确定性函数 / Skill | 按稳定区域 ID、revision、path 生成变更 |
| Pydantic 校验 | 后端 schema | 确保输出符合 `01_接口协议.md` |
| 返回 `AgentResponse` | `agent_service.py` | 统一包装接口响应 |

Skill 化原则：

- 能复用、能被 Agent 中台沉淀的能力，优先做成 Skill。
- 目录查询、指标查询、能力检查、权限检查、定义组装、ChangeSet 组装都可以逐步 Skill 化。
- 第一阶段为了快，可以先用 Python Tool / Service 实现；接口形状按 Skill 设计，后续迁到 Agent 中台不推翻。
- Agent 不直接编造数据源、指标、单位、权限和版本，只能通过 Skill / Tool 获取。

第一阶段建议先实现这些 Skill/Tool：

| Skill / Tool | 第一阶段实现方式 | 后续中台化方向 |
| --- | --- | --- |
| `CatalogSkill` | 包装 `catalog_mock_service` | 对接 Java Catalog / 数据目录 |
| `MetricSkill` | 包装 `metric_mock_service` | 对接 Java Metric / 指标目录 |
| `CapabilitySkill` | 包装目录能力检查 | 对接统一能力校验服务 |
| `DefinitionBuilderSkill` | 包装 `report_draft_service` | 沉淀为报表定义生成能力 |
| `ChangeSetBuilderSkill` | 包装 `report_draft_service` | 沉淀为报表变更生成能力 |

**落地状态**：上述能力已统一实现于 [app/tools/](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools) 目录（**一个工具一个文件**），共 13 个工具：

```text
只读事实（FACT）
  search_catalog / search_metric / check_capability
  get_current_draft / get_current_region / get_validation_result
确定性组装（BUILDER）
  build_definition_draft
终结工具（TERMINAL）
  submit_draft_plan / submit_clarification / submit_blocked
  submit_change_plan / submit_edit_plan / submit_fix_plan
```

统一契约（`NAME` / `DESCRIPTION` / `PARAMETERS` / `CATEGORY` / `IS_READ_ONLY` / `APPLICABLE_STATES` / `CONCLUSION_STATES` / `run()`）见 [base.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/base.py)，中台上架清单由 `registry.export_manifest()` 导出 —— **迁中台时只换实现不改契约**。详细方案见 `06_ReAct改造方案.md`。

**契约版本 `TOOL_CONTRACT_VERSION = 1.1.0`**（`1.0.0` → `1.1.0` 的唯一变更是新增 `CONCLUSION_STATES`）。

**`CONCLUSION_STATES`：终结工具声明自己可能落哪些状态**

同一个终结工具可能产生不止一个结论状态：

| 工具 | 结论状态 | 分叉条件 |
| --- | --- | --- |
| `submit_clarification` | `NEEDS_CLARIFICATION` / `ANALYZING` | 带 `missingInfo` → 追问；不带 → 停在 `ANALYZING` |
| `submit_edit_plan` | `USER_EDITING_TEMPLATE` / `NEEDS_CLARIFICATION` | 计划清楚 → 交给确定性引擎执行；说不清 → 追问 |

用元组而不是单值，才能把这层分叉显式化，也才能对**每个结论分支逐个**校验合法性。非终结工具不得声明该字段。

**节点白名单与合法转移的关系（核心不变量）**

> **工具在某节点可用 ⇔ 它的结论状态在该节点是合法转移。**

合法转移由 `ALLOWED_TRANSITIONS` 唯一决定（[agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/schemas/agent.py)），`is_conclusion_legal()` 封装判定：保持原状态算合法，其余必须在允许集合里。

这条不变量由 `registry.validate_tool_contracts()` 自动校验，有测试守着：

```python
def test_tool_contracts_are_self_consistent():
    problems = registry.validate_tool_contracts()
    assert problems == [], "工具契约不自洽：\n" + "\n".join(problems)
```

违反它意味着**模型能挑中一个落不下去的分支** —— 比如在 `VALIDATION_FAILED` 提交一个 `WAITING_USER_CONFIRM` 的结论。

**节点级白名单已经真正接入 AgentScope Toolkit**

`tools_for_state(state)` 的结果注册进 Toolkit，模型看不到当前节点不该调的工具。各节点可用终结工具：

```text
ANALYZING              → submit_draft_plan / submit_clarification / submit_blocked / submit_change_plan
NEEDS_CLARIFICATION    → submit_draft_plan / submit_clarification
DEFINITION_DRAFT_READY → submit_change_plan
WAITING_USER_CONFIRM   → submit_clarification / submit_blocked / submit_change_plan / submit_edit_plan
USER_EDITING_TEMPLATE  → submit_clarification / submit_edit_plan
VALIDATION_FAILED      → submit_fix_plan
其余节点               → （无终结工具，流水线节点不需要模型决策）
```

**中台清单导出**（`registry.export_manifest()` / `export_manifest_json()`）里每个工具一条：

```json
{
  "name": "submit_edit_plan",
  "description": "提交对当前草稿某个区块的编辑计划……",
  "parameters": { "type": "object", "properties": { "...": {} } },
  "category": "TERMINAL",
  "readOnly": false,
  "applicableStates": ["WAITING_USER_CONFIRM", "USER_EDITING_TEMPLATE"],
  "conclusionStates": ["USER_EDITING_TEMPLATE", "NEEDS_CLARIFICATION"]
}
```

### 7.4 已确认：Q4 第一阶段只做样例 1

结论：第一阶段只替换样例 1 的正常生成流程。

目标链路：

```text
用户：生成 A 机房昨日能耗日报
  -> AgentScope + 火山引擎理解需求
  -> 调用 CatalogSkill / MetricSkill / CapabilitySkill
  -> 输出“报表计划”
  -> DefinitionBuilderSkill 组装 ReportPolicyDraft + ReportDefinitionDraft
  -> Pydantic 校验
  -> 返回 WAITING_USER_CONFIRM
  -> workbench 正常展示
```

第一阶段不做：

- 不做样例 2 的追问。
- 不做样例 3 的权限越界。
- 不做样例 4 的能力不支持。
- 不做样例 5/6 的 ChangeSet 修改。
- 不做保存、校验、预览、发布。

保留策略：

- 样例 1 命中 AgentScope。
- 样例 2~6 继续走 FakeAgent。
- 如果 AgentScope 失败，进入 Q5 决定的兜底逻辑。

### 7.5 已确认：Q5 输出不合法的生产处理策略

结论：LLM 输出不合法时，不直接相信，也不直接报错。按工业生产常见策略处理：先校验，校验失败自动修复重试 1 次，仍失败降级 FakeAgent，最后才返回拦截。

处理链路：

```text
AgentScope + 火山输出
  -> JSON 解析
  -> Pydantic 校验
  -> 业务规则校验（状态机 / 数据源 / 指标 / 单位 / revision / path）
  -> 失败则把错误信息喂回模型，修复重试 1 次
  -> 仍失败则降级 FakeAgent
  -> FakeAgent 也无法处理则返回 BLOCKED_OR_UNSUPPORTED
  -> 全程记录日志
```

为什么只重试 1 次：

- 控制接口延迟。
- 控制 token 成本。
- 第一阶段有 FakeAgent 兜底，不需要多次烧模型。
- 后续可以根据失败日志再决定是否增加到 2 次。

日志至少记录：

| 字段 | 用途 |
| --- | --- |
| `conversationId` | 追踪用户会话 |
| `state` | 定位状态机阶段 |
| `provider` / `model` | 定位模型来源 |
| `promptHash` | 避免直接泄露完整 prompt，同时能定位版本 |
| `rawOutput` | 排查模型原始输出问题，生产可脱敏 |
| `validationErrors` | 记录 JSON / Pydantic / 业务校验失败原因 |
| `retryCount` | 观察是否频繁依赖修复重试 |
| `fallbackUsed` | 判断是否降级 FakeAgent |
| `latencyMs` / `tokenUsage` | 观测性能和成本 |

原则：LLM 只能给候选结果，不能成为最终事实来源。最终结构由 schema 校验，事实由 Skill / Tool 校验，权限、保存、校验、发布仍由后端控制。

---

## 8. 下一步

1. 开始代码实现 `AgentScopeReportAgent`。
2. 接入火山引擎配置。
3. 实现第一阶段 Skills/Tools。
4. 写 `scripts/smoke_llm.py` 验证样例 1。
