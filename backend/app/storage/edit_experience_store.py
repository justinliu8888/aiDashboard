"""编辑经验存储：两张表 + 幂等建表 + 脱敏 + 乐观锁。

边界（契约 0003 §3.2）：

* 复用**会话库**的连接与锁（照 ``feedback_store`` 的做法）：本模块**不
  import** ``session_service``，连接与锁由调用方注入，测试可以换库。
* 建表是**幂等迁移**，且**不在别人的事务里跑**（DDL 会隐式结束事务）。
* **不持久化整份业务数据、凭证、他人区块 ID**：
  - 正文（要求 / 补充）**截断**；
  - 操作清单里的具体区块 ID **换成目标角色**（``targetRegion`` /
    ``newBlock``）—— 经验要能带着方法搬到另一份草稿上；
  - 凭证样式的键值一律 ``[redacted]``。
* approve / reject / disable 走**乐观锁**（``version`` 列）：版本不符 →
  **拒绝且数据不变**。

字段口径：契约 §3.2 列出的列是**核心列**，全部照抄；这里另加两列 ——
``version``（乐观锁，任务书要求 approve/reject 也校验 ``expectedVersion``）
与 ``reason``（拒绝理由；不留就等于把审核意见丢了）。两者都是**追加**，
不改契约里任何一个既有列的含义。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------- 角色占位符 ----------
#
# 经验里不写「别的草稿的区块 ID」，只写**角色**。复用时由服务端把角色
# 绑到当前目标上（``bind_experience_to_target``）。

ROLE_TARGET_REGION = "targetRegion"
ROLE_NEW_BLOCK = "newBlock"
ROLE_SOURCE_REGION = "sourceRegion"


# ---------- 截断上限（字符）----------

#: 原要求 / 补充要求：**只留够检索与人工判断的摘要**，不存整段正文。
REQUEST_MAX_CHARS = 500
#: 管理员整理的方法。
METHOD_MAX_CHARS = 2000
#: 适用条件。
CONDITIONS_MAX_CHARS = 1000
#: 操作清单 / 结果摘要的 JSON 文本上限。
JSON_MAX_CHARS = 4000


# ---------- 脱敏规则 ----------

#: 凭证样式的键：命中即 ``[redacted]``。
_CREDENTIAL_KEY_RE = re.compile(
    r"(passw(or)?d|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"credential|authorization|cookie|private[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)

#: 正文里的凭证样式：``password=xxx`` / ``Bearer xxx`` / ``sk-xxxx``。
_CREDENTIAL_TEXT_RES = (
    re.compile(
        r"(?i)\b(passw(or)?d|passwd|pwd|secret|token|api[_-]?key|"
        r"access[_-]?key|authorization)\b\s*[:=]\s*[\"']?[^\s\"',;]+"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
)

#: 正文里的「具体实例 ID」：``region_xxx`` / ``blk-xxx`` / ``section_9``。
#:
#: 必须带分隔符（``_`` / ``-``）或数字，**不会**命中角色词 ``targetRegion``。
_INSTANCE_ID_RE = re.compile(
    r"\b(?:region|rgn|blk|block|section|seg|dataset|chart)[-_][A-Za-z0-9][A-Za-z0-9_\-]*\b",
    re.IGNORECASE,
)

#: 角色词表：键名 → 角色。
_ROLE_BY_NOUN = (
    ("region", ROLE_TARGET_REGION),
    ("block", ROLE_NEW_BLOCK),
    ("section", ROLE_NEW_BLOCK),
    ("segment", ROLE_NEW_BLOCK),
    ("dataset", "targetDataset"),
    ("chart", "targetChart"),
    ("table", "targetTable"),
)


def _role_for_key(key: str) -> str:
    """键名 → 目标角色。**不是实例 ID 的键返回空串**（值原样保留）。"""
    low = (key or "").lower()
    if not low:
        return ""
    if "new" in low:
        return ROLE_NEW_BLOCK
    if "source" in low or "origin" in low or low.startswith("from"):
        return ROLE_SOURCE_REGION
    for noun, role in _ROLE_BY_NOUN:
        if noun in low:
            return role
    if low in {"id", "targetid", "refid", "objectid", "elementid"}:
        return "targetId"
    return ""


def _is_id_key(key: str) -> bool:
    """这个键是不是「指向某个具体实例」的键？"""
    low = (key or "").lower()
    if low in {"id", "targetid", "refid", "objectid", "elementid"}:
        return True
    return bool(_role_for_key(key))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def excerpt(text: str, limit: int) -> str:
    """截断正文（超出加省略号）。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def redact_text(text: str) -> str:
    """去掉正文里的凭证样式内容。"""
    value = text or ""
    for pattern in _CREDENTIAL_TEXT_RES:
        value = pattern.sub(lambda m: _redact_match(m), value)
    return value


