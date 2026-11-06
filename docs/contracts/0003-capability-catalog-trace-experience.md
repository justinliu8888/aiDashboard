# 0003 — 能力总目录 / 执行追踪 / 编辑经验 契约

状态：2026-11-05 首次实施落地中。本文件是 T1/T2/T3/T4/T0 之间的**唯一接口约定**。
字段名以本文件为准；各工作包不得自行另立同义字段。

---

## 1. 能力总目录（T1 独占）

### 1.1 文件位置

| 用途 | 路径 |
| --- | --- |
| 生成器 / 读取接口 | `backend/app/services/capability_catalog_service.py` |
| 人可读总目录（唯一一份） | `backend/app/agents/prompts/capability_catalog.generated.md` |

目录文件是**生成物**，不入 git 冲突负担；它是运行时唯一权威说明来源。

### 1.2 服务公开接口（T2/T0 依赖，不得改签名）

```python
# backend/app/services/capability_catalog_service.py

CATALOG_PATH: Path            # = agents/prompts/capability_catalog.generated.md

def catalog_version() -> str:
    """已加载契约的指纹（sha256 前 16 位）。加载后固定不变。"""

def build_catalog() -> dict:
    """从真实注册项构造目录数据（纯函数，不落盘）。"""

def catalog_payload() -> dict:
    """机器可读元数据：{"version":..., "generatedAt":..., "entries":[...]}"""

def catalog_text(state: str | None = None) -> str:
    """人/模型共用的可读目录正文。state 为 None 时给全量。"""

def ensure_catalog_fresh(*, force: bool = False) -> str:
    """核对已加载契约指纹与目录文件；不一致则重新生成并原子替换。

    返回目录文件里的 version。生成失败抛异常（不静默回退旧手写说明）。
    """

def load_catalog_text() -> str:
    """读回目录文件正文。文件不存在或版本过期时抛异常。"""
```

### 1.3 目录条目字段（每条必备）

```python
{
  "id": str,                     # 稳定标识（Tool 名 / 能力 id / 旧 intent）
  "group": str,                  # 能力分组
  "entryType": "agent_tool" | "operation" | "legacy_intent",
  "status": "available" | "unsupported" | "not_wired",
  "purpose": str,                # 用途（人类可读）
  "params": dict,                # JSON Schema（来自真实参数模型）
  "required": [str],             # 必填参数名
  "targetSpec": str,             # 目标/引用要求
  "resultSpec": str,             # 返回值说明
  "states": [str],               # 适用节点（AgentState 值）
  "readOnly": bool,
  "sideEffects": str,            # 是否改草稿、何时正式写入
  "errors": [str],               # 主要错误含义
  "limits": [str],               # 主要运行限制
  "code": {                      # 源码定位
     "registration": "app/tools/registry.py::register_tool",
     "dispatch":    "app/tools/submit_edit_plan_tool.py::run",
     "validation":  "app/schemas/edit_plan.py::EditPlanPayload",
     "execution":   "app/services/edit_capabilities/blocks.py::exec_add_section",
  },
}
```

`code` 四项来源：Tool → registry + 工具模块自身 + `PARAMETERS` 声明处 + `run()`；
操作清单能力 → `edit_capabilities/base.py::register` + `blocks.py` 执行函数；
旧 intent → `schemas/edit_plan.py::EditIntent` + `services/template_edit_engine.py::TemplateEditEngine.apply`。

### 1.4 自动同步闭环

- 启动：`main.py` 调 `ensure_catalog_fresh(force=True)`，失败 → 启动报错（不静默）。
- 开发重载：`_build_agent` 每次构造前调 `ensure_catalog_fresh()`；
  契约指纹与目录文件不一致 → 重新生成 + 原子替换（tmp 文件 + `os.replace`）。
- 并发保护：`threading.Lock` + 模块级 `RLock`；生成期间用锁独占。
- 源码改变但 Python 注册对象仍旧时：**以已加载对象的真实内容为准生成**，
  并在目录文件头记录 `contractFingerprint`（真实注册项内容指纹）；
  目录 version 由该指纹派生。这样「源码改了但没重载」不会冒充同步 ——
  目录 version 不变，下一次真正重载后 version 才变。
