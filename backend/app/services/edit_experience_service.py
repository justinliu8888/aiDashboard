"""编辑经验业务服务：归档待审记录、审核、检索、复用绑定。

边界（契约 0003 §3.5 / §3.6）：

* **只有「用户接受 + 正式事务成功」才产生 ``pending`` 记录** ——
  本模块**只提供接口**，由 T0 在正式提交成功的事务里调用
  ``record_pending_edit``；正式提交失败**不留成功记录**。
* **同一 ``planId`` 重复归档 → 幂等**（同一个 recordId，不重复插入）。
* **已停止 / 取消的任务 → 不得迟到晋升**：``record_pending_edit`` 带
  ``task_status`` 守卫，取消 / 停止态直接不归档（返回空串）。
* **用户接受次数 ≠ 批准**：批准只能由管理员接口产生。
* 检索只返回 ``status='approved'``；``disabled`` **不返回**；最多 3 条。

本模块**不 import** ``session_service``（避免循环依赖）：连接与锁来自
调用方注入的仓库。需要仓库时用**惰性导入**取当前仓库（见
``_current_repository``）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from app.storage.edit_experience_store import (
    CONDITIONS_MAX_CHARS,
    METHOD_MAX_CHARS,
    REQUEST_MAX_CHARS,
    ROLE_NEW_BLOCK,
    ROLE_SOURCE_REGION,
    ROLE_TARGET_REGION,
    EditExperienceStore,
    NotFoundError,
    VersionConflictError,
    collect_instance_ids,
    redact_text,
    sanitize_structure,
    sanitize_text,
)

logger = logging.getLogger(__name__)


# ---------- 追踪服务（容错导入）----------
#
# T2 正在建 ``app/services/edit_trace_service.py``。**那个文件不是我的**，
# 所以这里容错导入：没有就跳过追踪（并在交付说明里标为待接缝）。

try:  # pragma: no cover — 取决于 T2 的进度
    from app.services import edit_trace_service
except ImportError:  # pragma: no cover — T2 还没落地
    edit_trace_service = None


# ---------- 常量 ----------

#: 检索上限（契约 §3.4：最多 3 条）。
MAX_SEARCH_RESULTS = 3

#: 任务状态守卫：这些状态**不归档**（已停止 / 已取消 → 不得迟到晋升）。
BLOCKED_TASK_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "stopped",
        "aborted",
        "failed",
        "timeout",
        "timedout",
        "error",
        "已取消",
        "已停止",
    }
)

#: 记录状态（契约 §3.2）。
RECORD_STATUSES = ("pending", "approved", "rejected")

#: 经验状态（契约 §3.2）。
EXPERIENCE_STATUSES = ("approved", "disabled")


class EditExperienceError(Exception):
    """编辑经验业务错误。``status`` 直接作为 HTTP 状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Atomic:
    """一段**原子**写入。

    * 自己开的账（调用方没有事务）→ 正常结束就提交，出错就回滚；
    * 别人开的账（调用方在事务里）→ 用 ``SAVEPOINT`` 兜住：
      成功 ``RELEASE``，出错 ``ROLLBACK TO``（**不动**调用方的事务）。

    批准一条记录要写两处（记录 + 经验）：**必须一起生效**，不能只生效一半。
    """

    def __init__(self, connection: Any, name: str = "exp_edit_experience") -> None:
        self._connection = connection
        self._name = name
        self._owns = not connection.in_transaction
        self._open = False

    def __enter__(self) -> "_Atomic":
        if not self._owns:
            self._connection.execute(f"SAVEPOINT {self._name}")
            self._open = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._owns:
            if exc_type is None:
                self._connection.commit()
            else:
                try:
                    self._connection.rollback()
                except Exception:  # noqa: BLE001 — 回滚失败不掩盖原始错误
                    logger.debug("[edit_experience] 回滚失败")
            return False
        if self._open:
            try:
                if exc_type is None:
                    self._connection.execute(f"RELEASE SAVEPOINT {self._name}")
                else:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {self._name}"
                    )
            except Exception:  # noqa: BLE001 — 兜底失败不掩盖原始错误
                logger.debug("[edit_experience] SAVEPOINT 收尾失败")
        return False


# ---------- 存储句柄 ----------

