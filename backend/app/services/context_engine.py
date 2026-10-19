"""ContextEngine：全局语义路由器。

职责：
    1. 轻量 Concierge 先承接用户，提供活人感，并判断是否需要进入重路由；
    2. 重 ContextEngine 判断是否打断当前流程、信息是否足够、下一步怎么走；
    3. 输出结构化 routing decision，交给 AgentService 执行；
    4. 不生成报表、不直接调用业务工具。

实现策略：
    - 主路径：轻量 Concierge 模型 → 重路由模型，输出严格 JSON；
    - 兜底：模型不可用 / 超时 / JSON 不合法时，用少量保守规则避免系统报硬错。
"""
from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from time import perf_counter
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.config import get_settings
from app.schemas.agent import AgentRequest, AgentState
from app.services.request_timeline import log_timeline_step


logger = logging.getLogger(__name__)

EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


def _short(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


async def _emit(
    emit: EmitEvent | None, event: str, data: dict[str, Any]
) -> None:
    if emit is not None:
        await emit(event, data)


class UserIntent(str, Enum):
    CONFIRM = "CONFIRM"
    MODIFY = "MODIFY"
    REJECT = "REJECT"
    ASK_QUESTION = "ASK_QUESTION"
    CHITCHAT = "CHITCHAT"
    CANCEL = "CANCEL"
    NEW_TASK = "NEW_TASK"
    PROVIDE_INFO = "PROVIDE_INFO"
    UNKNOWN = "UNKNOWN"


class RoutingAction(str, Enum):
    CONTINUE_CURRENT = "CONTINUE_CURRENT"
    JUMP_STATE = "JUMP_STATE"
    ASK_INTERRUPT_CONFIRMATION = "ASK_INTERRUPT_CONFIRMATION"
    ANSWER_IN_PLACE = "ANSWER_IN_PLACE"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"
    BLOCK = "BLOCK"


class ConciergeAction(str, Enum):
    DIRECT_REPLY = "DIRECT_REPLY"
    DEEP_ROUTE = "DEEP_ROUTE"


class ConciergeDecision(BaseModel):
    action: ConciergeAction = ConciergeAction.DEEP_ROUTE
    userIntent: UserIntent = UserIntent.UNKNOWN
    reply: str = ""
    reason: str = ""
    confidence: float = 0.0


class RouteDecision(BaseModel):
    userIntent: UserIntent = UserIntent.UNKNOWN
    routingAction: RoutingAction = RoutingAction.CONTINUE_CURRENT
    targetState: AgentState | None = None
    requiresUserConfirm: bool = False
    confidence: float = 0.0
    reply: str = ""
    requiredInfo: list[str] = Field(default_factory=list)
    collectedInfo: dict[str, Any] = Field(default_factory=dict)
    missingInfo: list[str] = Field(default_factory=list)
    isReadyForNextStep: bool = True
    nextQuestion: str = ""
    reason: str = ""
    engineStage: str = ""


_CONCIERGE_PROMPT = """
你是 AI 报表系统的轻量接待员（Concierge）。

你的任务不是做完整状态机判断，而是用很短时间决定：
1. 这句话能不能直接回复；
2. 还是需要进入重 ContextEngine 做状态机路由。

你要有活人感，回复自然、简短、带一点引导。

可以 DIRECT_REPLY 的场景：
- 简单打招呼：你好、hi、在吗；
- 用户问你能做什么、怎么用；
- 非状态改变的轻量闲聊；
- 当前没有足够业务意图，只需要引导用户说出报表需求。

必须 DEEP_ROUTE 的场景：
- 确认 / 取消 / 重做 / 不做了；
- 修改草稿或报表：删掉、改成、换成、不要这个；
- 用户在问当前草稿、指标、图表含义；
- 任何涉及当前 state 流转、保存、校验、预览、打断流程的句子；
- 任何你不确定是否会改变业务状态的句子。

输出 JSON：
{
  "action": "DIRECT_REPLY" 或 "DEEP_ROUTE",
  "userIntent": "CHITCHAT" / "ASK_QUESTION" / "MODIFY" / "CONFIRM" / "CANCEL" / "UNKNOWN" 等，
  "reply": "如果 DIRECT_REPLY，填自然回复；否则空字符串",
  "reason": "简短原因",
  "confidence": 0.0 到 1.0
}

DIRECT_REPLY 的回复示例：
- “你好～我是云枢的智能报表助手，可以帮你生成机房能耗报表，也可以修改已有报表。你可以试试说：生成 A 机房昨日能耗日报。”
- “我在～你可以直接说想生成什么报表，比如：生成 A 机房昨日能耗日报。”

只能输出 JSON，不要 markdown。
""".strip()


_SYSTEM_PROMPT = """
你是 AI 报表系统的 ContextEngine / Semantic Router。

你只负责判断用户当前这句话对状态机意味着什么，不负责生成报表，不负责调用工具。

你需要同时判断四件事：
1. userIntent：用户真实意图；
2. routingAction：系统下一步怎么走；
3. 当前 state 所需信息是否已经足够；
4. 是否需要打断当前流程，以及是否需要二次确认。

可选 userIntent：
- CONFIRM：确认继续当前结果或当前操作
- MODIFY：修改当前草稿 / 当前报表
- REJECT：不认可当前结果，要重做
- ASK_QUESTION：询问当前草稿、指标、图表、流程含义
- CHITCHAT：闲聊、打招呼、问你是谁、问你能做什么
- CANCEL：取消当前流程
- NEW_TASK：开始一个新的报表任务
- PROVIDE_INFO：补充上一轮追问的信息
- UNKNOWN：无法判断

可选 routingAction：
- CONTINUE_CURRENT：不拦截，交给当前 state 的业务逻辑继续处理
- JUMP_STATE：安全打断，跳到 targetState
- ASK_INTERRUPT_CONFIRMATION：危险打断，先问用户是否确认打断
- ANSWER_IN_PLACE：原地回答，不改变当前业务状态
- ASK_CLARIFICATION：信息不足，需要追问
- BLOCK：请求不支持或有风险，需要拦截

安全打断示例：
- WAITING_USER_CONFIRM 下用户说"昨日总耗电删掉" → MODIFY + JUMP_STATE + ANALYZING
- WAITING_USER_CONFIRM 下用户说"重做一版" → REJECT + JUMP_STATE + ANALYZING
- READY 下用户说"换成折线图" → MODIFY + JUMP_STATE + ANALYZING

WAITING_USER_CONFIRM 下三个固定按钮的识别规则（必须严格命中）：
- 用户只说"同意" → CONFIRM + JUMP_STATE + SAVE_DEFINITION_DRAFT
- 用户只说"修改" → MODIFY + JUMP_STATE + ANALYZING（在 reply 里引导用户说出具体改动）
- 用户只说"重新开始" → REJECT + JUMP_STATE + ANALYZING（重新生成一版）

危险打断示例：
- VALIDATING 下用户说“我不做了” → CANCEL + ASK_INTERRUPT_CONFIRMATION
- PREVIEWING 下用户说“重新来” → REJECT + ASK_INTERRUPT_CONFIRMATION
- SAVE_DEFINITION_DRAFT 下用户说“取消” → CANCEL + ASK_INTERRUPT_CONFIRMATION

信息不足示例：
- ANALYZING 下用户只说“生成报表”但没有对象、时间、指标 → ASK_CLARIFICATION
- NEEDS_CLARIFICATION 下用户补充了机房/时间/指标 → PROVIDE_INFO + CONTINUE_CURRENT

输出要求：
- 只能输出一个 JSON 对象；
- 不要 markdown；
- targetState 必须是状态机枚举之一或 null；
- confidence 是 0 到 1 的数字。
""".strip()


class ContextEngine:
    """模型版语义路由器。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: AsyncOpenAI | None = None
        if self._settings.volc_api_key:
            self._client = AsyncOpenAI(
                api_key=self._settings.volc_api_key,
                base_url=self._settings.volc_base_url,
                timeout=self._settings.volc_timeout,
                max_retries=0,
            )

    async def route(
        self, request: AgentRequest, emit: EmitEvent | None = None
    ) -> RouteDecision:
        total_started = perf_counter()
        await _emit(
            emit,
            "stage",
            {
                "stage": "context_route_start",
                "state": request.state.value,
                "message": request.message,
            },
        )
        logger.info(
            "[context_engine] step=route_start conversationId=%s state=%s message=%r concierge_timeout=%ss router_timeout=%ss",
            request.conversationId,
            request.state.value,
            request.message,
            self._settings.volc_context_concierge_timeout,
            self._settings.volc_context_router_timeout,
        )
        if self._client is None:
            log_timeline_step("fallback_no_client")
            await _emit(emit, "stage", {"stage": "fallback_no_client"})
            logger.warning(
                "[context_engine] step=fallback_no_client conversationId=%s reason=%s",
                request.conversationId,
                "VOLC_API_KEY 未配置",
            )
            decision = self._fallback_route(request, "VOLC_API_KEY 未配置")
            decision.engineStage = "fallback_no_client"
            return decision

        concierge = await self._run_concierge(request, emit=emit)
        logger.info(
            "[context_engine] step=concierge_decision conversationId=%s action=%s intent=%s confidence=%.2f reason=%r elapsed_total_ms=%.1f",
            request.conversationId,
            concierge.action.value,
            concierge.userIntent.value,
            concierge.confidence,
            concierge.reason,
            (perf_counter() - total_started) * 1000,
        )
        if concierge.action == ConciergeAction.DIRECT_REPLY and concierge.reply:
            log_timeline_step("route_direct_reply")
            await _emit(
                emit,
                "stage",
                {
                    "stage": "concierge_direct_reply",
                    "intent": concierge.userIntent.value,
                    "action": concierge.action.value,
                },
            )
            logger.info(
                "[context_engine] step=route_direct_reply conversationId=%s elapsed_total_ms=%.1f",
                request.conversationId,
                (perf_counter() - total_started) * 1000,
            )
            return RouteDecision(
                userIntent=concierge.userIntent,
                routingAction=RoutingAction.ANSWER_IN_PLACE,
                confidence=concierge.confidence,
                reply=concierge.reply,
                isReadyForNextStep=False,
                reason=f"concierge: {concierge.reason}",
                engineStage="concierge_direct_reply",
            )

        try:
            router_started = perf_counter()
            log_timeline_step("deep_route_start")
            await _emit(
                emit,
                "stage",
                {
                    "stage": "deep_route_start",
                    "model": self._settings.volc_context_router_model,
                },
            )
            logger.info(
                "[context_engine] step=deep_route_start conversationId=%s model=%s",
                request.conversationId,
                self._settings.volc_context_router_model,
            )
            raw = await self._chat_json_stream(
                model=self._settings.volc_context_router_model,
                temperature=0,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": self._compose_input(request)},
                ],
                response_format={"type": "json_object"},
                timeout=self._settings.volc_context_router_timeout,
                emit=emit,
                chunk_stage="deep_route_chunk",
            )
            log_timeline_step("deep_route_done")
            await _emit(
                emit,
                "stage",
                {
                    "stage": "deep_route_done",
                    "elapsedMs": round((perf_counter() - router_started) * 1000),
                },
            )
            logger.info(
                "[context_engine] step=deep_route_done conversationId=%s elapsed_ms=%.1f raw=%r",
                request.conversationId,
                (perf_counter() - router_started) * 1000,
                _short(raw),
            )
            decision = self._parse_decision(raw)
            decision.engineStage = "deep_route_done"
            logger.info(
                "[context_engine] step=route_return conversationId=%s elapsed_total_ms=%.1f action=%s intent=%s",
                request.conversationId,
                (perf_counter() - total_started) * 1000,
                decision.routingAction.value,
                decision.userIntent.value,
            )
            return decision
        except Exception as exc:  # noqa: BLE001
            log_timeline_step("deep_route_failed")
            await _emit(
                emit,
                "stage",
                {"stage": "deep_route_failed", "error": str(exc)},
            )
            logger.exception("ContextEngine 模型路由失败，走保守兜底：%s", exc)
            decision = self._fallback_route(request, str(exc))
            decision.engineStage = "deep_route_failed_fallback"
            return decision

    async def _run_concierge(
        self, request: AgentRequest, emit: EmitEvent | None = None
    ) -> ConciergeDecision:
        assert self._client is not None
        started = perf_counter()
        log_timeline_step("concierge_start")
        await _emit(
            emit,
            "stage",
            {
                "stage": "concierge_start",
                "model": self._settings.volc_context_concierge_model,
            },
        )
        logger.info(
            "[context_engine] step=concierge_start conversationId=%s model=%s",
            request.conversationId,
            self._settings.volc_context_concierge_model,
        )
        try:
            raw = await self._chat_json_stream(
                model=self._settings.volc_context_concierge_model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": _CONCIERGE_PROMPT},
                    {"role": "user", "content": self._compose_concierge_input(request)},
                ],
                response_format={"type": "json_object"},
                timeout=self._settings.volc_context_concierge_timeout,
                emit=emit,
                chunk_stage="concierge_chunk",
            )
            log_timeline_step("concierge_done")
            await _emit(
                emit,
                "stage",
                {
                    "stage": "concierge_done",
                    "elapsedMs": round((perf_counter() - started) * 1000),
                },
            )
            logger.info(
                "[context_engine] step=concierge_done conversationId=%s elapsed_ms=%.1f raw=%r",
                request.conversationId,
                (perf_counter() - started) * 1000,
                _short(raw),
            )
            return ConciergeDecision.model_validate(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            log_timeline_step("concierge_failed")
            await _emit(
                emit,
                "stage",
                {"stage": "concierge_failed", "error": str(exc)},
            )
            logger.warning(
                "[context_engine] step=concierge_failed conversationId=%s elapsed_ms=%.1f error=%s",
                request.conversationId,
                (perf_counter() - started) * 1000,
                exc,
            )
            return ConciergeDecision(
                action=ConciergeAction.DEEP_ROUTE,
                userIntent=UserIntent.UNKNOWN,
                reason=f"concierge failed: {exc}",
                confidence=0,
            )

    async def _chat_json_stream(
        self,
        *,
        model: str,
        temperature: float,
        messages: list[dict[str, str]],
        response_format: dict[str, str],
        timeout: int,
        emit: EmitEvent | None,
        chunk_stage: str,
    ) -> str:
        """用模型流式输出拼回最终 JSON 文本。"""
        assert self._client is not None
        started = perf_counter()
        idle_timeout = timeout
        max_total_seconds = max(120, timeout * 4)
        stream = await self._client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=messages,
            response_format=response_format,
            stream=True,
            timeout=timeout,
        )

        chunks: list[str] = []
        iterator = stream.__aiter__()
        while True:
            if perf_counter() - started > max_total_seconds:
                raise TimeoutError(f"stream total timeout after {max_total_seconds}s")
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            chunks.append(delta)
            await _emit(
                emit,
                "model_chunk",
                {
                    "stage": chunk_stage,
                    "delta": delta,
                    "elapsedMs": round((perf_counter() - started) * 1000),
                },
            )
        return "".join(chunks)

    def _parse_decision(self, raw: str) -> RouteDecision:
        try:
            data = json.loads(raw)
            return RouteDecision.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ContextEngine 输出不合法，raw=%r, err=%s", raw, exc)
            return RouteDecision(
                userIntent=UserIntent.UNKNOWN,
                routingAction=RoutingAction.ASK_CLARIFICATION,
                confidence=0,
                isReadyForNextStep=False,
                missingInfo=["intent"],
                nextQuestion="我没太明白你的意思。你是想确认、修改、重做，还是问问题？",
                reply="我没太明白你的意思。你是想确认、修改、重做，还是问问题？",
                reason="模型输出不合法",
            )

    def _compose_input(self, request: AgentRequest) -> str:
        ctx = request.context
        snapshots = request.snapshots
        draft = snapshots.reportDefinition
        policy = snapshots.reportPolicy
        validation = snapshots.lastValidationResult
        preview = snapshots.lastReportRun

        payload = {
            "currentState": request.state.value,
            "message": request.message,
            "context": {
                "designSessionId": ctx.designSessionId,
                "reportPolicyId": ctx.reportPolicyId,
                "reportDefinitionId": ctx.reportDefinitionId,
                "baseDefinitionRevision": ctx.baseDefinitionRevision,
                "selectedRegionId": ctx.selectedRegionId,
                "projectId": ctx.projectId,
                "subjectRef": ctx.subjectRef,
                "subjectId": ctx.subjectId,
            },
            "snapshotsSummary": {
                "hasPolicy": policy is not None,
                "hasDefinition": draft is not None,
                "hasChangeSet": snapshots.lastChangeSet is not None,
                "hasValidationResult": validation is not None,
                "hasPreview": preview is not None,
                "definitionSummary": self._summarize_definition(draft),
                "validationPassed": validation.passed if validation else None,
                "previewStatus": preview.status if preview else None,
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _compose_concierge_input(request: AgentRequest) -> str:
        payload = {
            "currentState": request.state.value,
            "message": request.message,
            "hasDefinition": request.snapshots.reportDefinition is not None,
            "hasChangeSet": request.snapshots.lastChangeSet is not None,
            "hasValidationResult": request.snapshots.lastValidationResult is not None,
            "hasPreview": request.snapshots.lastReportRun is not None,
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _summarize_definition(draft: Any | None) -> dict[str, Any] | None:
        if draft is None:
            return None
        regions = getattr(draft, "regions", []) or []
        return {
            "reportName": (getattr(draft, "metadata", {}) or {}).get("name"),
            "regionCount": len(regions),
            "regions": [
                {
                    "regionId": r.get("regionId") or r.get("id"),
                    "title": r.get("title") or r.get("displayName"),
                    "role": r.get("role"),
                    "componentType": (r.get("component") or {}).get("type"),
                }
                for r in regions[:12]
                if isinstance(r, dict)
            ],
        }

    def _fallback_route(self, request: AgentRequest, reason: str) -> RouteDecision:
        """保守兜底：只处理明显场景，不做复杂语义理解。"""
        text = (request.message or "").strip().lower()
        state = request.state
        dangerous_states = {
            AgentState.SAVE_DEFINITION_DRAFT,
            AgentState.VALIDATING,
            AgentState.PREVIEWING,
        }

        if any(w in text for w in ("取消", "不做了", "算了", "停止")):
            if state in dangerous_states:
                return RouteDecision(
                    userIntent=UserIntent.CANCEL,
                    routingAction=RoutingAction.ASK_INTERRUPT_CONFIRMATION,
                    targetState=AgentState.ANALYZING,
                    requiresUserConfirm=True,
                    confidence=0.75,
                    reply="当前流程正在进行中。你确定要放弃当前流程并重新开始吗？",
                    reason=f"fallback: {reason}",
                )
            return RouteDecision(
                userIntent=UserIntent.CANCEL,
                routingAction=RoutingAction.JUMP_STATE,
                targetState=AgentState.ANALYZING,
                confidence=0.7,
                reason=f"fallback: {reason}",
            )

        if state == AgentState.WAITING_USER_CONFIRM:
            # 三个按钮：同意 / 修改 / 重新开始
            if text in {"同意"} or any(
                w in text for w in ("确认", "好的", "可以", "ok", "继续", "按这个")
            ):
                return RouteDecision(
                    userIntent=UserIntent.CONFIRM,
                    routingAction=RoutingAction.JUMP_STATE,
                    targetState=AgentState.SAVE_DEFINITION_DRAFT,
                    confidence=0.9,
                    reason=f"fallback: {reason}",
                )
            if text in {"重新开始", "重做", "重新生成一版"} or any(
                w in text for w in ("重做", "重新生成", "重来", "重新开始")
            ):
                return RouteDecision(
                    userIntent=UserIntent.REJECT,
                    routingAction=RoutingAction.JUMP_STATE,
                    targetState=AgentState.ANALYZING,
                    confidence=0.85,
                    reply="好的，我按新的要求重新生成一版。",
                    reason=f"fallback: {reason}",
                )
            if text in {"修改"} or any(
                w in text for w in ("删", "删除", "去掉", "改", "换", "修改")
            ):
                return RouteDecision(
                    userIntent=UserIntent.MODIFY,
                    routingAction=RoutingAction.JUMP_STATE,
                    targetState=AgentState.ANALYZING,
                    confidence=0.8,
                    reply="好的，请告诉我具体想怎么改。",
                    reason=f"fallback: {reason}",
                )

        return RouteDecision(
            userIntent=UserIntent.UNKNOWN,
            routingAction=RoutingAction.CONTINUE_CURRENT,
            confidence=0.2,
            reason=f"fallback: {reason}",
        )

context_engine = ContextEngine()
