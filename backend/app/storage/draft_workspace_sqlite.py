"""草稿工作区的 SQLite 适配层（契约 0002 §6.1）。

只做**存储动作**，不含业务语义：
    - 草稿对象：建 / 改 / 查 / 列；
    - 版本：写版本、读版本、删掉 redo 分支；
    - 历史：追加动作、读动作流水；
    - 任务：建任务、改状态与检查点；
    - 主题：读写共享主题（§8 的存储侧）；
    - 锁：同草稿写互斥。

事务纪律（重要）：
    这里的写方法**不自己 commit**。事务由调用方用 `sqlite_store` 的
    **共享事务入口**（`SqliteSessionRepository.transaction`）开，
    这样「写版本 + 写历史 + 推进撤销指针」才落在**同一笔事务**里，
    中途失败不会留下半份状态。

连接与锁从 `SqliteSessionRepository` 注入：**不另开连接、不另造锁**。
表结构复用 `sqlite_store._SCHEMA`（契约 §6.1 的 5 张表 + 锁表），
这里**不另抄一份 DDL**。
"""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any

from app.storage.sqlite_store import SqliteSessionRepository


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[draft_store] 草稿 JSON 解析失败，已丢弃")
        return None
    return parsed if isinstance(parsed, dict) else None


def _definition_theme_ref(definition: Any) -> str:
    """定义里的主题引用（``theme.themeRef``，兼容 ``theme.themeId``）。"""
    if not isinstance(definition, dict):
        return ""
    theme = definition.get("theme") or {}
    if not isinstance(theme, dict):
        return ""
    ref = theme.get("themeRef") or theme.get("themeId") or ""
    return str(ref or "").strip()


