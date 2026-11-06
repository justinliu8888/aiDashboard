"""编辑经验管理接口：待审记录审核、可信经验整理 / 禁用。

路由表（契约 0003 §3.3 冻结）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | ``/admin/records`` | 待审 / 已审执行记录列表（分页 + status 过滤） |
| GET | ``/admin/records/{record_id}`` | 单条记录详情 |
| GET | ``/admin/experiences`` | 已批准经验列表 |
| POST | ``/admin/records/{record_id}/approve`` | 批准：``{method, conditions, expectedVersion}`` |
| POST | ``/admin/records/{record_id}/reject`` | 拒绝：``{reason?, expectedVersion}`` |
| PATCH | ``/admin/experiences/{experience_id}`` | 整理 / 禁用：``{method?, conditions?, disabled?, expectedVersion}`` |

鉴权**复用** ``app/services/feedback_auth.py``（``admin_configured`` /
``read_admin_username`` / ``request_origin_ok``）：**不另建一套密码逻辑**，
也不动 Demo 的 Basic Auth。

* 管理员**未配置** → **503**（管理入口安全关闭，不是放行）；
* **未登录** → **401**；
* **跨源写请求** → **403**；
* 权限拒绝**不改变数据**（拒绝发生在任何写入之前）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services import edit_experience_service
from app.services import feedback_auth
from app.services.edit_experience_service import EditExperienceError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/edit-experiences", tags=["edit-experiences"])

settings = get_settings()


# ---------- 请求体 ----------

class ApproveRecordRequest(BaseModel):
    """批准一条待审记录。

    ``method`` 是管理员整理的**可移植方法**（服务端会再做一次脱敏：
    具体区块 ID → 目标角色，凭证 → 打码）。
    """

    method: str = Field(min_length=1, description="管理员整理后的可复用方法")
    conditions: str = Field(default="", description="适用条件")
    expectedVersion: int = Field(ge=1, description="记录当前版本号（乐观锁）")


class RejectRecordRequest(BaseModel):
    """拒绝一条待审记录（不产生可复用经验）。"""

    reason: str | None = Field(default=None, description="拒绝理由")
    expectedVersion: int = Field(ge=1, description="记录当前版本号（乐观锁）")


class UpdateExperienceRequest(BaseModel):
    """整理 / 禁用一条已批准经验。"""

    method: str | None = Field(default=None, description="整理后的方法")
    conditions: str | None = Field(default=None, description="适用条件")
    disabled: bool | None = Field(default=None, description="true=禁用，false=恢复")
    expectedVersion: int = Field(ge=1, description="经验当前版本号（乐观锁）")


# ---------- 鉴权 ----------

def _not_configured_detail(request: Request) -> str:
    """管理入口没配置时的说明。**带上当前实例**。"""
    host = request.headers.get("host") or "未知主机"
    return (
        f"编辑经验管理入口未配置（当前实例 {host}）。"
        "需要设置 AI_FEEDBACK_ADMIN_USERNAME / "
        "AI_FEEDBACK_ADMIN_PASSWORD_HASH / AI_FEEDBACK_SESSION_SECRET。"
    )


def _require_same_origin(request: Request) -> None:
    """同源写请求保护。跨源 → 403。"""
    if not feedback_auth.request_origin_ok(request):
        raise HTTPException(status_code=403, detail="同源写请求保护：不接受跨站提交。")


def _require_admin(request: Request) -> str:
    """维护者鉴权。没配置 → 503；没登录 → 401。"""
    if not feedback_auth.admin_configured(settings):
        raise HTTPException(
            status_code=503, detail=_not_configured_detail(request)
        )
    username = feedback_auth.read_admin_username(request, settings)
    if not username:
        raise HTTPException(status_code=401, detail="请先登录管理员入口。")
    return username


def _fail(exc: EditExperienceError) -> HTTPException:
    """业务错误 → HTTP 状态码。**权限拒绝不改变数据**（写入之前就抛了）。"""
    return HTTPException(status_code=exc.status, detail=exc.message)


# ---------- 记录 ----------

@router.get("/admin/records")
def admin_list_records(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """执行记录列表：待审 / 已审都看得到，按新到旧。"""
    _require_admin(request)
    try:
        items = edit_experience_service.list_records(
            status=status, limit=limit, offset=offset
        )
        total = edit_experience_service.count_records(status=status)
    except EditExperienceError as exc:
        raise _fail(exc) from None
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/admin/records/{record_id}")
def admin_get_record(record_id: str, request: Request) -> dict[str, Any]:
    """单条记录详情（含脱敏后的操作清单与结果摘要）。"""
    _require_admin(request)
    record = edit_experience_service.get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在。")
    return record


@router.post("/admin/records/{record_id}/approve")
def admin_approve_record(
    record_id: str, payload: ApproveRecordRequest, request: Request
) -> dict[str, Any]:
    """批准一条待审记录 → 生成一条**可信经验**。

    * 版本不符 → **409**，**数据不变**；
    * 只有**管理员**能批准（用户接受次数 ≠ 批准）。
    """
    _require_same_origin(request)
    username = _require_admin(request)
    try:
        result = edit_experience_service.approve_record(
            record_id=record_id,
            method=payload.method,
            conditions=payload.conditions,
            expected_version=payload.expectedVersion,
            reviewer=username,
        )
    except EditExperienceError as exc:
        raise _fail(exc) from None
    logger.info(
        "[edit_experiences_router] 管理员 %s 批准了记录 %s", username, record_id
    )
    return {"ok": True, **result}


@router.post("/admin/records/{record_id}/reject")
def admin_reject_record(
    record_id: str, payload: RejectRecordRequest, request: Request
) -> dict[str, Any]:
    """拒绝一条待审记录。**不产生可复用经验**。"""
    _require_same_origin(request)
    username = _require_admin(request)
    try:
        result = edit_experience_service.reject_record(
            record_id=record_id,
            reason=payload.reason or "",
            expected_version=payload.expectedVersion,
            reviewer=username,
        )
    except EditExperienceError as exc:
        raise _fail(exc) from None
    logger.info(
        "[edit_experiences_router] 管理员 %s 拒绝了记录 %s", username, record_id
    )
    return {"ok": True, **result}


# ---------- 经验 ----------

@router.get("/admin/experiences")
def admin_list_experiences(
    request: Request,
    includeDisabled: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """已批准经验列表。``includeDisabled=true`` 时连禁用的一起给。"""
    _require_admin(request)
    items = edit_experience_service.list_experiences(
        include_disabled=includeDisabled, limit=limit, offset=offset
    )
    total = edit_experience_service.count_experiences(
        include_disabled=includeDisabled
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "includeDisabled": includeDisabled,
    }


@router.patch("/admin/experiences/{experience_id}")
def admin_update_experience(
    experience_id: str, payload: UpdateExperienceRequest, request: Request
) -> dict[str, Any]:
    """整理 / 禁用一条经验。

    * 版本不符 → **409**，**数据不变**；
    * 禁用的经验**检索不到**。
    """
    _require_same_origin(request)
    username = _require_admin(request)
    try:
        result = edit_experience_service.update_experience(
            experience_id=experience_id,
            method=payload.method,
            conditions=payload.conditions,
            disabled=payload.disabled,
            expected_version=payload.expectedVersion,
            reviewer=username,
        )
    except EditExperienceError as exc:
        raise _fail(exc) from None
    logger.info(
        "[edit_experiences_router] 管理员 %s 更新了经验 %s（禁用=%s）",
        username,
        experience_id,
        payload.disabled,
    )
    return {"ok": True, **result}


__all__ = ["router"]