_store_lock = threading.Lock()
_store: EditExperienceStore | None = None
_store_repository: Any = None
#: ``init_edit_experience_storage`` 记下的仓库（session_service 取不到时的兜底）。
_fallback_repository: Any = None


def store_for(repository: Any) -> EditExperienceStore:
    """拿当前仓库对应的编辑经验存储。

    **不在 import 时创建默认数据库** —— 连接与锁来自传入的仓库。
    仓库换了（测试换库、服务重启换库）就跟着重建。
    """
    global _store, _store_repository
    with _store_lock:
        if _store is None or _store_repository is not repository:
            store = EditExperienceStore(
                connection=repository.conn,
                lock=repository.lock,
            )
            try:
                store.ensure_schema()
            except Exception as exc:  # noqa: BLE001 — 建表失败不该把服务搞挂
                logger.warning(
                    "[edit_experience] 建表失败 errorType=%s", type(exc).__name__
                )
            _store = store
            _store_repository = repository
        return _store


def init_edit_experience_storage(repository: Any) -> None:
    """启动时把编辑经验表建好（幂等迁移）。

    **必须在事务外调用**：建表是 DDL，会隐式结束事务。
    路由中间件在第一次 ``/api/`` 请求前调它 —— 那时没有事务。
    """
    global _fallback_repository
    _fallback_repository = repository
    store = store_for(repository)
    store.ensure_schema()


def _current_repository() -> Any:
    """当前仓库。

    优先取**活的** ``session_service.repository``（测试会换库，缓存会过期）；
    取不到才用 ``init_edit_experience_storage`` 记下的兜底仓库。
    """
    try:
        from app.services.session_service import session_service

        repository = session_service.repository
        if repository is not None:
            return repository
    except Exception:  # noqa: BLE001 — 惰性导入失败就用兜底
        pass
    if _fallback_repository is None:
        raise EditExperienceError(503, "编辑经验存储尚未初始化。")
    return _fallback_repository


def _current_store() -> EditExperienceStore:
    return store_for(_current_repository())


# ---------- 追踪 ----------

def _redact(value: Any) -> Any:
    """追踪用的脱敏：优先用追踪服务自己的 ``redact``。"""
    if edit_trace_service is not None:
        redact = getattr(edit_trace_service, "redact", None)
        if callable(redact):
            try:
                return redact(value)
            except Exception:  # noqa: BLE001 — 脱敏失败不该掀翻主链路
                pass
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def trace_experience_event(stage: str, **fields: Any) -> dict[str, Any] | None:
    """写一条追踪事件。**追踪服务不在就跳过**（不掀翻主链路）。"""
    if edit_trace_service is None:
        return None
    try:
        return edit_trace_service.record(stage, **fields)
    except Exception as exc:  # noqa: BLE001 — 追踪失败不该掀翻主链路
        logger.debug(
            "[edit_experience] 追踪写入失败 stage=%s errorType=%s",
            stage,
            type(exc).__name__,
        )
        return None


def record_reuse_event(
    *,
    decision: str,
    experience_id: str,
    reason: str = "",
    method: str = "",
    capability_version: str = "",
) -> dict[str, Any] | None:
    """记录一次**经验复用**事件（采用 / 放弃 + 理由）。

    契约 §3.6：检索 / 复用事件进追踪日志（``stage='experience'``），
    **标明采用或放弃及理由**。
    """
    return trace_experience_event(
        "experience",
        name="edit_experience_reuse",
        params=_redact(
            {
                "decision": decision,
                "experienceId": experience_id,
                "reason": reason,
                "capabilityVersion": capability_version,
            }
        ),
        result=_redact({"method": method}),
        status="ok",
    )


# ---------- 归档（待审记录）----------

