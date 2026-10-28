"""AgentScopeReportAgent：业务 Agent 的 ReAct 主循环（基于 AgentScope 2.0）。

架构：
    - 单 Agent + Tools（工具统一在 app/tools/ 下）
    - LLM：火山方舟 Doubao-Seed-2.1-lite，通过 AgentScope 原生 VolcengineChatModel 接入
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
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from app.agents.fake_report_agent import FakeReportAgent
from app.config import get_settings
from app.schemas.agent import (
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
from app.services import routing_rules
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


class AgentScopeReportAgent:
    """AgentScope ReAct Agent + 火山方舟 Doubao，覆盖样例 1。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._fallback = FakeReportAgent()
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        self._edit_prompt = _EDIT_PROMPT_PATH.read_text(encoding="utf-8")
        self._query_prompt = _QUERY_PROMPT_PATH.read_text(encoding="utf-8")
        self._model, self._init_err = self._init_model()

    # ---------- 初始化 ----------

    def _init_model(self):
        """构造 VolcengineChatModel。失败则返回 (None, err)。

        Agent 实例在每次 handle 时新建（AgentScope Agent 有对话内部状态，不适合跨请求复用）。
        """
        if not self._settings.volc_api_key:
            return None, "VOLC_API_KEY 未配置"
        try:
            from agentscope.credential import VolcengineCredential
            from agentscope.formatter import VolcengineChatFormatter
            from agentscope.model import VolcengineChatModel

            credential = VolcengineCredential(
                api_key=self._settings.volc_api_key,
                base_url=self._settings.volc_base_url,
            )
            model = VolcengineChatModel(
                credential=credential,
                model=self._settings.volc_model,
                parameters=VolcengineChatModel.Parameters(
                    temperature=self._settings.volc_temperature,
                ),
                stream=False,
                formatter=VolcengineChatFormatter(),
            )
            return model, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("AgentScope 模型初始化失败：%s", exc)
            return None, str(exc)

    async def _build_agent(self, request: AgentRequest):
        """为单次 handle 创建一个全新的 AgentScope Agent（含 Toolkit）。

        预算按当前节点给：编辑场景查证步数少，生成场景多给几步。
        工具也按当前节点收窄：模型看不到这个节点不该调的工具。

        只读查询轮换一套工具：只注册事实工具和查询答案终结工具，
        执行类工具结构上就调不到。
        """
        from agentscope.agent import Agent

        query_mode = bool(getattr(request, "queryMode", False))
        toolkit = await to_agentscope_toolkit_async(
            tools_for_query() if query_mode else tools_for_state(request.state)
        )
        if query_mode:
            system_prompt = self._query_prompt
        elif request.state == AgentState.USER_EDITING_TEMPLATE:
            system_prompt = self._edit_prompt
        else:
            system_prompt = self._system_prompt
        return Agent(
            name="ReportDraftAgent",
            system_prompt=system_prompt,
            model=self._model,
            toolkit=toolkit,
            react_config=self._build_react_config(request.state),
        )

    def _build_react_config(self, state: AgentState):
        """按节点构造 ReAct 预算。

        AgentScope 默认 max_iters=50，报表场景根本用不到；这里按节点收紧，
        避免模型在错误的分支上空转。
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

    # ---------- 入口 ----------

    def _degraded_response(self, request: AgentRequest) -> AgentResponse:
        """降级响应。

        查询轮降级到 Fake 的查询路径（同一套事实工具、同一套事实口径）；
        编辑态降级到「查证不可用」；其余降级到 FakeAgent。
        """
        if getattr(request, "queryMode", False):
            return self._fallback.handle(request)
        if request.state == AgentState.USER_EDITING_TEMPLATE:
            return self._unavailable_edit_response(request)
        return self._fallback.handle(request)

    def handle(self, request: AgentRequest) -> AgentResponse:
        """同步入口：兼容老调用方（会新建 loop，可能遇到连接池失效问题）。"""
        if self._model is None:
            logger.info("AgentScope 未就绪（%s），降级 FakeAgent", self._init_err)
            return self._degraded_response(request)
        try:
            plan = asyncio.run(self._run_agent(request))
        except Exception as exc:  # noqa: BLE001
            logger.exception("AgentScope 运行异常，降级 FakeAgent：%s", exc)
            return self._degraded_response(request)
        return self._dispatch(request, plan)

    async def ahandle(self, request: AgentRequest) -> AgentResponse:
        """async 入口：复用 FastAPI 的事件循环，避免 httpx 连接池跨 loop 失效。"""
        if self._model is None:
            logger.info("AgentScope 未就绪（%s），降级 FakeAgent", self._init_err)
            return self._degraded_response(request)
        try:
            plan = await self._run_agent(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AgentScope 运行异常，降级 FakeAgent：%s", exc)
            return self._degraded_response(request)
        return self._dispatch(request, plan)

    def _dispatch(
        self, request: AgentRequest, plan: dict[str, Any] | None
    ) -> AgentResponse:
        """把本轮结论组装成 AgentResponse。

        结论有两个来源，优先级：终结工具提交 > 旧文本 JSON 解析。
        终结工具是结构化的、经过 Pydantic 校验的，比自由文本可靠。
        """
        if plan is None:
            logger.info("AgentScope 输出不合法，降级 FakeAgent")
            if getattr(request, "queryMode", False):
                return self._query_failure_response(request)
            return self._unavailable_edit_response(request) if request.state == AgentState.USER_EDITING_TEMPLATE else self._fallback.handle(request)

        submission = plan.get("submission")
        query_mode = bool(getattr(request, "queryMode", False))
        if query_mode:
            # 只读查询轮只接受查询答案提交：编辑类终结工具在这里不合法。
            if (
                not isinstance(submission, dict)
                or submission.get("tool") != "submit_query_answer"
            ):
                logger.warning("查询态拒绝非查询答案终结工具")
                return self._query_failure_response(request)
        elif request.state == AgentState.USER_EDITING_TEMPLATE and (
            not isinstance(submission, dict)
            or submission.get("tool") not in {"submit_edit_answer", "submit_edit_plan"}
        ):
            logger.warning("编辑态拒绝非编辑终结工具或旧式文本计划")
            return self._unavailable_edit_response(request)
        elif isinstance(submission, dict) and submission.get("tool") == "submit_query_answer":
            # 普通业务轮不接受只读答案提交：这个出口只在查询模式注册。
            logger.warning("非查询轮拒绝只读答案提交")
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

        logger.warning("AgentScope 返回未知 mode=%s，降级", mode)
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
    def _unavailable_edit_response(request: AgentRequest) -> AgentResponse:
        message = "本轮查证遇到技术问题，草稿没有修改。请稍后重试。"
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=message,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=request.snapshots.reportPolicy,
                reportDefinitionDraft=request.snapshots.reportDefinition,
            ),
            error=AgentError(code="EDIT_VERIFICATION_UNAVAILABLE", message=message),
            debug={"editOutcome": "technical_failure"},
        )

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

        logger.warning("未知终结工具 %s，忽略提交", tool)
        return None

    # ---------- Agent Loop（交给 AgentScope 的 ReAct 内核） ----------

    async def _run_agent(self, request: AgentRequest) -> dict[str, Any] | None:
        from agentscope.message import Msg, TextBlock

        agent = await self._build_agent(request)

        # 绑定本轮工具上下文：读草稿 / 读区块 / 读校验结果的工具从这里取状态，
        # 避免把大对象塞进模型参数。
        bind_tool_context(self._build_tool_context(request))
        bind_submission()
        bind_lookup_failure()
        try:
            user_msg = Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(type="text", text=self._compose_user_message(request))
                ],
            )

            # 第一次调用：由 AgentScope 跑完整 ReAct 循环（Reason → Tool → Observation → ...）
            reply_msg = await agent.reply(user_msg)
            if getattr(request, "queryMode", False) and take_lookup_failure():
                logger.warning("查询态事实工具查证失败，不接受本轮答案")
                return None
            if request.state in {AgentState.USER_EDITING_TEMPLATE, AgentState.WAITING_USER_CONFIRM} and take_lookup_failure():
                logger.warning("编辑态事实工具查证失败，不接受本轮候选结论")
                return None

            # 终结工具优先：模型调用了 submit_* 就以它的提交为准
            plan = self._plan_from_submission()
            if plan is not None:
                return plan

            plan = self._parse_plan(_extract_text(reply_msg))
            if plan is not None:
                return plan

            # 修复重试 1 次（Q5）
            logger.warning(
                "AgentScope 未提交终结工具 state=%s reply=%r",
                request.state.value, _extract_text(reply_msg)[:400],
            )
            logger.info("AgentScope 首轮输出不合法，走修复重试")
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
            reply_msg2 = await agent.reply(repair_msg)
            if getattr(request, "queryMode", False) and take_lookup_failure():
                logger.warning("查询态修复轮事实工具查证失败")
                return None
            if request.state in {AgentState.USER_EDITING_TEMPLATE, AgentState.WAITING_USER_CONFIRM} and take_lookup_failure():
                logger.warning("编辑态修复轮事实工具查证失败")
                return None
            plan = self._plan_from_submission()
            if plan is not None:
                return plan
            logger.warning(
                "AgentScope 修复轮未提交终结工具 state=%s reply=%r",
                request.state.value, _extract_text(reply_msg2)[:400],
            )
            return self._parse_plan(_extract_text(reply_msg2))
        finally:
            clear_tool_context()
            clear_submission()
            clear_lookup_failure()

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
            logger.warning("草稿组装被阻断 code=%s message=%s", exc.code, exc.message)
            return self._blocked_generate_response(request, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("草稿组装或校验失败：%s", exc)
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
        definition = request.snapshots.reportDefinition
        intent = plan.intent.value
        region_id = plan.target.regionId
        options = [option.model_dump(mode="json") for option in plan.options]
        region = next(
            (item for item in (definition.regions or []) if item.get("id") == region_id),
            None,
        ) if definition else None

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
        if plan.confidence < 0.7:
            return ask(
                plan.clarificationQuestion or "我还不能确定这次修改的具体内容，请确认目标和修改值。",
                options,
            )
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
            "RENAME_SECTION": "修改卡片标题",
            "ADD_SECTION": "新增卡片",
            "CHANGE_DATA_SOURCE": "更换数据源",
            "CHANGE_COMPILATION_GRAIN": "修改编译粒度",
            "CHANGE_TIME_RANGE": "修改时间窗",
            "CHANGE_METRIC": "更换指标",
            "CHANGE_VISUALIZATION": "更换展示形式",
            "CHANGE_TOP_N": "修改排名数量",
            "CHANGE_SORT": "修改排序",
        }
        details = [step.get("summary") or labels.get(action, "修改")]
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
