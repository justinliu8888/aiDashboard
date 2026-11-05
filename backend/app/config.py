"""后端运行时配置：从环境变量读取，`.env` 已被 .gitignore 保护。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 加载项目 backend/.env（如果存在）
_BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_DIR / ".env")


#: 支持的 LLM provider。
PROVIDER_MINIMAX = "minimax"
PROVIDER_VOLCENGINE = "volcengine"

#: provider 取值别名。未知取值直接报错，不静默挑一个。
_PROVIDER_ALIASES = {
    "minimax": PROVIDER_MINIMAX,
    "volcengine": PROVIDER_VOLCENGINE,
    "volc": PROVIDER_VOLCENGINE,
    "ark": PROVIDER_VOLCENGINE,
    "doubao": PROVIDER_VOLCENGINE,
}


def _env(*names: str, default: str = "") -> str:
    """按顺序读第一个非空的环境变量。

    新的 provider 中立变量优先，旧的 ``VOLC_*`` 作为兼容回退：
    旧 ``.env`` 不改也能跑，显式切回方舟时也不用重配。
    """
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_int(*names: str, default: int) -> int:
    return int(_env(*names, default=str(default)))


def _env_float(*names: str, default: float) -> float:
    return float(_env(*names, default=str(default)))


class Settings:
    """运行时配置。"""

    # Agent 实现选择：fake / agentscope
    agent_impl: str = os.getenv("AGENT_IMPL", "fake").lower()

    # ===== LLM provider 统一配置 =====
    #
    # 当前运行配置：``minimax`` + ``MiniMax-M3``，思考显式关闭。
    # 显式切回火山方舟：``LLM_PROVIDER=volcengine``，其余走 ``VOLC_*``。
    # **不自动回退**：provider 没配好就明确失败，不悄悄换一家继续跑。
    llm_provider: str = os.getenv("LLM_PROVIDER", PROVIDER_MINIMAX).strip().lower()

    # 通用覆盖：任何 provider 都吃，优先级最高。
    llm_api_key: str = _env("LLM_API_KEY")
    llm_base_url: str = _env("LLM_BASE_URL")
    llm_model: str = _env("LLM_MODEL")

    # MiniMax（OpenAI 兼容协议）
    minimax_api_key: str = _env("MINIMAX_API_KEY")
    minimax_base_url: str = _env("MINIMAX_BASE_URL", default="https://api.minimax.cn/v1")
    minimax_model: str = _env("MINIMAX_MODEL", default="MiniMax-M3")

    # 火山方舟（保留显式切回路径；仅在 LLM_PROVIDER=volcengine 时生效）
    volc_api_key: str = _env("VOLC_API_KEY")
    volc_base_url: str = _env(
        "VOLC_BASE_URL", default="https://ark.cn-beijing.volces.com/api/v3"
    )
    volc_model: str = _env("VOLC_MODEL", default="doubao-seed-2-1-lite-260915")

    # 阶段模型覆盖：留空 = 跟随当前 provider 的默认模型。
    llm_context_concierge_model: str = _env("LLM_CONTEXT_CONCIERGE_MODEL")
    llm_context_router_model: str = _env("LLM_CONTEXT_ROUTER_MODEL")
    llm_design_model: str = _env("LLM_DESIGN_MODEL")
    # 旧名：只作为方舟路径下的阶段覆盖。
    volc_context_concierge_model: str = _env("VOLC_CONTEXT_CONCIERGE_MODEL")
    volc_context_router_model: str = _env("VOLC_CONTEXT_ROUTER_MODEL")

    # 通用参数（旧 ``VOLC_*`` 名继续认）
    llm_temperature: float = _env_float("LLM_TEMPERATURE", "VOLC_TEMPERATURE", default=0.2)
    llm_timeout: int = _env_int("LLM_TIMEOUT", "VOLC_TIMEOUT", default=30)
    # 方案设计（#19）的专用时限。完整方案 JSON 比短问答慢得多，
    # 复用通用超时（30 秒）会稳定超时。
    llm_design_timeout: int = _env_int(
        "LLM_DESIGN_TIMEOUT", "VOLC_DESIGN_TIMEOUT", default=120
    )
    llm_context_concierge_timeout: int = _env_int(
        "LLM_CONTEXT_CONCIERGE_TIMEOUT", "VOLC_CONTEXT_CONCIERGE_TIMEOUT", default=30
    )
    llm_context_router_timeout: int = _env_int(
        "LLM_CONTEXT_ROUTER_TIMEOUT", "VOLC_CONTEXT_ROUTER_TIMEOUT", default=30
    )

    # 可靠性
    llm_repair_retries: int = int(os.getenv("LLM_REPAIR_RETRIES", "1"))

    # 模型诊断（#57）：失败诊断记录落在**受限本机目录**——
    # 仅当前用户可读写，不在前端公开目录、不在 Git 仓库、不进 API 响应。
    diagnostics_enabled: bool = os.getenv(
        "AI_DIAGNOSTICS_ENABLED", "true"
    ).lower() not in {"0", "false", "no", "off"}
    diagnostics_dir: Path = Path(
        os.getenv(
            "AI_DIAGNOSTICS_DIR",
            str(Path.home() / "Library/Logs/haewu-ai-report/diagnostics"),
        )
    ).expanduser()
    # 单条诊断记录上限（字节）、诊断目录总容量上限（字节）、保留期限（天）。
    diagnostics_max_record_bytes: int = int(
        os.getenv("AI_DIAGNOSTICS_MAX_RECORD_BYTES", str(64 * 1024))
    )
    diagnostics_max_total_bytes: int = int(
        os.getenv("AI_DIAGNOSTICS_MAX_TOTAL_BYTES", str(10 * 1024 * 1024))
    )
    diagnostics_retention_days: int = int(
        os.getenv("AI_DIAGNOSTICS_RETENTION_DAYS", "7")
    )
    # 精确原文模式：**默认关闭**。开启还要同时满足「未过期」与「在范围内」，
    # 且凭据样式的内容永远被抹掉。本轮只实现开关，不开启。
    diagnostics_exact_content: bool = os.getenv(
        "AI_DIAGNOSTICS_EXACT_CONTENT", "false"
    ).lower() in {"1", "true", "yes", "on"}
    diagnostics_exact_content_until: str = os.getenv(
        "AI_DIAGNOSTICS_EXACT_CONTENT_UNTIL", ""
    )
    diagnostics_exact_content_scope: str = os.getenv(
        "AI_DIAGNOSTICS_EXACT_CONTENT_SCOPE", ""
    )

    # ReAct 预算：AgentScope 默认 max_iters=50，报表场景用不了那么多步
    react_max_iters: int = int(os.getenv("REACT_MAX_ITERS", "6"))
    react_max_iters_edit: int = int(os.getenv("REACT_MAX_ITERS_EDIT", "4"))
    #: 能力格式（动态操作清单 + 整批试做 + 同一轮修正）的预算。
    #:
    #: 这条路要够：读草稿 → 查维度 → 提交（试做失败）→ 改正后再提交。
    #: 仍然**有上限**：用完仍未通过就安全结束，原草稿不变。
    react_max_iters_edit_capability: int = int(
        os.getenv("REACT_MAX_ITERS_EDIT_CAPABILITY", "8")
    )
    react_max_iters_fix: int = int(os.getenv("REACT_MAX_ITERS_FIX", "5"))
    react_structured_output_grace_iters: int = int(
        os.getenv("REACT_STRUCTURED_OUTPUT_GRACE_ITERS", "2")
    )

    # Observation 裁剪：单次工具返回的条目上限，防止上下文爆炸
    observation_max_items: int = int(os.getenv("OBSERVATION_MAX_ITEMS", "20"))
    observation_error_max_chars: int = int(os.getenv("OBSERVATION_ERROR_MAX_CHARS", "200"))

    # 会话存储：SQLite 单文件，落在 backend/data/ 下
    session_db_path: Path = Path(
        os.getenv("AI_SESSION_DB_PATH", str(_BACKEND_DIR / "data" / "haewu.db"))
    ).expanduser()

    # 公网 Demo 使用浏览器 Basic Auth，用户名为 demo，令牌作为密码。
    access_token: str = os.getenv("AI_ACCESS_TOKEN", "")
    require_access_token: bool = os.getenv("AI_REQUIRE_ACCESS_TOKEN", "0").lower() in (
        "1", "true", "yes"
    )

    # ---------- 反馈收件箱（20261104）----------
    #
    # 维护者凭据**在部署环境配置**，不写前端、不提交仓库、不与 Demo 共用。
    # 三项缺任何一项 → 管理登录**安全关闭**（503），不是放行。
    feedback_admin_username: str = os.getenv("AI_FEEDBACK_ADMIN_USERNAME", "")
    #: 有盐的密码哈希（``pbkdf2_sha256$iterations$salt$hash``）。
    #: 本机生成：``python -m app.services.feedback_auth --generate-hash``
    feedback_admin_password_hash: str = os.getenv("AI_FEEDBACK_ADMIN_PASSWORD_HASH", "")
    #: 维护者会话 cookie 的签名密钥（HMAC-SHA256）。
    feedback_session_secret: str = os.getenv("AI_FEEDBACK_SESSION_SECRET", "")
    #: 私有截图目录。默认在**服务数据目录**里，**不在**公开静态目录。
    feedback_attachment_dir: Path = Path(
        os.getenv(
            "AI_FEEDBACK_ATTACHMENT_DIR",
            str(_BACKEND_DIR / "data" / "feedback_attachments"),
        )
    ).expanduser()
    #: 反馈与附件的保留期限（天）。
    feedback_retention_days: int = int(os.getenv("AI_FEEDBACK_RETENTION_DAYS", "180"))
    #: 匿名浏览器身份的保留期限（天）。它只是防重复，不是账号。
    feedback_visitor_ttl_days: int = int(
        os.getenv("AI_FEEDBACK_VISITOR_TTL_DAYS", "180")
    )
    #: 单张截图上限（字节）。
    feedback_max_attachment_bytes: int = int(
        os.getenv("AI_FEEDBACK_MAX_ATTACHMENT_BYTES", str(5 * 1024 * 1024))
    )
    #: 提交节流：窗口内最多提交几次。
    feedback_submit_rate_limit: int = int(
        os.getenv("AI_FEEDBACK_SUBMIT_RATE_LIMIT", "20")
    )
    feedback_submit_rate_window_seconds: int = int(
        os.getenv("AI_FEEDBACK_SUBMIT_RATE_WINDOW_SECONDS", "600")
    )
    #: 维护者会话有效期（秒）。
    feedback_admin_session_seconds: int = int(
        os.getenv("AI_FEEDBACK_ADMIN_SESSION_SECONDS", str(8 * 3600))
    )

    # ---------- 解析后的有效配置 ----------
    #
    # 这一层是「当前到底在用哪家、哪个模型、怎么调」的唯一答案。
    # 调用方只读这里的解析结果，不自己拼 provider。

    @property
    def provider_name(self) -> str:
        """当前 provider 名称。未知取值直接报错，不静默挑一家。"""
        resolved = _PROVIDER_ALIASES.get(self.llm_provider)
        if resolved is None:
            raise ValueError(
                f"未知 LLM_PROVIDER={self.llm_provider!r}，"
                f"支持：{PROVIDER_MINIMAX} / {PROVIDER_VOLCENGINE}"
            )
        return resolved

    @property
    def provider_is_volcengine(self) -> bool:
        return self.provider_name == PROVIDER_VOLCENGINE

    @property
    def active_api_key(self) -> str:
        if self.llm_api_key:
            return self.llm_api_key
        return self.volc_api_key if self.provider_is_volcengine else self.minimax_api_key

    @property
    def active_base_url(self) -> str:
        """模型接口的 **API 根地址**（不含 /chat/completions）。

        OpenAI SDK 会自己拼路径；把完整 URL 当 base_url 传会拼出
        ``.../chat/completions/chat/completions``。
        """
        if self.llm_base_url:
            return self.llm_base_url
        return self.volc_base_url if self.provider_is_volcengine else self.minimax_base_url

    @property
    def active_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return self.volc_model if self.provider_is_volcengine else self.minimax_model

    @property
    def context_concierge_model(self) -> str:
        if self.llm_context_concierge_model:
            return self.llm_context_concierge_model
        if self.provider_is_volcengine and self.volc_context_concierge_model:
            return self.volc_context_concierge_model
        return self.active_model

    @property
    def context_router_model(self) -> str:
        if self.llm_context_router_model:
            return self.llm_context_router_model
        if self.provider_is_volcengine and self.volc_context_router_model:
            return self.volc_context_router_model
        return self.active_model

    @property
    def design_model(self) -> str:
        return self.llm_design_model or self.active_model

    @property
    def request_extra_body(self) -> dict[str, Any]:
        """请求体附加字段。

        MiniMax 默认会把思考过程内联进 content，直接 ``json.loads`` 会炸；
        显式 ``thinking.type=disabled`` 才是干净的纯 JSON。
        方舟不认这个字段，所以按 provider 区分。
        """
        if self.provider_is_volcengine:
            return {}
        return {"thinking": {"type": "disabled"}}

    @property
    def real_model_mode(self) -> bool:
        """真实模型模式（不是 fake 规则实现）。"""
        return self.agent_impl == "agentscope"

    # ---------- 旧名兼容 ----------
    #
    # 外部脚本与旧代码仍可能读这些名字，值与新的中立名一致。

    @property
    def volc_temperature(self) -> float:
        return self.llm_temperature

    @property
    def volc_timeout(self) -> int:
        return self.llm_timeout

    @property
    def volc_design_timeout(self) -> int:
        return self.llm_design_timeout

    @property
    def volc_context_concierge_timeout(self) -> int:
        return self.llm_context_concierge_timeout

    @property
    def volc_context_router_timeout(self) -> int:
        return self.llm_context_router_timeout

@lru_cache
def get_settings() -> Settings:
    return Settings()