def _redact_match(match: Any) -> str:
    text = match.group(0)
    if "=" in text:
        head, _, _ = text.partition("=")
        return f"{head}=[redacted]"
    if ":" in text:
        head, _, _ = text.partition(":")
        return f"{head}:[redacted]"
    return "[redacted]"


def sanitize_text(
    text: str,
    *,
    limit: int,
    extra_ids: dict[str, str] | None = None,
) -> str:
    """正文脱敏 + 截断。

    * 凭证样式 → ``[redacted]``；
    * 具体实例 ID → 目标角色（``targetRegion`` / ``newBlock`` …）；
    * 超长 → 截断。
    """
    value = redact_text((text or "").strip())
    for identifier, role in (extra_ids or {}).items():
        if identifier and identifier in value:
            value = value.replace(identifier, role)
    value = _INSTANCE_ID_RE.sub(lambda m: _role_for_key(m.group(0)), value)
    return excerpt(value, limit)


def collect_instance_ids(node: Any) -> dict[str, str]:
    """扫一遍结构化数据，收集「具体实例 ID → 角色」的映射。"""
    found: dict[str, str] = {}

    def _walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                role = _role_for_key(str(key))
                if role and isinstance(value, str) and value.strip():
                    found.setdefault(value.strip(), role)
                _walk(value)
        elif isinstance(item, list):
            for value in item:
                _walk(value)

    _walk(node)
    return found


def sanitize_structure(node: Any) -> Any:
    """结构化数据脱敏：凭证键 → ``[redacted]``，实例 ID → 目标角色。

    两遍走：先收集实例 ID，再统一替换 —— 免得「值藏在别的键里」漏网。
    """
    identifiers = collect_instance_ids(node)

    def _walk(item: Any) -> Any:
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            for key, value in item.items():
                key_text = str(key)
                if _CREDENTIAL_KEY_RE.search(key_text):
                    out[key_text] = "[redacted]"
                    continue
                if _is_id_key(key_text):
                    out[key_text] = _replace_ids(value, _role_for_key(key_text))
                    continue
                out[key_text] = _walk(value)
            return out
        if isinstance(item, list):
            return [_walk(value) for value in item]
        if isinstance(item, str):
            return _replace_ids(item, None)
        return item

    def _replace_ids(value: Any, role: str | None) -> Any:
        if isinstance(value, str):
            if role:
                return role
            text = value
            for identifier, identifier_role in identifiers.items():
                if identifier and identifier in text:
                    text = text.replace(identifier, identifier_role)
            return text
        if isinstance(value, list):
            return [_replace_ids(item, role) for item in value]
        if isinstance(value, dict):
            return {str(k): _replace_ids(v, None) for k, v in value.items()}
        return value

    return _walk(node)


