"""AgentScopeReportAgent：业务 Agent 的 ReAct 主循环（基于 AgentScope 2.0）。

架构：
    - 单 Agent + Tools（工具统一在 app/tools/ 下）
    - LLM：通过 AgentScope 的 OpenAI 兼容模型接入（当前 provider = MiniMax，
  MiniMax-M3，思考显式关闭）；provider 由 ``app.config`` 统一配置。
    - ReAct 循环：Reason → Tool → Observation → ... → 终结工具提交结论
    - 可靠性：LLM 输出不合法 → 修复重试 1 次 → 仍失败则由上层降级 FakeAgent

节点级工具白名单：
    每次请求只把**当前节点可用**的工具注册进 Toolkit
    （registry.tools_for_state），模型看不到不该在此时调的工具。

确定性边界：
    模型只产出结论（终结工具的载荷），状态转移、草稿组装、实际修改
    都由后端代码执行，模型改不了。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from app.agents.fake_report_agent import FakeReportAgent
from app.config import get_settings
from app.schemas.agent import (
    LOCK_SCOPE_INPUT_AND_MENU,
    AgentBlock,
    AgentRequest,
    AgentResponse,
    AgentResponseDomainObjects,
    AgentError,
    AgentQuestion,
    AgentState,
    QuestionOption,
    ToolRequest,
)
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_policy import ReportPolicyDraft
from app.services.report_draft_service import (
    DefinitionBuildError,
    compose_unsupported_style_disclosure,
)
from app.services.internal_id_sanitizer import (
    sanitize_change_set_summaries,
    sanitize_internal_ids,
)
from app.services.report_log_safety import (
    log_failure,
    log_info,
    log_warning,
    model_text_shape,
    safe_identifier,
    safe_reason,
)
from app.services import routing_rules
from app.services import edit_trace_service
from app.tools.context import (
    ToolContext,
    bind_tool_context,
    clear_tool_context,
)
from app.tools.registry import (
    bind_lookup_failure,
    clear_lookup_failure,
    get_runner,
    take_lookup_failure,
    to_agentscope_toolkit_async,
    tools_for_query,
    tools_for_state,
)
from app.tools.submission import (
    bind_submission,
    clear_submission,
    take_submission,
)


logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "report_generation.md"
_EDIT_PROMPT_PATH = Path(__file__).parent / "prompts" / "template_edit.md"
_QUERY_PROMPT_PATH = Path(__file__).parent / "prompts" / "query_answer.md"


# --------------------------------------------------------------------------
# 追踪用中间件：把「这次调用是谁」固定下来
# --------------------------------------------------------------------------
#
# 为什么需要它：
#
# * AgentScope **并行**执行同一批工具调用（``asyncio.gather``）——
#   每个调用跑在**各自的任务**里，彼此看不到对方写入的状态；
# * 框架的 ``TOOL_CALL_START`` 事件与真正的执行器**不在同一个协程**：
#   事件是消费者侧读到的，执行发生在框架自己的任务里；
# * 工具函数本身**拿不到** ``tool_call_id``。
#
# 所以「这次调用是哪一次调用」只能在**工具执行的那一刻**、在**执行任务
# 自己**的上下文里固定下来，再通过不可变对象传给提交 / 试做事件。
#
# 这个中间件就是那个接缝：``on_acting`` 在框架调用工具函数**之前**触发，
# 且与工具函数跑在**同一个任务**里 —— ContextVar 的写入对工具可见。


class _TraceCallMiddleware:
    """在**工具执行的那一刻**绑定这次调用的归属。

    只做观察：绑定一个不可变的 ``CallContext``，不改工具参数、
    不改返回值、不改执行顺序。

    **不继承框架的 ``MiddlewareBase``**：框架只用到两个成员
    （``is_implemented`` 与 ``on_acting``），鸭子类型即可 ——
    这样本模块不必 import 框架的中间件包（那条 import 链在本环境里
    会连带拉起可选依赖，不值得为一次观察点冒险）。
    """

    def is_implemented(self, hook_name: str) -> bool:
        """只实现 ``on_acting`` 这一个钩子。"""
        return hook_name == "on_acting"

    async def on_acting(self, agent, input_kwargs, next_handler):
        """把这次调用的 reply / 轮次 / 调用 id 固定成任务局部上下文。"""
        tool_call = (input_kwargs or {}).get("tool_call")
        call_id = str(getattr(tool_call, "id", "") or "")
        tool_name = str(getattr(tool_call, "name", "") or "")
        iteration = AgentScopeReportAgent._iteration_of(agent)
        reply_index = 0
        trace = edit_trace_service.current_trace()
        if trace is not None:
            try:
                reply_index = int(trace.get("replyIndex", 0) or 0)
            except (TypeError, ValueError):
                reply_index = 0
        edit_trace_service.bind_call_context(
            edit_trace_service.CallContext(
                reply_index=reply_index,
                iteration=iteration,
                tool_call_id=call_id,
                tool_name=tool_name,
            )
        )
        try:
            async for item in next_handler():
                yield item
        finally:
            # 这一次调用结束 → 清掉任务局部的归属。
            # 不清的话，同一任务里后续的调用会带着上一次的 id。
            edit_trace_service.clear_call_context()



#: 句子里出现「样式/版式/标题/柱状图…」这类词时，才去纠正它的「已生效」说法。
_STYLE_SUBJECT_RE = re.compile(
    r"(样式|版式|布局|标题|柱状图|条形图|等宽|独占[一1]行|字号|对齐|颜色)"
)

#: 声称「已经照做」的动词。
_APPLIED_VERB_RE = re.compile(
    r"已(?:经)?(?:作为|以|按)?[^，。；、\n]{0,16}?(?:一并)?(?:提交|应用|落实|生效)"
)

#: 纠正后的措辞：明确说「没生效」，而不是「已提交」。
_APPLIED_CLAIM_REPLACEMENT = "当前模板不支持，未生效"

#: 模型「随时可以改成柱状图」这类承诺 —— 图形类型在模板层是写死的 LINE，
#: 承诺能改就是承诺了一件做不到的事，必须一起纠正。
_CHART_KIND_OFFER_RE = re.compile(
    r"(?:如需|如果需要|若需要|需要)?(?:把|将|再)?(?:趋势图|图表|折线图)?"
    r"(?:改成|换成|改为|调成|变更为|变成)[^，。；、\n]{0,8}"
    r"(?:柱状图|条形图)[^，。；、\n]{0,12}"
    r"(?:随时告诉我|告诉我|跟我说|说一声|我来改|就可以改)?"
)


def _neutralize_applied_style_claims(reply: str) -> str:
    """纠正模型「已按要求提交/应用了版式样式」的说法。

    模型在版式诉求没落实时会说「已作为样式要求一并提交」——
    这句话会被读成「已经做到了」，正是要修的偏离。
    这里把它换成「当前模板不支持，未生效」。

    只改**同时**含「样式/版式类词」和「已生效类动词」的句子，
    不会误伤「已生成 A 机房昨日能耗日报草稿」这类如实描述。
    """
    text = reply or ""
    if not text:
        return text

    def _fix_sentence(sentence: str) -> str:
        if not _STYLE_SUBJECT_RE.search(sentence):
            return sentence
        sentence = _APPLIED_VERB_RE.sub(_APPLIED_CLAIM_REPLACEMENT, sentence)
        return _CHART_KIND_OFFER_RE.sub(_APPLIED_CLAIM_REPLACEMENT, sentence)

    # 按句号/问号/感叹号/换行切句，保留分隔符。
    parts = re.split(r"([。！？\n])", text)
    fixed: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            fixed.append(_fix_sentence(part))
        else:
            fixed.append(part)
    return "".join(fixed)


def _trial_failure_detail() -> str:
    """试做失败的**具体**内容（哪一步、哪条约束、为什么）。

    没有失败时返回空串。失败原文取自本轮追踪里**最后一次**试做失败 ——
    这是真实发生过的事，不是推断。没有原文就什么都不加（不编原因）。
    """
    try:
        failure = edit_trace_service.last_trial_failure()
    except Exception:  # noqa: BLE001 — 诊断信息不该让整轮失败
        return ""
    if not failure:
        return ""
    detail = str(failure.get("message") or "").strip()
    if not detail:
        constraint = str(failure.get("constraint") or "").strip()
        reason = str(failure.get("reason") or "").strip()
        detail = constraint or reason
    if not detail:
        return ""
    op = str(failure.get("op") or "").strip()
    prefix = f"具体原因（{op}）：" if op else "具体原因："
    return f"{prefix}{detail}"


class AgentScopeReportAgent:
    """AgentScope ReAct Agent + 真实模型（provider 由配置决定），覆盖样例 1。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._fallback = FakeReportAgent()
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        self._edit_prompt = _EDIT_PROMPT_PATH.read_text(encoding="utf-8")
        self._query_prompt = _QUERY_PROMPT_PATH.read_text(encoding="utf-8")
        self._model, self._init_err = self._init_model()

    # ---------- 初始化 ----------

    def _init_model(self):
        """构造 AgentScope 的 OpenAI 兼容模型（当前 provider = MiniMax）。

        Agent 实例在每次 handle 时新建（AgentScope Agent 有对话内部状态，不适合跨请求复用）。

        ``err`` 只回**原因类别**，不回异常原文：它会被写进日志，
        而初始化异常里可能带着带凭据的 base_url。
        """
        settings = self._settings
        if not settings.active_api_key:
            return None, f"{settings.provider_name} API Key 未配置"
        try:
            from agentscope.credential import OpenAICredential
            from agentscope.formatter import OpenAIChatFormatter
            from agentscope.model import OpenAIChatModel

            credential = OpenAICredential(
                api_key=settings.active_api_key,
                # base_url 是 **API 根地址**，SDK 自己拼 /chat/completions。
                base_url=settings.active_base_url,
            )
            model = OpenAIChatModel(
                credential=credential,
                model=settings.active_model,
                parameters=OpenAIChatModel.Parameters(
                    temperature=settings.llm_temperature,
                ),
                stream=False,
                formatter=OpenAIChatFormatter(),
                # **每次请求都带上**（AgentScope 2.0 的 extra_body 逐请求下发）：
                # MiniMax 默认把思考内联进 content，会污染 JSON 解析；
                # 显式关思考，拿到的是纯 JSON 正文。
                extra_body=settings.request_extra_body,
            )
            return model, None
        except Exception as exc:  # noqa: BLE001
            log_failure(logger, "AgentScope 模型初始化失败", exc)
            return None, f"模型初始化失败（{safe_reason(exc)}）"

    async def _build_agent(self, request: AgentRequest):
        """为单次 handle 创建一个全新的 AgentScope Agent（含 Toolkit）。

        预算按当前节点给：编辑场景查证步数少，生成场景多给几步。
        工具也按当前节点收窄：模型看不到这个节点不该调的工具。

        只读查询轮换一套工具：只注册事实工具和查询答案终结工具，
        执行类工具结构上就调不到。

        **能力说明来自总目录**（契约 0003 §1）：编辑态的说明 =
        ``template_edit.md``（只保留流程）+ **当前版本**的能力总目录。
        目录在**每次构造 Agent 时**核对并按需重新生成 ——
        不在 ``__init__`` 缓存，所以改了能力之后下一次构造就能拿到新版本。
        """
        from agentscope.agent import Agent

        query_mode = bool(getattr(request, "queryMode", False))
        toolkit = await to_agentscope_toolkit_async(
            tools_for_query() if query_mode else tools_for_state(request.state)
        )
        if query_mode:
            system_prompt = self._query_prompt
        elif request.state == AgentState.USER_EDITING_TEMPLATE:
            system_prompt = self._edit_system_prompt()
        else:
            system_prompt = self._system_prompt
        return Agent(
            name="ReportDraftAgent",
            system_prompt=system_prompt,
            model=self._model,
            toolkit=toolkit,
            react_config=self._build_react_config(request.state),
            # 追踪接缝：在**工具执行的那一刻**把这次调用的归属固定下来。
            # 只观察，不改行为。
            middlewares=[_TraceCallMiddleware()],
        )

    def _edit_system_prompt(self) -> str:
        """编辑态的说明：流程规则 + **当前版本**的能力总目录。

        目录生成失败 → **明确报错**（抛出去），不静默回退到旧手写说明：
        回退会让模型按一份已经不认识的参数去调工具。
        """
        from app.services.capability_catalog_service import (
            catalog_text,
            ensure_catalog_fresh,
        )

        # 核对已加载契约与目录文件；不一致就重新生成（原子替换）。
        ensure_catalog_fresh()
        body = catalog_text(AgentState.USER_EDITING_TEMPLATE.value)
        return f"{self._edit_prompt}\n\n{body}"

    def _build_react_config(self, state: AgentState):
        """按节点构造 ReAct 预算。

        AgentScope 默认 max_iters=50，报表场景根本用不到；这里按节点收紧，
        避免模型在错误的分支上空转。

        **编辑态只有一个预算**（``react_max_iters_edit``，默认 8 圈）：
        新的编辑要求与同一事项的后续调整用同一个上限，不再因为
        「会话里还没有 editItem」退回 4 圈 —— 那正是首次请求在提交前
        就耗尽预算的原因。
        """
        from agentscope.agent import ReActConfig

        settings = self._settings
        if state == AgentState.USER_EDITING_TEMPLATE:
            max_iters = settings.react_max_iters_edit
        elif state == AgentState.VALIDATION_FAILED:
            max_iters = settings.react_max_iters_fix
        else:
            max_iters = settings.react_max_iters

        return ReActConfig(
            max_iters=max_iters,
            structured_output_grace_iters=settings.react_structured_output_grace_iters,
        )

    @staticmethod
    def _grace_iters_config(settings) -> int:
        """框架结构化输出宽限轮数。

        这是框架为了把结构化输出落地而给的**额外**圈数，与编辑预算
        **分开计数** —— 它不是「第二次完整催交」，也不是预算的一部分。
        """
        try:
            return int(
                getattr(settings, "react_structured_output_grace_iters", 0) or 0
            )
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _budget_source(settings) -> str:
        """本轮预算**实际**来自哪个环境变量（用于日志如实说明）。"""
        import os

        if os.getenv("REACT_MAX_ITERS_EDIT"):
            return "REACT_MAX_ITERS_EDIT"
        if os.getenv("REACT_MAX_ITERS_EDIT_CAPABILITY"):
            return "REACT_MAX_ITERS_EDIT_CAPABILITY（旧名，兼容回退）"
        return "默认值 8（环境变量未配置）"

    # ---------- 入口 ----------

    def _degraded_response(self, request: AgentRequest) -> AgentResponse:
        """降级响应。

        查询轮降级到 Fake 的查询路径（同一套事实工具、同一套事实口径）；
        编辑态降级到**按真实原因分类**的失败；其余降级到 FakeAgent。
        """
        if getattr(request, "queryMode", False):
            return self._fallback.handle(request)
        if request.state == AgentState.USER_EDITING_TEMPLATE:
            return self._unavailable_edit_response(
                request, edit_trace_service.ERR_MODEL_UNAVAILABLE
            )
        return self._fallback.handle(request)

    def handle(self, request: AgentRequest) -> AgentResponse:
        """同步入口：兼容老调用方（会新建 loop，可能遇到连接池失效问题）。"""
        if self._model is None:
            # 模型没接上：**明确失败**，不降级 FakeAgent。
            # 降级下去用户拿到的是一条规则拼出来的回复，看着像正常结果，
            # 实际上模型根本没跑 —— 配置漏了却看不出来。
            log_warning(
                logger,
                "AgentScope 未就绪（%s），本轮明确失败",
                self._init_err,
            )
            return self._model_unavailable_response(request)
        self._start_request_trace(request)
        try:
            plan, reason = asyncio.run(self._run_agent(request))
        except Exception as exc:  # noqa: BLE001
            log_failure(logger, "AgentScope 运行异常，降级 FakeAgent", exc)
            edit_trace_service.record(
                edit_trace_service.STAGE_ERROR,
                name="agent_exception",
                errorCategory=self._classify_exception(exc),
                status="error",
                extra={"errorType": type(exc).__name__},
            )
            return self._degraded_response(request)
        finally:
            pass
        return self._attach_trace_debug(self._dispatch(request, plan, reason))

    async def ahandle(self, request: AgentRequest) -> AgentResponse:
        """async 入口：复用 FastAPI 的事件循环，避免 httpx 连接池跨 loop 失效。"""
        if self._model is None:
            # 同 handle：模型没接上是**明确失败**，不是降级。
            log_warning(
                logger,
                "AgentScope 未就绪（%s），本轮明确失败",
                self._init_err,
            )
            return self._model_unavailable_response(request)
        self._start_request_trace(request)
        try:
            plan, reason = await self._run_agent(request)
        except Exception as exc:  # noqa: BLE001
            log_failure(logger, "AgentScope 运行异常，降级 FakeAgent", exc)
            edit_trace_service.record(
                edit_trace_service.STAGE_ERROR,
                name="agent_exception",
                errorCategory=self._classify_exception(exc),
                status="error",
                extra={"errorType": type(exc).__name__},
            )
            return self._degraded_response(request)
        return self._attach_trace_debug(self._dispatch(request, plan, reason))

    @staticmethod
    def _attach_trace_debug(response: AgentResponse) -> AgentResponse:
        """给响应挂上本轮追踪标识与摘要。

        **成功轮与失败轮都挂**：排查「这次到底跑了几圈、调了哪些工具」
        时，成功与失败要能在同一处对照。
        """
        summary = edit_trace_service.trace_summary()
        trace_id = str(summary.get("traceId") or "")
        if not trace_id:
            return response
        debug = dict(response.debug or {})
        debug.setdefault("traceId", trace_id)
        debug.setdefault("traceSummary", summary)
        response.debug = debug
        return response

    def _start_request_trace(self, request: AgentRequest) -> None:
        """开一条**请求作用域**的追踪（成功轮与失败轮都记）。

        目录版本取**本轮固定**的值：构造 Agent 时用的是哪个版本，
        这里就记哪个版本 —— 便于把日志与当时人打开的目录对上。
        """
        catalog_version = ""
        try:
            from app.services.capability_catalog_service import catalog_version as _version

            catalog_version = _version()
        except Exception:  # noqa: BLE001 — 取不到就留空，不影响主流程
            catalog_version = ""
        edit_trace_service.ensure_trace(
            session_id=request.sessionId or request.conversationId or "",
            draft_id=(
                request.context.reportDefinitionId
                or request.context.subjectId
                or ""
            ),
            catalog_version=catalog_version,
            # 本轮请求的消息标识：迟到的事件靠它判断「是不是本请求的」。
            message_id=str(request.messageId or ""),
        )

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        """把异常分成「模型接口」与「其它」两类。

        只有**确实**是模型接口不可用/超时才算那两类；其余一律归
        ``unknown``（原因尚未确定），不假装已经查明。
        """
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "timeout" in name or "timeout" in text or "timed out" in text:
            return edit_trace_service.ERR_MODEL_TIMEOUT
        if any(
            token in name or token in text
            for token in ("authentication", "apikey", "unauthorized", "permission")
        ):
            return edit_trace_service.ERR_MODEL_UNAVAILABLE
        if any(
            token in name or token in text
            for token in ("connection", "apiconnection", "serviceunavailable", "internalserver")
        ):
            return edit_trace_service.ERR_MODEL_UNAVAILABLE
        return edit_trace_service.ERR_UNKNOWN

    @staticmethod
    def _model_unavailable_response(request: AgentRequest) -> AgentResponse:
        """模型不可用：如实说失败，不拿规则拼一条像模像样的回复。

        与 ``_query_failure_response`` / ``_unavailable_edit_response`` 同口径：
        带可辨识的失败码，前端能把它渲染成失败而不是正常答复。
        """
        message = "模型服务当前不可用，本轮没有生成结果。请稍后重试。"
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state,
            reply=message,
            designSessionId=request.context.designSessionId,
            error=AgentError(code="MODEL_UNAVAILABLE", message=message),
            debug={"modelOutcome": "model_unavailable"},
        )

    def _dispatch(
        self,
        request: AgentRequest,
        plan: dict[str, Any] | None,
        reason: str = "",
    ) -> AgentResponse:
        """把本轮结论组装成 AgentResponse。

        结论有两个来源，优先级：终结工具提交 > 旧文本 JSON 解析。
        终结工具是结构化的、经过 Pydantic 校验的，比自由文本可靠。
        """
        if plan is None:
            # 静默降级要留一条可查的原因（#61）：只写原因类别，不写模型原文。
            log_info(
                logger,
                "AgentScope 输出不合法，降级 FakeAgent reason=%s",
                reason or "未给出原因",
            )
            if getattr(request, "queryMode", False):
                return self._query_failure_response(request)
            if request.state == AgentState.USER_EDITING_TEMPLATE:
                return self._unavailable_edit_response(request, reason)
            return self._fallback.handle(request)

        submission = plan.get("submission")
        query_mode = bool(getattr(request, "queryMode", False))
        if query_mode:
            # 只读查询轮只接受查询答案提交：编辑类终结工具在这里不合法。
            if (
                not isinstance(submission, dict)
                or submission.get("tool") != "submit_query_answer"
            ):
                log_warning(logger, "查询态拒绝非查询答案终结工具")
                return self._query_failure_response(request)
        elif request.state == AgentState.USER_EDITING_TEMPLATE and (
            not isinstance(submission, dict)
            or submission.get("tool") not in {"submit_edit_answer", "submit_edit_plan"}
        ):
            log_warning(logger, "编辑态拒绝非编辑终结工具或旧式文本计划")
            return self._unavailable_edit_response(
                request, edit_trace_service.ERR_PARAMS_INVALID
            )
        elif isinstance(submission, dict) and submission.get("tool") == "submit_query_answer":
            # 普通业务轮不接受只读答案提交：这个出口只在查询模式注册。
            log_warning(logger, "非查询轮拒绝只读答案提交")
            return self._fallback.handle(request)
        if submission:
            response = self._dispatch_submission(request, submission)
            if response is not None:
                return response

        mode = plan.get("mode")
        if mode == "FALLBACK":
            return self._unavailable_edit_response(request) if request.state == AgentState.USER_EDITING_TEMPLATE else self._fallback.handle(request)
        if mode == "GENERATE":
            if query_mode:
                # 查询轮不接受生成结论：结构上也没有组装工具。
                return self._query_failure_response(request)
            return self._assemble_generate_response(request, plan)
        if mode == "CHITCHAT":
            return self._assemble_chitchat_response(request, plan)

        log_warning(
            logger,
            "AgentScope 返回未知 mode（不在 GENERATE/FALLBACK/CHITCHAT 内），降级 mode=%s",
            safe_identifier(mode),
        )
        return self._unavailable_edit_response(request) if request.state == AgentState.USER_EDITING_TEMPLATE else self._fallback.handle(request)

    @staticmethod
    def _query_failure_response(request: AgentRequest) -> AgentResponse:
        """查询失败：如实说失败，不降级成要求补充生成条件的引导。"""
        message = (
            "这次查询没有完成，暂时无法确认当前对话的情况。"
            "草稿和待确认操作都没有变化，可以稍后再问一次。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state,
            reply=message,
            designSessionId=request.context.designSessionId,
            error=AgentError(code="QUERY_UNAVAILABLE", message=message),
            debug={"queryMode": True, "queryOutcome": "technical_failure"},
        )

    @staticmethod
    def _unavailable_edit_response(
        request: AgentRequest, reason: str = ""
    ) -> AgentResponse:
        """编辑态失败：**按真实原因分类**，不再一律说「技术问题」。

        失败阶段 / 已知原因 / 修改是否生效 / 可用的下一步 —— 四件事都
        写清楚。通用兜底只用于「原因尚未确定」。
        """
        category = str(reason or "") or edit_trace_service.ERR_UNKNOWN
        # 认不出来的类别一律按「原因尚未确定」表述 —— 不把裸码抛给用户。
        known = edit_trace_service.ERROR_MESSAGES.get(
            category
        ) or edit_trace_service.ERROR_MESSAGES[edit_trace_service.ERR_UNKNOWN]
        if category == edit_trace_service.ERR_UNKNOWN:
            # 通用兜底：**逐字保留**既有文案。
            #
            # 这一句前端与既有测试都认（``EDIT_VERIFICATION_UNAVAILABLE``
            # 的失败渲染），改了就是破坏既有行为。分类信息放在
            # ``debug.errorCategory`` / ``debug.failureReason`` 里 ——
            # 「原因尚未确定」写在那里（契约 §2.4：摘要里写明原因尚未确定）。
            message = "本轮查证遇到技术问题，草稿没有修改。请稍后重试。"
        # 预算耗尽要说清「未形成可执行计划」，不能暗示接口或数据查询坏了。
        elif category == edit_trace_service.ERR_BUDGET_EXHAUSTED:
            message = (
                "本次编辑没有完成：本轮推理用完了预算，仍然没有提交可执行的修改计划。"
                "草稿和待确认操作都没有变化。"
                "可以再说一次要改什么，或者把要求说得更具体一点。"
            )
        elif category == edit_trace_service.ERR_MODEL_UNAVAILABLE:
            message = (
                "本次编辑没有完成：模型接口当前不可用，本轮没有生成修改计划。"
                "草稿和待确认操作都没有变化。请稍后重试。"
            )
        elif category == edit_trace_service.ERR_MODEL_TIMEOUT:
            message = (
                "本次编辑没有完成：模型请求超时，本轮没有生成修改计划。"
                "草稿和待确认操作都没有变化。请稍后重试。"
            )
        elif category == edit_trace_service.ERR_TOOL_ERROR:
            message = (
                "本次编辑没有完成：查询工具执行报错，没有形成可执行的修改计划。"
                "草稿和待确认操作都没有变化。可以稍后重试。"
            )
        elif category == edit_trace_service.ERR_TOOL_LOOKUP_FAILED:
            message = (
                "本次编辑没有完成：没有查到要修改的目标，或该能力当前不支持，"
                "因此没有形成可执行的修改计划。草稿和待确认操作都没有变化。"
                "可以换一种说法，或直接说明要改哪张卡片。"
            )
        elif category == edit_trace_service.ERR_PARAMS_INVALID:
            message = (
                "本次编辑没有完成：提交的修改参数不合法，没有形成可执行的修改计划。"
                + _trial_failure_detail()
                + "草稿和待确认操作都没有变化。请把要求说得再具体一点。"
            )
        elif category == edit_trace_service.ERR_TRIAL_FAILED:
            message = (
                "本次编辑没有完成：修改计划在预执行校验时没有通过，原草稿保持不变。"
                + _trial_failure_detail()
                + "可以调整要求后重试。"
            )
        elif category == edit_trace_service.ERR_ACCEPT_FAILED:
            message = (
                "本次编辑没有完成：接受修改时正式提交失败（草稿版本或权限已变化），"
                "草稿没有被修改。请重新核对目标后再试。"
            )
        elif category == edit_trace_service.ERR_NOT_SUBMITTED:
            message = (
                "本次编辑没有完成：本轮结束，但没有提交可执行的修改计划。"
                "草稿和待确认操作都没有变化。可以把要求说得更具体一点。"
            )
        else:
            message = (
                "本次编辑没有完成：原因尚未确定，草稿和待确认操作都没有变化。"
                "请稍后重试。"
            )
        # 把这一轮的**失败类别**写进追踪：摘要里要写明「原因尚未确定」
        # 或具体原因（契约 §2.4 / §2.5）。只做一次，没有 trace 就跳过。
        if edit_trace_service.current_trace() is not None:
            edit_trace_service.record(
                edit_trace_service.STAGE_ERROR,
                name="edit_failed",
                replyIndex=1,
                params={},
                result={"submitted": False, "applied": False},
                status="error",
                errorCategory=category,
            )
        summary = edit_trace_service.trace_summary()
        debug: dict[str, Any] = {
            # ``technical_failure`` 是**既有**的失败标记（前端与既有测试都认），
            # 分类信息另放在 ``errorCategory`` / ``failureReason`` 里。
            "editOutcome": "technical_failure",
            "errorCategory": category,
            "failureReason": known,
            "traceId": summary.get("traceId", ""),
            "traceSummary": summary,
        }
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=message,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=request.snapshots.reportPolicy,
                reportDefinitionDraft=request.snapshots.reportDefinition,
            ),
            error=AgentError(
                code=AgentScopeReportAgent._edit_error_code(category),
                message=message,
            ),
            debug=debug,
        )

    @staticmethod
    def _edit_error_code(category: str) -> str:
        """失败类别 → 协议错误码。

        ``EDIT_VERIFICATION_UNAVAILABLE`` 保留为「原因尚未确定」的兜底 ——
        前端已有按这个码渲染失败的逻辑，不能破坏。
        """
        return {
            edit_trace_service.ERR_BUDGET_EXHAUSTED: "EDIT_BUDGET_EXHAUSTED",
            edit_trace_service.ERR_MODEL_UNAVAILABLE: "EDIT_MODEL_UNAVAILABLE",
            edit_trace_service.ERR_MODEL_TIMEOUT: "EDIT_MODEL_TIMEOUT",
            edit_trace_service.ERR_MODEL_ERROR: "EDIT_MODEL_UNAVAILABLE",
            edit_trace_service.ERR_TOOL_ERROR: "EDIT_TOOL_FAILED",
            edit_trace_service.ERR_TOOL_LOOKUP_FAILED: "EDIT_TOOL_FAILED",
            edit_trace_service.ERR_PARAMS_INVALID: "EDIT_PARAMS_INVALID",
            edit_trace_service.ERR_TRIAL_FAILED: "EDIT_TRIAL_FAILED",
            edit_trace_service.ERR_ACCEPT_FAILED: "EDIT_ACCEPT_FAILED",
            edit_trace_service.ERR_NOT_SUBMITTED: "EDIT_VERIFICATION_UNAVAILABLE",
        }.get(category, "EDIT_VERIFICATION_UNAVAILABLE")

    def _dispatch_submission(
        self, request: AgentRequest, submission: dict[str, Any]
    ) -> AgentResponse | None:
        """把终结工具的提交转成对应终点的响应。

        返回 None 表示"这个提交我处理不了"，交给调用方回落到旧路径。
        """
        tool = submission.get("tool")
        payload = submission.get("payload") or {}

        if tool == "submit_draft_plan":
            return self._assemble_generate_response(request, payload)
        if tool == "submit_clarification":
            return self._assemble_clarification_response(request, payload)
        if tool == "submit_blocked":
            return self._assemble_blocked_response(request, payload)
        if tool == "submit_change_plan":
            return self._assemble_change_response(request, payload)
        if tool == "submit_edit_answer" and request.state in {
            AgentState.USER_EDITING_TEMPLATE, AgentState.WAITING_USER_CONFIRM,
        }:
            return self._assemble_edit_answer(request, payload)
        if tool == "submit_query_answer" and getattr(request, "queryMode", False):
            return self._assemble_query_answer(request, payload)
        if tool == "submit_edit_plan":
            return self._assemble_edit_response(request, payload)
        if tool == "submit_fix_plan":
            return self._assemble_fix_response(request, payload)

        log_warning(
            logger,
            "未知终结工具（不在已知工具名内），忽略提交 tool=%s",
            safe_identifier(tool),
        )
        return None

    # ---------- Agent Loop（交给 AgentScope 的 ReAct 内核） ----------

    async def _run_agent(
        self, request: AgentRequest
    ) -> tuple[dict[str, Any] | None, str]:
        """跑一轮 ReAct，返回 ``(plan, reason)``。

        ``plan`` 为 None 时，``reason`` 说明**为什么**没拿到可用的结论 ——
        降级到 FakeAgent 时要能在日志里查得到原因（#61）。
        """
        from agentscope.message import Msg, TextBlock

        agent = await self._build_agent(request)

        # 绑定本轮工具上下文：读草稿 / 读区块 / 读校验结果的工具从这里取状态，
        # 避免把大对象塞进模型参数。
        bind_tool_context(self._build_tool_context(request))
        bind_submission()
        bind_lookup_failure()
        edit_state = request.state in {
            AgentState.USER_EDITING_TEMPLATE,
            AgentState.WAITING_USER_CONFIRM,
        }
        # 预算在**启动 ReAct 前**定下来并记录：实际值 + 来源 + 理由。
        budget = self._build_react_config(request.state).max_iters
        grace_iters = self._grace_iters_config(self._settings)
        edit_trace_service.set_budget(budget)
        edit_trace_service.record(
            edit_trace_service.STAGE_AGENT_REPLY,
            name="react_budget",
            replyIndex=1,
            params={
                "maxIters": budget,
                "source": self._budget_source(self._settings),
                "reason": "新旧编辑统一预算，不依赖是否已有修改事项",
                # 框架结构化输出宽限：独立于编辑预算另计，**不是**一次额外催交。
                "structuredOutputGraceIters": grace_iters,
            },
            result={"configured": True},
        )
        log_info(
            logger,
            "[agent] 本轮编辑预算 maxIters=%d source=%s reason=新旧编辑统一预算 "
            "graceIters=%d（框架结构化输出宽限，单独计数）",
            budget,
            self._budget_source(self._settings),
            grace_iters,
        )
        # 草稿版本：这一轮是在**哪个版本**上做的（没有草稿就记「未取得」）。
        definition = request.snapshots.reportDefinition
        draft_revision = (
            int((definition.metadata or {}).get("revision") or 0)
            if definition is not None
            else None
        )
        edit_trace_service.record(
            edit_trace_service.STAGE_AGENT_REPLY,
            name="draft_snapshot",
            replyIndex=1,
            result={
                "draftId": request.context.reportDefinitionId or "",
                "draftRevision": draft_revision,
            },
        )
        try:
            user_msg = Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(type="text", text=self._compose_user_message(request))
                ],
            )

            # 第一次调用：由 AgentScope 跑完整 ReAct 循环（Reason → Tool → Observation → ...）
            reply_msg, reply_info = await self._reply_with_trace(
                agent, user_msg, reply_index=1
            )
            # **真实**迭代数与停止原因：来自框架状态，不是 HTTP 次数。
            iterations = self._real_iterations(agent)
            finished_reason = reply_info.get("finishedReason") or self._finished_reason(
                reply_msg
            )
            edit_trace_service.record(
                edit_trace_service.STAGE_AGENT_REPLY,
                name="react_reply",
                replyIndex=1,
                iteration=iterations,
                params={},
                result={
                    "finishedReason": finished_reason,
                    "replyLength": len(_extract_text(reply_msg)),
                },
                # ``completed`` 不等于「没有提交」：按**真实**提交事实说。
                stopReason=self._stop_reason_of(
                    finished_reason,
                    submitted=edit_trace_service.has_accepted_submission(),
                ),
                status="ok",
            )
            # 框架结构化输出宽限：超出 max_iters 的部分**单独**记录。
            #
            # 这是框架为了让结构化输出落地而给的额外圈数，**不是**一次
            # 「第二次完整催交」—— 名字与口径都不能那样叫。
            grace_used = None
            if iterations is not None:
                grace_used = max(0, iterations - budget)
            edit_trace_service.record(
                edit_trace_service.STAGE_AGENT_REPLY,
                name="react_grace_iters",
                replyIndex=1,
                iteration=iterations,
                params={
                    "maxIters": budget,
                    "graceItersConfigured": grace_iters,
                    "graceItersUsed": grace_used,
                    "note": "框架结构化输出宽限，单独计数，不是一次额外的完整推理轮次",
                },
                result={
                    "iterations": iterations,
                    "exceededMaxIters": bool(grace_used),
                },
            )
            if getattr(request, "queryMode", False) and take_lookup_failure():
                log_warning(logger, "查询态事实工具查证失败，不接受本轮答案")
                return None, "事实工具查证失败"
            if edit_state and take_lookup_failure():
                # 编辑态：事实工具查证失败**不废掉**本轮候选。
                #
                # 事实工具是帮模型**定位**目标卡片用的，不是「准不准加这个
                # 指标」的准入闸门 —— 目录里没有的指标要走目录外通路，不是
                # 拒绝的理由（需求：不查、不索引、不校验真实数据源或预定义
                # 字段，来决定用户能不能加指标）。
                #
                # 目标认错了怎么办？用户面前还有那张**待确认卡片**：它写清楚
                # 改哪张卡、怎么展示，用户点头才落草稿。查证失败时更该把
                # 候选交给用户看，而不是让整轮变成「技术问题」把人堵死。
                log_warning(logger, "编辑态事实工具查证失败，候选仍交用户确认")

            # 终结工具优先：模型调用了 submit_* 就以它的提交为准
            plan = self._plan_from_submission()
            if plan is not None:
                return plan, ""

            plan = self._parse_plan(_extract_text(reply_msg))
            if plan is not None:
                return plan, ""

            # 编辑态：**不再**跑第二轮完整 ReAct 催交（需求第 5 节）。
            #
            # 循环内的错误反馈（参数不合法 / 引用错 / 试做失败）本来就在
            # 同一个 Agent 的同一个循环里发生 —— 那条路径没有动。
            # 循环**结束**仍没有有效提交时，如实记录停止原因并结束：
            # 不把预算耗尽、未提交、参数非法混成同一个技术错误。
            if edit_state:
                first_reply = _extract_text(reply_msg)
                log_warning(
                    logger,
                    "AgentScope 编辑态未提交终结工具 state=%s iterations=%s "
                    "finishedReason=%s replyLength=%d replyShape=%s",
                    request.state.value,
                    iterations,
                    finished_reason,
                    len(first_reply),
                    model_text_shape(first_reply),
                )
                category = self._classify_no_submission(
                    finished_reason=finished_reason,
                    iterations=iterations,
                    budget=budget,
                    tool_category=edit_trace_service.latest_error_category(),
                )
                edit_trace_service.record(
                    edit_trace_service.STAGE_ERROR,
                    name="react_no_submission",
                    replyIndex=1,
                    iteration=iterations,
                    params={},
                    result={"submitted": False, "tried": False},
                    status="error",
                    errorCategory=category,
                    stopReason=self._stop_reason_of(finished_reason),
                )
                return None, category

            # 其它状态（查询等）：保留既有的修复重试策略，不顺带删。
            #
            # 修复重试 1 次（Q5）
            # 模型返回原文不进日志：只留长度与形状级摘要（#61）。
            first_reply = _extract_text(reply_msg)
            log_warning(
                logger,
                "AgentScope 未提交终结工具 state=%s replyLength=%d replyShape=%s",
                request.state.value,
                len(first_reply),
                model_text_shape(first_reply),
            )
            log_info(logger, "AgentScope 首轮输出不合法，走修复重试")
            repair_msg = Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(
                        type="text",
                        text=self._compose_repair_hint(request),
                    )
                ],
            )
            reply_msg2, reply_info2 = await self._reply_with_trace(
                agent, repair_msg, reply_index=2
            )
            if getattr(request, "queryMode", False) and take_lookup_failure():
                log_warning(logger, "查询态修复轮事实工具查证失败")
                return None, "修复轮事实工具查证失败"
            plan = self._plan_from_submission()
            if plan is not None:
                return plan, ""
            repair_reply = _extract_text(reply_msg2)
            log_warning(
                logger,
                "AgentScope 修复轮未提交终结工具 state=%s replyLength=%d replyShape=%s",
                request.state.value,
                len(repair_reply),
                model_text_shape(repair_reply),
            )
            plan = self._parse_plan(repair_reply)
            if plan is None:
                return None, "两轮都没有可解析的终结提交"
            return plan, ""
        finally:
            clear_tool_context()
            clear_submission()
            clear_lookup_failure()

    async def _reply_with_trace(
        self,
        agent,
        message,
        *,
        reply_index: int,
    ) -> tuple[Any, dict[str, Any]]:
        """跑一轮 ReAct，并把**逐圈**的工具事件写进追踪。

        优先用 ``reply_stream``：它给出真实的 ``tool_call_id``、工具名与
        工具结果状态 —— 这是「逐圈追踪」里最有定位价值的三样东西。
        Agent 没有流式接口时（老替身 / 旧框架）退回 ``reply()``：
        迭代编号仍取**框架真实计数**，``toolCallId`` 记为未取得（空串）。

        返回 ``(最终消息, 本轮信息)``；本轮信息里有停止原因与工具调用统计。
        """
        from agentscope.message import Msg

        # 本轮是第几次 reply：工具侧事件靠它继承 replyIndex。
        edit_trace_service.set_reply_index(reply_index)

        stream = getattr(agent, "reply_stream", None)
        if stream is None:
            reply_msg = await agent.reply(message)
            error = getattr(reply_msg, "error", None)
            return reply_msg, {
                "finishedReason": self._finished_reason(reply_msg),
                "errorType": str(getattr(error, "type", "") or "") if error else "",
                "toolCalls": 0,
                "toolErrors": 0,
            }

        final_msg: Any = None
        finished_reason = ""
        error_type = ""
        tool_calls = 0
        tool_errors = 0
        pending: dict[str, dict[str, Any]] = {}
        # 工具的**真实返回**（TOOL_RESULT_TEXT_DELTA）：后台结果的来源。
        tool_payloads: dict[str, list[str]] = {}
        # 模型的**公开**文本（TEXT_BLOCK_DELTA）：思考摘要的**唯一**来源。
        #
        # 隐藏的 THINKING_BLOCK_* 是模型的内部思维链 —— 这里**不订阅、
        # 不读取、不转发**。要写「思考摘要」只能用公开文本。
        public_texts: dict[str, list[str]] = {}
        public_recorded: set[str] = set()

        async for chunk in stream(message, yield_final_msg=True):
            # 框架只会产出「事件」或「最终消息」两类；事件一定有 ``type``。
            # 没有 ``type`` 的就当最终消息（老替身返回的是普通对象）。
            if isinstance(chunk, Msg) or not hasattr(chunk, "type"):
                final_msg = chunk
                continue
            kind = str(getattr(chunk, "type", "") or "")
            if kind == "TEXT_BLOCK_DELTA":
                # 公开文本：模型说给人听的那部分。思考摘要只认这里。
                delta = str(getattr(chunk, "delta", "") or "")
                if delta:
                    iteration = self._iteration_of(agent)
                    public_texts.setdefault(
                        _round_bucket(iteration), []
                    ).append(delta)
                    # 工具自己记的事件靠它归到正确的轮次。
                    edit_trace_service.set_current_iteration(iteration)
            elif kind == "TEXT_BLOCK_END":
                self._record_public_text(
                    agent,
                    public_texts,
                    public_recorded,
                    reply_index=reply_index,
                    bucket=_round_bucket(self._iteration_of(agent)),
                    text=str(getattr(chunk, "text", "") or ""),
                )
            elif kind == "TOOL_CALL_START":
                call_id = str(getattr(chunk, "tool_call_id", "") or "")
                iteration = self._iteration_of(agent)
                pending[call_id] = {
                    "name": str(getattr(chunk, "tool_call_name", "") or ""),
                    "args": "",
                    "startedAt": time.perf_counter(),
                    # 归属在**开始时固定**：结束时沿用它，不再重新读
                    # 「当前轮」猜 —— 并行调用时那个值可能是别的调用的。
                    "iteration": iteration,
                }
                # 工具侧事件（提交 / 试做 / 候选）自己不知道第几轮 ——
                # 把**真实**轮次写进追踪上下文，它们靠它归轮。
                edit_trace_service.set_current_iteration(iteration)
                # 「调用开始了」也记一条：这一轮能在工具返回**之前**
                # 就出现在界面上（不等最后一刻才给结果）。
                edit_trace_service.record(
                    edit_trace_service.STAGE_TOOL_CALL,
                    name=str(getattr(chunk, "tool_call_name", "") or ""),
                    toolCallId=call_id,
                    iteration=iteration,
                    replyIndex=reply_index,
                    params={},
                    result={},
                    status="ok",
                    # 开始阶段还没有业务结论：如实标「未取得」。
                    businessOk=None,
                )
            elif kind == "TOOL_CALL_DELTA":
                call_id = str(getattr(chunk, "tool_call_id", "") or "")
                entry = pending.setdefault(
                    call_id,
                    {"name": "", "args": "", "startedAt": time.perf_counter()},
                )
                entry["args"] += str(getattr(chunk, "delta", "") or "")
            elif kind == "TOOL_RESULT_TEXT_DELTA":
                # 工具的**真实返回文本**：后台结果的第一来源。
                call_id = str(getattr(chunk, "tool_call_id", "") or "")
                delta = str(getattr(chunk, "delta", "") or "")
                if delta:
                    tool_payloads.setdefault(call_id, []).append(delta)
            elif kind == "TOOL_RESULT_END":
                call_id = str(getattr(chunk, "tool_call_id", "") or "")
                entry = pending.pop(
                    call_id,
                    {"name": "", "args": "", "startedAt": None},
                )
                state = str(getattr(chunk, "state", "") or "")
                failed = state == "error"
                tool_calls += 1
                if failed:
                    tool_errors += 1
                params, params_parsed = _parse_tool_args(entry.get("args") or "")
                started = entry.get("startedAt")
                duration = (
                    int((time.perf_counter() - started) * 1000)
                    if isinstance(started, float)
                    else 0
                )
                # 轮次沿用**开始时固定**的那个 —— 不在这里重新读
                # 「当前轮」：并行调用时它可能已经被别的调用带偏。
                iteration = entry.get("iteration")
                if not isinstance(iteration, int):
                    iteration = self._iteration_of(agent)
                # 业务结果：只认工具**真实返回**的载荷。
                payload = _parse_tool_payload(
                    "".join(tool_payloads.pop(call_id, []))
                )
                (
                    business_ok,
                    business_message,
                    business_reason,
                    business_issues,
                    business_detail,
                    phase,
                    draft_changed,
                ) = _business_from_payload(entry.get("name"), payload)
                edit_trace_service.record(
                    edit_trace_service.STAGE_TOOL_CALL,
                    name=entry.get("name") or "",
                    toolCallId=call_id,
                    iteration=iteration,
                    replyIndex=reply_index,
                    params=params,
                    result={
                        "state": state,
                        "paramsParsed": params_parsed,
                        # 工具的真实返回：后台结果的来源。
                        "business": payload,
                    },
                    status="error" if failed else "ok",
                    # 工具**执行**报错（框架报的工具结果状态）就是
                    # ``tool_error``；提交参数不合法 / 试做失败是**工具自己**
                    # 报的类别，优先级更高，不会被这里盖掉。
                    errorCategory=(
                        edit_trace_service.ERR_TOOL_ERROR if failed else ""
                    ),
                    durationMs=duration,
                    businessOk=business_ok,
                    businessMessage=business_message,
                    businessReason=business_reason,
                    businessIssues=business_issues,
                    businessDetail=business_detail,
                    phase=phase,
                    draftChanged=draft_changed,
                    # 提交形状指纹：判「又提交了同一份没改好的东西」。
                    submitShape=_submit_shape(entry.get("name") or "", params),
                )
            elif kind == "REPLY_END":
                finished_reason = str(getattr(chunk, "finished_reason", "") or "")
                error = getattr(chunk, "error", None)
                if error is not None:
                    error_type = str(getattr(error, "type", "") or "")

        # 收尾：把没赶上 TEXT_BLOCK_END 的公开文本也记下来。
        self._record_public_text(
            agent,
            public_texts,
            public_recorded,
            reply_index=reply_index,
        )

        if final_msg is None:
            log_warning(
                logger,
                "AgentScope 流式回复没有产出最终消息 replyIndex=%d",
                reply_index,
            )
        return final_msg, {
            "finishedReason": finished_reason,
            "errorType": error_type,
            "toolCalls": tool_calls,
            "toolErrors": tool_errors,
        }

    @staticmethod
    def _record_public_text(
        agent,
        public_texts: dict[str, list[str]],
        public_recorded: set[str],
        *,
        reply_index: int,
        bucket: str | None = None,
        text: str = "",
    ) -> None:
        """把这一轮模型的**公开**文本记进追踪（思考摘要的唯一来源）。

        隐藏的 ``THINKING_BLOCK_*`` **不订阅、不读取、不转发** ——
        这里只处理 ``TEXT_BLOCK_*``。
        """
        targets = [bucket] if bucket is not None else list(public_texts.keys())
        for key in targets:
            if not key or key in public_recorded:
                continue
            content = text or "".join(public_texts.get(key) or [])
            if not str(content or "").strip():
                continue
            iteration = (
                int(key) if key.isdigit() else AgentScopeReportAgent._iteration_of(agent)
            )
            public_recorded.add(key)
            edit_trace_service.record(
                edit_trace_service.STAGE_AGENT_REPLY,
                name=edit_trace_service.EVENT_PUBLIC_TEXT,
                iteration=iteration,
                replyIndex=reply_index,
                params={"text": content},
                result={"chars": len(content)},
            )
            # 记完就把这一段清掉：避免同一段文本被重复记。
            public_texts.pop(key, None)

    @staticmethod
    def _iteration_of(agent) -> int | None:
        """**当前正在跑的这一轮**是第几轮（1 起）。

        框架在**一轮结束后**才把 ``cur_iter`` 加一，所以事件到达那一刻
        ``cur_iter`` 是「已完成的轮数」；当前轮 = ``cur_iter + 1``。
        取不到就返回 ``None``（「未取得」），不伪造。
        """
        value = AgentScopeReportAgent._real_iterations(agent)
        if value is None:
            return None
        return value + 1

    @staticmethod
    def _real_iterations(agent) -> int | None:
        """本轮**真实**的 ReAct 迭代数。

        来自框架状态（``agent.state.cur_iter``）—— 不是 HTTP 请求次数，
        也不是猜的。取不到就返回 ``None``（「未取得」），不伪造。
        """
        try:
            value = getattr(getattr(agent, "state", None), "cur_iter", None)
            if isinstance(value, int) and value >= 0:
                return value
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _finished_reason(reply_msg) -> str:
        """框架给出的停止原因（``completed`` / ``exceed_max_iters`` / …）。"""
        try:
            reason = getattr(reply_msg, "finished_reason", None)
            if reason is None:
                return ""
            return str(getattr(reason, "value", reason))
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _stop_reason_of(finished_reason: str, *, submitted: bool = False) -> str:
        """把框架停止原因翻译成我们自己的停止原因文案。

        ``completed`` **不等于**「没有提交」：到底提交没提交，看**真实的
        提交事实**（追踪里有没有真的提交成功），不靠猜。
        """
        if finished_reason == "exceed_max_iters":
            return "预算耗尽：达到迭代上限仍未提交终结工具"
        if finished_reason == "completed":
            return (
                "循环正常结束：已提交终结工具"
                if submitted
                else "循环结束但没有提交终结工具"
            )
        if finished_reason == "error":
            return "本轮以错误结束"
        if finished_reason == "interrupted":
            return "本轮被中断"
        return ""

    @staticmethod
    def _classify_no_submission(
        *,
        finished_reason: str,
        iterations: int | None,
        budget: int,
        tool_category: str = "",
    ) -> str:
        """编辑态「没有有效提交」时的**失败分类**。

        预算耗尽 / 参数非法 / 试做失败 / 工具报错 / 未提交 —— **不混用**。

        ``tool_category`` 是工具侧已经报出来的**具体**原因（试做失败、
        参数不合法、工具报错、查不到目标）。有它就以它为准：那是模型
        这一轮真正卡住的地方，比「预算用完了」更能指引下一步。
        预算耗尽与模型侧原因仍然记在 ``stopReason`` 里，不丢。
        """
        if tool_category:
            return tool_category
        if finished_reason == "exceed_max_iters":
            return edit_trace_service.ERR_BUDGET_EXHAUSTED
        if finished_reason == "error":
            return edit_trace_service.ERR_MODEL_ERROR
        if finished_reason == "interrupted":
            return edit_trace_service.ERR_NOT_SUBMITTED
        # 没到上限就结束 —— 可能没提交，也可能输出不合法。
        # 「参数不合法」由提交工具自己报（submit_edit_plan 返回 error），
        # 走到这里说明**根本没调用**提交工具 → 归为「未提交」。
        if iterations is not None and budget > 0 and iterations >= budget:
            return edit_trace_service.ERR_BUDGET_EXHAUSTED
        return edit_trace_service.ERR_NOT_SUBMITTED

    @staticmethod
    def _build_tool_context(request: AgentRequest) -> ToolContext:
        """把请求里的快照整理成工具可见的只读上下文。"""
        ctx = request.context
        snapshots = request.snapshots
        return ToolContext(
            state=request.state,
            subject_ref=ctx.subjectRef,
            subject_id=ctx.subjectId,
            project_id=ctx.projectId,
            selected_region_id=ctx.selectedRegionId,
            base_definition_revision=ctx.baseDefinitionRevision,
            definition=snapshots.reportDefinition,
            policy=snapshots.reportPolicy,
            last_change_set=snapshots.lastChangeSet,
            last_validation_result=(
                snapshots.lastValidationResult.model_dump(mode="json")
                if snapshots.lastValidationResult
                else None
            ),
            last_report_run=(
                snapshots.lastReportRun.model_dump(mode="json")
                if snapshots.lastReportRun
                else None
            ),
            # 用户**这一轮**的原话：提交工具用它兜底写入修改事项的
            # ``originalRequest``。模型不一定传这个参数，但服务端本来
            # 就知道用户说了什么 —— 不能让归档记录丢掉最关键的那句话。
            extra={"userMessage": str(request.message or "")},
        )

    @staticmethod
    def _compose_repair_hint(request: AgentRequest) -> str:
        """修复重试提示：只列当前节点真的能调的终结工具。

        列一堆调不了的工具，模型会去尝试然后失败，反而浪费预算。
        """
        from app.tools.base import ToolCategory

        names = [
            tool.NAME
            for tool in tools_for_state(request.state)
            if tool.CATEGORY == ToolCategory.TERMINAL
        ]
        if not names:
            return (
                "你还没有提交本轮结论。\n"
                "当前节点没有可用的终结工具，请直接用一句话说明情况。"
            )
        hint = (
            "你还没有提交本轮结论。\n"
            f"请调用一个终结工具来提交（{' / '.join(names)}），"
            "不要直接输出文字、JSON 或 markdown 围栏。"
        )
        if "submit_clarification" in names:
            hint += "\n确实没有可提交的工具时，调用 submit_clarification 并把 missingInfo 留空。"
        return hint

    @staticmethod
    def _plan_from_submission() -> dict[str, Any] | None:
        """终结工具的提交 → 内部 plan 结构。

        submission 原样带出去，由 ``_dispatch_submission`` 按工具名分发。
        """
        submission = take_submission()
        if submission is None:
            return None
        return {"submission": submission}

    # ---------- 输入 / 输出解析 ----------

    @staticmethod
    def _compose_user_message(request: AgentRequest) -> str:
        """组装本轮输入。

        除了原始输入，还要给模型三样"它自己看不到但需要知道"的东西：
            - 当前草稿摘要：改报表时没有它，模型根本不知道有哪些区块
            - 路由层已收集的信息：避免重复追问用户已经说过的事
            - 用户选中的区块：编辑语境下的目标
        """
        ctx = request.context
        lines = [
            f"用户输入：{request.message}",
            f"当前 state：{request.state.value}",
            f"subjectRef：{ctx.subjectRef}",
            f"subjectId：{ctx.subjectId}",
            f"baseDefinitionRevision：{ctx.baseDefinitionRevision}",
        ]

        if ctx.selectedRegionId:
            lines.append(f"用户选中的区块：{ctx.selectedRegionId}")
        visible_ids = getattr(ctx, "visibleRegionIds", None)
        if visible_ids:
            lines.append(f"客户可见正文卡片 ID 顺序：{', '.join(visible_ids)}")

        collected = getattr(request, "collectedInfo", None)
        if collected:
            lines.append("已收集的信息：")
            for key, value in collected.items():
                lines.append(f"  - {key}：{value}")

        draft_summary = AgentScopeReportAgent._summarize_draft(request)
        if draft_summary:
            lines.append("当前草稿：")
            lines.append(draft_summary)

        return "\n".join(lines)

    @staticmethod
    def _summarize_draft(request: AgentRequest) -> str:
        """当前草稿的概览（不是完整 JSON）。

        完整草稿通过 `get_current_draft` / `get_current_region` 工具按需取，
        这里只给一份能定位区块的最小摘要。
        """
        definition = request.snapshots.reportDefinition
        if definition is None:
            return ""

        regions = definition.regions or []
        lines = [
            f"  报表名：{(definition.metadata or {}).get('name', '')}",
            f"  区块数：{len(regions)}",
        ]
        for region in regions:
            component = region.get("component") or {}
            metric = component.get("metric") or {}
            lines.append(
                f"  - {region.get('id', '')}｜{region.get('name', '')}"
                f"｜{component.get('type', '')}"
                f"｜指标 {metric.get('id', '')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_plan(raw: str) -> dict[str, Any] | None:
        text = (raw or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or "mode" not in obj:
            return None
        if obj["mode"] not in {"GENERATE", "FALLBACK", "CHITCHAT"}:
            return None
        return obj

    # ---------- 组装协议响应 ----------

    def _assemble_generate_response(
        self, request: AgentRequest, plan: dict[str, Any]
    ) -> AgentResponse:
        runner = get_runner("build_definition_draft")
        assert runner is not None

        # 未生效的版式/样式诉求：**确定性**识别，不依赖模型怎么表达。
        # 三处来源统一核对：原始输入（用户原话）+ 路由槽位（collectedInfo）+
        # 最终定义（模型写进 styleHints 被装配层拒掉的，在组装层合并）。
        # 只靠模型把诉求写进 styleHints 的老路径，覆盖不全：模型可能把诉求
        # 写进 collectedInfo 自由文本、或者只写在回复散文里。
        unsupported_requests = routing_rules.collect_unsupported_style_requests(
            message=request.message,
            collected_info=getattr(request, "collectedInfo", None),
        )

        try:
            built = runner(
                reportName=plan.get("reportName") or "能耗日报",
                roomId=plan.get("roomId") or request.context.subjectId,
                includeTrend=bool(plan.get("includeTrend", True)),
                # 路由层解析出来的结构化条件必须原样进入组装层，
                # 否则用户要的时间窗 / 数据源 / 过滤会在这一步被默认值覆盖。
                sourceRef=plan.get("sourceRef"),
                grainUnit=plan.get("grainUnit"),
                timeWindow=plan.get("timeWindow"),
                metricRefs=plan.get("metricRefs"),
                componentRequests=plan.get("componentRequests"),
                excludedRegions=plan.get("excludedRegions"),
                filters=plan.get("filters"),
                limit=plan.get("limit"),
                topN=plan.get("topN"),
                derivedColumns=plan.get("derivedColumns"),
                styleHints=plan.get("styleHints"),
                unsupportedStyleRequests=unsupported_requests,
                capabilityEvidence=plan.get("capabilityEvidence"),
            )
            policy = ReportPolicyDraft.model_validate(built["policyDraft"])
            definition = ReportDefinitionDraft.model_validate(built["definitionDraft"])
        except DefinitionBuildError as exc:
            # 条件不被目录 / 指标 / 授权范围支持：明确阻断，
            # 不能退回默认草稿 —— 那会让用户拿到一份统计的是别的东西的报表。
            # 阻断说明里带着**载荷字段值**（指标名 / 数据源名，可能含用户数据）
            # → **不进日志**（#61 AC#1）；只留错误码与原因类型 ——
            # 与 template_preview_flow 里同一个 `DefinitionBuildError` 的写法一致。
            log_warning(
                logger,
                "草稿组装被阻断 code=%s errorType=%s",
                exc.code,
                type(exc).__name__,
            )
            return self._blocked_generate_response(request, exc)
        except Exception as exc:  # noqa: BLE001
            log_failure(logger, "草稿组装或校验失败", exc)
            return self._fallback.handle(request)

        reply = plan.get("reply") or f"已生成 {policy.name} 草稿，请确认。"
        # 组装层汇总的「没生效诉求」要在这里说清楚：
        # 用户提了「柱状图 / 左右等宽 / 标题22pt」这类当前模板做不到的要求，
        # 报表照常出，但要明确告诉用户哪几项**没有生效**，
        # 不能用「已提交」「已一并处理」这类说法替代结果。
        unsupported = (definition.metadata or {}).get("unsupportedStyleRequests") or []
        if unsupported:
            disclosure = compose_unsupported_style_disclosure(unsupported)
            if disclosure:
                reply = f"{disclosure}\n{_neutralize_applied_style_claims(reply)}"
        session_id = request.context.designSessionId or f"session_{uuid.uuid4().hex[:8]}"

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.WAITING_USER_CONFIRM,
            reply=reply,
            designSessionId=session_id,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=policy,
                reportDefinitionDraft=definition,
            ),
            questions=[
                AgentQuestion(
                    id="confirm_draft",
                    prompt="请对当前草稿做出选择：",
                    expects="SINGLE_CHOICE",
                    options=[
                        QuestionOption(value="同意", label="同意"),
                        QuestionOption(value="修改", label="修改"),
                        QuestionOption(value="重新开始", label="重新开始"),
                    ],
                )
            ],
            toolRequests=[
                ToolRequest(
                    id="req_save_1",
                    type="SAVE_DEFINITION_DRAFT",
                    target="JavaReportService",
                )
            ],
            uiActions=[
                "preview_framework",
                "confirm_framework",
                "reject_framework",
                "request_modification",
            ],
        )

    def _blocked_generate_response(
        self, request: AgentRequest, error: DefinitionBuildError
    ) -> AgentResponse:
        """组装层拒绝出草稿时的阻断响应。

        越权范围 / 未知指标 / 粒度或数据源不支持 —— 这些必须明确告诉用户，
        而不是默默换一份默认草稿。
        """
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.BLOCKED_OR_UNSUPPORTED,
            reply=error.message,
            designSessionId=request.context.designSessionId,
            blocks=[
                AgentBlock(
                    code=error.code,
                    message=error.message,
                    alternatives=error.alternatives,
                )
            ],
        )

    def _assemble_chitchat_response(
        self, request: AgentRequest, plan: dict[str, Any]
    ) -> AgentResponse:
        """闲聊 / 引导类回复：停在 ANALYZING，只给 reply + 候选引导语。"""
        reply = plan.get("reply") or "你好～我是云枢的智能报表助手，有什么可以帮你的？"
        suggestions = plan.get("suggestions") or [
            "生成 A 机房昨日能耗日报",
            "帮我做一张本月各机房耗电排名",
        ]

        questions: list[AgentQuestion] = []
        if suggestions:
            questions.append(
                AgentQuestion(
                    id="chitchat_suggestions",
                    prompt="你可以试试这些：",
                    expects="SINGLE_CHOICE",
                    options=[
                        QuestionOption(value=s, label=s) for s in suggestions[:4]
                    ],
                )
            )

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=reply,
            designSessionId=request.context.designSessionId,
            questions=questions,
            uiActions=["suggest_prompt"],
        )

    # ---------- 终结工具 → 协议响应 ----------

    def _assemble_clarification_response(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """节点 2：缺信息追问。

        ``missingInfo`` 为空 → 这是闲聊/引导类提交，按 chitchat 处理。
        """
        missing = payload.get("missingInfo") or []
        if not missing:
            return self._assemble_chitchat_response(
                request, {"reply": payload.get("reply") or ""}
            )

        questions: list[AgentQuestion] = []
        for index, field in enumerate(missing):
            options = field.get("options") or []
            questions.append(
                AgentQuestion(
                    id=field.get("field") or f"clarify_{index}",
                    prompt=field.get("label") or payload.get("nextQuestion") or "请补充信息：",
                    expects=field.get("expects") or "TEXT",
                    options=[
                        QuestionOption(value=o.get("value", ""), label=o.get("label", ""))
                        for o in options
                    ],
                )
            )

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.NEEDS_CLARIFICATION,
            reply=payload.get("reply")
            or payload.get("nextQuestion")
            or "还需要确认一些信息才能继续。",
            designSessionId=request.context.designSessionId,
            questions=questions,
            uiActions=["clarify_framework"],
        )

    def _assemble_blocked_response(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """节点 3：能力不支持 / 权限越界 / 无法处理。"""
        message = payload.get("message") or "当前无法处理该诉求。"
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.BLOCKED_OR_UNSUPPORTED,
            reply=payload.get("reply") or message,
            designSessionId=request.context.designSessionId,
            blocks=[
                AgentBlock(
                    code=payload.get("code") or "CAPABILITY_NOT_SUPPORTED",
                    message=message,
                    alternatives=payload.get("alternatives") or [],
                )
            ],
            uiActions=["blocked_framework"],
        )

    def _assemble_change_response(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """节点 4：修改方案。

        这里只**转述**模型给的改动，不执行 —— 真正的草稿变更由
        ChangeSet → 确定性引擎负责。
        """
        changes = payload.get("changes") or []

        if not changes:
            return self._assemble_clarification_response(
                request,
                {
                    "reply": payload.get("reply") or "还需要确认改哪里、怎么改。",
                    "missingInfo": [
                        {
                            "field": "edit_target",
                            "label": "想改哪一块？",
                            "expects": "TEXT",
                        }
                    ],
                },
            )

        lines = []
        for change in changes:
            target = change.get("regionId") or "草稿"
            note = change.get("summary") or change.get("path") or ""
            lines.append(f"- {target}：{note}")

        # 清单和模型自己的话都要给用户看：清单说明改了什么，reply 说明怎么想的
        detail = "已整理出修改方案，请确认：\n" + "\n".join(lines)
        reply = payload.get("reply")
        reply = f"{reply}\n{detail}" if reply else detail

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.DEFINITION_DRAFT_READY,
            reply=reply,
            designSessionId=request.context.designSessionId,
            questions=[
                AgentQuestion(
                    id="confirm_change",
                    prompt="请对修改方案做出选择：",
                    expects="SINGLE_CHOICE",
                    options=[
                        QuestionOption(value="同意", label="同意"),
                        QuestionOption(value="修改", label="修改"),
                        QuestionOption(value="重新开始", label="重新开始"),
                    ],
                )
            ],
            uiActions=[
                "preview_framework",
                "confirm_framework",
                "reject_framework",
                "request_modification",
            ],
        )

    @staticmethod
    def _edit_domain_objects(request: AgentRequest) -> AgentResponseDomainObjects:
        return AgentResponseDomainObjects(
            reportPolicyDraft=request.snapshots.reportPolicy,
            reportDefinitionDraft=request.snapshots.reportDefinition,
        )

    def _assemble_edit_answer(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """只回答经工具查证的问题，不提交 EditPlan 或 ChangeSet。"""
        from app.tools.schemas import EditAnswerPayload

        try:
            answer = EditAnswerPayload.model_validate(payload)
        except Exception:
            return self._unavailable_edit_response(request)
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=answer.reply,
            designSessionId=request.context.designSessionId,
            domainObjects=self._edit_domain_objects(request),
            debug={
                "editOutcome": "answer",
                "verifiedFacts": answer.facts,
                "limitations": answer.limitations,
                "suggestions": answer.suggestions,
            },
        )

    def _assemble_query_answer(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """只读查询的答案：状态不动，不带任何执行动作。

        状态与业务对象的归一化由 ``AgentService._normalize_query_answer`` 兜底，
        这里只保证「模型交上来的东西本身是合法答案」。
        """
        from app.tools.schemas import QueryAnswerPayload

        try:
            answer = QueryAnswerPayload.model_validate(payload)
        except Exception:
            return self._query_failure_response(request)
        reply = answer.reply
        if answer.needsClarification and answer.clarificationQuestion:
            reply = f"{reply}\n{answer.clarificationQuestion}"
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state,
            reply=reply,
            designSessionId=request.context.designSessionId,
            debug={
                "queryMode": True,
                "queryOutcome": "clarification" if answer.needsClarification else "answer",
                "verifiedFacts": answer.facts,
                "limitations": answer.limitations,
                "suggestions": answer.suggestions,
                "needsClarification": answer.needsClarification,
            },
        )

    def _assemble_capability_candidate(
        self, request: AgentRequest, plan: Any
    ) -> AgentResponse:
        """能力格式计划的候选。

        这条路上的计划**已经整批试做通过**了（试做在 ``submit_edit_plan``
        工具里做，失败就不写提交槽）。所以这里不再重复试做，只把
        **试做核验出来的事实**变成一份待确认候选：

        * 确认文字**只依据实际差异**（``editFacts``），不抄模型「打算做什么」；
        * 摘要、图表类型、分类口径、位置说明都来自事实；
        * 接受前**不渲染任何候选图表**（只有文字确认）。
        """
        from app.services.edit_trial_service import summarize_facts

        facts = dict(plan.editFacts or {})
        summary = str(facts.get("summary") or "").strip()
        if not summary:
            summary = summarize_facts(facts) if facts else str(plan.summary or "")
        summary = sanitize_internal_ids(summary)
        definition = request.snapshots.reportDefinition
        revision = (
            int((definition.metadata or {}).get("revision") or 0)
            if definition is not None
            else int(facts.get("baseRevision") or 0)
        )
        primary = str(facts.get("primaryTargetRegionId") or plan.target.regionId or "")
        operation_steps = [step.model_dump(mode="json") for step in plan.operations]
        candidate = {
            "planId": f"plan_{uuid.uuid4().hex}",
            "draftRevision": revision,
            "targetRegionId": primary,
            "operations": operation_steps,
            "summary": summary,
            "editPlan": plan.model_dump(mode="json"),
            "editFacts": facts,
            #: 这份候选**已经过整批试做**：接受时直接整批生效，不再重新规划。
            "checked": True,
        }
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=f"待确认的修改：\n{summary}\n接受后才会修改草稿。",
            designSessionId=request.context.designSessionId,
            domainObjects=self._edit_domain_objects(request),
            questions=[AgentQuestion(
                id="pending_edit_plan_confirm",
                prompt="确认这次修改：",
                expects="SINGLE_CHOICE",
                selectionOnly=True,
                # 「其他」：用户在**这张卡片里**写的自由文本 = 对当前修改
                # 事项的**纠正 / 补充**（比如「两张都使用饼图」），不是另一条
                # 无关消息 —— 服务端会把它关联回当前事项并重建候选。
                allowsOther=True,
                otherLabel="其他",
                otherPlaceholder="也可以直接在这里写要怎么改",
                options=[
                    QuestionOption(value=value, label=value)
                    for value in ("接受修改", "继续调整", "取消")
                ],
            )],
            uiActions=["pending_edit_plan"],
            debug={
                "pendingEditPlan": candidate,
                "editPlanSource": "react_capability",
                "editOutcome": "candidate",
            },
        )

    def _assemble_edit_response(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """只组装候选或具体追问；接受之前不调用编辑引擎。"""
        from app.schemas.edit_plan import EditIntent
        from app.tools.schemas import EditPlanPayload

        try:
            plan = EditPlanPayload.model_validate(payload)
        except Exception:
            return self._unavailable_edit_response(request)
        # 能力格式：已经整批试做通过，直接按**实际差异**组候选。
        if getattr(plan, "is_capability_plan", False):
            return self._assemble_capability_candidate(request, plan)
        definition = request.snapshots.reportDefinition
        intent = plan.intent.value
        region_id = plan.target.regionId
        options = [option.model_dump(mode="json") for option in plan.options]
        # 目标区域要**递归**找：组里的子卡在 ``definition.regions`` 顶层
        # 找不到（只有顶层那层是平铺的）。以前只查顶层 → 组内子卡的
        # 目标被判成"没有找到要修改的卡片"（BUG-007）。
        region = None
        if definition is not None and region_id:
            from app.services.report_presentation_service import find_region

            region = find_region(definition, region_id)

        def ask(question: str, choices: list[dict] | None = None) -> AgentResponse:
            selected = [
                QuestionOption.model_validate(item) for item in (choices or [])
            ]
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply=question,
                designSessionId=request.context.designSessionId,
                domainObjects=self._edit_domain_objects(request),
                questions=[AgentQuestion(
                    id="template_edit_clarification",
                    prompt=question,
                    expects="SINGLE_CHOICE" if selected else "TEXT",
                    options=selected,
                    selectionOnly=bool(selected),
                    # **待决澄清期间锁住普通输入**（契约：状态白名单）。
                    #
                    # 以前这张卡没有 lockScope：没有选项时按 menu_only 推导，
                    # 普通输入就**开着** —— 用户在澄清还没处理时就能发新消息
                    # 逃离当前步骤（BUG-009）。
                    #
                    # 现在显式写 input_and_menu：普通输入、发送、Enter 与
                    # 快捷入口全程锁住；要说的话走这张卡里的**专用输入**
                    # （allowsOther），只接本步的补充说明。
                    lockScope=LOCK_SCOPE_INPUT_AND_MENU,
                    allowsOther=not selected,
                    otherLabel="补充说明",
                    otherPlaceholder="也可以直接在这里补充这一步需要的信息",
                )],
                uiActions=["ask_template_edit_detail"],
                debug={
                    "editOutcome": "clarification",
                    "editingTargetRegionId": region_id or None,
                    "editingIntent": intent,
                },
            )

        if plan.needsClarification or intent == EditIntent.UNKNOWN.value:
            if any(word in request.message for word in ("好看", "展示", "图表", "样式", "视觉")):
                options = self._display_options_for(request, region_id) or options
            return ask(plan.clarificationQuestion or plan.reply or "这次修改具体想调整哪一项？", options)
        if definition is None:
            return ask("当前没有报表草稿；请先生成或加载草稿。")
        if intent == EditIntent.CHANGE_TIME_RANGE.value and "昨天下午" in request.message:
            from app.services.edit_time_resolver import resolve_time_expression

            resolved_time = resolve_time_expression(request.message)
            if resolved_time.status != "RESOLVED":
                return ask(
                    resolved_time.question or "“昨天下午”具体是几点到几点？请提供起止时间。",
                    [{"value": item.value, "label": item.label} for item in resolved_time.options],
                )
        if "第" in request.message and any(
            word in request.message for word in ("个", "张", "块", "幅")
        ):
            from app.services.edit_reference_resolver import resolve_visible_reference

            visible_ids = getattr(request.context, "visibleRegionIds", None)
            if not visible_ids:
                return ask("请确认当前页面可见正文卡片的顺序和目标卡片名称。")
            resolved = resolve_visible_reference(
                request.message, definition.regions,
                visible_region_ids=visible_ids,
                selected_region_id=request.context.selectedRegionId,
            )
            if resolved.status != "RESOLVED" or resolved.staleRegionIds:
                return ask(resolved.question or "可见卡片顺序已变化，请重新确认目标。")
            if resolved.regionId != region_id:
                return ask(f"第 {resolved.visibleIndex} 个正文卡片是「{resolved.label}」，请确认要修改这一张。")
        if intent == EditIntent.SUGGEST_VISUALIZATION.value:
            return ask(
                plan.reply or "请选一种适合该卡片的展示方式：",
                self._display_options_for(request, region_id),
            )
        if intent != EditIntent.RENAME_REPORT.value and region is None:
            return ask("在当前草稿中没有找到要修改的卡片，请选择目标。")
        if intent in {EditIntent.RENAME_REPORT.value, EditIntent.RENAME_SECTION.value}:
            title = plan.operation.newTitle or ""
            if not title or title not in request.message:
                return ask("请明确新标题；当前候选名称并未出现在你的原话中。")
        steps = plan.operations or [plan]
        for step in steps:
            action = step.intent.value
            operation = step.operation
            missing = {
                EditIntent.CHANGE_DATA_SOURCE.value: ("sourceRef", operation.sourceRef),
                EditIntent.CHANGE_COMPILATION_GRAIN.value: (
                    "grain.unit", (operation.grain or {}).get("unit"),
                ),
                EditIntent.CHANGE_TIME_RANGE.value: ("timeWindow", operation.timeWindow),
                EditIntent.CHANGE_METRIC.value: ("metricRef", operation.metricRef),
                EditIntent.CHANGE_TOP_N.value: ("topN", operation.topN),
                EditIntent.CHANGE_SORT.value: ("sortField", operation.sortField),
            }.get(action)
            if missing and not missing[1]:
                return ask(f"修改「{(region or {}).get('name') or region_id}」还缺少 {missing[0]}，请明确具体值。")
            if action == EditIntent.ADD_METRIC.value and not operation.addMetrics:
                # 追加指标：至少要有一个要加的指标（名字、单位、怎么显示）。
                return ask(
                    f"要在「{(region or {}).get('name') or region_id}」上追加哪几个指标？"
                    "把指标名字列出来就行。"
                )
            if action == EditIntent.APPLY_PRESENTATION.value and not operation.presentationOperations:
                # 展示批次：至少要给出一条操作，否则这轮什么也没改。
                return ask(
                    "这次要改的是哪一处排版或外观（组成一组 / 取消分组 / 隐藏 / 复制 / 列数 / 间距 / 字号 / 颜色）？"
                )
            if action == EditIntent.SAVE_THEME.value and not operation.themeOperations:
                # 保存共享主题：至少要给出主题名与外观参数。
                return ask(
                    "要保存的主题叫什么名字、外观要改成什么样？"
                    "主题 id 只能用 get_current_draft 返回的 availableThemes 里的。"
                )
        if plan.confidence < 0.7:
            return ask(
                plan.clarificationQuestion or "我还不能确定这次修改的具体内容，请确认目标和修改值。",
                options,
            )
        # ===== 候选前预检（契约 §2.1）=====
        #
        # 模型给的展示操作**不保证**参数形状合法（``fontSize: 28`` 而不是
        # ``{"value":28,"unit":"PX"}``、``afterRegionId`` 这种执行器不认的
        # 字段、``columns`` 放错层级 …）。以前这些都要等用户点「接受修改」
        # 才炸（BUG-001 / BUG-002 / BUG-004 / BUG-006）。
        #
        # 现在在**副本**上先跑一遍：不合法就**不出候选**，改成一句安全澄清；
        # 通过就把**规范化后**的那一批写回候选 —— 候选摘要与执行器真正
        # 跑的参数是**同一份数据**。
        if intent == EditIntent.APPLY_PRESENTATION.value or any(
            step.intent.value == EditIntent.APPLY_PRESENTATION.value
            for step in steps
        ):
            from app.services.report_presentation_service import preflight_presentation

            for step in steps:
                if step.intent.value != EditIntent.APPLY_PRESENTATION.value:
                    continue
                operations = step.operation.presentationOperations or []
                preflight = preflight_presentation(definition, operations)
                if not preflight.ok:
                    # **安全澄清**，不是把校验原文甩给用户。
                    # 原文（"width 的取值格式不对"）是给开发看的；
                    # 用户要看到的是"没改成功、为什么、接下来怎么办"。
                    return ask(
                        "这次修改的排版参数还不合法"
                        f"（{preflight.failure}），"
                        "草稿没有变化，也没有生成待接受的修改计划。"
                        "请把要改的地方说得更具体一点（比如要几列、间距多少、"
                        "改哪张卡片），我再出一次候选。"
                    )
                step.operation.presentationOperations = preflight.operations
        operation_steps = (
            [step.model_dump(mode="json") for step in plan.operations]
            if plan.operations else [{
                "intent": intent,
                "operation": plan.operation.model_dump(mode="json", exclude_none=True),
            }]
        )
        name = (region or {}).get("name") or (definition.metadata or {}).get("name") or region_id
        summary = plan.summary or f"对「{name}」执行 {intent}：" + "；".join(
            self._describe_edit_step(step) for step in operation_steps
        )
        # 模型写的摘要里可能带内部标识（``metric.machine_room.pue``、
        # ``region_1``、``capabilityPending``）—— 用户看不懂也不该看。
        # 规则路径（agent_service）走同一套清洗，两边口径一致。
        summary = sanitize_internal_ids(summary)
        operation_steps = sanitize_change_set_summaries(operation_steps)
        revision = int((definition.metadata or {}).get("revision") or 0)
        candidate = {
            "planId": f"plan_{uuid.uuid4().hex}",
            "draftRevision": revision,
            "targetRegionId": region_id,
            "operations": [
                {**step, "summary": step.get("summary") or self._describe_edit_step(step)}
                for step in operation_steps
            ],
            "summary": summary,
            "editPlan": plan.model_dump(mode="json"),
        }
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=f"待确认的修改：{summary}\n接受后才会校验并修改草稿。",
            designSessionId=request.context.designSessionId,
            domainObjects=self._edit_domain_objects(request),
            questions=[AgentQuestion(
                id="pending_edit_plan_confirm",
                prompt="确认这次修改：",
                expects="SINGLE_CHOICE",
                selectionOnly=True,
                # 「其他」：用户在**这张卡片里**写的自由文本 = 对当前问题的
                # 纠正（同一个待办），不是另一条无关消息。
                allowsOther=True,
                otherLabel="其他",
                otherPlaceholder="也可以直接在这里写要怎么改",
                options=[
                    QuestionOption(value=value, label=value)
                    for value in ("接受修改", "继续调整", "取消")
                ],
            )],
            uiActions=["pending_edit_plan"],
            debug={
                "editOutcome": "candidate",
                "editPlanSource": "react",
                "editingTargetRegionId": region_id or None,
                "pendingEditPlan": candidate,
            },
        )

    @staticmethod
    def _describe_edit_step(step: dict[str, Any]) -> str:
        operation = step.get("operation") or {}
        action = str(step.get("intent") or "")
        labels = {
            "RENAME_REPORT": "修改报表名称",
            "DELETE_SECTION": "删除卡片",
            "DELETE_METRIC": "删除卡片里的指标",
            "RENAME_SECTION": "修改卡片标题",
            "ADD_SECTION": "新增卡片",
            "ADD_METRIC": "在卡片上追加指标",
            "CHANGE_DATA_SOURCE": "更换数据源",
            "CHANGE_COMPILATION_GRAIN": "修改编译粒度",
            "CHANGE_TIME_RANGE": "修改时间窗",
            "CHANGE_METRIC": "更换指标",
            "CHANGE_VISUALIZATION": "更换展示形式",
            "CHANGE_TOP_N": "修改排名数量",
            "CHANGE_SORT": "修改排序",
        }
        details = [step.get("summary") or labels.get(action, "修改")]
        if action == "ADD_METRIC":
            names = "、".join(
                str(item.get("label") or item.get("intent") or "")
                for item in (operation.get("addMetrics") or [])
                if isinstance(item, dict)
            )
            if names:
                details = [f"追加指标：{names}"]
        if action == "DELETE_METRIC":
            names = "、".join(
                str(item.get("label") or item.get("intent") or "")
                for item in (operation.get("deleteMetrics") or [])
                if isinstance(item, dict)
            )
            if names:
                details = [f"删除指标：{names}（卡片保留）"]
        for key, label in (
            ("newTitle", "标题"), ("sourceRef", "数据源"),
            ("metricRef", "指标"), ("timeWindowLabel", "时间窗"),
        ):
            if operation.get(key):
                details.append(f"{label}={operation[key]}")
        grain = operation.get("grain") or {}
        if grain.get("unit"):
            details.append(f"粒度={grain['unit']}")
        return "，".join(details)

    @staticmethod
    def _display_options_for(request: AgentRequest, region_id: str) -> list[dict]:
        """按区块的数据形态给出合法的展示形式候选。

        候选不是模型编的，是从确定性引擎的规则表里查出来的 ——
        模型只负责"用户想要哪种风格"，具体有哪些可选项由代码说了算。
        """
        from app.services.template_edit_engine import (
            DISPLAY_OPTIONS_BY_SHAPE,
            ROLE_DATA_SHAPE,
        )

        definition = request.snapshots.reportDefinition
        if definition is None:
            return []

        region = next(
            (r for r in (definition.regions or []) if r.get("id") == region_id),
            None,
        )
        if region is None:
            return []

        shape = ROLE_DATA_SHAPE.get(str(region.get("role")), "CATEGORY_RANKING")
        options = DISPLAY_OPTIONS_BY_SHAPE.get(shape, {}).get("options", [])
        return [
            {
                "value": f"把{region.get('name') or region_id}改成{option.get('label', '')}",
                "label": option.get("label", ""),
            }
            for option in options
        ]

    def _assemble_fix_response(
        self, request: AgentRequest, payload: dict[str, Any]
    ) -> AgentResponse:
        """节点 8：校验失败后的修复方案。"""
        fixes = payload.get("fixes") or []
        if not fixes:
            return self._assemble_clarification_response(
                request,
                {
                    "reply": payload.get("reply") or "需要说明打算怎么修。",
                    "missingInfo": [
                        {
                            "field": "fix_action",
                            "label": "打算怎么处理？",
                            "expects": "TEXT",
                        }
                    ],
                },
            )

        lines = []
        for fix in fixes:
            note = fix.get("summary") or fix.get("action") or ""
            lines.append(f"- {note}")

        detail = "已整理出修复方案，请确认：\n" + "\n".join(lines)
        reply = payload.get("reply")
        reply = f"{reply}\n{detail}" if reply else detail

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=reply,
            designSessionId=request.context.designSessionId,
            questions=[
                AgentQuestion(
                    id="confirm_fix",
                    prompt="请对修复方案做出选择：",
                    expects="SINGLE_CHOICE",
                    options=[
                        QuestionOption(value="同意", label="同意"),
                        QuestionOption(value="修改", label="修改"),
                    ],
                )
            ],
            uiActions=["confirm_framework", "reject_framework"],
        )


