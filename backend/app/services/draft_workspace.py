"""草稿工作区：可撤销的草稿对象 + 持久提交（契约 0002 §6）。

这一层是**草稿级**能力的地基：打开草稿、接受一次修改、撤销 / 重做、
显式保存、另存为。它**不是**第二套对话提交逻辑 —— 对话入口
（提候选 → 接受）照旧，只是把接受的结果落到这里。

版本语义（契约 §6.3，务必照着理解）：
    - `revision` 是**编辑链**的版本号：每落一次写入 +1，**单调递增**。
      撤销 / 重做也 +1 —— 撤销只把**内容**退回去，绝不把乐观锁数值退回去，
      否则前端拿旧版本号再提交会被当成旧草稿拦掉。
    - `savedRevision` 是**显式保存版**的版本号（指向 `draft_versions` 里
      `kind='saved'` 的那一行），与 `revision` **互不占用**：
      保存不发布，只是留一个显式保存版。
    - 撤销移动历史游标并恢复内容；新提交**清掉旧 redo 分支**。

一个「接受」= 一个**修改单位**：写定义 + 写版本 + 写历史 + 推进撤销指针，
四件事落在**同一笔事务**里（`sqlite_store` 的共享事务入口），
中途失败不会留下半份状态。

撤销游标指向的**修改单位**是 `draft_versions` 里 `kind='edit'` 的那些行：
基线是第 0 个单位，每一次「接受」追加一个。撤销 / 重做只移动游标，
**不删内容行**（只有新提交才把游标之后的分支截掉）。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.storage.draft_workspace_sqlite import DraftWorkspaceSqlite
from app.storage.sqlite_store import SqliteSessionRepository


logger = logging.getLogger(__name__)


# ---------- 错误 ----------

class DraftWorkspaceError(Exception):
    """草稿工作区错误基类。

    `code` 给调用方分流（路由据此选 HTTP 状态码），`message` 给用户看。
    """

    code = "draft_error"

    def __init__(self, message: str = "", **extra: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra


class DraftNotFound(DraftWorkspaceError):
    """草稿对象不存在。"""

    code = "draft_not_found"


class DraftAlreadyExists(DraftWorkspaceError):
    """草稿对象已存在（建基线时撞了同一个 draftId）。"""

    code = "draft_already_exists"


class DraftRevisionConflict(DraftWorkspaceError):
    """乐观锁冲突：客户端看到的版本和服务端不一致。"""

    code = "draft_revision_conflict"


class DraftUndoUnavailable(DraftWorkspaceError):
    """没有可撤销的修改单位了。"""

    code = "draft_undo_unavailable"


class DraftRedoUnavailable(DraftWorkspaceError):
    """没有可重做的修改单位了。"""

    code = "draft_redo_unavailable"


class DraftTaskNotFound(DraftWorkspaceError):
    """任务不存在。"""

    code = "task_not_found"


class DraftLocked(DraftWorkspaceError):
    """草稿被别的持有者锁着。"""

    code = "draft_locked"


class DraftTaskNotActive(DraftWorkspaceError):
    """任务已经不在「还能停 / 还能继续」的状态里。"""

    code = "task_not_active"


# ---------- 状态对象 ----------

@dataclass
class WorkspaceState:
    """一个草稿工作区的完整状态。

    字段名用前端约定的驼峰：`revision` 是编辑链版本号，
    `savedRevision` 是显式保存版的版本号，**两者不同义**。
    """

    draftId: str
    revision: int
    savedRevision: int | None
    definition: dict[str, Any]
    businessState: str
    prompt: str
    pendingChoice: str | None
    pendingEditPlan: dict[str, Any] | None
    undoPointer: int
    redoPointer: int
    activeTask: dict[str, Any] | None
    themeId: str | None
    updatedAt: str
    sessionId: str | None = None
    name: str = ""
    #: 草稿备注：用户自己写的一句话说明（落 `draft_objects.description`）。
    #: 与草稿名**分属两件事** —— 名字是标识，备注是给人看的说明。
    description: str = ""
    canUndo: bool = False
    canRedo: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好字典（路由直接拿它当响应体）。"""
        return {
            "draftId": self.draftId,
            "revision": self.revision,
            "savedRevision": self.savedRevision,
            "definition": self.definition,
            "businessState": self.businessState,
            "prompt": self.prompt,
            "pendingChoice": self.pendingChoice,
            "pendingEditPlan": self.pendingEditPlan,
            "undoPointer": self.undoPointer,
            "redoPointer": self.redoPointer,
            "activeTask": self.activeTask,
            "themeId": self.themeId,
            "updatedAt": self.updatedAt,
            "sessionId": self.sessionId,
            "name": self.name,
            "description": self.description,
            "canUndo": self.canUndo,
            "canRedo": self.canRedo,
        }