def _dump_json(value: Any, *, fallback: str) -> str:
    """序列化成 JSON 文本（有界）。**不抛异常** —— 存不下的记空结构。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = fallback
    return excerpt(text, JSON_MAX_CHARS)


# ---------- 异常 ----------

class EditExperienceStoreError(Exception):
    """存储层错误基类。"""


class NotFoundError(EditExperienceStoreError):
    """目标行不存在。"""


class VersionConflictError(EditExperienceStoreError):
    """乐观锁版本不符 —— **数据不变**。"""


# ---------- 建表 ----------

_SCHEMA = """
-- 执行记录：一次「用户接受 + 正式提交成功」的待审案例。
-- 幂等键：plan_id（同一 planId 重复归档 → 同一条记录）。
CREATE TABLE IF NOT EXISTS edit_experience_records (
    record_id          TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL DEFAULT '',
    draft_id           TEXT NOT NULL DEFAULT '',
    plan_id            TEXT NOT NULL DEFAULT '',
    item_id            TEXT NOT NULL DEFAULT '',
    original_request   TEXT NOT NULL DEFAULT '',
    supplement         TEXT NOT NULL DEFAULT '',
    operations_json    TEXT NOT NULL DEFAULT '[]',
    result_json        TEXT NOT NULL DEFAULT '{}',
    capability_version TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    reason             TEXT,
    reviewer           TEXT,
    created_at         TEXT NOT NULL,
    reviewed_at        TEXT,
    version            INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_experience_records_plan
    ON edit_experience_records(plan_id) WHERE plan_id <> '';
CREATE INDEX IF NOT EXISTS idx_edit_experience_records_status
    ON edit_experience_records(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_edit_experience_records_created
    ON edit_experience_records(created_at DESC);

-- 可信经验：管理员整理过的**可移植**方法。
-- 只有 status='approved' 的行会被检索命中；'disabled' 不返回。
CREATE TABLE IF NOT EXISTS edit_experiences (
    experience_id      TEXT PRIMARY KEY,
    record_id          TEXT NOT NULL DEFAULT '',
    method             TEXT NOT NULL DEFAULT '',
    conditions         TEXT NOT NULL DEFAULT '',
    capability_version TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'approved',   -- approved | disabled
    reviewer           TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_experiences_record
    ON edit_experiences(record_id) WHERE record_id <> '';
CREATE INDEX IF NOT EXISTS idx_edit_experiences_status
    ON edit_experiences(status, updated_at DESC);
"""


# ---------- id ----------

def _new_record_id() -> str:
    return f"rec_{uuid.uuid4().hex[:24]}"


def _new_experience_id() -> str:
    return f"exp_{uuid.uuid4().hex[:24]}"


class EditExperienceStore:
    """编辑经验存储。连接与锁由外部注入（复用会话库）。"""

    def __init__(self, *, connection: sqlite3.Connection, lock: Any) -> None:
        self._conn = connection
        self._lock = lock or threading.RLock()
        self._schema_ready = False

    # ---------- 迁移 ----------

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> Any:
        return self._lock

    def ensure_schema(self) -> None:
        """幂等建表。

        **不在别人的事务里跑**：DDL 会隐式结束事务（``executescript``
        会先 commit）。所以事务进行中时直接跳过，等事务外再补。
        """
        with self._lock:
            if self._schema_ready:
                return
            if self._conn.in_transaction:
                logger.info("[edit_experience_store] 事务进行中，建表延后")
                return
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._schema_ready = True
            logger.info("[edit_experience_store] 编辑经验表已就绪")

    # ---------- 执行记录 ----------

    def insert_record(
        self,
        *,
        record_id: str,
        session_id: str = "",
        draft_id: str = "",
        plan_id: str = "",
        item_id: str = "",
        original_request: str = "",
        supplement: str = "",
        operations_json: str = "[]",
        result_json: str = "{}",
        capability_version: str = "",
        status: str = "pending",
        reason: str | None = None,
        created_at: str | None = None,
        commit: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """写入一条执行记录。返回 ``(记录行, 是否本次新建)``。

        **幂等**：同一个 ``plan_id`` 重复归档 → 返回**原来那条**，
        **不覆盖内容、不重复插入**。
        """
        created = created_at or now_iso()
        with self._lock:
            existing = self._find_record_by_dedupe(
                plan_id, session_id=session_id, item_id=item_id
            )
            if existing is not None:
                return existing, False
            try:
                self._conn.execute(
                    """
                    INSERT INTO edit_experience_records (
                        record_id, session_id, draft_id, plan_id, item_id,
                        original_request, supplement, operations_json, result_json,
                        capability_version, status, reason, created_at, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        record_id,
                        session_id or "",
                        draft_id or "",
                        plan_id or "",
                        item_id or "",
                        original_request or "",
                        supplement or "",
                        operations_json or "[]",
                        result_json or "{}",
                        capability_version or "",
                        status or "pending",
                        reason,
                        created,
                    ),
                )
                if commit:
                    self._conn.commit()
            except sqlite3.IntegrityError:
                # 并发下另一个写入者抢先落了同一个 planId → 取回那一条。
                existing = self._find_record_by_dedupe(
                    plan_id, session_id=session_id, item_id=item_id
                )
                if existing is None:
                    raise
                return existing, False
            row = self._conn.execute(
                "SELECT * FROM edit_experience_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        if row is None:  # pragma: no cover — 理论上不会发生
            raise EditExperienceStoreError("编辑经验记录写入失败")
        return dict(row), True

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._find_record(record_id)

    def _find_record(self, record_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM edit_experience_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        return dict(row) if row else None

    def find_record_by_plan(self, plan_id: str) -> dict[str, Any] | None:
        """按 ``plan_id`` 找记录。**空 plan_id 不参与幂等**。"""
        if not plan_id:
            return None
        with self._lock:
            return self._find_record_by_plan(plan_id)

    def _find_record_by_plan(self, plan_id: str) -> dict[str, Any] | None:
        if not plan_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM edit_experience_records WHERE plan_id = ? LIMIT 1",
            (plan_id,),
        ).fetchone()
        return dict(row) if row else None

    def find_record_by_dedupe(
        self, plan_id: str, *, session_id: str = "", item_id: str = ""
    ) -> dict[str, Any] | None:
        """按幂等键找记录。

        * 有 ``plan_id`` → 按它找（唯一索引兜底，硬保证）。
        * 没有 ``plan_id`` → 退到 ``session_id + item_id``（尽力而为）。
        """
        if plan_id:
            return self._find_record_by_plan(plan_id)
        if not session_id and not item_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM edit_experience_records "
            "WHERE plan_id = '' AND session_id = ? AND item_id = ? LIMIT 1",
            (session_id or "", item_id or ""),
        ).fetchone()
        return dict(row) if row else None

    def _find_record_by_dedupe(
        self, plan_id: str, *, session_id: str = "", item_id: str = ""
    ) -> dict[str, Any] | None:
        if plan_id:
            return self._find_record_by_plan(plan_id)
        if not session_id and not item_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM edit_experience_records "
            "WHERE plan_id = '' AND session_id = ? AND item_id = ? LIMIT 1",
            (session_id or "", item_id or ""),
        ).fetchone()
        return dict(row) if row else None

    def list_records(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM edit_experience_records {where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT * FROM edit_experience_records
                 {where}
                 ORDER BY created_at DESC, rowid DESC
                 LIMIT ? OFFSET ?
                """,
                (*params, int(limit), int(offset)),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def update_record_status(
        self,
        *,
        record_id: str,
        status: str,
        reviewer: str,
        expected_version: int,
        reason: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """改审核状态。**乐观锁**：版本不符 → 拒绝，**数据不变**。"""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE edit_experience_records
                   SET status = ?, reviewer = ?, reviewed_at = ?, reason = ?,
                       version = version + 1
                 WHERE record_id = ? AND version = ?
                """,
                (
                    status,
                    reviewer or "",
                    now_iso(),
                    reason,
                    record_id,
                    int(expected_version),
                ),
            )
            if cursor.rowcount == 0:
                if self._find_record(record_id) is None:
                    raise NotFoundError(record_id)
                raise VersionConflictError(record_id)
            if commit:
                self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM edit_experience_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return dict(row)

    # ---------- 可信经验 ----------

    def insert_experience(
        self,
        *,
        experience_id: str,
        record_id: str = "",
        method: str = "",
        conditions: str = "",
        capability_version: str = "",
        status: str = "approved",
        reviewer: str = "",
        created_at: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """写入一条可信经验。返回经验行。"""
        created = created_at or now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO edit_experiences (
                    experience_id, record_id, method, conditions, capability_version,
                    status, reviewer, created_at, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    experience_id,
                    record_id or "",
                    method or "",
                    conditions or "",
                    capability_version or "",
                    status or "approved",
                    reviewer or "",
                    created,
                    created,
                ),
            )
            if commit:
                self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM edit_experiences WHERE experience_id = ?",
                (experience_id,),
            ).fetchone()
        if row is None:  # pragma: no cover — 理论上不会发生
            raise EditExperienceStoreError("编辑经验写入失败")
        return dict(row)

    def get_experience(self, experience_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._find_experience(experience_id)

    def _find_experience(self, experience_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM edit_experiences WHERE experience_id = ?",
            (experience_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_experiences(
        self,
        *,
        include_disabled: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where = "" if include_disabled else "WHERE status = 'approved'"
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM edit_experiences {where}"
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT * FROM edit_experiences
                 {where}
                 ORDER BY updated_at DESC, rowid DESC
                 LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def update_experience(
        self,
        *,
        experience_id: str,
        method: str | None = None,
        conditions: str | None = None,
        disabled: bool | None = None,
        reviewer: str = "",
        expected_version: int,
        commit: bool = True,
    ) -> dict[str, Any]:
        """整理 / 禁用一条经验。**乐观锁**：版本不符 → 拒绝，**数据不变**。"""
        sets: list[str] = []
        params: list[Any] = []
        if method is not None:
            sets.append("method = ?")
            params.append(method)
        if conditions is not None:
            sets.append("conditions = ?")
            params.append(conditions)
        if disabled is not None:
            sets.append("status = ?")
            params.append("disabled" if disabled else "approved")
        if not sets:
            row = self._find_experience(experience_id)
            if row is None:
                raise NotFoundError(experience_id)
            return row
        sets.append("reviewer = ?")
        params.append(reviewer or "")
        sets.append("updated_at = ?")
        params.append(now_iso())
        sets.append("version = version + 1")
        params.append(experience_id)
        params.append(int(expected_version))
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE edit_experiences SET {', '.join(sets)} "
                "WHERE experience_id = ? AND version = ?",
                params,
            )
            if cursor.rowcount == 0:
                if self._find_experience(experience_id) is None:
                    raise NotFoundError(experience_id)
                raise VersionConflictError(experience_id)
            if commit:
                self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM edit_experiences WHERE experience_id = ?",
                (experience_id,),
            ).fetchone()
        return dict(row)

    # ---------- 检索候选 ----------

    def search_candidates(
        self, *, tokens: list[str], limit: int = 200
    ) -> list[dict[str, Any]]:
        """SQLite 文本检索：只取 ``status='approved'`` 的行。

        ``disabled`` **不在这里**（它不是 approved）。
        """
        clauses = ["status = 'approved'"]
        params: list[Any] = []
        if tokens:
            pieces: list[str] = []
            for token in tokens:
                pieces.append("method LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(token)}%")
                pieces.append("conditions LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(token)}%")
            clauses.append("(" + " OR ".join(pieces) + ")")
        sql = (
            "SELECT * FROM edit_experiences WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_approved(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """全部已批准经验（**不含 disabled**）。检索兜底用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM edit_experiences WHERE status = 'approved' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]


def _escape_like(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


__all__ = [
    "CONDITIONS_MAX_CHARS",
    "EditExperienceStore",
    "EditExperienceStoreError",
    "METHOD_MAX_CHARS",
    "NotFoundError",
    "REQUEST_MAX_CHARS",
    "ROLE_NEW_BLOCK",
    "ROLE_SOURCE_REGION",
    "ROLE_TARGET_REGION",
    "VersionConflictError",
    "collect_instance_ids",
    "excerpt",
    "now_iso",
    "redact_text",
    "sanitize_structure",
    "sanitize_text",
]
