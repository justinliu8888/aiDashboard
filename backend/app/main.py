"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.access import DemoAccessMiddleware
from app.config import get_settings
from app.routers import agent as agent_router
from app.routers import drafts as drafts_router
from app.routers import edit_experiences as edit_experiences_router
from app.routers import feedback as feedback_router
from app.routers import mock_capability
from app.routers import report as report_router
from app.routers import sessions as sessions_router
from app.logging_config import configure_app_file_logging


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
try:
    configure_app_file_logging()
except OSError as exc:
    # 异常原文不进日志（#61）：留栈不留原文 —— 异常对象换成 None，
    # 消息正文仍是不含敏感信息的事实陈述。写文件失败不影响启动，
    # 继续用控制台日志。
    logging.getLogger(__name__).exception(
        "无法写入后端文件日志，继续使用控制台日志",
        exc_info=(type(exc), None, exc.__traceback__),
    )

app = FastAPI(title="智能报表 AI 后端", version="1.0.0")
settings = get_settings()

# 本地联调允许任意来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if settings.require_access_token or settings.access_token else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
class _FeedbackSchemaMiddleware:
    """第一次 ``/api/`` 请求前把反馈表建好（幂等迁移）。

    为什么放中间件而不是 import 时：建表是 DDL，**会隐式结束事务** ——
    在别人的事务里建表会把那笔事务提前提交掉。中间件跑在路由之前，
    那时没有事务。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/api/"):
            try:
                from app.services.edit_experience_service import (
                    init_edit_experience_storage,
                )
                from app.services.feedback_service import init_feedback_storage
                from app.services.session_service import session_service

                init_feedback_storage(session_service.repository)
                # 编辑经验表（20261105）：与反馈表同一套迁移入口。
                init_edit_experience_storage(session_service.repository)
            except Exception:  # noqa: BLE001 — 建表失败不该把服务搞挂
                logging.getLogger(__name__).warning(
                    "[feedback] 反馈/经验表初始化失败，反馈与经验功能暂不可用"
                )
        await self.app(scope, receive, send)


app.add_middleware(
    DemoAccessMiddleware,
    token=settings.access_token,
    require_token=settings.require_access_token,
    model_configured=bool(settings.volc_api_key) or settings.agent_impl == "agentscope",
)
# 建表放在 Demo 鉴权**之内**：未授权的请求连表都碰不到。
app.add_middleware(_FeedbackSchemaMiddleware)

app.include_router(agent_router.router)
app.include_router(mock_capability.router)
app.include_router(report_router.router)
app.include_router(sessions_router.router)
# 草稿工作区（契约 0002 §7）：打开 / 保存 / 撤销 / 重做 / 另存为。
# 它是草稿级入口，**不替代**对话里的候选 → 接受那条提交链路。
app.include_router(drafts_router.router)
# 反馈收件箱（20261104）：提交反馈 / 客户端事件 / 维护者管理。
# 它**不改变**任何报表业务状态，只是往反馈自己的表里写。
app.include_router(feedback_router.router)
# 编辑经验（20261105）：待审核执行记录 + 可信经验审核。
# 同样**不改变**报表业务状态 —— 归档由接受路径在同一事务里写。
app.include_router(edit_experiences_router.router)


# 能力总目录（20261105）：启动时核对并生成。
#
# 生成失败 → **启动报错**，不静默用旧的手写说明：
# 目录过期时模型会按一份已经不认识的参数去调工具。
def _sync_capability_catalog() -> None:
    from app.services.capability_catalog_service import ensure_catalog_fresh

    version = ensure_catalog_fresh(force=True)
    logging.getLogger(__name__).info(
        "[capability_catalog] 能力总目录已就绪 version=%s", version
    )


try:
    _sync_capability_catalog()
except Exception as exc:  # noqa: BLE001
    logging.getLogger(__name__).error(
        "[capability_catalog] 能力总目录生成失败，服务不启动：%s",
        type(exc).__name__,
        exc_info=(type(exc), None, exc.__traceback__),
    )
    raise


# 挂载前端静态目录：backend/../frontend
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
_WORKBENCH_URL = "/playground/pages/workbench.html"


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url=_WORKBENCH_URL)


@app.get("/playground")
@app.get("/playground/")
def playground_root() -> RedirectResponse:
    return RedirectResponse(url=_WORKBENCH_URL)


class _NoCacheStaticFiles(StaticFiles):
    """开发期静态资源：**不缓存**。

    工作台的前端脚本改得很勤。浏览器对没有 ``Cache-Control`` 的静态资源
    会按启发式规则缓存（甚至连 HTML 都缓存），于是「改了没生效」「调试
    代码跑的是旧版」这类问题极难查 —— 明明服务端已经是新文件，页面还在
    跑旧脚本。

    这里统一回 ``no-store``：改完刷新就是新的。生产部署另有 CDN / 缓存
    策略，不受这里影响。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


if _FRONTEND_DIR.exists():
    app.mount(
        "/playground",
        _NoCacheStaticFiles(directory=str(_FRONTEND_DIR), html=True),
        name="playground",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