@dataclass
class DraftTask:
    """长任务（一次要跑很久的修改）的服务端状态。"""

    taskId: str
    draftId: str
    operationId: str
    state: str
    checkpoint: dict[str, Any] | None = None
    commitToken: str | None = None
    createdAt: str = ""
    updatedAt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.taskId,
            "draftId": self.draftId,
            "operationId": self.operationId,
            "state": self.state,
            "checkpoint": self.checkpoint,
            "commitToken": self.commitToken,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
        }


# ---------- 内部工具 ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clamp_cursor(cursor: int, unit_count: int) -> int:
    """把撤销游标夹到合法范围（0 = 基线，unit_count-1 = 最后一个单位）。"""
    if unit_count <= 0:
        return 0
    return max(0, min(cursor, unit_count - 1))


def _as_text(value: Any) -> str | None:
    """把待确认选择统一成文本。

    会话快照里的 `pendingChoice` 可能是结构化对象；工作区状态里它声明为
    文本，结构化的内容用 JSON 文本带着，调用方要对象自己解析。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _stamp_revision(definition: Any, revision: int) -> Any:
    """把定义的 ``metadata.revision`` 对齐到**当前编辑版本**。

    撤销 / 重做恢复的是**历史内容**，但版本号单调递增（契约 §6.3）：
    内容回到上一个修改单位，版本号不能退回去。历史行里存的是**当时**的
    版本号，直接读出来会与顶层 ``revision`` 对不上 —— 顶层是新的、内容是
    旧的，前端拿到的定义与版本号互相矛盾。

    只对齐**本来就带版本号**的定义：没有 `metadata.revision` 的定义不
    凭空多一个字段（那会改掉定义内容）。
    """
    if not isinstance(definition, dict):
        return definition
    metadata = definition.get("metadata")
    if not isinstance(metadata, dict) or "revision" not in metadata:
        return definition
    stamped = dict(definition)
    stamped["metadata"] = {**metadata, "revision": revision}
    return stamped


# ---------- 工作区 ----------

class DraftWorkspace:
    """草稿工作区。

    连接 / 锁 / 事务都复用 `SqliteSessionRepository`：
    **不另开连接、不另造锁**，写事务共用同一个 `BEGIN IMMEDIATE` 纪律。
    """

    def __init__(
        self,
        *,
        repository: SqliteSessionRepository | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        """默认用会话库**同一个** SQLite 文件（草稿表就长在那里）。"""
        self._explicit_repository = repository
        self._db_path = db_path
        # 只有「既没传 repository、也没传 db_path」时才跟着会话服务走。
        # 显式给了路径的实例（测试里的临时库）自己说了算。
        self._follow_session_repo = repository is None and db_path is None
        if repository is None:
            path = Path(db_path) if db_path is not None else get_settings().session_db_path
            repository = SqliteSessionRepository(path)
        self._repo_instance = repository
        self._adapter_instance = DraftWorkspaceSqlite(repository=repository)
        # **锁只有一把**：仓库的写锁。
        #
        # 这里原来另造了一把 RLock，于是同一件事有两把锁、两种相反的取法：
        #   读路径 load_draft   → 先服务层锁，再仓库锁（适配器拿的）
        #   写路径 undo/commit  → 先仓库锁（事务），再服务层锁
        # 两个线程各走一条就互相等待 —— 整个草稿接口卡死（``/health`` 仍 200）。
        # 复现与回归脚本：``OUT/checks/deadlock-repro.py``。
        #
        # 改成与适配器同一把（仓库）锁之后，只剩一种取法，AB-BA 不可能成立。
        self._lock = repository.lock

    # ---------- 仓库解析（按调用时） ----------

    def _sync_repository(self) -> None:
        """跟着会话服务走同一个 SQLite 文件 —— **按调用时**解析。

        为什么不是导入时定死：测试会把 ``session_service._repo`` 换成临时库，
        草稿表必须跟着换 —— 否则**测试会把草稿写进用户的库**。
        生产上两者本来就是同一个文件（草稿表就长在会话库里）。

        显式传了 ``repository`` 的实例不受影响（它自己说了算）。
        """
        if not self._follow_session_repo:
            return
        try:
            from app.services.session_service import session_service

            repo = getattr(session_service, "_repo", None)
        except Exception:  # noqa: BLE001 — 解析不到就沿用现在的
            return
        if repo is not None and repo is not self._repo_instance:
            self._repo_instance = repo
            self._adapter_instance = DraftWorkspaceSqlite(repository=repo)
            # 换仓库就换锁：锁跟着仓库走（见 __init__ 的说明）。
            self._lock = repo.lock

    @property
    def repo(self) -> SqliteSessionRepository:
        self._sync_repository()
        return self._repo_instance

    @property
    def adapter(self) -> DraftWorkspaceSqlite:
        self._sync_repository()
        return self._adapter_instance

    # ---------- 基础设施 ----------

    @property
    def repository(self) -> SqliteSessionRepository:
        return self.repo

    def transaction(self):
        """共享事务入口（`sqlite_store` 提供的那个）。

        上层要把「会话快照写 + 草稿版本写」放进同一笔事务时用它：

            with draft_workspace.transaction() as conn:
                conn.execute("UPDATE sessions SET snapshot = ? WHERE id = ?", ...)
                draft_workspace.commit_edit(...)   # 内部事务复用这一笔
        """
        return self.repo.transaction()

    # ---------- 读 ----------

    def list_drafts(self) -> list[dict[str, Any]]:
        """草稿列表：只有 `draftId` / `name` / `updatedAt`。"""
        with self._lock:
            rows = self.adapter.list_draft_objects()
        return [
            {
                "draftId": row["draft_id"],
                "name": row["name"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def load_draft(self, draft_id: str) -> WorkspaceState | None:
        """打开一个草稿：读回定义、版本、撤销指针。草稿不存在返回 None。"""
        with self._lock:
            obj = self.adapter.get_draft_object(draft_id)
            if obj is None:
                return None
            units = self._units(draft_id)
            task = self.adapter.active_task(draft_id)
            return self._build_state(obj, units, task)

    # ---------- 建基线 ----------

    def create_draft(
        self,
        draft_id: str,
        *,
        session_id: str,
        name: str,
        definition: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> WorkspaceState:
        """建立草稿**基线**（revision 1），**不产生撤销条目**。

        用途：一份报表刚生成出来时先落一份基线，之后每「接受一次修改」
        才是一条撤销单位。基线本身没有历史行，`canUndo` 为 False。

        `snapshot` 是这一版的**现场**（业务状态 / 提示 / 菜单）：
        撤销、重做、「撤销（回到上一步）」都靠它把现场还原回去。
        """
        with self.repo.transaction():
            if self.adapter.get_draft_object(draft_id) is not None:
                raise DraftAlreadyExists(f"草稿已存在：{draft_id}")
            self.adapter.upsert_draft_object(
                draft_id=draft_id,
                name=name or "",
                description="",
                session_id=session_id or None,
                current_revision=1,
                saved_revision=None,
                undo_cursor=0,
                theme_id=None,
            )
            self.adapter.insert_draft_version(
                draft_id,
                1,
                "edit",
                definition or {},
                snapshot,
                "baseline",
            )
            # 历史**不写**：基线不是一次「修改」，不占撤销条目。
        logger.info("[draft_workspace] 已建基线 draftId=%s revision=1", draft_id)
        state = self.load_draft(draft_id)
        assert state is not None  # 刚建必然存在
        return state

    # ---------- 提交修改 ----------

    def commit_edit(
        self,
        draft_id: str,
        *,
        definition: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        label: str = "",
    ) -> WorkspaceState:
        """接受一次修改 = **一个撤销单位**。

        写定义 + 写版本 + 写历史 + 推进撤销指针，**同一笔事务**。
        新提交**清掉旧 redo 分支**。

        `expected_revision=None` 表示不做乐观锁检查（首次提交用）；
        不一致抛 `DraftRevisionConflict`（code="draft_revision_conflict"）。
        """
        with self.repo.transaction():
            obj = self.adapter.get_draft_object(draft_id)
            if obj is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            current = int(obj["current_revision"])
            if expected_revision is not None and int(expected_revision) != current:
                raise DraftRevisionConflict(
                    f"草稿版本不一致：期望 {expected_revision}，实际 {current}",
                    expected=expected_revision,
                    actual=current,
                )
            units = self._units(draft_id)
            cursor = _clamp_cursor(int(obj["undo_cursor"]), len(units))
            # 清 redo 分支：游标之后的修改单位被这次新提交顶掉。
            dropped = units[cursor + 1:]
            if dropped:
                self.adapter.delete_draft_versions(
                    draft_id, [unit["revision"] for unit in dropped]
                )
            revision = self._next_revision(draft_id, current)
            self.adapter.insert_draft_version(
                draft_id,
                revision,
                "edit",
                definition or {},
                snapshot,
                label or "",
            )
            self.adapter.append_history(
                draft_id,
                self.adapter.history_max_seq(draft_id) + 1,
                revision,
                "commit",
                label or "",
            )
            self.adapter.upsert_draft_object(
                draft_id=draft_id,
                current_revision=revision,
                undo_cursor=cursor + 1,
            )
        logger.info(
            "[draft_workspace] 已提交修改 draftId=%s revision=%s", draft_id, revision
        )
        state = self.load_draft(draft_id)
        assert state is not None  # 刚写必然存在
        return state

    # ---------- 显式保存 ----------

    def save(
        self, draft_id: str, *, expected_revision: int | None = None,
        name: str | None = None, remark: str | None = None,
    ) -> WorkspaceState:
        """显式保存版：`savedRevision` 前进，**不自动发布**。

        保存会留一条 `kind='saved'` 的版本快照；`savedRevision` 记的是
        **那条保存版的版本号**（指向 `draft_versions` 里的一行），
        与编辑链版本号 `revision` **互不占用**：保存不动编辑链，
        也不动撤销单位。

        `remark` 是**用户写的备注**，落在 `draft_objects.description`：
        草稿名是标识、备注是给人看的说明，两者是两件事。
        **没给就不动原备注**（传空字符串才是「清空备注」）。
        """
        with self.repo.transaction():
            obj = self.adapter.get_draft_object(draft_id)
            if obj is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            current = int(obj["current_revision"])
            if expected_revision is not None and int(expected_revision) != current:
                raise DraftRevisionConflict(
                    f"草稿版本不一致：期望 {expected_revision}，实际 {current}",
                    expected=expected_revision,
                    actual=current,
                )
            units = self._units(draft_id)
            cursor = _clamp_cursor(int(obj["undo_cursor"]), len(units))
            active = units[cursor] if units else None
            revision = self._next_revision(draft_id, current)
            saved_name = name.strip() if name is not None else obj["name"]
            # 备注：只有**显式给了**才覆盖。`None` = 这次不改备注，
            # 空字符串 = 清空备注 —— 两种意思不能混。
            saved_remark = (
                remark.strip() if remark is not None else str(obj.get("description") or "")
            )
            definition = dict((active or {}).get("definition") or {})
            if name is not None and isinstance(definition.get("metadata"), dict):
                definition["metadata"] = {**definition["metadata"], "name": saved_name}
                if active is not None:
                    self.repo.conn.execute(
                        "UPDATE draft_versions SET definition_json = ? "
                        "WHERE draft_id = ? AND revision = ? AND kind = 'edit'",
                        (json.dumps(definition, ensure_ascii=False), draft_id, active["revision"]),
                    )
            self.adapter.insert_draft_version(
                draft_id,
                revision,
                "saved",
                definition,
                (active or {}).get("snapshot"),
                "saved",
            )
            self.adapter.append_history(
                draft_id,
                self.adapter.history_max_seq(draft_id) + 1,
                revision,
                "save",
                "",
            )
            # 保存**不推进编辑链版本号**：revision 仍是当前编辑版本。
            self.adapter.upsert_draft_object(
                draft_id=draft_id,
                name=saved_name,
                description=saved_remark,
                saved_revision=revision,
            )
        logger.info(
            "[draft_workspace] 已显式保存 draftId=%s savedRevision=%s",
            draft_id,
            revision,
        )
        state = self.load_draft(draft_id)
        assert state is not None  # 刚写必然存在
        return state

    # ---------- 撤销 / 重做 ----------

    def undo(
        self, draft_id: str, *, expected_revision: int | None = None
    ) -> WorkspaceState:
        """撤销一步：内容回到上一个修改单位。

        **版本号继续单调递增** —— 撤销不回退乐观锁数值（契约 §6.3）。

        `expected_revision` 与提交一样生效：客户端看到的版本与服务端不一致
        就拒绝（两个窗口各改各的时，旧版本的撤销不能落到新内容上）。
        """
        with self.repo.transaction():
            obj = self.adapter.get_draft_object(draft_id)
            if obj is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            current = int(obj["current_revision"])
            if expected_revision is not None and int(expected_revision) != current:
                raise DraftRevisionConflict(
                    f"草稿版本不一致：期望 {expected_revision}，实际 {current}",
                    expected=expected_revision,
                    actual=current,
                )
            units = self._units(draft_id)
            cursor = _clamp_cursor(int(obj["undo_cursor"]), len(units))
            if cursor <= 0:
                raise DraftUndoUnavailable("没有可撤销的修改了")
            revision = self._next_revision(draft_id, current)
            self.adapter.append_history(
                draft_id,
                self.adapter.history_max_seq(draft_id) + 1,
                revision,
                "undo",
                "",
            )
            self.adapter.upsert_draft_object(
                draft_id=draft_id,
                current_revision=revision,
                undo_cursor=cursor - 1,
            )
        logger.info("[draft_workspace] 已撤销 draftId=%s revision=%s", draft_id, revision)
        state = self.load_draft(draft_id)
        assert state is not None  # 刚写必然存在
        return state

    def redo(
        self, draft_id: str, *, expected_revision: int | None = None
    ) -> WorkspaceState:
        """重做一步：内容前进到被撤销掉的修改单位。

        `expected_revision` 同样生效（理由同 `undo`）。
        """
        with self.repo.transaction():
            obj = self.adapter.get_draft_object(draft_id)
            if obj is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            current = int(obj["current_revision"])
            if expected_revision is not None and int(expected_revision) != current:
                raise DraftRevisionConflict(
                    f"草稿版本不一致：期望 {expected_revision}，实际 {current}",
                    expected=expected_revision,
                    actual=current,
                )
            units = self._units(draft_id)
            cursor = _clamp_cursor(int(obj["undo_cursor"]), len(units))
            if cursor >= len(units) - 1:
                raise DraftRedoUnavailable("没有可重做的修改了")
            revision = self._next_revision(draft_id, current)
            self.adapter.append_history(
                draft_id,
                self.adapter.history_max_seq(draft_id) + 1,
                revision,
                "redo",
                "",
            )
            self.adapter.upsert_draft_object(
                draft_id=draft_id,
                current_revision=revision,
                undo_cursor=cursor + 1,
            )
        logger.info("[draft_workspace] 已重做 draftId=%s revision=%s", draft_id, revision)
        state = self.load_draft(draft_id)
        assert state is not None  # 刚写必然存在
        return state

    # ---------- 另存为 ----------

    def save_as(
        self,
        draft_id: str,
        *,
        name: str,
        expected_revision: int | None = None,
        remark: str | None = None,
    ) -> WorkspaceState:
        """另存为：新 `draftId`，**原草稿不变**。

        新草稿**不复制**聊天（`session_id` 留空，由上层另行绑定）、
        **不复制** redo 分支、**不复制**旧修改历史；只带当前内容与主题引用。

        `expected_revision` 对**源草稿**生效：两个窗口各改各的时，
        拿旧版本号来另存为会被拒绝。

        `remark` 给了就用它当新草稿的备注；不给就沿用源草稿的备注
        （另存为本来就要把这份内容整套带走）。
        """
        with self.repo.transaction():
            obj = self.adapter.get_draft_object(draft_id)
            if obj is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            current = int(obj["current_revision"])
            if expected_revision is not None and int(expected_revision) != current:
                raise DraftRevisionConflict(
                    f"草稿版本不一致：期望 {expected_revision}，实际 {current}",
                    expected=expected_revision,
                    actual=current,
                )
            units = self._units(draft_id)
            cursor = _clamp_cursor(int(obj["undo_cursor"]), len(units))
            active = units[cursor] if units else None
            new_id = f"draft_{uuid.uuid4().hex[:12]}"
            definition = dict((active or {}).get("definition") or {})
            if isinstance(definition.get("metadata"), dict):
                definition["metadata"] = {**definition["metadata"], "name": name}
            new_remark = (
                remark.strip() if remark is not None else str(obj.get("description") or "")
            )
            self.adapter.upsert_draft_object(
                draft_id=new_id,
                name=name or "未命名草稿",
                description=new_remark,
                # 聊天不跟着复制走：另存为之后由上层另行绑定会话。
                session_id=None,
                current_revision=1,
                saved_revision=None,
                undo_cursor=0,
                theme_id=obj["theme_id"],
            )
            self.adapter.insert_draft_version(
                new_id,
                1,
                "edit",
                definition,
                (active or {}).get("snapshot"),
                "saved_as",
            )
            # 原草稿**一个字节都不改**：不写它的历史、不动它的版本与游标。
        logger.info(
            "[draft_workspace] 已另存为 draftId=%s newDraftId=%s", draft_id, new_id
        )
        state = self.load_draft(new_id)
        assert state is not None  # 刚建必然存在
        return state

    def bind_session(self, draft_id: str, session_id: str | None) -> None:
        """把草稿对象绑定到一条会话。

        另存为 / 打开草稿都要切**对象身份**：切完之后这条会话的后续提交
        要落到新对象上，而不是还挂在原来那个对象上（契约 0002 §6.4）。

        原对象一个字节都不改 —— 这里只写新对象的 ``session_id``。
        """
        if not session_id:
            return
        with self.repo.transaction():
            if self.adapter.get_draft_object(draft_id) is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            self.adapter.upsert_draft_object(
                draft_id=draft_id, session_id=session_id
            )

    def delete(self, draft_id: str) -> None:
        """Delete one unlocked object, keeping conversations and other drafts."""
        with self.repo.transaction():
            if self.adapter.get_draft_object(draft_id) is None:
                raise DraftNotFound("这个草稿不存在。")
            self.require_unlocked(draft_id)
            if self.adapter.active_task(draft_id) is not None:
                raise DraftLocked("这个草稿还有未结束的任务，请先结束任务。")
            self.repo.conn.execute(
                "UPDATE sessions SET snapshot = json_remove(snapshot, '$.draftId') "
                "WHERE json_extract(snapshot, '$.draftId') = ?",
                (draft_id,),
            )
            self.adapter.delete_draft_object(draft_id)

    # ---------- 任务状态机 ----------

    def begin_task(
        self,
        draft_id: str,
        *,
        task_id: str | None = None,
        operation_id: str = "",
        checkpoint: dict[str, Any] | None = None,
    ) -> DraftTask:
        """开一个长任务（RUNNING）。

        **同一轮重发复用同一个 taskId**（taskId 由 messageId 推导）：
        已存在的任务**接着跑**，检查点**不清** —— 续跑要靠它，
        而且**不会**把已经停掉 / 撤销掉的任务复活（那要显式走
        ``resume_task``）。
        """
        with self.repo.transaction():
            if self.adapter.get_draft_object(draft_id) is None:
                raise DraftNotFound(f"草稿不存在：{draft_id}")
            task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
            existing = self.adapter.get_task(task_id)
            if existing is None:
                self.adapter.upsert_task(
                    task_id=task_id,
                    draft_id=draft_id,
                    operation_id=operation_id or "",
                    state="RUNNING",
                    checkpoint_json=checkpoint,
                )
            else:
                # 同一轮重发：状态**不动**（停了就是停了），检查点合并。
                merged = dict(existing.get("checkpoint") or {})
                merged.update(checkpoint or {})
                state = str(existing["state"])
                if state == "FAILED":
                    # 同一轮**重试**：失败的任务重新开跑。
                    state = "RUNNING"
                self.adapter.update_task_state(
                    task_id, state, checkpoint_json=merged
                )
        return self._get_task(task_id)

    def checkpoint_task(
        self, task_id: str, checkpoint: dict[str, Any], *, merge: bool = False
    ) -> DraftTask:
        """存检查点：任务状态不变，检查点内容替换。

        `merge=True` 时**合并**进已有检查点（续跑要保留之前记下的进度）。
        """
        with self.repo.transaction():
            task = self.adapter.get_task(task_id)
            if task is None:
                raise DraftTaskNotFound(f"任务不存在：{task_id}")
            payload = checkpoint or {}
            if merge:
                merged = dict(task.get("checkpoint") or {})
                merged.update(payload)
                payload = merged
            self.adapter.update_task_state(
                task_id,
                task["state"],
                checkpoint_json=payload,
            )
        return self._get_task(task_id)

    def complete_task(
        self, task_id: str, *, commit_token: str | None = None
    ) -> DraftTask:
        """任务完成（COMPLETED）。`commit_token` 是这次提交的凭证。"""
        return self._set_task_state(task_id, "COMPLETED", commit_token=commit_token)

    def stop_task(self, task_id: str) -> DraftTask:
        """停止任务（STOPPED），并**作废它的提交凭证**。

        只有**还在跑**的任务能停：已经完成 / 已撤销的任务再点「停止」
        不能把它改成 STOPPED —— 那会让对象一直被锁着（没人能解锁）。
        """
        with self.repo.transaction():
            task = self.adapter.get_task(task_id)
            if task is None:
                raise DraftTaskNotFound(f"任务不存在：{task_id}")
            state = str(task["state"])
            if state not in ("RUNNING", "STOPPING"):
                raise DraftTaskNotActive(f"这一轮已经结束了（{state}），停不了。")
            self.adapter.update_task_state(task_id, "STOPPED")
            self.adapter.clear_task_commit_token(task_id)
        return self._get_task(task_id)

    def fail_task(self, task_id: str, *, reason: str = "") -> DraftTask:
        """任务失败（FAILED）。失败原因记进检查点，便于复盘。"""
        with self.repo.transaction():
            task = self.adapter.get_task(task_id)
            if task is None:
                raise DraftTaskNotFound(f"任务不存在：{task_id}")
            checkpoint = dict(task.get("checkpoint") or {})
            if reason:
                checkpoint["reason"] = reason
            self.adapter.update_task_state(
                task_id,
                "FAILED",
                checkpoint_json=checkpoint,
            )
        return self._get_task(task_id)

    def abort_task(
        self, task_id: str, *, reason: str = "", stoppable: bool = False
    ) -> DraftTask:
        """这一轮**没落成**：任务收尾。

        `stoppable=True`（续跑失败）→ 回到 **STOPPED**：两个选项还在，
        用户还能继续处理这个现场；否则 → FAILED。
        """
        with self.repo.transaction():
            task = self.adapter.get_task(task_id)
            if task is None:
                raise DraftTaskNotFound(f"任务不存在：{task_id}")
            checkpoint = dict(task.get("checkpoint") or {})
            if reason:
                checkpoint["reason"] = reason
            self.adapter.update_task_state(
                task_id,
                "STOPPED" if stoppable else "FAILED",
                checkpoint_json=checkpoint,
            )
            if stoppable:
                self.adapter.clear_task_commit_token(task_id)
        return self._get_task(task_id)

    def rollback_task(self, task_id: str) -> DraftTask:
        """任务回滚（ROLLED_BACK）：已经落下的改动由调用方按检查点补偿。"""
        return self._set_task_state(task_id, "ROLLED_BACK")

    def resume_task(self, task_id: str) -> DraftTask:
        """从**服务端检查点**继续（状态回到 RUNNING，检查点保留）。

        「只完成本次剩余工作、不重复已完成阶段」由拿到检查点的调用方负责；
        这一层保证：检查点还在、状态回到可继续、并且**记下这是第几次续跑**
        （续跑失败时要能回到可处理的停止现场）。

        已经完成 / 已撤销的任务不能再续跑（那是另一条分支）。
        """
        with self.repo.transaction():
            task = self.adapter.get_task(task_id)
            if task is None:
                raise DraftTaskNotFound(f"任务不存在：{task_id}")
            state = str(task["state"])
            if state == "COMPLETED":
                raise DraftTaskNotActive("这一轮已经完成了，不能再继续。")
            if state == "ROLLED_BACK":
                raise DraftTaskNotActive("这一轮已经撤销了，不能再继续。")
            checkpoint = dict(task.get("checkpoint") or {})
            checkpoint["resumeCount"] = int(checkpoint.get("resumeCount") or 0) + 1
            checkpoint["resumedAt"] = _now()
            checkpoint["resumedFrom"] = state
            self.adapter.update_task_state(
                task_id,
                "RUNNING",
                checkpoint_json=checkpoint,
            )
        return self._get_task(task_id)

    def load_task(self, task_id: str) -> DraftTask | None:
        """读一个任务；不存在返回 None。"""
        with self._lock:
            row = self.adapter.get_task(task_id)
        return self._to_task(row) if row is not None else None

    def list_tasks(self, draft_id: str) -> list[DraftTask]:
        """列一个草稿的所有任务。"""
        with self._lock:
            rows = self.adapter.list_tasks(draft_id)
        return [self._to_task(row) for row in rows]

    # ---------- 锁 ----------

    def acquire(self, draft_id: str, owner: str) -> bool:
        """拿草稿写锁。**同一个 owner 重复拿是允许的**（幂等 = 续跑）。

        锁状态存在库里（`draft_locks` 表），**不另开连接**：
        多进程部署时同草稿的写互斥也成立。

        判定是**一条语句**（``INSERT ... ON CONFLICT DO NOTHING``）：
        拿不到就说明别人已经拿着 —— 不会出现两个 owner 同时以为自己拿到了，
        也不会留下「先看锁、再写」之间的窗口。
        """
        with self.repo.transaction():
            return self.adapter.acquire_lock(draft_id, owner)

    def require_unlocked(self, draft_id: str, *, owner: str | None = None) -> None:
        """写之前检查锁。**必须在同一笔事务里调用**（不留可抢占的窗口）。

        `owner` 是「本轮持有者」：
            - 锁在**别人**手里 → 抛 `DraftLocked`（路由回 409）；
            - 锁在**自己**手里 → 放行（**重入**：同一轮续跑就是重入）。
        `owner=None` 表示「任何锁都不放行」—— 草稿级动作
        （保存 / 撤销 / 重做 / 另存为）用这个：另一个窗口正在改同一个对象时，
        本窗口的动作要被挡住（契约 0002 §7）。
        """
        current = self.adapter.lock_owner(draft_id)
        if not current:
            return
        if owner is not None and current == owner:
            return
        raise DraftLocked("这个草稿正在被另一个窗口修改，请先在那边停止或等它结束。")

    def release(self, draft_id: str, owner: str) -> bool:
        """放草稿写锁。不是持有者放不放掉（返回 False）。"""
        with self.repo.transaction():
            return self.adapter.release_lock(draft_id, owner)

    def is_locked(self, draft_id: str) -> bool:
        """草稿当前是否被锁着。"""
        with self._lock:
            return self.adapter.is_locked(draft_id)

    def lock_owner(self, draft_id: str) -> str | None:
        """锁的持有者；没锁返回 None。"""
        with self._lock:
            return self.adapter.lock_owner(draft_id)

    # ---------- 主题（存储侧，§8 的接线由 P4 做） ----------

    def load_theme(self, theme_id: str) -> dict[str, Any] | None:
        """读一个主题。"""
        with self._lock:
            return self.adapter.get_theme(theme_id)

    def save_theme(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: dict[str, Any],
        builtin: bool = False,
    ) -> dict[str, Any]:
        """写一个主题（存在就更新，主题版本号 +1）。"""
        with self.repo.transaction():
            return self.adapter.upsert_theme(
                theme_id=theme_id,
                name=name or theme_id,
                tokens=tokens or {},
                builtin=builtin,
            )

    def list_themes(self) -> list[dict[str, Any]]:
        """列主题。"""
        with self._lock:
            return self.adapter.list_themes()

    # ---------- 内部 ----------

    def _units(self, draft_id: str) -> list[dict[str, Any]]:
        """该草稿的**修改单位**（`kind='edit'` 的版本行，按版本号升序）。

        基线是第 0 个单位；每一次「接受」追加一个。
        """
        with self._lock:
            versions = self.adapter.list_draft_versions(draft_id)
        return [row for row in versions if row["kind"] == "edit"]

    def _next_revision(self, draft_id: str, current_revision: int) -> int:
        """下一个版本号：编辑链版本号与版本表里的最大号取大者 +1。

        两者取大者是为了不和已有的版本行撞主键（保存版也会占一个版本号）。
        """
        return max(current_revision, self.adapter.max_version_revision(draft_id)) + 1

    def _get_task(self, task_id: str) -> DraftTask:
        with self._lock:
            row = self.adapter.get_task(task_id)
        if row is None:
            raise DraftTaskNotFound(f"任务不存在：{task_id}")
        return self._to_task(row)

    @staticmethod
    def _to_task(row: dict[str, Any]) -> DraftTask:
        return DraftTask(
            taskId=row["task_id"],
            draftId=row["draft_id"],
            operationId=row["operation_id"],
            state=row["state"],
            checkpoint=row.get("checkpoint"),
            commitToken=row.get("commit_token"),
            createdAt=row.get("created_at") or "",
            updatedAt=row.get("updated_at") or "",
        )

    def _set_task_state(
        self,
        task_id: str,
        state: str,
        *,
        commit_token: str | None = None,
    ) -> DraftTask:
        with self.repo.transaction():
            task = self.adapter.get_task(task_id)
            if task is None:
                raise DraftTaskNotFound(f"任务不存在：{task_id}")
            self.adapter.update_task_state(
                task_id,
                state,
                commit_token=commit_token,
            )
        return self._get_task(task_id)

    @staticmethod
    def _build_state(
        obj: dict[str, Any],
        units: list[dict[str, Any]],
        task: dict[str, Any] | None,
    ) -> WorkspaceState:
        """把「草稿对象 + 修改单位列表 + 当前任务」拼成工作区状态。

        游标语义：
            - `undoPointer` = 当前生效的单位下标（0 = 基线）；
            - `redoPointer` = **还可重做几步**（游标之后的单位个数）。

        `activeTask` 用**驼峰**（`DraftTask.to_dict`）：前端按
        ``activeTask.state`` / ``activeTask.taskId`` 读它 ——
        刷新之后靠它还原「已停止」的现场（契约 0002 §7）。
        """
        cursor = _clamp_cursor(int(obj["undo_cursor"]), len(units))
        active = units[cursor] if units else None
        # 定义的版本号对齐到**当前编辑版本**：撤销恢复的是历史内容，
        # 但版本号单调递增（契约 §6.3）—— 顶层 `revision` 与定义里的
        # `metadata.revision` 必须是同一个数（前端按定义里的那个数当
        # 基准版本回传，两边对不上就会把下一轮当成旧草稿）。
        definition = _stamp_revision(
            (active or {}).get("definition") or {}, int(obj["current_revision"])
        )
        snapshot = (active or {}).get("snapshot") or {}
        pending_plan = snapshot.get("pendingEditPlan")
        redo_pointer = max(0, len(units) - 1 - cursor)
        active_task = None
        if task is not None:
            try:
                active_task = DraftTask(
                    taskId=str(task.get("task_id") or ""),
                    draftId=str(task.get("draft_id") or ""),
                    operationId=str(task.get("operation_id") or ""),
                    state=str(task.get("state") or ""),
                    checkpoint=task.get("checkpoint"),
                    commitToken=task.get("commit_token"),
                    createdAt=str(task.get("created_at") or ""),
                    updatedAt=str(task.get("updated_at") or ""),
                ).to_dict()
            except Exception:  # noqa: BLE001 — 任务读不出来不该拖垮整个状态
                logger.warning("[draft_workspace] 任务状态转换失败 draftId=%s", obj["draft_id"])
                active_task = None
        return WorkspaceState(
            draftId=obj["draft_id"],
            revision=int(obj["current_revision"]),
            savedRevision=obj["saved_revision"],
            definition=definition,
            businessState=str(snapshot.get("businessState") or ""),
            prompt=str(snapshot.get("prompt") or ""),
            pendingChoice=_as_text(snapshot.get("pendingChoice")),
            pendingEditPlan=pending_plan if isinstance(pending_plan, dict) else None,
            undoPointer=cursor,
            redoPointer=redo_pointer,
            activeTask=active_task,
            themeId=obj["theme_id"],
            updatedAt=obj["updated_at"],
            sessionId=obj["session_id"],
            name=obj["name"] or "",
            description=str(obj.get("description") or ""),
            canUndo=cursor > 0,
            canRedo=redo_pointer > 0,
        )


#: 模块级单例：路由层 `from app.services.draft_workspace import draft_workspace`。
draft_workspace = DraftWorkspace()
