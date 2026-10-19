"""AgentScopeReportAgent：第一阶段真大脑（基于 AgentScope 2.0）。

架构（Q1~Q5）：
    - 单 Agent + Skills/Tools
    - LLM：火山方舟 Doubao-Seed-2.1-lite，通过 AgentScope 原生 VolcengineChatModel 接入
    - Skills：通过 Skill registry 注册到 AgentScope Toolkit
    - 可靠性：LLM 输出不合法 → 修复重试 1 次 → 仍失败则由上层降级 FakeAgent

能力范围（Q4）：
    - 只处理样例 1：生成机房能耗日报（GENERATE）
    - 其他意图 → Agent 自己返回 FALLBACK → 上层降级 FakeAgent
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from app.agents.fake_report_agent import FakeReportAgent
from app.config import get_settings
from app.schemas.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseDomainObjects,
    AgentQuestion,
    AgentState,
    QuestionOption,
    ToolRequest,
)
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_policy import ReportPolicyDraft
from app.skills.registry import get_runner, to_agentscope_toolkit_async


logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "report_generation.md"


class AgentScopeReportAgent:
    """AgentScope ReAct Agent + 火山方舟 Doubao，覆盖样例 1。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._fallback = FakeReportAgent()
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
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

    async def _build_agent(self):
        """为单次 handle 创建一个全新的 AgentScope Agent（含 Toolkit）。"""
        from agentscope.agent import Agent

        toolkit = await to_agentscope_toolkit_async()
        return Agent(
            name="ReportDraftAgent",
            system_prompt=self._system_prompt,
            model=self._model,
            toolkit=toolkit,
        )

    # ---------- 入口 ----------

    def handle(self, request: AgentRequest) -> AgentResponse:
        """同步入口：兼容老调用方（会新建 loop，可能遇到连接池失效问题）。"""
        if self._model is None:
            logger.info("AgentScope 未就绪（%s），降级 FakeAgent", self._init_err)
            return self._fallback.handle(request)
        try:
            plan = asyncio.run(self._run_agent(request))
        except Exception as exc:  # noqa: BLE001
            logger.exception("AgentScope 运行异常，降级 FakeAgent：%s", exc)
            return self._fallback.handle(request)
        return self._dispatch(request, plan)

    async def ahandle(self, request: AgentRequest) -> AgentResponse:
        """async 入口：复用 FastAPI 的事件循环，避免 httpx 连接池跨 loop 失效。"""
        if self._model is None:
            logger.info("AgentScope 未就绪（%s），降级 FakeAgent", self._init_err)
            return self._fallback.handle(request)
        try:
            plan = await self._run_agent(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AgentScope 运行异常，降级 FakeAgent：%s", exc)
            return self._fallback.handle(request)
        return self._dispatch(request, plan)

    def _dispatch(
        self, request: AgentRequest, plan: dict[str, Any] | None
    ) -> AgentResponse:
        if plan is None:
            logger.info("AgentScope 输出不合法，降级 FakeAgent")
            return self._fallback.handle(request)

        mode = plan.get("mode")
        if mode == "FALLBACK":
            return self._fallback.handle(request)
        if mode == "GENERATE":
            return self._assemble_generate_response(request, plan)
        if mode == "CHITCHAT":
            return self._assemble_chitchat_response(request, plan)

        logger.warning("AgentScope 返回未知 mode=%s，降级", mode)
        return self._fallback.handle(request)

    # ---------- Agent Loop（交给 AgentScope 的 ReAct 内核） ----------

    async def _run_agent(self, request: AgentRequest) -> dict[str, Any] | None:
        from agentscope.message import Msg, TextBlock

        agent = await self._build_agent()

        user_msg = Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text=self._compose_user_message(request))],
        )

        # 第一次调用：由 AgentScope 跑完整 ReAct 循环（Reason → Tool → Observation → ...）
        reply_msg = await agent.reply(user_msg)
        plan = self._parse_plan(_extract_text(reply_msg))
        if plan is not None:
            return plan

        # 修复重试 1 次（Q5）
        logger.info("AgentScope 首轮输出不合法，走修复重试")
        repair_msg = Msg(
            name="user",
            role="user",
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "你刚才的最终输出不是合法 JSON（或 mode 字段不对）。"
                        "请只输出一个 JSON 对象：mode=GENERATE 或 FALLBACK，"
                        "不要包含任何其他文字和 markdown 围栏。"
                    ),
                )
            ],
        )
        reply_msg2 = await agent.reply(repair_msg)
        return self._parse_plan(_extract_text(reply_msg2))

    # ---------- 输入 / 输出解析 ----------

    @staticmethod
    def _compose_user_message(request: AgentRequest) -> str:
        ctx = request.context
        return (
            f"用户输入：{request.message}\n"
            f"当前 state：{request.state.value}\n"
            f"projectId：{ctx.projectId}\n"
            f"subjectRef：{ctx.subjectRef}\n"
            f"subjectId：{ctx.subjectId}\n"
            f"designSessionId：{ctx.designSessionId}\n"
            f"baseDefinitionRevision：{ctx.baseDefinitionRevision}"
        )

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

        try:
            built = runner(
                reportName=plan.get("reportName") or "能耗日报",
                roomId=plan.get("roomId") or request.context.subjectId,
                includeTrend=bool(plan.get("includeTrend", True)),
            )
            policy = ReportPolicyDraft.model_validate(built["policyDraft"])
            definition = ReportDefinitionDraft.model_validate(built["definitionDraft"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("草稿组装或校验失败：%s", exc)
            return self._fallback.handle(request)

        reply = plan.get("reply") or f"已生成 {policy.name} 草稿，请确认。"
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