- **禁止**要求用户手动跑导出命令。

### 1.5 提示词接线

- `template_edit.md` 最终**只保留流程**（查证 → 试做 → 反馈修正 → 用户确认）。
  不得再手写能力名 / 参数 / 分类 / 白名单。
- 编辑 Agent 的 system prompt = `template_edit.md` 正文 + 目录正文（按 state 过滤）拼接。
- 拼接在**每次 `_build_agent`** 时做，不在 `__init__` 缓存。
- `submit_edit_plan_tool.py` 的 `DESCRIPTION` 里的能力清单段删除，改为引用目录；
  该工具的 `PARAMETERS`（真实契约）保持不变。

---

## 2. 执行追踪（T2 独占）

### 2.1 文件

`backend/app/services/edit_trace_service.py`

### 2.2 事件字段（统一，全部必填，除标注外）

```python
{
  "traceId": str,            # 本轮请求追踪标识
  "sessionId": str,
  "itemId": str,             # 修改事项（可空）
  "planId": str,             # 候选计划（可空）
  "draftId": str,
  "catalogVersion": str,     # 本轮固定的能力目录版本
  "stage": str,              # 见 2.3
  "replyIndex": int,         # 第几次 agent.reply()（1 起）
  "iteration": int | None,   # 真实 ReAct 迭代编号（框架事件取得；未取得为 None）
  "toolCallId": str,         # 框架工具调用标识（无则空串）
  "name": str,               # 工具名或能力名
  "params": dict,            # 脱敏参数
  "result": dict,            # 关键返回（脱敏 + 裁剪）
  "truncated": bool,         # 结果是否被裁剪
  "durationMs": int,
  "status": str,             # "ok" | "error"
  "errorCategory": str,      # 见 2.4；无错为空串
  "budget": int,             # 本轮预算
  "stopReason": str,         # 停止原因；未停止为空串
  "at": str,                 # ISO 时间
}
```

### 2.3 stage 取值

`"agent_reply"`, `"tool_call"`, `"submit"`, `"trial"`, `"candidate"`,
`"acceptance"`, `"apply"`, `"experience"`, `"error"`

### 2.4 errorCategory 取值（**互不混用**）

| 取值 | 含义 |
| --- | --- |
| `model_unavailable` | 模型接口不可用 / 认证失败 |
| `model_timeout` | 模型请求超时 |
| `model_error` | 模型返回错误 |
| `tool_lookup_failed` | 事实工具查不到目标 / 返回不支持 |
| `tool_error` | 工具执行报错 |
| `params_invalid` | 模型输出或提交参数不合法 |
| `trial_failed` | 预执行某步校验失败 |
| `acceptance_failed` | 接受时版本/权限/锁/正式提交失败 |
| `budget_exhausted` | 预算耗尽仍未提交 |
| `not_submitted` | 循环结束但未提交（预算未耗尽） |
| `unknown` | 无法进一步定位 |

通用兜底只用 `unknown`，且摘要里写明「原因尚未确定」。

### 2.5 公开接口

```python
def current_trace() -> dict | None: ...
def start_trace(*, session_id, draft_id="", catalog_version="") -> dict: ...
def record(stage: str, **fields) -> dict: ...          # 写一条事件并返回它
def trace_summary() -> dict: ...                       # 本轮诊断摘要（可返回给用户）
def bind_trace(trace: dict) / clear_trace() -> None: ...
def redact(value, *, limit=200) -> Any: ...            # 脱敏（去密钥样式内容）
```

- 上下文用 `ContextVar`（请求作用域），异步/并行工具**不串线**。
- `trace_summary()` 返回结构：`{"stage", "stopReason", "errorCategory", "budget",
  "iterations", "toolCalls", "submitted", "tried", "candidateRegistered",
  "applied", "messages": [用户可读行], "traceId"}`。
- 存储：复用现有 `logging_config` 的文件日志机制；配置项走 `config.py`：
  `edit_trace_enabled` / `edit_trace_max_bytes` / `edit_trace_retention_days` /
  `edit_trace_dir`。默认写入**受限本机目录**（同 diagnostics 风格），不进 API 响应。

---

## 3. 编辑经验（T3 独占；T0 接正式事务）