def _parse_tool_args(raw: str) -> tuple[dict[str, Any], bool]:
    """工具调用参数（增量 JSON 片段）→ 参数字典。

    解析不出来就**说解析不出来**（空字典 + ``False``）：不把半截 JSON
    当成参数写进追踪，也不伪造参数。
    """
    text = (raw or "").strip()
    if not text:
        return {}, False
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}, False
    if isinstance(parsed, dict):
        return parsed, True
    return {}, False


def _parse_tool_payload(text: str) -> dict[str, Any] | None:
    """工具的**真实返回**（文本）→ 载荷字典。

    这是「后台结果」的第一来源：工具自己返回的业务载荷。
    解析不出来就返回 ``None`` —— 那就是「后台结果未取得」，
    **不能**拿框架的 ``state`` 顶替。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # 有些工具返回的是「一段说明 + 一段 JSON」：只取 JSON 那段。
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _business_from_payload(
    tool_name: str,
    payload: dict[str, Any] | None,
) -> tuple[bool | None, str, str, list[dict[str, Any]], dict[str, Any], str, bool | None]:
    """从工具的**真实返回**里取业务事实。

    返回 ``(businessOk, businessMessage, businessReason, businessIssues,
    businessDetail, phase, draftChanged)``。

    * ``businessOk`` 为 ``None`` = **未取得**（绝不拿框架 ``state`` 顶替）；
    * ``phase`` 说明这一轮走到了哪一步（计划 / 试做 / 候选）；
    * ``draftChanged`` 说明**正式**草稿有没有被改 —— 提交类工具只产出
      计划与候选，**不改正式草稿**（试做跑在副本上）。
    """
    if not isinstance(payload, dict):
        return None, "", "", [], {}, "", None

    business_ok: bool | None = None
    if "accepted" in payload:
        business_ok = bool(payload.get("accepted"))
    elif "ok" in payload:
        business_ok = bool(payload.get("ok"))

    message = ""
    if payload.get("error"):
        message = str(payload.get("error"))
    elif payload.get("summary"):
        message = str(payload.get("summary"))

    reason = str(payload.get("reason") or "")
    issues = [
        issue
        for issue in (payload.get("errors") or [])
        if isinstance(issue, dict)
    ]
    detail: dict[str, Any] = {}
    if payload.get("resolvedOperations") is not None:
        detail["resolvedOperations"] = payload.get("resolvedOperations")
    if payload.get("checked") is not None:
        detail["checked"] = payload.get("checked")
    if payload.get("tool"):
        detail["tool"] = str(payload.get("tool"))

    phase = ""
    draft_changed: bool | None = None
    if str(tool_name or "").startswith("submit_"):
        phase = edit_trace_service.PHASE_PLAN
        if payload.get("checked") is True:
            # 过了试做 → 形成了待确认候选。
            phase = edit_trace_service.PHASE_CANDIDATE
        elif issues:
            # 试做没过（带具体错误清单）→ 停在试做这一步。
            phase = edit_trace_service.PHASE_TRIAL
        # 提交类工具只写计划/候选，**不碰正式草稿**。
        draft_changed = False
    return business_ok, message, reason, issues, detail, phase, draft_changed


def _submit_shape(tool_name: str, params: dict[str, Any]) -> str:
    """提交形状的指纹：用来判「又提交了同一份没改好的东西」。

    只看**形状**（能力、目标、参数）—— 值改了就说明改好了，不算重复。
    """
    if not str(tool_name or "").startswith("submit_"):
        return ""
    shape = {
        "intent": params.get("intent"),
        "operations": params.get("operations"),
        "operation": params.get("operation"),
        "target": params.get("target"),
    }
    shape = {
        key: value for key, value in shape.items() if value not in (None, "", [], {})
    }
    if not shape:
        return ""
    try:
        canonical = json.dumps(shape, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(shape)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _round_bucket(iteration: int | None) -> str:
    """轮次的分组键（取不到就是 ``"unknown"``，不假装是哪一轮）。"""
    return str(iteration) if isinstance(iteration, int) else "unknown"


def _extract_text(msg) -> str:
    """从 AgentScope 的 Msg 里拎出最终文本内容。

    Msg.content 可能是 str，也可能是 list[TextBlock | ...]。
    """
    if msg is None:
        return ""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            # TextBlock 可能是 dict 或 pydantic 对象
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return str(content)
