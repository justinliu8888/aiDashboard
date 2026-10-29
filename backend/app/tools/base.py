"""工具统一契约。

所有 Agent 可调用的工具集中放在 ``app/tools/`` 下，**一个工具一个文件**，
每个文件对外暴露同一组模块级常量 + 一个 ``run()``：

    NAME              工具名（中台 skill id）
    DESCRIPTION       工具描述（中台 skill 描述，直接给模型看）
    PARAMETERS        JSON Schema（入参契约）
    CATEGORY          工具分类（见 ToolCategory）
    IS_READ_ONLY      是否只读。终结工具为 False，权限语义不能沿用只读
    APPLICABLE_STATES 适用节点（AgentState 取值），空表示全节点可用
    CONCLUSION_STATES 终结工具可能产生的结论状态（只有终结工具有）
    run(**kwargs)     执行体，返回 dict

统一契约的收益：
    1. 本地注册进 AgentScope Toolkit（registry.to_agentscope_toolkit_async）；
    2. 导出成中台 skill 清单（registry.export_manifest），上线时无需改代码。

工具本身不保存业务状态。需要读「当前草稿 / 上次校验结果」的工具，
统一从 ``app.tools.context`` 的会话上下文取，避免把大对象塞进模型参数。
"""
from __future__ import annotations

from enum import Enum

from app.schemas.agent import ALLOWED_TRANSITIONS, AgentState


class ToolCategory(str, Enum):
    """工具分类。

    FACT      只读事实查询：查目录、指标、能力、当前草稿等
    BUILDER   确定性组装：按计划组装草稿 / ChangeSet，不含模型判断
    TERMINAL  终结工具：提交本轮结构化结论，调用即代表本轮决策完成
    """

    FACT = "FACT"
    BUILDER = "BUILDER"
    TERMINAL = "TERMINAL"


# 工具契约版本，上中台时带在清单里
# 1.1.0：新增 CONCLUSION_STATES（终结工具声明可产生的结论状态）
# 1.2.0：新增 SAME_STATE_CONCLUSION —— 「原地回答」型终结工具的结论标记
TOOL_CONTRACT_VERSION = "1.2.0"


#: 「原地回答」型终结工具的结论标记：结论状态 = 调用时的当前节点。
#:
#: 查询回答不推进业务，状态必须保持不变。同一节点内继续推进本来就是合法转移，
#: 所以不需要为每个节点组合维护一张转移表。
SAME_STATE_CONCLUSION = "SAME"


def is_conclusion_legal(state: AgentState, conclusion: AgentState | str) -> bool:
    """在 ``state`` 节点提交 ``conclusion`` 结论，状态转移是否合法。

    判定依据就是协议里的 ``ALLOWED_TRANSITIONS``（见 app/schemas/agent.py）：
    保持原状态算合法（同一节点内继续推进），其余必须在允许的转移集合里。
    ``SAME_STATE_CONCLUSION`` 表示「原地回答」，恒合法。
    """
    if conclusion == SAME_STATE_CONCLUSION:
        return True
    if state == conclusion:
        return True
    return conclusion in ALLOWED_TRANSITIONS.get(state, set())


# ---------- 节点分组 ----------
#
# 用来给每个工具声明 APPLICABLE_STATES。
# 目的是**按节点收窄模型能调的东西**：比如编辑草稿时不该再给"重新生成草稿"
# 这类生成类终结工具，否则模型可能把用户刚改好的草稿整个覆盖掉。
#
# 终结工具的节点白名单不是随便挑的，判定原则是：
#   **工具在某节点可用 ⇔ 它的结论状态在该节点是合法转移**
# （见 app/schemas/agent.py 的 ALLOWED_TRANSITIONS 和上面的 is_conclusion_legal）。
# 这样模型拿到的终结工具，一定能落成一个合法的协议状态。

# 生成态：用户在描述需求，还没有草稿
GENERATION_STATES = (AgentState.ANALYZING.value, AgentState.NEEDS_CLARIFICATION.value)

