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

    # 公网 Demo 使用浏览器 Basic Auth，用户名为 demo，令牌作为密码。
    access_token: str = os.getenv("AI_ACCESS_TOKEN", "")
    require_access_token: bool = os.getenv("AI_REQUIRE_ACCESS_TOKEN", "0").lower() in (
        "1", "true", "yes"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
