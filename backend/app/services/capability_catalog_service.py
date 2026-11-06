"""能力总目录服务：**唯一一份**「现在能做什么」的权威说明。

**目录从哪来**：每一条都是从**真实注册项**长出来的 —— 已注册 Tool、
清单能力（``edit_capabilities``）、旧编辑意图（``EditIntent``）、
展示操作白名单（``report_presentation_service.ALLOWED_OPERATIONS``）、
规则快路（``agent_service`` / ``template_edit_engine``）。
参数契约直接取注册项自己的 JSON Schema，**不手抄、不另维护一张参数表**。

**人和模型读同一份**：
``backend/app/agents/prompts/capability_catalog.generated.md`` 是生成物
（人直接打开即可）；``catalog_text(state)`` 给模型拼提示词，
**筛选也来自同一份目录数据**，不另写一份参数说明。

**版本与自动同步**：目录版本由**已加载注册项的真实内容**派生
（sha256 前 16 位）。源码改了但 Python 没重载时，已加载对象没变 →
版本不变 → 目录不会冒充「已同步」；真正重载后版本才变，
``ensure_catalog_fresh()`` 随即重新生成并**原子替换**文件。
生成失败**抛异常**，不静默回退到旧的手写说明。

契约见 ``docs/contracts/0003-capability-catalog-trace-experience.md`` §1。
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------

#: 后端工程根目录（``backend/``）。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: 能力总目录文件（**生成物**，人直接打开即可）。
CATALOG_PATH = (
    _BACKEND_ROOT / "app" / "agents" / "prompts" / "capability_catalog.generated.md"
)

#: 生成过程独占锁：并发请求同时刷新目录时只生成一次。
_LOCK = threading.RLock()

#: 目录条目必备字段（契约 §1.3）—— 少一个都不算合格条目。
_ENTRY_FIELDS: tuple[str, ...] = (
    "id",
    "group",
    "entryType",
    "status",
    "purpose",
    "params",
    "required",
    "targetSpec",
    "resultSpec",
    "states",
    "readOnly",
    "sideEffects",
    "errors",
    "limits",
    "code",
)

#: 不参与指纹计算的字段（每次生成都会变，不能影响版本）。
_VOLATILE_KEYS: tuple[str, ...] = ("generatedAt",)

#: 入口类型的中文说法。
_ENTRY_TYPE_LABEL: dict[str, str] = {
    "agent_tool": "Agent Tool —— 模型可直接调用",
    "operation": "后端执行的操作 —— 不是独立 Tool",
    "legacy_intent": "旧格式编辑意图 —— 不是独立 Tool",
}

#: 机器可读元数据在文件末尾的 ```json 代码块（**唯一**的机器可读来源）。
_METADATA_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


# --------------------------------------------------------------------------
# 工具的补充说明
#
# 这里只写「注册项里读不出来的语义」（目标要求 / 返回形状 / 错误 / 限制）。
# 新增工具时**不必**改这张表：没有补充说明的工具走通用兜底文案，
# 目录照样收录，参数与状态仍然取自真实注册项。
# --------------------------------------------------------------------------

_TOOL_DETAILS: dict[str, dict[str, Any]] = {
    "search_catalog": {
        "targetSpec": "sourceRef / subjectRef 可空；不传时返回当前账号可见的全部目录。",
        "resultSpec": "{items: 目录项[], total: 总数, truncated: 是否被裁剪}",
        "errors": [
            "没有授权范围时直接报错，不返回空列表冒充「没有目录」",
            "目录服务不可用时抛错，由统一注册层转成 error 结果（不降级成空结果）",
        ],
        "limits": ["结果按 observation_max_items 裁剪，可用 limit 参数指定条数"],
    },
    "search_metric": {
        "targetSpec": "metricRef / subjectRef 可空；不传时返回当前账号可见的全部指标。",
        "resultSpec": "{items: 指标项[], total: 总数, truncated: 是否被裁剪}",
        "errors": [
            "没有授权范围时直接报错，不返回空列表冒充「没有指标」",
            "指标目录不可用时抛错，由统一注册层转成 error 结果",
        ],
        "limits": ["结果按 observation_max_items 裁剪，可用 limit 参数指定条数"],
    },
    "check_capability": {
        "targetSpec": "sourceRef 必须是目录里真实存在的数据源 id；grainUnit 取粒度单位。",
        "resultSpec": "{sourceRef, grainUnit, supported: bool, supportedGrains: [...]}",
        "errors": [
            "数据源不存在或越权 → supported=false 且不返回任何能力细节（不泄漏越权信息）",
        ],
        "limits": ["一次只核对一个数据源的一个粒度"],
    },
    "get_current_draft": {
        "targetSpec": "无参数。",
        "resultSpec": (
            "当前草稿定义：regions（含正文卡片 id 清单）、可用共享主题 availableThemes、"
            "统计口径与时间范围等"
        ),
        "errors": ["当前没有草稿 → 返回 found=false 与原因，不编造草稿内容"],
        "limits": ["只读当前会话的草稿，不跨会话"],
    },
    "get_current_region": {
        "targetSpec": "regionId 必须来自 get_current_draft 返回的区块 id（不按标题猜）。",
        "resultSpec": "{found: bool, region: 区块详情}；找不到时附 availableRegionIds",
        "errors": [
            "没有草稿上下文 → found=false + 原因",
            "regionId 不存在 → found=false + 可用区块 id 清单",
        ],
        "limits": ["一次只读一个区块"],
    },
    "get_current_session_status": {
        "targetSpec": "无参数。",
        "resultSpec": (
            "{status, scope, sessionExists, businessStage, hasDraft, reportName, "
            "draftRevision, pendingSteps, pendingEditPlan, pendingChoice, savedDefinition, "
            "preview, clarificationRounds, source, reason}"
        ),
        "errors": ["会话记录不存在 → 返回 status 与 reason，不报错"],
        "limits": ["只读当前会话"],
    },
    "get_validation_result": {
        "targetSpec": "无参数。",
        "resultSpec": "{available: bool, passed: bool, issues: 问题[], total, truncated}",
        "errors": ["当前没有校验上下文 → available=false + 原因", "还没有跑过校验 → available=false + 原因"],
        "limits": ["只在 VALIDATION_FAILED 节点可用"],
    },
    "get_recorded_progress": {
        "targetSpec": "无参数；会话 id 取自请求级上下文。",
        "resultSpec": "{status: FOUND/ABSENT/UNAVAILABLE/FORBIDDEN, ...}（四态事实状态）",
        "errors": ["上下文缺失 → UNAVAILABLE；越权 → FORBIDDEN，不返回细节"],
        "limits": ["只读当前会话的已记录进度"],
    },
    "check_product_capability": {
        "targetSpec": "sourceRef / metricRef / grainUnit / component 均可空，按传入项核对。",
        "resultSpec": "{status: 四态事实状态, outcome: 领域结论, ...}",
        "errors": [
            "授权范围缺失 → MISSING_CONTEXT（事实查到，限制在别处披露）",
            "未登记 → NOT_REGISTERED（折算为 ABSENT）；越权 → NOT_AUTHORIZED（折算为 FORBIDDEN）",
        ],
        "limits": ["只核对能力边界，不执行修改"],
    },
    "list_simulated_dimensions": {
        "targetSpec": "无参数。",
        "resultSpec": "{status, dimensions: 维度[], payloadContract: 载荷契约, note}",
        "errors": ["维度目录不可用时抛错，由统一注册层转成 error 结果"],
        "limits": [
            "维度只有三个（alertLevel / alertRegion / alertType）",
            "数值均为**模拟**数据，载荷带 simulated 标记，不接真实告警源",
        ],
    },
    "build_definition_draft": {
        "targetSpec": "按草稿计划的载荷组装。",
        "resultSpec": "组装出的草稿定义与 ChangeSet（服务端现场，不改正式草稿）。",
        "errors": ["组装失败（口径不兼容等）→ 抛错，由统一注册层转成 error 结果"],
        "limits": ["只在 ANALYZING / NEEDS_CLARIFICATION 节点可用"],
    },
    "submit_draft_plan": {
        "targetSpec": "无（面向用户的生成计划）。",
        "resultSpec": "{accepted: true, tool: 'submit_draft_plan'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：一轮只认第一次提交"],
    },
    "submit_clarification": {
        "targetSpec": "无。",
        "resultSpec": "{accepted: true, tool: 'submit_clarification'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：一轮只认第一次提交"],
    },
    "submit_blocked": {
        "targetSpec": "无。",
        "resultSpec": "{accepted: true, tool: 'submit_blocked'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：代表本轮判定为不支持 / 被拦截"],
    },
    "submit_change_plan": {
        "targetSpec": "修改方案的目标与操作。",
        "resultSpec": "{accepted: true, tool: 'submit_change_plan'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：一轮只认第一次提交"],
    },
    "submit_edit_answer": {
        "targetSpec": "无（只回答，不承载修改操作）。",
        "resultSpec": "{accepted: true, tool: 'submit_edit_answer'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：只回答，不创建 EditPlan"],
    },
    "submit_edit_plan": {
        "targetSpec": (
            "target.regionId（或 target.ref 指向本次新增区块）；"
            "能力格式里每一步的 target 各自独立"
        ),
        "resultSpec": (
            "{accepted: true, tool: 'submit_edit_plan', checked, summary, resolvedOperations}；"
            "能力格式试做失败 → {accepted: false, error, errors, hint}"
        ),
        "errors": [
            "载荷不合法 → {accepted: false, error: '载荷不合法：...'}",
            "能力格式试做失败 → 返回**具体**错误（哪一步、哪个目标、哪条约束、为什么），"
            "草稿不变，按反馈改正 operations 后再次调用",
            "服务端读不到当前草稿 → {accepted: false, error}，草稿不变",
        ],
        "limits": [
            "终结工具：一轮只认第一次提交",
            "能力格式整批试做：任一步失败则整批不生效",
        ],
        # 真正执行能力格式计划的地方是试做服务（包装层只是转发）。
        "executionRef": "app/services/edit_trial_service.py::run_trial",
    },
    "submit_fix_plan": {
        "targetSpec": "针对校验问题清单的修复动作。",
        "resultSpec": "{accepted: true, tool: 'submit_fix_plan'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：只在 VALIDATION_FAILED 节点可用"],
    },
    "submit_query_answer": {
        "targetSpec": "无。",
        "resultSpec": "{accepted: true, tool: 'submit_query_answer'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": [
            "终结工具：只在只读查询模式下注册（IS_QUERY_ONLY）",
            "只回答，不推进业务状态（结论状态 SAME）",
        ],
    },
}

#: 工具分组的分类标签。
_TOOL_GROUP_BY_CATEGORY: dict[str, str] = {
    "FACT": "工具 / 事实查询（只读）",
    "BUILDER": "工具 / 确定性组装",
    "TERMINAL": "工具 / 终结提交",
}

#: 工具的通用兜底文案（没有补充说明的新工具走这里）。
_TOOL_FALLBACK: dict[str, dict[str, Any]] = {
    "FACT": {
        "targetSpec": "见参数契约。",
        "resultSpec": "事实查询结果；具体字段见该工具的实现。",
        "errors": [
            "没有授权范围时直接报错，不返回空结果冒充「查不到」",
            "工具异常会被统一注册层截断成 error 结果返回",
        ],
        "limits": ["结果条数受 observation_max_items 约束"],
    },
    "BUILDER": {
        "targetSpec": "按载荷组装。",
        "resultSpec": "组装结果（草稿定义与 ChangeSet）。",
        "errors": ["组装失败 → 抛错，由统一注册层转成 error 结果"],
        "limits": ["只在声明的适用节点可用"],
    },
    "TERMINAL": {
        "targetSpec": "按载荷契约提交本轮结论。",
        "resultSpec": "{accepted: true, tool: '<工具名>'}",
        "errors": ["载荷不合法 → {accepted: false, error: '载荷不合法：...'}"],
        "limits": ["终结工具：一轮只认第一次提交"],
    },
}

#: 工具的副作用说明（按分类）。
_TOOL_SIDE_EFFECTS: dict[str, str] = {
    "FACT": "只读：不改草稿、不写业务数据。",
    "BUILDER": "在服务端现场组装草稿，不直接改正式草稿。",
    "TERMINAL": (
        "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；"
        "草稿的正式改动由用户接受候选后的事务落库。"
    ),
}


# --------------------------------------------------------------------------
# 旧编辑意图的补充说明
# --------------------------------------------------------------------------

#: 旧意图 → 分组。
_LEGACY_INTENT_GROUP: dict[str, str] = {
    "RENAME_REPORT": "旧意图 / 报表与区块增删改名",
    "DELETE_SECTION": "旧意图 / 报表与区块增删改名",
    "RENAME_SECTION": "旧意图 / 报表与区块增删改名",
    "ADD_SECTION": "旧意图 / 报表与区块增删改名",
    "DELETE_METRIC": "旧意图 / 指标增删替换",
    "ADD_METRIC": "旧意图 / 指标增删替换",
    "CHANGE_METRIC": "旧意图 / 指标增删替换",
    "CHANGE_DATA_SOURCE": "旧意图 / 数据口径与时间",
    "CHANGE_COMPILATION_GRAIN": "旧意图 / 数据口径与时间",
    "CHANGE_TIME_RANGE": "旧意图 / 数据口径与时间",
    "CHANGE_TOP_N": "旧意图 / 数据口径与时间",
    "CHANGE_SORT": "旧意图 / 数据口径与时间",
    "CHANGE_VISUALIZATION": "旧意图 / 展示方式",
    "SUGGEST_VISUALIZATION": "旧意图 / 展示方式",
    "APPLY_PRESENTATION": "旧意图 / 展示与样式",
    "SAVE_THEME": "旧意图 / 共享主题",
    "UNKNOWN": "旧意图 / 未识别",
}

#: 旧意图 → 用途说明（业务语义）。
_LEGACY_INTENT_PURPOSE: dict[str, str] = {
    "RENAME_REPORT": "改报表名称（不动任何区块）。",
    "DELETE_SECTION": "删掉一整张卡片（区块）。",
    "DELETE_METRIC": "在已有卡片里删掉某一项指标（卡片本身保留）。",
    "RENAME_SECTION": "改某张卡片的标题。",
    "ADD_SECTION": "新增一张卡片（旧格式入口；能力格式请用 ADD_SECTION 能力）。",
    "ADD_METRIC": "在已有卡片里**追加**指标（保留原有指标）。",
    "CHANGE_DATA_SOURCE": "换一个数据源。",
    "CHANGE_VISUALIZATION": "改展示类型（componentType / chartKind）。",
    "SUGGEST_VISUALIZATION": "只给展示方式建议，**不改草稿**。",
    "CHANGE_COMPILATION_GRAIN": "改统计粒度。",
    "CHANGE_TIME_RANGE": "改时间范围。",
    "CHANGE_METRIC": "替换某张卡片里的指标。",
    "CHANGE_TOP_N": "改 Top N。",
    "CHANGE_SORT": "改排序字段与方向。",
    "APPLY_PRESENTATION": "一批受约束的展示操作（分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题）。",
    "SAVE_THEME": "把当前外观存成 / 更新一个**共享主题**（改主题本身，不改报表定义）。",
    "UNKNOWN": "无法识别的意图：转成追问，不改草稿。",
}

#: 旧意图 → 真正执行的引擎方法。
_LEGACY_INTENT_EXECUTION: dict[str, str] = {
    "RENAME_REPORT": "_apply_rename_report",
    "DELETE_SECTION": "_apply_delete",
    "DELETE_METRIC": "_apply_delete_metric",
    "RENAME_SECTION": "_apply_rename",
    "ADD_SECTION": "_apply_add",
    "ADD_METRIC": "_apply_add_metric",
    "CHANGE_DATA_SOURCE": "_apply_change_source",
    "CHANGE_VISUALIZATION": "_apply_change_visualization",
    "SUGGEST_VISUALIZATION": "_markdown_display_options",
    "CHANGE_COMPILATION_GRAIN": "_apply_change_grain",
    "CHANGE_TIME_RANGE": "_apply_change_time_range",
    "CHANGE_METRIC": "_apply_change_metric",
    "CHANGE_TOP_N": "_apply_change_top_n",
    "CHANGE_SORT": "_apply_change_sort",
    "APPLY_PRESENTATION": "_apply_presentation",
    "SAVE_THEME": "_apply_save_theme",
    "UNKNOWN": "apply",
}

#: 旧意图 → 只读与否。
_LEGACY_INTENT_READ_ONLY: dict[str, bool] = {
    "SUGGEST_VISUALIZATION": True,
    "UNKNOWN": True,
}

#: 旧意图 → 额外限制。
_LEGACY_INTENT_LIMITS: dict[str, list[str]] = {
    "ADD_METRIC": ["追加保留原指标；「替换 / 只留 / 换成」才是 CHANGE_METRIC"],
    "DELETE_METRIC": ["只删卡片内的指标项，卡片本身保留；目标不唯一要追问"],
    "APPLY_PRESENTATION": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
    ],
    "SAVE_THEME": [
        "未确认不落库：confirm=false 时只算影响摘要并追问",
        "不改任何报表的定义（regions / layout / format 不动），定义版本也不 bump",
    ],
    "SUGGEST_VISUALIZATION": ["只回建议，不改草稿，不占修改澄清轮次"],
    "UNKNOWN": ["不改草稿；转成一次追问"],
}


# --------------------------------------------------------------------------
# 规则快路（确定性快路径，不是独立 Tool）
# --------------------------------------------------------------------------

_RULE_FAST_PATHS: list[dict[str, Any]] = [
    {
        "id": "RULE_TEMPLATE_EDIT",
        "group": "规则快路（确定性快路径，不是独立 Tool）",
        "purpose": (
            "规则快路（**不是独立 Tool**，模型调不到）："
            "编辑态收到明确可判定的修改请求时，规则层直接解析出编辑计划并形成候选，"
            "不经模型。模型只处理规则认不出来的部分。"
        ),
        "targetSpec": "目标由「前端可见正文卡片 ID 的顺序」与用户点名共同确定；认不准就追问。",
        "resultSpec": "AgentResponse：待确认候选 + 「接受修改 / 撤回上一步 / 继续修改」菜单。",
        "readOnly": False,
        "sideEffects": "只形成待确认候选；草稿的正式改动由用户接受后的事务落库。",
        "errors": [
            "指代词没选中对象 / 序数越界 → 转成一次澄清",
            "时间窗说不清 → 给具体时间段选项让用户选",
        ],
        "limits": [
            "只在 WAITING_USER_CONFIRM / USER_EDITING_TEMPLATE 生效",
            "只在规则明确命中时接管，认不出来就交回模型支路",
        ],
        "code": {
            "registration": "app/services/agent_service.py::AgentService._try_handle_template_edit",
            "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
            "validation": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
            "execution": "app/services/template_edit_engine.py::TemplateEditEngine.apply",
        },
    },
    {
        "id": "RULE_READONLY_QUERY_ANSWER",
        "group": "规则快路（确定性快路径，不是独立 Tool）",
        "purpose": (
            "规则快路（**不是独立 Tool**，模型调不到）："
            "编辑态的只读能力查询（如「支持哪些粒度」）能用确定性证据回答时直接答。"
        ),
        "targetSpec": "无（只回答当前草稿的能力范围）。",
        "resultSpec": "AgentResponse：只追加聊天，不产生待确认计划，不推进业务状态。",
        "readOnly": True,
        "sideEffects": "只读：不改草稿、不产生候选、不占修改澄清轮次。",
        "errors": ["查不到证据时返回空，不编造能力"],
        "limits": ["只回答只读问题；涉及修改的请求一律走修改支路"],
        "code": {
            "registration": "app/services/agent_service.py::AgentService._try_answer_readonly_query",
            "dispatch": "app/services/agent_service.py::AgentService._try_answer_readonly_query",
            "validation": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
            "execution": "app/services/agent_service.py::AgentService._try_answer_readonly_query",
        },
    },
    {
        "id": "RULE_CORRECT_PLAN",
        "group": "规则快路（确定性快路径，不是独立 Tool）",
        "purpose": (
            "规则快路（**不是独立 Tool**，模型调不到）："
            "用户对**当前待办**的纠正（「其他」自由文本）落到同一份计划上，"
            "不重开一轮普通对话。"
        ),
        "targetSpec": "目标必须仍是待办计划指的那张卡片；换卡片是新任务，不走这里。",
        "resultSpec": "修正后的 EditPlan（认不出来时返回 None → 同一张卡片里继续追问）。",
        "readOnly": False,
        "sideEffects": "只修正计划本身，不改草稿。",
        "errors": ["纠正后仍说不清要改成什么样 → 返回 None（同一张卡片里继续追问）"],
        "limits": ["只处理对当前待办的纠正，不接受换目标的指令"],
        "code": {
            "registration": "app/services/template_edit_engine.py::TemplateEditEngine.correct_plan",
            "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
            "validation": "app/schemas/edit_plan.py::EditPlan",
            "execution": "app/services/template_edit_engine.py::TemplateEditEngine.correct_plan",
        },
    },
    {
        "id": "RULE_EDIT_PLAN_PARSE",
        "group": "规则快路（确定性快路径，不是独立 Tool）",
        "purpose": (
            "规则快路（**不是独立 Tool**，模型调不到）："
            "把一句自然语言修改要求解析成旧格式 EditPlan（intent / target / operation）。"
        ),
        "targetSpec": "目标由可见正文卡片顺序、用户点名与卡片别名共同确定。",
        "resultSpec": "EditPlan；认不出来时 intent=UNKNOWN 且 needsClarification=true。",
        "readOnly": False,
        "sideEffects": "只解析出计划，不改草稿。",
        "errors": ["认不出来 → intent=UNKNOWN + 追问", "目标不唯一 → 追问是哪一张"],
        "limits": ["只产出旧格式计划；能力格式由模型组装 operations 后走试做"],
        "code": {
            "registration": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
            "dispatch": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
            "validation": "app/schemas/edit_plan.py::EditPlan",
            "execution": "app/services/template_edit_engine.py::TemplateEditEngine.apply",
        },
    },
]


# --------------------------------------------------------------------------
# 源码定位：解析 ``路径::名字`` 引用
# --------------------------------------------------------------------------

def _module_name_of(obj: Any) -> str:
    """工具在注册表里是**模块**（不是实例）—— 取它的模块名。"""
    name = getattr(obj, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return str(getattr(obj, "__module__", ""))


def _file_of_module(module_name: str) -> str:
    """``app.services.foo`` → ``app/services/foo.py``。"""
    module = importlib.import_module(module_name)
    path = getattr(module, "__file__", "") or ""
    if path.endswith(".py"):
        try:
            return str(Path(path).resolve().relative_to(_BACKEND_ROOT)).replace(os.sep, "/")
        except ValueError:
            return str(Path(path).resolve()).replace(os.sep, "/")
    package = getattr(module, "__path__", None)
    if package is not None:
        init = Path(list(package)[0]) / "__init__.py"
        if init.exists():
            try:
                return str(init.resolve().relative_to(_BACKEND_ROOT)).replace(os.sep, "/")
            except ValueError:
                return str(init.resolve()).replace(os.sep, "/")
    return module_name.replace(".", "/") + ".py"


def verify_source_ref(ref: str) -> bool:
    """校验 ``路径::名字`` 指向的文件真实存在、名字真实存在。

    目录里的每条 ``code`` 都要过这一关 —— 写错的源码定位在**生成时**就报错，
    不让错误的导航位置混进目录。
    """
    if "::" not in ref:
        return False
    file_part, _, name_part = ref.partition("::")
    abs_path = _BACKEND_ROOT / file_part
    if not abs_path.is_file():
        return False
    module_name = file_part[:-3].replace("/", ".") if file_part.endswith(".py") else ""
    if not module_name:
        return False
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — 解析不了就当定位无效
        return False
    obj: Any = module
    for part in name_part.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return False
    return True


def _require_ref(ref: str) -> str:
    """源码定位必须是真的 —— 假的直接让生成失败。"""
    if not verify_source_ref(ref):
        raise ValueError(f"能力目录里的源码定位不存在：{ref}")
    return ref


@lru_cache(maxsize=128)
def _module_ast(module_name: str) -> ast.Module:
    """解析模块 AST（按模块缓存，避免每次生成重复解析）。"""
    module = importlib.import_module(module_name)
    source = inspect.getsource(module)
    return ast.parse(source)


def _import_map(nodes: Any) -> dict[str, tuple[str, str]]:
    """收集 ``from app.x import y`` → ``{本地名: (app.x, y)}``。"""
    result: dict[str, tuple[str, str]] = {}
    for node in nodes:
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not module.startswith("app."):
            continue
        for alias in node.names:
            result[alias.asname or alias.name] = (module, alias.name)
    return result


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _resolve_callable(module_path: str, attr: str) -> str | None:
    """把 ``模块.名字`` 解析成**真实定义位置** ``路径::限定名``。

    工具模块里的 ``from app.x import y`` 常常是再导出，真实实现可能在别处；
    这里在运行时取到对象本身，用它的 ``__module__`` / ``__qualname__``
    定位到真正干活的那一行。
    """
    try:
        module = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001
        return None
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    real_module = getattr(obj, "__module__", None)
    qualname = getattr(obj, "__qualname__", None)
    if not isinstance(real_module, str) or not isinstance(qualname, str):
        return None
    if "<locals>" in qualname:
        return None
    try:
        file_path = _file_of_module(real_module)
    except Exception:  # noqa: BLE001
        return None
    return f"{file_path}::{qualname}"


def _tool_execution_ref(tool: Any) -> str:
    """工具 ``run()`` 真正调到的业务实现（找不到就指向 run 本身）。

    工具模块通常只是**包装**：``run()`` 调服务层的函数。
    目录要指向**真正执行的位置**，不能只指包装文件。
    """
    module_name = _module_name_of(tool)
    own = f"{_file_of_module(module_name)}::run"
    try:
        tree = _module_ast(module_name)
    except Exception:  # noqa: BLE001
        return own
    run_node = _find_function(tree, "run")
    if run_node is None:
        return own
    imports = _import_map(ast.walk(tree))
    calls: list[tuple[int, bool, bool, str, str]] = []
    # 分支里的调用（if / elif 的分支体）排在后面 —— 主干上的调用才是
    # 「这个工具到底在干什么」。
    in_branch: set[int] = set()
    for node in ast.walk(run_node):
        if isinstance(node, ast.If):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    in_branch.add(id(sub))
    for node in ast.walk(run_node):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            target = imports.get(node.func.id)
            if target is not None:
                calls.append(
                    (node.lineno, id(node) not in in_branch, False, target[0], target[1])
                )
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base = imports.get(node.func.value.id)
            if base is not None:
                calls.append(
                    (
                        node.lineno,
                        id(node) not in in_branch,
                        True,
                        base[0],
                        f"{base[1]}.{node.func.attr}",
                    )
                )
    calls.sort(key=lambda item: item[0])
    resolved_calls = [
        (lineno, on_main, is_method, ref)
        for lineno, on_main, is_method, module, attr in calls
        if (ref := _resolve_callable(module, attr)) is not None
    ]
    # 优先「主干上的服务实例方法调用」—— 那是真正干活的入口；
    # 再依次退到主干上的服务函数、分支里的调用、工具层；最后才指 run()。
    tiers = (
        (True, True, "app/services/"),
        (True, False, "app/services/"),
        (False, True, "app/services/"),
        (False, False, "app/services/"),
        (True, True, "app/"),
        (True, False, "app/"),
        (False, True, "app/"),
        (False, False, "app/"),
    )
    for want_main, want_method, prefix in tiers:
        for _, on_main, is_method, ref in resolved_calls:
            if on_main == want_main and is_method == want_method and ref.startswith(prefix):
                return _require_ref(ref)
    return own


def _parameters_decl_ref(tool: Any) -> str:
    """PARAMETERS 声明处 —— 真实契约的所在地。"""
    module_name = _module_name_of(tool)
    own = f"{_file_of_module(module_name)}::PARAMETERS"
    try:
        tree = _module_ast(module_name)
    except Exception:  # noqa: BLE001
        return own
    imports = _import_map(tree.body)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "PARAMETERS" not in names:
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "model_json_schema"
            and isinstance(value.func.value, ast.Name)
        ):
            model_name = value.func.value.id
            target = imports.get(model_name)
            if target is not None:
                return _require_ref(f"{_file_of_module(target[0])}::{target[1]}")
    return own


def _reason_codes(func: Any) -> list[str]:
    """从能力执行函数里抽出真实的错误原因码。

    能力失败时抛 ``CapabilityError(reason=...)``；这些码就是目录里
    「主要错误」的**真实来源**，不手抄。
    """
    try:
        source = inspect.getsource(func)
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001
        return []
    codes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name != "CapabilityError":
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
                code = str(keyword.value.value)
                if code and code not in codes:
                    codes.append(code)
    return codes


def _require_keys_of(func: Any) -> tuple[list[str], list[str]]:
    """从展示操作的处理函数里抽出真实的必填 / 可选参数键。

    展示操作没有 pydantic 参数模型，键是 ``_require_keys(params, 必填, 可选)``
    声明的 —— 这里从源码里读出来，不手抄。

    **实现放在共享契约层**（``edit_capabilities.contract``）：目录与运行时
    校验读的是**同一份**读法，不会各自解释一遍处理函数。
    """
    from app.services.edit_capabilities.contract import require_keys_of

    return require_keys_of(func)


# --------------------------------------------------------------------------
# 目录条目构造
# --------------------------------------------------------------------------

def _entry(
    *,
    id: str,
    group: str,
    entryType: str,
    status: str,
    purpose: str,
    params: dict[str, Any],
    required: list[str],
    targetSpec: str,
    resultSpec: str,
    states: list[str],
    readOnly: bool,
    sideEffects: str,
    errors: list[str],
    limits: list[str],
    code: dict[str, str],
) -> dict[str, Any]:
    """造一条目录条目（字段严格按契约 §1.3，缺一不可）。"""
    entry = {
        "id": id,
        "group": group,
        "entryType": entryType,
        "status": status,
        "purpose": purpose,
        "params": params,
        "required": required,
        "targetSpec": targetSpec,
        "resultSpec": resultSpec,
        "states": states,
        "readOnly": readOnly,
        "sideEffects": sideEffects,
        "errors": errors,
        "limits": limits,
        "code": code,
    }
    missing = [field for field in _ENTRY_FIELDS if field not in entry]
    if missing:
        raise ValueError(f"目录条目 {id} 缺字段：{missing}")
    extra = [key for key in entry if key not in _ENTRY_FIELDS]
    if extra:
        raise ValueError(f"目录条目 {id} 有契约外字段：{extra}")
    return entry


def _schema_limits(params: dict[str, Any]) -> list[str]:
    """从 JSON Schema 里抽出真实约束（取值域 / 上下限），不手抄。"""
    limits: list[str] = []
    properties = params.get("properties")
    if not isinstance(properties, dict):
        return limits
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        enum_values = spec.get("enum")
        if isinstance(enum_values, list) and enum_values:
            limits.append(
                f"{name} 取值域：{'、'.join(str(item) for item in enum_values)}"
            )
        if "maximum" in spec:
            limits.append(f"{name} ≤ {spec['maximum']}")
        if "minimum" in spec:
            limits.append(f"{name} ≥ {spec['minimum']}")
        if "maxLength" in spec:
            limits.append(f"{name} 最多 {spec['maxLength']} 个字符")
        if "maxItems" in spec:
            limits.append(f"{name} 最多 {spec['maxItems']} 项")
    return limits


def _edit_states() -> list[str]:
    """清单能力 / 旧意图的适用节点 —— 取自 ``submit_edit_plan`` 的真实声明。"""
    from app.tools import registry

    for tool in registry.list_tools():
        if tool.NAME == "submit_edit_plan":
            return list(tool.APPLICABLE_STATES)
    return ["WAITING_USER_CONFIRM", "USER_EDITING_TEMPLATE"]


def _tool_entries() -> list[dict[str, Any]]:
    """已注册 Agent Tool —— 参数与状态全部取自注册项本身。"""
    from app.tools import registry

    entries: list[dict[str, Any]] = []
    for tool in registry.list_tools():
        category = tool.CATEGORY.value
        detail = _TOOL_DETAILS.get(tool.NAME) or _TOOL_FALLBACK.get(category) or {}
        params = dict(tool.PARAMETERS)
        required = [str(item) for item in (params.get("required") or [])]
        module_name = _module_name_of(tool)
        limits = list(detail.get("limits") or [])
        limits.extend(_schema_limits(params))
        entries.append(
            _entry(
                id=tool.NAME,
                group=_TOOL_GROUP_BY_CATEGORY.get(category, "工具"),
                entryType="agent_tool",
                status="available",
                purpose=str(tool.DESCRIPTION),
                params=params,
                required=required,
                targetSpec=str(detail.get("targetSpec") or "无特殊目标要求。"),
                resultSpec=str(detail.get("resultSpec") or "见该工具的实现。"),
                states=list(tool.APPLICABLE_STATES),
                readOnly=bool(tool.IS_READ_ONLY),
                sideEffects=_TOOL_SIDE_EFFECTS.get(
                    category, "见该工具的实现。"
                ),
                errors=list(detail.get("errors") or []),
                limits=limits,
                code={
                    "registration": _require_ref("app/tools/registry.py::_TOOLS"),
                    "dispatch": _require_ref(f"{_file_of_module(module_name)}::run"),
                    "validation": _parameters_decl_ref(tool),
                    "execution": _require_ref(detail["executionRef"])
                    if detail.get("executionRef")
                    else _tool_execution_ref(tool),
                },
            )
        )
    return entries


def _capability_entries() -> list[dict[str, Any]]:
    """清单能力（操作清单里的能力）—— 参数取自能力自己的 ``params_schema``。"""
    from app.services.edit_capabilities import all_capabilities
    from app.services.edit_capabilities.contract import build_contract

    states = _edit_states()
    contract = build_contract()
    entries: list[dict[str, Any]] = []
    for capability in all_capabilities():
        spec = contract.find(capability.id)
        schema = (
            capability.params_schema.model_json_schema()
            if capability.params_schema is not None
            else {}
        )
        required = [str(item) for item in (schema.get("required") or [])]
        executor = capability.executor
        executor_module = executor.__module__
        executor_name = getattr(executor, "__qualname__", executor.__name__)
        executor_file = _file_of_module(executor_module)
        execution_ref = f"{executor_file}::{executor_name}"
        if not verify_source_ref(execution_ref):
            # 执行器定位不到（少见：闭包 / lambda）—— 退回注册处，
            # 宁可指得粗一点，也不让整个目录生成不出来。
            execution_ref = "app/services/edit_capabilities/base.py::register"
        reasons = _reason_codes(executor)
        errors = [
            f"能力失败抛 CapabilityError（step / op / target / constraint / reason），"
            f"reason={code}" for code in reasons
        ]
        if not errors:
            errors = ["能力失败抛 CapabilityError（带 step / op / target / constraint / reason）"]
        errors.append("参数不合法 → reason=invalid_params，不执行")
        errors.append("目标引用不认识 → reason=unknown_ref / missing_target / unknown_region")
        limits = list(_schema_limits(schema))
        if capability.needs_target:
            limits.append(
                "每一步都要有明确目标：target.regionId（草稿里已有区块）"
                "或 target.ref（本次新增区块的引用名）"
            )
        else:
            limits.append("不需要目标（自己新建区块 / 整批作用于草稿）")
        if capability.creates_target:
            limits.append("新增的区块可用 params.ref 起稳定引用名，后续步骤用 target.ref 指它")
        # 提交位置与包装要求：**来自共享契约**（不是这里手写的说法）。
        if spec is not None:
            for item in spec.limits:
                if item not in limits:
                    limits.append(item)
            limits.append(f"提交位置：{_submission_text(spec)}")
            limits.append(f"包装要求：{json.dumps(spec.envelope, ensure_ascii=False)}")
            limits.append(
                "最小结构示例（只给格式）："
                + json.dumps(spec.example(), ensure_ascii=False)
            )
        entries.append(
            _entry(
                id=capability.id,
                group=f"清单能力 / {capability.family}",
                entryType="operation",
                status="available",
                purpose=(
                    f"{capability.description} "
                    "（**操作清单里的能力，不是独立 Tool**：模型在 "
                    "submit_edit_plan 的 operations 里给出 op + target + params，"
                    "由后端在草稿副本上执行。）"
                    + (
                        f"**提交位置**：{_submission_text(spec)}。"
                        f"**包装要求**：{json.dumps(spec.envelope, ensure_ascii=False)}。"
                        if spec is not None
                        else ""
                    )
                ),
                params=_capability_params(spec) if spec is not None else schema,
                required=required,
                targetSpec=(
                    "每一步各自带 target：target.regionId 指向草稿里已有区块，"
                    "target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。"
                ),
                resultSpec=(
                    "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）"
                    "；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，"
                    "外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）"
                ),
                states=states,
                readOnly=False,
                sideEffects=(
                    "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，"
                    "才在正式事务里写入草稿（版本 +1）。"
                ),
                errors=errors,
                limits=limits,
                code={
                    "registration": _require_ref(
                        "app/services/edit_capabilities/base.py::register"
                    ),
                    "dispatch": _require_ref("app/tools/submit_edit_plan_tool.py::run"),
                    "validation": _require_ref(
                        "app/services/edit_capabilities/base.py::validate_params"
                    ),
                    "execution": _require_ref(execution_ref),
                },
            )
        )
    return entries


def _submission_text(spec: Any) -> str:
    """提交位置的人话说明（来自共享契约）。"""
    from app.services.edit_capabilities.contract import submission_location

    return submission_location(spec.op)


def _capability_params(spec: Any) -> dict[str, Any]:
    """能力参数的真实 JSON Schema（来自共享契约的字段契约）。"""
    properties = {item.name: item.to_json_schema() for item in spec.fields}
    schema: dict[str, Any] = {
        "type": "object",
        "description": f"{spec.op} 的参数对象。",
        "properties": properties,
    }
    required = [item.name for item in spec.fields if item.required]
    if required:
        schema["required"] = required
    return schema


def _legacy_intent_entries() -> list[dict[str, Any]]:
    """旧编辑意图（旧格式入口）—— 不是独立 Tool，由编辑引擎执行。"""
    from app.schemas.edit_plan import EditIntent, EditOperation, EditPlan

    states = _edit_states()
    operation_schema = EditOperation.model_json_schema()
    plan_schema = EditPlan.model_json_schema()
    engine_file = _file_of_module("app.services.template_edit_engine")
    entries: list[dict[str, Any]] = []
    for intent in EditIntent:
        name = intent.value
        execution = _LEGACY_INTENT_EXECUTION.get(name)
        if not execution:
            raise ValueError(f"旧意图 {name} 没有登记执行实现")
        errors = [
            "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
            "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
            "依据的生效组合已变 → 拒绝（combination_conflict）",
            "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）",
        ]
        limits = list(_LEGACY_INTENT_LIMITS.get(name) or [])
        limits.append("单步修改只针对一个区块；要一次改多处请用能力格式 operations")
        if name == "APPLY_PRESENTATION":
            limits.append(
                "旧格式入口：展示操作写在 operation.presentationOperations 里"
                "（每项形如 {\"op\": \"<操作名>\", \"params\": {...}}）；"
                "能力格式里它们写在 SET_PRESENTATION 的 params.operations 里 —— "
                "两者不要混用，展示操作名不要直接放进 submit_edit_plan.operations 的顶层"
            )
        entries.append(
            _entry(
                id=name,
                group=_LEGACY_INTENT_GROUP.get(name, "旧意图 / 其他"),
                entryType="legacy_intent",
                status="available",
                purpose=(
                    f"{_LEGACY_INTENT_PURPOSE.get(name, '')} "
                    "（**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 "
                    "intent / target / operation 里给出，"
                    "由 template_edit_engine.TemplateEditEngine.apply 执行。）"
                ),
                params=operation_schema,
                required=[str(item) for item in (operation_schema.get("required") or [])],
                targetSpec=(
                    "target.regionId 或 target.matchedText 指明要改哪张卡片；"
                    "组合修改用 operations 列表（旧格式每步共用顶层 target）"
                ),
                resultSpec=(
                    "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / "
                    "needsClarification；形成待确认候选，用户接受后才正式写入"
                ),
                states=states,
                readOnly=_LEGACY_INTENT_READ_ONLY.get(name, False),
                sideEffects=(
                    "在草稿副本上执行；通过后形成待确认候选，"
                    "用户接受时才在正式事务里写入草稿。"
                ),
                errors=errors,
                limits=limits,
                code={
                    "registration": _require_ref("app/schemas/edit_plan.py::EditIntent"),
                    "dispatch": _require_ref(
                        "app/services/agent_service.py::AgentService._try_handle_template_edit"
                    ),
                    "validation": _require_ref("app/schemas/edit_plan.py::EditPlan"),
                    "execution": _require_ref(f"{engine_file}::TemplateEditEngine.{execution}"),
                },
            )
        )
    # plan_schema 只用于确认契约真实存在（参数模型在这里声明）。
    if not plan_schema:
        raise ValueError("EditPlan 的参数契约为空")
    return entries


def _presentation_entries() -> list[dict[str, Any]]:
    """展示操作（包装能力内部的操作）—— 键与取值域取自真实实现。"""
    from app.services import report_presentation_service as presentation
    from app.services.edit_capabilities.contract import build_contract

    states = _edit_states()
    service_file = _file_of_module("app.services.report_presentation_service")
    contract = build_contract()
    package = contract.package_of("SET_PRESENTATION")
    entries: list[dict[str, Any]] = []
    for op in presentation.ALLOWED_OPERATIONS:
        handler = presentation._HANDLERS.get(op)
        if handler is None:
            raise ValueError(f"展示操作 {op} 没有对应的执行函数")
        spec = contract.find(op)
        required, optional = _require_keys_of(handler)
        properties: dict[str, Any] = {}
        for item in (spec.fields if spec is not None else ()):
            properties[item.name] = item.to_json_schema()
        params: dict[str, Any] = {
            "type": "object",
            "description": (
                f"展示操作 {op} 的参数对象。"
                + (f"必填：{'、'.join(required)}。" if required else "没有必填键。")
                + (f"可选：{'、'.join(optional)}。" if optional else "")
                + "多出来的键会被拒绝。"
            ),
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            params["required"] = list(required)
        errors = [
            "op 不在白名单 → invalid_operation",
            "参数键不对 → invalid_operation（展示服务的参数校验）",
            "删除 / 移动导致引用断裂 → broken_reference（附引用清单）",
        ]
        limits = [
            "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
            "不改统计口径、时间范围与设备范围",
            "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        ]
        if spec is not None:
            for item in spec.limits:
                if item not in limits:
                    limits.append(item)
            limits.append(f"最小结构示例（只给格式）：{json.dumps(spec.example(), ensure_ascii=False)}")
        package_op = package.op if package else "SET_PRESENTATION"
        package_field = package.payload_field if package else "operations"
        entries.append(
            _entry(
                id=op,
                group="展示操作 / 受约束白名单",
                entryType="operation",
                status="available",
                purpose=(
                    f"受约束的展示操作 {op}（**不是独立 Tool，也不是顶层能力**）："
                    + (f"{spec.description} " if spec is not None else "")
                    + f"必须放在 {package_op} 的 params.{package_field} 里"
                    f'（形如 {{"op": "{op}", "params": {{...}}}}），'
                    "不能直接作为 submit_edit_plan.operations 的顶层项。"
                ),
                params=params,
                required=list(required),
                targetSpec=(
                    "params 里的 regionId / groupId / memberRegionIds 等引用字段"
                    "必须来自当前草稿"
                    "（get_current_draft 返回的区块 id 清单）；"
                    + (
                        "；".join(spec.references)
                        if spec is not None and spec.references
                        else ""
                    )
                ),
                resultSpec=(
                    "整批展示操作的结果：PresentationResult"
                    "（ok / changeSet / affectedRegionIds / issues）"
                ),
                states=states,
                readOnly=False,
                sideEffects=(
                    "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。"
                ),
                errors=errors,
                limits=limits,
                code={
                    "registration": _require_ref(
                        "app/services/report_presentation_service.py::ALLOWED_OPERATIONS"
                    ),
                    "dispatch": _require_ref(
                        "app/services/report_presentation_service.py::apply_presentation"
                    ),
                    "validation": _require_ref(
                        "app/services/report_presentation_service.py::normalize_presentation_operations"
                    ),
                    "execution": _require_ref(f"{service_file}::{handler.__name__}"),
                },
            )
        )
    return entries


def _rule_fast_path_entries() -> list[dict[str, Any]]:
    """规则快路 —— 明确标注「不是独立 Tool」。"""
    entries: list[dict[str, Any]] = []
    for spec in _RULE_FAST_PATHS:
        entries.append(
            _entry(
                id=spec["id"],
                group=spec["group"],
                entryType="operation",
                status="available",
                purpose=spec["purpose"],
                params={},
                required=[],
                targetSpec=spec["targetSpec"],
                resultSpec=spec["resultSpec"],
                states=["WAITING_USER_CONFIRM", "USER_EDITING_TEMPLATE"],
                readOnly=bool(spec["readOnly"]),
                sideEffects=spec["sideEffects"],
                errors=list(spec["errors"]),
                limits=list(spec["limits"]),
                code={key: _require_ref(value) for key, value in spec["code"].items()},
            )
        )
    return entries


def _optional_tool_entries() -> list[dict[str, Any]]:
    """尚未交付的可选工具 —— 标 ``not_wired``，**不暴露为可调用**。

    模块交付后 registry 会把它注册进 ``_TOOLS``，目录随即按真实模块重写，
    状态从 ``not_wired`` 变成 ``available`` —— 这就是自动同步。
    """
    from app.tools import registry

    entries: list[dict[str, Any]] = []
    for module_path in getattr(registry, "_OPTIONAL_TOOL_MODULES", ()):
        if registry.get_runner(
            (registry._OPTIONAL_TOOL_CONTRACTS.get(module_path) or {}).get("NAME", "")
        ):
            # 已注册进 _TOOLS —— 由 _tool_entries 收录，这里不重复。
            continue
        contract = registry._OPTIONAL_TOOL_CONTRACTS.get(module_path)
        if not contract:
            continue
        entries.append(
            _entry(
                id=contract["NAME"],
                group="工具 / 未接线的可选工具",
                entryType="agent_tool",
                status="not_wired",
                purpose=(
                    f"{contract['DESCRIPTION']} "
                    f"（**尚未接线**：模块 {module_path} 还没交付，"
                    "模型现在调不到它；模块到位后目录自动变成 available。）"
                ),
                params=dict(contract["PARAMETERS"]),
                required=[str(item) for item in (contract["PARAMETERS"].get("required") or [])],
                targetSpec="见参数契约。",
                resultSpec="命中时返回 id / method / conditions / capabilityVersion / score / 来源 recordId。",
                states=list(contract["APPLICABLE_STATES"]),
                readOnly=bool(contract["IS_READ_ONLY"]),
                sideEffects="只读：不改草稿、不写业务数据。",
                errors=[
                    "无匹配 → 返回空结果（正常继续，不报错）",
                    "技术错误 → 明确报错并记 tool_error",
                ],
                limits=["最多返回 3 条", "只返回 status='approved' 且未禁用的经验"],
                code={
                    "registration": _require_ref("app/tools/registry.py::_TOOLS"),
                    "dispatch": f"{module_path.replace('.', '/')}.py::run",
                    "validation": f"{module_path.replace('.', '/')}.py::PARAMETERS",
                    "execution": f"{module_path.replace('.', '/')}.py::run",
                },
            )
        )
    return entries


def _simulated_dimensions() -> list[dict[str, Any]]:
    """模拟分类维度目录（真实来源：``edit_capabilities/chart_data.py``）。"""
    from app.services.edit_capabilities.chart_data import dimension_catalog

    return dimension_catalog()


def build_catalog() -> dict[str, Any]:
    """从真实注册项构造目录数据（纯函数，不落盘）。

    返回 ``{"entries": [...], "simulatedDimensions": [...], "counts": {...},
    "contract": {...}}``。每条 entry 的字段严格按契约 §1.3。

    ``contract`` 是**共享契约**的机器可读形态：提交位置、包装要求、
    参数契约、引用规则、限制与最小示例。**它参与指纹** ——
    改了参数定义 / 限制 / 提交位置，目录版本就会变。
    """
    from app.services.edit_capabilities.contract import build_contract

    entries: list[dict[str, Any]] = []
    entries.extend(_tool_entries())
    entries.extend(_capability_entries())
    entries.extend(_legacy_intent_entries())
    entries.extend(_presentation_entries())
    entries.extend(_rule_fast_path_entries())
    entries.extend(_optional_tool_entries())

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["entryType"]] = counts.get(entry["entryType"], 0) + 1
    return {
        "entries": entries,
        "simulatedDimensions": _simulated_dimensions(),
        "counts": counts,
        "contract": build_contract().to_dict(),
    }


# --------------------------------------------------------------------------
# 版本与指纹
# --------------------------------------------------------------------------

def _canonical(payload: dict[str, Any]) -> str:
    clean = {key: value for key, value in payload.items() if key not in _VOLATILE_KEYS}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def catalog_version() -> str:
    """已加载契约的指纹（sha256 前 16 位）。

    指纹来自**已加载注册项的真实内容**：源码改了但 Python 没重载时，
    已加载对象没变 → 版本不变 → 目录不会冒充「已同步」。
    """
    return _fingerprint(build_catalog())[:16]


def catalog_payload() -> dict[str, Any]:
    """机器可读元数据：``{"version", "contractFingerprint", "generatedAt", "entries", ...}``。"""
    data = build_catalog()
    fingerprint = _fingerprint(data)
    payload: dict[str, Any] = {
        "version": fingerprint[:16],
        "contractFingerprint": fingerprint,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    payload.update(data)
    return payload


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def _section_of(entry: dict[str, Any]) -> str:
    if entry["entryType"] == "agent_tool":
        return "一、已注册 Agent Tool（模型可直接调用）"
    if entry["entryType"] == "legacy_intent":
        return "三、旧编辑意图（旧格式入口，由 TemplateEditEngine.apply 执行）"
    group = entry["group"]
    if group.startswith("清单能力"):
        return "二、清单能力（操作清单里的能力，由后端执行）"
    if group.startswith("展示操作"):
        return "四、展示操作白名单（受约束的展示操作）"
    if group.startswith("规则快路"):
        return "五、规则快路（确定性快路径）"
    return "六、其它"


_SECTION_ORDER: list[str] = [
    "一、已注册 Agent Tool（模型可直接调用）",
    "二、清单能力（操作清单里的能力，由后端执行）",
    "三、旧编辑意图（旧格式入口，由 TemplateEditEngine.apply 执行）",
    "四、展示操作白名单（受约束的展示操作）",
    "五、规则快路（确定性快路径）",
    "六、其它",
]


def _render_entry(
    entry: dict[str, Any], *, seen: dict[str, str], disambiguate: bool = False
) -> list[str]:
    lines: list[str] = []
    heading = f"### {entry['id']}"
    if disambiguate:
        # 同一个名字在目录里出现多次（如 ADD_SECTION 既是清单能力、
        # 又是旧编辑意图）—— 标题里补上分组，人一眼能分清。
        heading += f"（{entry['group']}）"
    lines.append(heading)
    lines.append("")
    lines.append(
        f"- **入口类型**：`{entry['entryType']}` —— {_ENTRY_TYPE_LABEL.get(entry['entryType'], '')}"
    )
    lines.append(f"- **分组**：{entry['group']}")
    lines.append(f"- **状态**：`{entry['status']}`")
    lines.append(f"- **用途**：{entry['purpose']}")
    schema_key = json.dumps(entry["params"], ensure_ascii=False, sort_keys=True)
    first_use = seen.get(schema_key)
    if first_use is None:
        seen[schema_key] = entry["id"]
        lines.append("- **参数**（JSON Schema，取自真实契约）：")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(entry["params"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    elif entry["params"]:
        lines.append(
            f"- **参数**：与上文「{first_use}」的参数契约完全相同"
            "（完整内容见文件末尾机器可读元数据的 `entries` 字段）"
        )
    else:
        lines.append("- **参数**：（无入参）")
    lines.append(f"- **必填参数**：{'、'.join(entry['required']) or '（无）'}")
    lines.append(f"- **目标 / 引用要求**：{entry['targetSpec']}")
    lines.append(f"- **返回值**：{entry['resultSpec']}")
    lines.append(f"- **适用节点**：{'、'.join(entry['states']) or '（全部）'}")
    lines.append(f"- **只读**：{'是' if entry['readOnly'] else '否'}")
    lines.append(f"- **副作用**：{entry['sideEffects']}")
    lines.append("- **主要错误**：")
    for item in entry["errors"] or ["（无）"]:
        lines.append(f"  - {item}")
    lines.append("- **运行限制**：")
    for item in entry["limits"] or ["（无）"]:
        lines.append(f"  - {item}")
    lines.append("- **源码定位**：")
    for key in ("registration", "dispatch", "validation", "execution"):
        lines.append(f"  - {key}：`{entry['code'].get(key, '')}`")
    lines.append("")
    return lines


def _render_document(payload: dict[str, Any], *, state: str | None) -> str:
    """渲染目录正文（不含末尾的机器可读元数据块）。"""
    entries = payload["entries"]
    if state is not None:
        entries = [
            entry
            for entry in entries
            if not entry["states"] or state in entry["states"]
        ]
    counts = payload.get("counts") or {}
    id_counts: dict[str, int] = {}
    for entry in payload["entries"]:
        id_counts[entry["id"]] = id_counts.get(entry["id"], 0) + 1
    lines: list[str] = []
    lines.append("# 报表 Agent 能力总目录")
    lines.append("")
    lines.append(
        "> **本文件是自动生成物，请勿手改。**"
        "由 `backend/app/services/capability_catalog_service.py` 从"
        "**真实注册项**生成：已注册 Tool、清单能力、旧编辑意图、"
        "展示操作白名单、规则快路。参数契约直接取注册项自己的 JSON Schema，"
        "不手抄、不另维护参数表。"
    )
    lines.append("")
    lines.append(f"- **目录版本**（contractFingerprint 派生）：`{payload['version']}`")
    lines.append(f"- **契约指纹**：`{payload['contractFingerprint']}`")
    lines.append(f"- **生成时间**：{payload['generatedAt']}")
    lines.append(
        f"- **条目总数**：{len(payload['entries'])}"
        f"（Agent Tool {counts.get('agent_tool', 0)}"
        f" / 清单能力与展示操作、规则快路 {counts.get('operation', 0)}"
        f" / 旧编辑意图 {counts.get('legacy_intent', 0)}）"
    )
    if state is not None:
        lines.append(f"- **本次筛选节点**：`{state}`（共 {len(entries)} 条适用）")
    lines.append("")
    lines.append("## 入口类型说明")
    lines.append("")
    lines.append(
        "- `agent_tool` —— **模型可以直接调用的 Tool**，已注册在 "
        "`app/tools/registry.py` 的 `_TOOLS` 里。"
    )
    lines.append(
        "- `operation` —— **操作清单里的能力**，由后端执行，"
        "**不是独立 Tool**：模型在 `submit_edit_plan` 的 `operations` 里给出 "
        "`{\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}`。"
    )
    lines.append(
        "- `legacy_intent` —— **旧格式编辑意图**，由 "
        "`template_edit_engine.TemplateEditEngine.apply` 执行，**不是独立 Tool**。"
    )
    lines.append("")
    lines.append(
        "> 目录里**名字可能重复**：`ADD_SECTION` / `DELETE_SECTION` 既是清单能力，"
        "也是旧编辑意图 —— 两者的入口形式完全不同。"
        "**唯一键是 `(entryType, id)`**，标题里带分组以便区分。"
    )
    lines.append("")

    for section in _SECTION_ORDER:
        section_entries = [entry for entry in entries if _section_of(entry) == section]
        if not section_entries:
            continue
        lines.append(f"## {section}")
        lines.append("")
        seen: dict[str, str] = {}
        for entry in section_entries:
            lines.extend(
                _render_entry(
                    entry,
                    seen=seen,
                    disambiguate=id_counts.get(entry["id"], 0) > 1,
                )
            )

    dimensions = payload.get("simulatedDimensions") or []
    if dimensions:
        lines.append("## 七、模拟分类维度目录")
        lines.append("")
        lines.append(
            "来源：`app/services/edit_capabilities/chart_data.py::SIMULATED_DIMENSIONS`。"
            "**数值是模拟的**，载荷带 `simulated` 标记，不接真实告警源。"
        )
        lines.append("")
        for dimension in dimensions:
            lines.append(
                f"- **{dimension.get('title', '')}**（`{dimension.get('dimension', '')}`）"
                f"：单位 {dimension.get('unit', '')}；口径 {dimension.get('statistic', '')}；"
                f"别名 {'、'.join(dimension.get('aliases') or [])}"
            )
            lines.append(
                "  - 分类与数值："
                + "、".join(
                    f"{item.get('label', '')}={item.get('value', '')}"
                    for item in dimension.get("categories") or []
                )
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_file_text(payload: dict[str, Any], *, state: str | None = None) -> str:
    """完整文件内容：正文 + 末尾的机器可读元数据块。"""
    body = _render_document(payload, state=state)
    metadata = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        body
        + "\n"
        + "<!-- 机器可读元数据（本文件的唯一机器可读来源；上面的正文是给人看的） -->\n"
        + "```json\n"
        + metadata
        + "\n```\n"
    )


def catalog_text(state: str | None = None) -> str:
    """人 / 模型共用的可读目录正文。

    ``state`` 为 None 给全量；给具体 state 则按 ``states`` 过滤 ——
    **筛选来自同一份目录数据**，不另写一份参数说明。
    """
    payload = catalog_payload()
    return _render_document(payload, state=state)


# --------------------------------------------------------------------------
# 落盘：原子替换 + 并发保护
# --------------------------------------------------------------------------

def _read_metadata(text: str) -> dict[str, Any] | None:
    matches = _METADATA_BLOCK_RE.findall(text)
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(text: str) -> None:
    """写临时文件 + ``os.replace`` —— 模型不会读到写了一半的目录。"""
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(CATALOG_PATH.parent),
        prefix=".capability_catalog.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, CATALOG_PATH)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _split_document(text: str) -> tuple[str, dict[str, Any] | None]:
    """把目录文件拆成「正文」和「机器可读元数据」。"""
    marker = "<!-- 机器可读元数据"
    cut = text.find(marker)
    body = text[:cut] if cut > 0 else text
    return body.rstrip() + "\n", _read_metadata(text)


def _without_volatile(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _VOLATILE_KEYS}


def ensure_catalog_fresh(*, force: bool = False) -> str:
    """核对已加载契约指纹与目录文件；不一致则重新生成并原子替换。

    返回目录文件里的 version。**生成失败抛异常**（不静默回退旧手写说明）。
    """
    with _LOCK:
        payload = catalog_payload()
        version = payload["version"]
        existing_text = ""
        existing: dict[str, Any] | None = None
        if CATALOG_PATH.exists():
            try:
                existing_text = CATALOG_PATH.read_text(encoding="utf-8")
                existing = _read_metadata(existing_text)
            except OSError:
                existing_text = ""
                existing = None
        if existing is not None and not force:
            if existing.get("contractFingerprint") == payload["contractFingerprint"]:
                # 目录与已加载契约一致 —— 不重写（文件内容保持不变）。
                return str(existing.get("version") or version)
        new_body = _render_document(payload, state=None)
        if existing is not None and existing_text:
            old_body, _ = _split_document(existing_text)
            if (
                old_body.strip() == new_body.strip()
                and _without_volatile(existing) == _without_volatile(payload)
            ):
                # 内容没变就不动文件（generatedAt 不该无意义地刷新）。
                return version
        _atomic_write(_render_file_text(payload))
        logger.info(
            "[capability_catalog] 已更新目录 version=%s entries=%s path=%s",
            version,
            len(payload["entries"]),
            CATALOG_PATH,
        )
        return version


def load_catalog_text() -> str:
    """读回目录文件正文。文件不存在或版本过期时抛异常。"""
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"能力目录文件不存在：{CATALOG_PATH}")
    text = CATALOG_PATH.read_text(encoding="utf-8")
    payload = _read_metadata(text)
    if payload is None:
        raise ValueError(f"能力目录文件缺少机器可读元数据块：{CATALOG_PATH}")
    current = catalog_version()
    if str(payload.get("version") or "") != current:
        raise ValueError(
            f"能力目录已过期（文件 {payload.get('version')} ≠ 当前 {current}）："
            f"{CATALOG_PATH}；先调 ensure_catalog_fresh()。"
        )
    marker = "<!-- 机器可读元数据"
    cut = text.find(marker)
    body = text[:cut] if cut > 0 else text
    return body.rstrip() + "\n"

__all__ = [
    "CATALOG_PATH",
    "build_catalog",
    "catalog_payload",
    "catalog_text",
    "catalog_version",
    "ensure_catalog_fresh",
    "load_catalog_text",
    "verify_source_ref",
]