def record_pending_edit(
    *,
    session_id: str,
    draft_id: str,
    plan_id: str,
    item_id: str,
    original_request: str,
    supplement: str,
    operations: Any,
    result: Any,
    capability_version: str,
    task_status: str | None = None,
) -> str:
    """归档一条**待审核**记录。返回 ``record_id``。

    * **只在「用户接受 + 正式事务成功」后调用**（T0 在成功的事务里调）。
    * **幂等**：同一个 ``plan_id`` 重复调用 → 返回**同一个** record_id，
      **不产生重复记录**，也不覆盖已有内容。
    * **守卫**：``task_status`` 落在 ``BLOCKED_TASK_STATUSES``（已停止 /
      已取消 / 失败）→ **不归档**，返回空串。
    * **脱敏**：正文截断；操作清单里的具体区块 ID **换成目标角色**。

    事务语义：调用方**已经在事务里**时，本函数只写不提交（跟着调用方
    一起回滚）；调用方没有事务时，本函数自己提交。两种情况下语义都是
    「正式提交成功才有记录」。
    """
    if task_status and str(task_status).strip().lower() in BLOCKED_TASK_STATUSES:
        logger.info(
            "[edit_experience] 任务已停止/取消，不归档 sessionId=%s planId=%s",
            session_id,
            plan_id,
        )
        return ""

    store = _current_store()
    identifiers = collect_instance_ids(operations)
    request_text = sanitize_text(
        original_request, limit=REQUEST_MAX_CHARS, extra_ids=identifiers
    )
    supplement_text = sanitize_text(
        supplement, limit=REQUEST_MAX_CHARS, extra_ids=identifiers
    )
    operations_payload = sanitize_structure(operations)
    result_payload = sanitize_structure(result)

    connection = store.connection
    owns_transaction = not connection.in_transaction
    record_id = ""
    row, _created = store.insert_record(
        record_id=_new_record_id(),
        session_id=session_id or "",
        draft_id=draft_id or "",
        plan_id=plan_id or "",
        item_id=item_id or "",
        original_request=request_text,
        supplement=supplement_text,
        operations_json=_dump(operations_payload, "[]"),
        result_json=_dump(result_payload, "{}"),
        capability_version=capability_version or "",
        status="pending",
        commit=owns_transaction,
    )
    record_id = str(row.get("record_id") or "")
    if owns_transaction:
        connection.commit()
    logger.info(
        "[edit_experience] 归档待审记录 recordId=%s planId=%s status=%s",
        record_id,
        plan_id,
        row.get("status"),
    )
    return record_id


def record_failed_event(
    *,
    session_id: str = "",
    draft_id: str = "",
    plan_id: str = "",
    item_id: str = "",
    original_request: str = "",
    reason: str = "",
    capability_version: str = "",
) -> str:
    """记一条**失败诊断**（不是可复用经验）。

    正式提交失败 → **不留成功记录**；这里只留一条 ``rejected`` 状态的
    诊断记录，方便管理员看「这类要求为什么没成」。
    """
    store = _current_store()
    connection = store.connection
    owns_transaction = not connection.in_transaction
    row, _created = store.insert_record(
        record_id=_new_record_id(),
        session_id=session_id or "",
        draft_id=draft_id or "",
        plan_id="",
        item_id=item_id or "",
        original_request=sanitize_text(original_request, limit=REQUEST_MAX_CHARS),
        supplement="",
        operations_json="[]",
        result_json=_dump({"failed": True, "reason": reason}, "{}"),
        capability_version=capability_version or "",
        status="rejected",
        reason=sanitize_text(reason, limit=REQUEST_MAX_CHARS),
        commit=owns_transaction,
    )
    if owns_transaction:
        connection.commit()
    return str(row.get("record_id") or "")


# ---------- 读取 ----------