### 3.1 文件

```
backend/app/storage/edit_experience_store.py
backend/app/services/edit_experience_service.py
backend/app/routers/edit_experiences.py
backend/app/tools/search_edit_experience_tool.py
```

### 3.2 表结构（两张，复用会话库连接与锁）

`edit_experience_records`（执行记录）：

| 列 | 说明 |
| --- | --- |
| `record_id` | 稳定主键 `rec_...` |
| `session_id` / `draft_id` / `plan_id` / `item_id` | 请求关联 |
| `original_request` | 原要求（脱敏截断） |
| `supplement` | 补充要求（脱敏截断，可空） |
| `operations_json` | 实际操作清单（脱敏） |
| `result_json` | 实际结果摘要（脱敏） |
| `capability_version` | 能力目录版本 |
| `status` | `pending` / `approved` / `rejected` |
| `created_at` / `reviewed_at` / `reviewer` | 审核信息 |

`edit_experiences`（可信经验）：

| 列 | 说明 |
| --- | --- |
| `experience_id` | 主键 `exp_...` |
| `record_id` | 来源 recordId |
| `method` | 管理员整理的**可移植方法** |
| `conditions` | 适用条件 |
| `capability_version` | 所需能力版本 |
| `status` | `approved` / `disabled` |
| `created_at` / `updated_at` / `reviewer` | 审核信息 |
| `version` | 并发版本号（乐观锁） |

**禁止**持久化整份业务数据、凭证、他人区块 ID。

### 3.3 管理接口契约（前缀 `/api/edit-experiences`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/records` | 待审核/已审核执行记录列表（分页 + status 过滤） |
| GET | `/admin/records/{record_id}` | 单条记录详情 |
| GET | `/admin/experiences` | 已批准经验列表 |
| POST | `/admin/records/{record_id}/approve` | 批准：body `{method, conditions, expectedVersion}` → 创建经验 |
| POST | `/admin/records/{record_id}/reject` | 拒绝：body `{reason?, expectedVersion}` |
| PATCH | `/admin/experiences/{experience_id}` | 整理/禁用：body `{method?, conditions?, disabled?, expectedVersion}` |

- 鉴权：复用 `app/services/feedback_auth.py`（`admin_configured` / `read_admin_username`
  / `request_origin_ok`）。**不另建一套密码逻辑。**
- 未登录 → **401**；管理员未配置 → **503**；跨源写 → **403**。
- 权限拒绝**不改变数据**。

### 3.4 检索工具（注册进 `tools/registry.py`，由 T1 登记）

```
NAME = "search_edit_experience"
DESCRIPTION = "检索已批准且未禁用的编辑经验（方法 + 适用条件）。"
PARAMETERS = {"type":"object","properties":{
    "query":{"type":"string","description":"用户本次修改要求的关键词"},
    "maxResults":{"type":"integer","description":"最多返回几条，默认 3，上限 3"}},
  "required":["query"]}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = ("USER_EDITING_TEMPLATE", "WAITING_USER_CONFIRM")
```

- 检索方式：SQLite 文本检索 + 规范化关键词评分；**最多返回 3 条**。
- 只返回 `status='approved'`；`disabled` 不返回。
- 无匹配 → 返回空结果（正常继续），**不报错**；技术错误 → 明确报错并记 `tool_error`。
- 返回体只含：id、method、conditions、capabilityVersion、score、来源 recordId。

### 3.5 准入规则（硬约束）

- 只有「用户接受 + 正式事务成功」才产生 `pending` 记录。
- 同一 `planId` 正式提交重复 → **幂等**，不产生重复记录。
- 正式提交失败 → **不留成功记录**（可记失败诊断，但不是可复用经验）。
- 已停止/取消的任务 → **不得迟到晋升**。
- 用户接受次数 ≠ 批准。批准只能由管理员接口产生。

### 3.6 经验复用（同一 Agent）

- 新编辑事项**先检索再分析**；有命中 → 复用方法并**重新绑定当前目标**再试做。
- 经验内容里的区块 ID **不得直接照搬**（服务端在返回时替换为当前目标角色）。
- 检索/复用事件进追踪日志（stage=`tool_call` / `experience`），标明采用或放弃及理由。
- 补充同一事项时，检索**不得覆盖原要求**。

