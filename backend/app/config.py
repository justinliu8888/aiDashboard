"""后端运行时配置：从环境变量读取，`.env` 已被 .gitignore 保护。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 加载项目 backend/.env（如果存在）
_BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_DIR / ".env")


class Settings:
    """运行时配置。"""

    # Agent 实现选择：fake / agentscope
    agent_impl: str = os.getenv("AGENT_IMPL", "fake").lower()

    # 火山方舟
    volc_api_key: str = os.getenv("VOLC_API_KEY", "")
    volc_base_url: str = os.getenv(
        "VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    )
    volc_model: str = os.getenv("VOLC_MODEL", "doubao-seed-1-6-250615")
    volc_temperature: float = float(os.getenv("VOLC_TEMPERATURE", "0.2"))
    volc_timeout: int = int(os.getenv("VOLC_TIMEOUT", "30"))

    # ContextEngine 两段式模型：轻量接待员 + 重路由器
    volc_context_concierge_model: str = os.getenv(
        "VOLC_CONTEXT_CONCIERGE_MODEL", os.getenv("VOLC_MODEL", "doubao-seed-1-6-250615")
    )
    volc_context_router_model: str = os.getenv(
        "VOLC_CONTEXT_ROUTER_MODEL", os.getenv("VOLC_MODEL", "doubao-seed-1-6-250615")
    )
    volc_context_concierge_timeout: int = int(os.getenv("VOLC_CONTEXT_CONCIERGE_TIMEOUT", "30"))
    volc_context_router_timeout: int = int(os.getenv("VOLC_CONTEXT_ROUTER_TIMEOUT", "30"))

    # 方案设计（#19）的专用时限。完整方案 JSON 比短问答慢得多
    # （实测真实模型约 45 秒），复用 volc_timeout（30 秒）会稳定超时。
    volc_design_timeout: int = int(os.getenv("VOLC_DESIGN_TIMEOUT", "120"))

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

    # 访问令牌：留空则不校验（仅限 localhost 使用）；挂公网 Tunnel 时必须设置
    access_token: str = os.getenv("AI_ACCESS_TOKEN", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