class DraftWorkspaceSqlite:
    """草稿工作区的 SQLite 实现（纯存储层）。"""

    def __init__(self, *, repository: SqliteSessionRepository) -> None:
        """注入存储仓库：连接、锁与共享事务入口都从它那里来。"""
        self._repo = repository
        self._lock = repository.lock
        #: ``draft_themes.default_style_json`` 是否已补过列（幂等补列只做一次）。
        self._theme_style_column_ready = False

    @property
    def repository(self) -> SqliteSessionRepository:
        return self._repo

    @property
    def connection(self) -> sqlite3.Connection:
        return self._repo.conn

    @property
    def lock(self) -> threading.RLock:
        return self._repo.lock

    # ---------- 草稿对象 ----------

    def upsert_draft_object(
        self,
        *,
        draft_id: str,
        name: str | None = None,
        description: str | None = None,
        session_id: str | None = None,
        current_revision: int | None = None,
        saved_revision: int | None = None,
        undo_cursor: int | None = None,
        theme_id: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """建草稿对象；已存在就只更新**给了的**那些字段。

        `None` 表示「这次不动这个字段」—— 想把它清成空就传空字符串。
        返回落库后的那一行。
        """
        now = updated_at or _now()
        with self._lock:
            conn = self.connection
            conn.execute(
                """
                INSERT INTO draft_objects (
                    draft_id, name, description, session_id,
                    current_revision, saved_revision, undo_cursor,
                    theme_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(draft_id) DO NOTHING
                """,
                (
                    draft_id,
                    name or "",
                    description or "",
                    session_id,
                    current_revision if current_revision is not None else 0,
                    saved_revision,
                    undo_cursor if undo_cursor is not None else 0,
                    theme_id,
                    created_at or now,
                    now,
                ),
            )
            fields: dict[str, Any] = {}
            if name is not None:
                fields["name"] = name
            if description is not None:
                fields["description"] = description
            if session_id is not None:
                fields["session_id"] = session_id
            if current_revision is not None:
                fields["current_revision"] = current_revision
            if saved_revision is not None:
                fields["saved_revision"] = saved_revision
            if undo_cursor is not None:
                fields["undo_cursor"] = undo_cursor
            if theme_id is not None:
                fields["theme_id"] = theme_id
            fields["updated_at"] = now
            assignments = ", ".join(f"{column} = ?" for column in fields)
            conn.execute(
                f"UPDATE draft_objects SET {assignments} WHERE draft_id = ?",  # noqa: S608 — 列名来自固定白名单
                [*fields.values(), draft_id],
            )
        row = self.get_draft_object(draft_id)
        assert row is not None  # 刚写入必然存在
        return row

    def get_draft_object(self, draft_id: str) -> dict[str, Any] | None:
        """读草稿对象；没有就返回 None。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM draft_objects WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_draft_objects(self) -> list[dict[str, Any]]:
        """列草稿对象：**只返回 id / name / updated_at**（打开列表只显示名称与时间）。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT draft_id, name, updated_at FROM draft_objects"
                " ORDER BY updated_at DESC, draft_id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_draft_object(self, draft_id: str) -> bool:
        """删草稿对象及其附属行。删不掉返回 False。"""
        with self._lock:
            conn = self.connection
            conn.execute("DELETE FROM draft_versions WHERE draft_id = ?", (draft_id,))
            conn.execute("DELETE FROM draft_history WHERE draft_id = ?", (draft_id,))
            conn.execute("DELETE FROM draft_tasks WHERE draft_id = ?", (draft_id,))
            conn.execute("DELETE FROM draft_locks WHERE draft_id = ?", (draft_id,))
            cursor = conn.execute(
                "DELETE FROM draft_objects WHERE draft_id = ?", (draft_id,)
            )
            return cursor.rowcount > 0

    # ---------- 版本 ----------

    def insert_draft_version(
        self,
        draft_id: str,
        revision: int,
        kind: str,
        definition_json: dict[str, Any] | str,
        snapshot_json: dict[str, Any] | str | None = None,
        label: str = "",
    ) -> dict[str, Any]:
        """写一条版本快照。`kind` 取 'edit'（编辑单位）或 'saved'（显式保存版）。"""
        now = _now()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO draft_versions (
                    draft_id, revision, kind, definition_json,
                    snapshot_json, label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    revision,
                    kind,
                    _dump(definition_json) or "{}",
                    _dump(snapshot_json),
                    label,
                    now,
                ),
            )
        return {
            "draft_id": draft_id,
            "revision": int(revision),
            "kind": kind,
            "definition": _load(_dump(definition_json)) or {},
            "snapshot": _load(_dump(snapshot_json)),
            "label": label,
            "created_at": now,
        }

    def get_draft_version(self, draft_id: str, revision: int) -> dict[str, Any] | None:
        """读一条版本快照。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM draft_versions WHERE draft_id = ? AND revision = ?",
                (draft_id, revision),
            ).fetchone()
        return self._row_to_version(row) if row is not None else None

    def list_draft_versions(self, draft_id: str) -> list[dict[str, Any]]:
        """列版本快照（按版本号升序）。

        `kind` 过滤在**服务层**做：存储层只负责按草稿取回全部版本。
        """
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM draft_versions WHERE draft_id = ? ORDER BY revision ASC",
                (draft_id,),
            ).fetchall()
        return [self._row_to_version(row) for row in rows]

    def max_version_revision(self, draft_id: str) -> int:
        """该草稿已有的最大版本号；没有版本时是 0。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT MAX(revision) AS max_rev FROM draft_versions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None or row["max_rev"] is None:
            return 0
        return int(row["max_rev"])

    def delete_draft_versions(self, draft_id: str, revisions: list[int]) -> int:
        """删掉指定的版本快照（清 redo 分支用）。"""
        if not revisions:
            return 0
        with self._lock:
            placeholders = ", ".join("?" for _ in revisions)
            cursor = self.connection.execute(
                f"DELETE FROM draft_versions WHERE draft_id = ? AND revision IN ({placeholders})",  # noqa: S608 — 占位符拼接
                [draft_id, *revisions],
            )
            return cursor.rowcount

    # ---------- 历史 ----------

    def append_history(
        self,
        draft_id: str,
        seq: int,
        revision: int,
        action: str,
        label: str = "",
    ) -> dict[str, Any]:
        """追加一条动作流水（`commit` / `undo` / `redo` / `save`）。"""
        now = _now()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO draft_history (
                    draft_id, seq, revision, action, label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (draft_id, seq, revision, action, label, now),
            )
        return {
            "draft_id": draft_id,
            "seq": int(seq),
            "revision": int(revision),
            "action": action,
            "label": label,
            "created_at": now,
        }

    def list_history(self, draft_id: str) -> list[dict[str, Any]]:
        """列动作流水（按序号升序）。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM draft_history WHERE draft_id = ? ORDER BY seq ASC",
                (draft_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def history_max_seq(self, draft_id: str) -> int:
        """该草稿历史里最大的序号；没有历史时是 0。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT MAX(seq) AS max_seq FROM draft_history WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None or row["max_seq"] is None:
            return 0
        return int(row["max_seq"])

    # ---------- 任务 ----------

    def upsert_task(
        self,
        *,
        task_id: str,
        draft_id: str,
        operation_id: str = "",
        state: str = "RUNNING",
        checkpoint_json: dict[str, Any] | None = None,
        commit_token: str | None = None,
    ) -> dict[str, Any]:
        """建任务；同 `task_id` 已存在就更新状态 / 检查点 / 凭证。"""
        now = _now()
        with self._lock:
            conn = self.connection
            conn.execute(
                """
                INSERT INTO draft_tasks (
                    task_id, draft_id, operation_id, state,
                    checkpoint_json, commit_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    checkpoint_json = excluded.checkpoint_json,
                    commit_token = excluded.commit_token,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    draft_id,
                    operation_id,
                    state,
                    _dump(checkpoint_json),
                    commit_token,
                    now,
                    now,
                ),
            )
        row = self.get_task(task_id)
        assert row is not None  # 刚写入必然存在
        return row

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """读一个任务；没有就返回 None。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM draft_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def update_task_state(
        self,
        task_id: str,
        state: str,
        checkpoint_json: dict[str, Any] | None = None,
        commit_token: str | None = None,
    ) -> dict[str, Any] | None:
        """改任务状态 / 检查点 / 提交凭证。任务不存在返回 None。

        `checkpoint_json=None` 表示「这次不改检查点」，与「清空检查点」区分开：
        要清空就传空字典。`commit_token=None` 同理（不清凭证）。
        """
        assignments = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state, _now()]
        if checkpoint_json is not None:
            assignments.append("checkpoint_json = ?")
            params.append(_dump(checkpoint_json))
        if commit_token is not None:
            assignments.append("commit_token = ?")
            params.append(commit_token)
        params.append(task_id)
        with self._lock:
            self.connection.execute(
                f"UPDATE draft_tasks SET {', '.join(assignments)} WHERE task_id = ?",  # noqa: S608 — 片段来自固定字面量
                params,
            )
        return self.get_task(task_id)

    def clear_task_commit_token(self, task_id: str) -> bool:
        """作废提交凭证（任务停止时用）。"""
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE draft_tasks SET commit_token = NULL, updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            return cursor.rowcount > 0

    def active_task(self, draft_id: str) -> dict[str, Any] | None:
        """该草稿当前**还没收尾**的任务（RUNNING / STOPPING / STOPPED）。

        ``STOPPED`` 也在里面：被停掉的那一轮**还没有处理完** ——
        用户还要选「继续」或「撤销」。刷新之后前端靠这个字段还原现场，
        少了它，刷新一次就看不到那两个选项了（契约 0002 §7）。
        """
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM draft_tasks
                WHERE draft_id = ? AND state IN ('RUNNING', 'STOPPING', 'STOPPED')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def list_tasks(self, draft_id: str) -> list[dict[str, Any]]:
        """列该草稿的所有任务。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM draft_tasks WHERE draft_id = ? ORDER BY created_at ASC",
                (draft_id,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    # ---------- 主题 ----------

    def _ensure_theme_style_column(self) -> None:
        """幂等补列：``draft_themes.default_style_json``。

        契约 §6.1 的建表语句里没有这一列（默认样式是 P4 后来加的），
        老库里读不到 —— 这里用 ``ALTER TABLE`` **补一次**，补过就不再补。
        只加列，不动既有数据。
        """
        with self._lock:
            if self._theme_style_column_ready:
                return
            conn = self.connection
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(draft_themes)")
            }
            if "default_style_json" not in columns:
                conn.execute(
                    "ALTER TABLE draft_themes ADD COLUMN default_style_json TEXT"
                )
                logger.info("[draft_store] 已迁移 draft_themes.default_style_json 列")
            self._theme_style_column_ready = True

    def upsert_theme(
        self,
        *,
        theme_id: str,
        name: str,
        tokens: dict[str, Any],
        builtin: bool = False,
        theme_revision: int | None = None,
        default_style: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写主题（存在就更新并把主题版本号 +1）。

        ``default_style`` 缺省表示「这次不动默认样式」：更新时保留旧值，
        新建时为空。
        """
        now = _now()
        with self._lock:
            self._ensure_theme_style_column()
            conn = self.connection
            existing = conn.execute(
                "SELECT theme_revision, default_style_json FROM draft_themes WHERE theme_id = ?",
                (theme_id,),
            ).fetchone()
            if default_style is not None:
                style_json = _dump(default_style) or "{}"
            elif existing is not None:
                style_json = existing["default_style_json"] or "{}"
            else:
                style_json = "{}"
            if existing is None:
                revision = theme_revision if theme_revision is not None else 1
                conn.execute(
                    """
                    INSERT INTO draft_themes (
                        theme_id, theme_revision, name, tokens_json,
                        default_style_json, builtin, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        theme_id,
                        revision,
                        name,
                        _dump(tokens) or "{}",
                        style_json,
                        1 if builtin else 0,
                        now,
                        now,
                    ),
                )
            else:
                revision = (
                    theme_revision
                    if theme_revision is not None
                    else int(existing["theme_revision"]) + 1
                )
                conn.execute(
                    """
                    UPDATE draft_themes
                    SET theme_revision = ?, name = ?, tokens_json = ?,
                        default_style_json = ?, builtin = ?, updated_at = ?
                    WHERE theme_id = ?
                    """,
                    (
                        revision,
                        name,
                        _dump(tokens) or "{}",
                        style_json,
                        1 if builtin else 0,
                        now,
                        theme_id,
                    ),
                )
        return {
            "theme_id": theme_id,
            "theme_revision": revision,
            "name": name,
            "tokens": tokens,
            "default_style": _load(style_json) or {},
            "builtin": builtin,
            "created_at": now,
            "updated_at": now,
        }

    def get_theme(self, theme_id: str) -> dict[str, Any] | None:
        """读一个主题。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM draft_themes WHERE theme_id = ?", (theme_id,)
            ).fetchone()
        return self._row_to_theme(row) if row is not None else None

    def list_themes(self) -> list[dict[str, Any]]:
        """列主题。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM draft_themes ORDER BY theme_id ASC"
            ).fetchall()
        return [self._row_to_theme(row) for row in rows]

    # ---------- 主题存储（`ThemeStorage` 协议的实现） ----------

    def list_theme_ids(self) -> list[str]:
        """存储里全部主题 id（含预置被改过的版本）。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT theme_id FROM draft_themes ORDER BY theme_id ASC"
            ).fetchall()
        return [str(row["theme_id"]) for row in rows]

    def load_theme(self, theme_id: str) -> dict[str, Any] | None:
        """按 id 读一个完整主题对象（驼峰键，对齐 `ThemeStorage` 协议）。

        没有这一行就返回 ``None`` —— 由上层回落预置主题。
        """
        key = str(theme_id or "").strip()
        if not key:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM draft_themes WHERE theme_id = ?", (key,)
            ).fetchone()
        return self._row_to_theme_object(row) if row is not None else None

    def save_theme(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: dict[str, Any],
        default_style: dict[str, Any],
        theme_revision: int,
    ) -> dict[str, Any]:
        """**原子**写入一个新版本并返回完整主题对象。

        一次写就是**一个完整版本**（tokens + defaultStyle 一起落），
        不留半份状态；写完立刻提交（自带一笔共享事务）。
        """
        key = str(theme_id or "").strip()
        if not key:
            raise ValueError("theme_id 不能为空")
        now = _now()
        with self._lock:
            self._ensure_theme_style_column()
            with self._repo.transaction():
                conn = self.connection
                existing = conn.execute(
                    "SELECT builtin, created_at FROM draft_themes WHERE theme_id = ?",
                    (key,),
                ).fetchone()
                builtin = int(existing["builtin"]) if existing is not None else 0
                created_at = str(existing["created_at"]) if existing is not None else now
                conn.execute(
                    """
                    INSERT INTO draft_themes (
                        theme_id, theme_revision, name, tokens_json,
                        default_style_json, builtin, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(theme_id) DO UPDATE SET
                        theme_revision = excluded.theme_revision,
                        name = excluded.name,
                        tokens_json = excluded.tokens_json,
                        default_style_json = excluded.default_style_json,
                        builtin = excluded.builtin,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        int(theme_revision),
                        str(name),
                        _dump(tokens) or "{}",
                        _dump(default_style) or "{}",
                        builtin,
                        created_at,
                        now,
                    ),
                )
        return {
            "themeId": key,
            "name": str(name),
            "builtin": bool(builtin),
            "themeRevision": int(theme_revision),
            "tokens": copy.deepcopy(tokens),
            "defaultStyle": copy.deepcopy(default_style),
            "createdAt": created_at,
            "updatedAt": now,
        }

    def affected_drafts(self, theme_id: str) -> list[dict[str, Any]]:
        """正在用这个主题的草稿清单（``[{"draftId", "name"}]``）。

        扫每个草稿**当前版本**的定义（``draft_versions.definition_json``），
        看顶层 ``theme.themeRef`` 是不是这个主题。

        这里**只读不改**：草稿定义一个字都不动 ——
        联动靠「下次 resolve / open / refresh 时重新解析主题」实现。
        """
        target = str(theme_id or "").strip()
        if not target:
            return []
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT o.draft_id AS draft_id, o.name AS name,
                       v.definition_json AS definition_json
                FROM draft_objects o
                JOIN draft_versions v
                  ON v.draft_id = o.draft_id AND v.revision = o.current_revision
                ORDER BY o.draft_id ASC
                """
            ).fetchall()
        affected: list[dict[str, Any]] = []
        for row in rows:
            definition = _load(row["definition_json"]) or {}
            if _definition_theme_ref(definition) == target:
                affected.append(
                    {
                        "draftId": str(row["draft_id"]),
                        "name": str(row["name"] or ""),
                    }
                )
        return affected

    # ---------- 锁 ----------

    def acquire_lock(self, draft_id: str, owner: str) -> bool:
        """拿锁。锁着且不是同一个 owner 时返回 False。

        `INSERT ... ON CONFLICT DO NOTHING` 是一条语句就够的原子判定：
        拿不到就说明别人已经拿着，不会出现两个 owner 同时以为自己拿到了。
        """
        with self._lock:
            conn = self.connection
            cursor = conn.execute(
                "INSERT INTO draft_locks (draft_id, owner, acquired_at) VALUES (?, ?, ?)"
                " ON CONFLICT(draft_id) DO NOTHING",
                (draft_id, owner, _now()),
            )
            if cursor.rowcount > 0:
                return True
            row = conn.execute(
                "SELECT owner FROM draft_locks WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return row is not None and str(row["owner"]) == owner

    def release_lock(self, draft_id: str, owner: str) -> bool:
        """放锁。不是持有者放不掉（返回 False）。"""
        with self._lock:
            cursor = self.connection.execute(
                "DELETE FROM draft_locks WHERE draft_id = ? AND owner = ?",
                (draft_id, owner),
            )
            return cursor.rowcount > 0

    def is_locked(self, draft_id: str) -> bool:
        """该草稿当前是否被锁着。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT owner FROM draft_locks WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return row is not None

    def lock_owner(self, draft_id: str) -> str | None:
        """锁的持有者；没锁返回 None。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT owner FROM draft_locks WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return str(row["owner"]) if row is not None else None

    # ---------- 行转换 ----------

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "draft_id": row["draft_id"],
            "revision": int(row["revision"]),
            "kind": row["kind"],
            "definition": _load(row["definition_json"]) or {},
            "snapshot": _load(row["snapshot_json"]),
            "label": row["label"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "draft_id": row["draft_id"],
            "operation_id": row["operation_id"],
            "state": row["state"],
            "checkpoint": _load(row["checkpoint_json"]),
            "commit_token": row["commit_token"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_theme(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        default_style: dict[str, Any] = {}
        if "default_style_json" in keys:
            default_style = _load(row["default_style_json"]) or {}
        return {
            "theme_id": row["theme_id"],
            "theme_revision": int(row["theme_revision"]),
            "name": row["name"],
            "tokens": _load(row["tokens_json"]) or {},
            "default_style": default_style,
            "builtin": bool(row["builtin"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_theme_object(row: sqlite3.Row) -> dict[str, Any]:
        """主题对象（驼峰键）：对齐 ``ThemeStorage`` 协议与前端契约 §8.1。"""
        keys = row.keys()
        default_style: dict[str, Any] = {}
        if "default_style_json" in keys:
            default_style = _load(row["default_style_json"]) or {}
        return {
            "themeId": str(row["theme_id"]),
            "name": str(row["name"] or ""),
            "builtin": bool(row["builtin"]),
            "themeRevision": int(row["theme_revision"]),
            "tokens": _load(row["tokens_json"]) or {},
            "defaultStyle": default_style,
            "createdAt": str(row["created_at"] or ""),
            "updatedAt": str(row["updated_at"] or ""),
        }