---

## 4. 编辑预算（T2 独占，`config.py` 收口）

| 配置 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `react_max_iters_edit` | `REACT_MAX_ITERS_EDIT` | **8** | 新旧编辑**共用**的唯一编辑预算 |
| `react_max_iters_edit_capability` | — | — | **废弃**：保留为 `react_max_iters_edit` 的只读别名，不再生效 |
| `react_max_iters` | `REACT_MAX_ITERS` | 6 | 查询/生成态，不变 |
| `react_max_iters_fix` | `REACT_MAX_ITERS_FIX` | 5 | 修复态，不变 |

- 优先级：`REACT_MAX_ITERS_EDIT` > `REACT_MAX_ITERS_EDIT_CAPABILITY`（旧名，兼容）> 默认 8。
  **只有一个开关真正生效**，不新增第二个实质冲突的开关。
- `AgentScopeReportAgent._capability_budget_active()` **删除**；
  编辑态一律用 `react_max_iters_edit`。
- 编辑态 `_run_agent` **无有效提交即结束**，不再第二次 `agent.reply()` 催交。
- 保留循环内错误反馈（参数错 / 引用错 / 试做失败退回同一循环修正），
  且**失败不占成功提交槽**。
- 框架结构化输出宽限单独计数与记录，不改名为「第二次催交」。

---

## 5. 生命周期与事务（T0 独占）

| 前置 | 允许 | 成功 | 失败保护 |
| --- | --- | --- | --- |
| 稳定编辑态，新修改 | 后端校验状态/草稿身份/写锁 | 同一有界 ReAct → 候选 | 未提交/试做失败保留正式草稿 |
| AI 工作/跳转/停止中 | 前端禁发送，后端拒冲突 | 确认停止才进下一态 | 取消浏览器请求 ≠ 服务端停止 |
| 待选择/待接受候选 | 只允许合法选项或显式同事项补充 | 补充替换旧候选 | 版本/身份不符拒绝 |
| 接受成功 | **同一事务**内：更新草稿、消费候选、写历史、写待审核记录 | 回草稿级 | 事务失败整笔回滚，不部分生效 |
| 取消/撤销/恢复 | 走既有入口 | 定义/状态/提示恢复 | 不回放旧请求，不复活已消费候选 |

- **终态记录与活动现场分开**：接受成功 / 取消后清理当前 `editItem`；
  后续新事项不复用已结束旧清单；候选补充仍保存同一事项有效内容。
- 清理**不删除**聊天或撤销历史。
- 撤销保持原内容/菜单/权限恢复语义；已批准经验被管理员禁用 ≠ 用户草稿撤销。

---

## 6. 工作包文件所有权（唯一真相）

| 包 | 独占文件 |
| --- | --- |
| T1 | `services/capability_catalog_service.py`、`agents/prompts/capability_catalog.generated.md`、`tools/registry.py`、`services/edit_capabilities/*`（仅目录所需元数据） |
| T2 | `agents/agentscope_report_agent.py`、`config.py`、`services/edit_trace_service.py`、`agents/prompts/template_edit.md`、`tools/submit_edit_plan_tool.py`、`services/edit_trial_service.py` |
| T3 | `storage/edit_experience_store.py`、`services/edit_experience_service.py`、`routers/edit_experiences.py`、`tools/search_edit_experience_tool.py` |
| T4 | `frontend/pages/feedback_inbox.{html,js,css}`、`frontend/pages/workbench.html` |
| T0 | `services/agent_service.py`、`services/session_service.py`、`routers/agent.py`、`main.py`、`.trae/rules/project_rules.md`、`docs/contracts/0003-*.md`、执行记录 |

- `tools/registry.py` 由 **T1 统一登记** T3 的搜索工具与 T2 的包装接缝，其他人不动。
- `feedback.py` / `feedback_auth.py` 如需共享鉴权改造由 **T3 独占**，T0 不同时编辑。
- 新增测试文件按 `test_capability_catalog.py`（T1）/ `test_edit_trace_budget.py`（T2）/
  `test_edit_experience_api.py`（T3）分开；共享集成用例归 T0。