def list_records(
    *, status: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """执行记录列表（分页 + 状态过滤）。"""
    if status and status not in RECORD_STATUSES:
        raise EditExperienceError(422, f"记录状态取值不合法：{status}")
    store = _current_store()
    rows, _total = store.list_records(status=status, limit=limit, offset=offset)
    return [_public_record(row) for row in rows]


def count_records(*, status: str | None = None) -> int:
    """执行记录总条数（给分页用）。"""
    store = _current_store()
    _rows, total = store.list_records(status=status, limit=0, offset=0)
    return int(total)


def get_record(record_id: str) -> dict[str, Any] | None:
    """单条执行记录。找不到 → ``None``。"""
    if not record_id:
        return None
    store = _current_store()
    row = store.get_record(record_id)
    return _public_record(row) if row else None


def list_experiences(
    *, include_disabled: bool = False, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """可信经验列表。``include_disabled=True`` 时连禁用的一起给。"""
    store = _current_store()
    rows, _total = store.list_experiences(
        include_disabled=include_disabled, limit=limit, offset=offset
    )
    return [_public_experience(row) for row in rows]


def count_experiences(*, include_disabled: bool = False) -> int:
    """可信经验总条数（给分页用）。"""
    store = _current_store()
    _rows, total = store.list_experiences(
        include_disabled=include_disabled, limit=0, offset=0
    )
    return int(total)


def get_experience_status(experience_id: str) -> str | None:
    """取经验的当前状态（``approved`` / ``disabled``）。找不到 → ``None``。

    **采用前必须复查**：检索结果是**那一刻**的快照，管理员随时可能禁用。
    """
    if not experience_id:
        return None
    store = _current_store()
    row = store.get_experience(experience_id)
    return str(row.get("status") or "") if row else None


# ---------- 审核 ----------

def approve_record(
    *,
    record_id: str,
    method: str,
    conditions: str = "",
    expected_version: int,
    reviewer: str,
) -> dict[str, Any]:
    """批准一条待审记录 → 生成一条**可信经验**。

    * ``expected_version`` 与当前版本不符 → **409，数据不变**；
    * 记录不存在 → **404**；
    * 已审核过的记录 → **409**（不重复批准）；
    * ``method`` 会做**可移植化**：具体区块 ID → 目标角色，凭证 → 脱敏。
    """
    if not record_id:
        raise EditExperienceError(404, "记录不存在。")
    if not (method or "").strip():
        raise EditExperienceError(422, "方法不能为空。")

    store = _current_store()
    row = store.get_record(record_id)
    if row is None:
        raise EditExperienceError(404, "记录不存在。")
    if str(row.get("status") or "") != "pending":
        raise EditExperienceError(409, "这条记录已经审核过了。")
    if int(row.get("version") or 0) != int(expected_version):
        raise EditExperienceError(409, "记录已被他人修改，请刷新后重试。")

    identifiers = collect_instance_ids(_load_json(row.get("operations_json"), []))
    portable_method = sanitize_text(
        method, limit=METHOD_MAX_CHARS, extra_ids=identifiers
    )
    portable_conditions = sanitize_text(
        conditions, limit=CONDITIONS_MAX_CHARS, extra_ids=identifiers
    )
    capability_version = str(row.get("capability_version") or "")

    connection = store.connection
    try:
        with _Atomic(connection):
            updated = store.update_record_status(
                record_id=record_id,
                status="approved",
                reviewer=reviewer or "",
                expected_version=int(expected_version),
                reason=None,
                commit=False,
            )
            experience = store.insert_experience(
                experience_id=_new_experience_id(),
                record_id=record_id,
                method=portable_method,
                conditions=portable_conditions,
                capability_version=capability_version,
                status="approved",
                reviewer=reviewer or "",
                commit=False,
            )
    except VersionConflictError:
        raise EditExperienceError(409, "记录已被他人修改，请刷新后重试。") from None
    except NotFoundError:
        raise EditExperienceError(404, "记录不存在。") from None

    logger.info(
        "[edit_experience] 管理员 %s 批准了记录 %s → 经验 %s",
        reviewer,
        record_id,
        experience.get("experience_id"),
    )
    return {
        "record": _public_record(updated),
        "experience": _public_experience(experience),
    }


def reject_record(
    *,
    record_id: str,
    reason: str = "",
    expected_version: int,
    reviewer: str,
) -> dict[str, Any]:
    """拒绝一条待审记录。**不产生可复用经验**。

    * 版本不符 → **409，数据不变**；记录不存在 → **404**；
    * 已审核过 → **409**。
    """
    if not record_id:
        raise EditExperienceError(404, "记录不存在。")
    store = _current_store()
    row = store.get_record(record_id)
    if row is None:
        raise EditExperienceError(404, "记录不存在。")
    if str(row.get("status") or "") != "pending":
        raise EditExperienceError(409, "这条记录已经审核过了。")
    if int(row.get("version") or 0) != int(expected_version):
        raise EditExperienceError(409, "记录已被他人修改，请刷新后重试。")

    connection = store.connection
    try:
        with _Atomic(connection):
            updated = store.update_record_status(
                record_id=record_id,
                status="rejected",
                reviewer=reviewer or "",
                expected_version=int(expected_version),
                reason=sanitize_text(reason, limit=REQUEST_MAX_CHARS) or None,
                commit=False,
            )
    except VersionConflictError:
        raise EditExperienceError(409, "记录已被他人修改，请刷新后重试。") from None
    except NotFoundError:
        raise EditExperienceError(404, "记录不存在。") from None

    logger.info(
        "[edit_experience] 管理员 %s 拒绝了记录 %s", reviewer, record_id
    )
    return {"record": _public_record(updated)}


def update_experience(
    *,
    experience_id: str,
    method: str | None = None,
    conditions: str | None = None,
    disabled: bool | None = None,
    expected_version: int,
    reviewer: str,
) -> dict[str, Any]:
    """整理 / 禁用一条经验。

    * ``expected_version`` 不符 → **409，数据不变**；
    * 不存在 → **404**；
    * ``method`` / ``conditions`` 同样做可移植化与脱敏。
    """
    if not experience_id:
        raise EditExperienceError(404, "经验不存在。")
    store = _current_store()
    row = store.get_experience(experience_id)
    if row is None:
        raise EditExperienceError(404, "经验不存在。")
    if int(row.get("version") or 0) != int(expected_version):
        raise EditExperienceError(409, "经验已被他人修改，请刷新后重试。")

    identifiers: dict[str, str] = {}
    source_record_id = str(row.get("record_id") or "")
    if source_record_id:
        source = store.get_record(source_record_id)
        if source:
            identifiers = collect_instance_ids(
                _load_json(source.get("operations_json"), [])
            )
    portable_method = (
        sanitize_text(method, limit=METHOD_MAX_CHARS, extra_ids=identifiers)
        if method is not None
        else None
    )
    portable_conditions = (
        sanitize_text(conditions, limit=CONDITIONS_MAX_CHARS, extra_ids=identifiers)
        if conditions is not None
        else None
    )

    connection = store.connection
    try:
        with _Atomic(connection):
            updated = store.update_experience(
                experience_id=experience_id,
                method=portable_method,
                conditions=portable_conditions,
                disabled=disabled,
                reviewer=reviewer or "",
                expected_version=int(expected_version),
                commit=False,
            )
    except VersionConflictError:
        raise EditExperienceError(409, "经验已被他人修改，请刷新后重试。") from None
    except NotFoundError:
        raise EditExperienceError(404, "经验不存在。") from None

    logger.info(
        "[edit_experience] 管理员 %s 更新了经验 %s（禁用=%s）",
        reviewer,
        experience_id,
        disabled,
    )
    return {"experience": _public_experience(updated)}


# ---------- 检索 ----------

def search_experiences(*, query: str, limit: int = 3) -> list[dict[str, Any]]:
    """检索已批准且未禁用的经验。**最多 3 条**。

    * 只返回 ``status='approved'``；``disabled`` **不返回**；
    * 无匹配 → **返回空列表**（正常继续，**不报错**）；
    * 技术错误 → 抛明确异常（调用方据此记 ``tool_error``），
      **不能冒充「没有相似案例」**。

    返回项只含：id / method / conditions / capabilityVersion / score /
    来源 recordId。
    """
    limit = _clamp_limit(limit)
    tokens = _tokenize(query)
    if not tokens:
        return []

    store = _current_store()
    rows = store.search_candidates(tokens=tokens)
    if not rows:
        # 兜底：分词没命中不代表没有（中文分词边界），再扫一遍已批准经验。
        rows = store.list_approved()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        score = _score(row, tokens)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("updated_at") or "")))

    results: list[dict[str, Any]] = []
    for score, row in scored[:limit]:
        results.append(
            {
                "id": str(row.get("experience_id") or ""),
                "method": str(row.get("method") or ""),
                "conditions": str(row.get("conditions") or ""),
                "capabilityVersion": str(row.get("capability_version") or ""),
                "score": round(float(score), 3),
                "recordId": str(row.get("record_id") or ""),
            }
        )
    return results