# 编辑态：草稿已经在手上，改的是它
EDIT_STATES = (
    AgentState.DEFINITION_DRAFT_READY.value,
    AgentState.WAITING_USER_CONFIRM.value,
    AgentState.USER_EDITING_TEMPLATE.value,
)

# 修复态：校验没过，要对着问题清单修
FIX_STATES = (AgentState.VALIDATION_FAILED.value,)

# 流水线态：草稿已定，节点 6-12 由后端流水线推进，AI 不改状态
PIPELINE_STATES = (
    AgentState.SAVE_DEFINITION_DRAFT.value,
    AgentState.VALIDATING.value,
    AgentState.PREVIEWING.value,
    AgentState.PREVIEW_READY.value,
    AgentState.RENDERING.value,
    AgentState.READY.value,
    AgentState.INTERRUPT_CONFIRMING.value,
    AgentState.BLOCKED_OR_UNSUPPORTED.value,
)

# 业务 Agent 可能被调用到的全部节点。
# 路由层放行（CONTINUE_CURRENT）后就会落到业务 Agent，节点 6-12 期间
# 用户插一句话也会走到这里，所以这些节点一个都不能漏。
BUSINESS_AGENT_STATES = (
    GENERATION_STATES
    + EDIT_STATES
    + FIX_STATES
    + PIPELINE_STATES
)

# 只读事实工具：任何业务节点都能用。
# 它们不改变状态，所以不存在"在某个节点不合法"的问题；
# 节点级收窄只针对终结工具和组装工具。
LOOKUP_STATES = BUSINESS_AGENT_STATES

# 只读查询模式下允许使用的终结工具（只有它：提交答案，不推进业务）。
# 查询链路在结构上就不开放保存 / 预览 / 应用修改等执行权限，
# 不能只靠 Prompt 声称「请不要修改」。
QUERY_TERMINAL_TOOLS = ("submit_query_answer",)

#: 只在只读查询模式下提供的工具用这个模块级标记声明（``IS_QUERY_ONLY = True``）。
#:
#: 普通业务轮的 Toolkit 里不注册它们 —— 模型在生成 / 修改 / 确认这些轮里
#: 看不到「提交只读答案」这个出口，只能用它该用的终结工具。
IS_QUERY_ONLY_ATTR = "IS_QUERY_ONLY"

# ---- 终结工具的节点白名单 ----
#
# 每个终结工具的结论状态写在它自己的文件里（CONCLUSION_STATES），
# 下面只声明"在哪些节点可以提交它"。两者的关系由 tests/test_tools.py 断言。
#
# 为什么用复数：同一个终结工具可能产生不止一个结论状态。
# 例如 submit_clarification 带 missingInfo 时是 NEEDS_CLARIFICATION，
# 不带（闲聊/引导）时停在 ANALYZING。写成元组才能把这层分叉显式化，
# 也才能对每个结论分支逐个校验合法性。

# 交草稿计划 → 结论状态 WAITING_USER_CONFIRM
DRAFT_PLAN_STATES = GENERATION_STATES

# 交修改方案 → 结论状态 DEFINITION_DRAFT_READY
CHANGE_PLAN_STATES = (
    AgentState.ANALYZING.value,
    AgentState.DEFINITION_DRAFT_READY.value,
    AgentState.WAITING_USER_CONFIRM.value,
)

# 交编辑计划 → 结论状态 USER_EDITING_TEMPLATE
EDIT_PLAN_STATES = (
    AgentState.WAITING_USER_CONFIRM.value,
    AgentState.USER_EDITING_TEMPLATE.value,
)

# 交修复方案 → 结论状态 ANALYZING
FIX_PLAN_STATES = FIX_STATES

# 交追问 / 闲聊 → 结论状态 NEEDS_CLARIFICATION
CLARIFICATION_STATES = (
    AgentState.ANALYZING.value,
    AgentState.NEEDS_CLARIFICATION.value,
    AgentState.WAITING_USER_CONFIRM.value,
)

# 交拦截结论 → 结论状态 BLOCKED_OR_UNSUPPORTED
BLOCKED_STATES = (
    AgentState.ANALYZING.value,
    AgentState.WAITING_USER_CONFIRM.value,
)