def bind_experience_to_target(
    experience: dict[str, Any], target_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把经验里的**角色**绑到当前目标上。

    契约 §3.6：经验内容里的区块 ID **不得直接照搬** —— 服务端返回的是
    角色（``targetRegion`` / ``newBlock``），复用时由这里换成**当前**
    草稿里的真实 ID。

    ``target_context`` 形如 ``{"targetRegionId": "region_abc"}``；
    缺哪个角色就**保留角色词**（那仍然是合法的写法）。
    """
    context = target_context or {}
    bound = dict(experience or {})
    bound["method"] = _bind_text(str(experience.get("method") or ""), context)
    bound["conditions"] = _bind_text(str(experience.get("conditions") or ""), context)
    bound["bound"] = True
    return bound


def _bind_text(text: str, context: dict[str, Any]) -> str:
    """角色词 → 当前目标 ID（长的先替换，避免互相吃掉）。"""
    if not text:
        return text
    result = text
    for role in (
        ROLE_SOURCE_REGION,
        "targetDataset",
        "targetChart",
        "targetTable",
        "targetWidget",
        "targetId",
        ROLE_TARGET_REGION,
        ROLE_NEW_BLOCK,
    ):
        replacement = context.get(role)
        if not replacement:
            replacement = context.get(_context_key(role))
        if replacement:
            result = result.replace(role, str(replacement))
    return result


def _context_key(role: str) -> str:
    """角色 → 上下文里的键名（``targetRegion`` → ``targetRegionId``）。"""
    if role.endswith("Id"):
        return role
    return f"{role}Id"


# ---------- 内部工具 ----------

def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = MAX_SEARCH_RESULTS
    return max(1, min(value, MAX_SEARCH_RESULTS))


_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


def _tokenize(text: str) -> list[str]:
    """规范化关键词：英文按词、中文按**二元组**。"""
    lowered = (text or "").lower()
    tokens: set[str] = set(_ASCII_TOKEN_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
            continue
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    return sorted(token for token in tokens if token)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _score(row: dict[str, Any], tokens: list[str]) -> float:
    """规范化关键词评分：方法命中权重高于适用条件。"""
    method = _normalize(str(row.get("method") or ""))
    conditions = _normalize(str(row.get("conditions") or ""))
    if not method and not conditions:
        return 0.0
    hits = 0
    for token in tokens:
        if token in method:
            hits += 2
        elif token in conditions:
            hits += 1
    if not hits:
        return 0.0
    return hits / float(2 * len(tokens))


def _load_json(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _dump(value: Any, fallback: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return fallback


def _new_record_id() -> str:
    import uuid

    return f"rec_{uuid.uuid4().hex[:24]}"


def _new_experience_id() -> str:
    import uuid

    return f"exp_{uuid.uuid4().hex[:24]}"


def _public_record(row: dict[str, Any]) -> dict[str, Any]:
    """记录行 → 公开协议（camelCase）。"""
    return {
        "recordId": str(row.get("record_id") or ""),
        "sessionId": str(row.get("session_id") or ""),
        "draftId": str(row.get("draft_id") or ""),
        "planId": str(row.get("plan_id") or ""),
        "itemId": str(row.get("item_id") or ""),
        "originalRequest": str(row.get("original_request") or ""),
        "supplement": str(row.get("supplement") or ""),
        "operations": _load_json(row.get("operations_json"), []),
        "result": _load_json(row.get("result_json"), {}),
        "capabilityVersion": str(row.get("capability_version") or ""),
        "status": str(row.get("status") or ""),
        "reason": str(row.get("reason") or ""),
        "reviewer": str(row.get("reviewer") or ""),
        "createdAt": str(row.get("created_at") or ""),
        "reviewedAt": str(row.get("reviewed_at") or ""),
        "version": int(row.get("version") or 0),
    }


def _public_experience(row: dict[str, Any]) -> dict[str, Any]:
    """经验行 → 公开协议（camelCase）。"""
    return {
        "experienceId": str(row.get("experience_id") or ""),
        "recordId": str(row.get("record_id") or ""),
        "method": str(row.get("method") or ""),
        "conditions": str(row.get("conditions") or ""),
        "capabilityVersion": str(row.get("capability_version") or ""),
        "status": str(row.get("status") or ""),
        "reviewer": str(row.get("reviewer") or ""),
        "createdAt": str(row.get("created_at") or ""),
        "updatedAt": str(row.get("updated_at") or ""),
        "version": int(row.get("version") or 0),
    }


__all__ = [
    "BLOCKED_TASK_STATUSES",
    "EXPERIENCE_STATUSES",
    "EditExperienceError",
    "MAX_SEARCH_RESULTS",
    "RECORD_STATUSES",
    "bind_experience_to_target",
    "count_experiences",
    "count_records",
    "get_experience_status",
    "get_record",
    "init_edit_experience_storage",
    "list_experiences",
    "list_records",
    "record_failed_event",
    "record_pending_edit",
    "record_reuse_event",
    "reject_record",
    "approve_record",
    "search_experiences",
    "store_for",
    "trace_experience_event",
    "update_experience",
]
