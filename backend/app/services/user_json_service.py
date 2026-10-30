"""用户 JSON 输入的编排层（工单 #64）。

把三件事接起来：

    校验（:mod:`app.services.json_input_service`）
      → 存一份（:mod:`app.storage.json_input_store`）
      → 生效组合（:mod:`app.services.effective_combination`）

**边界（规格第 21 / 27 条）**

- 原始记录数组**只保存这一份**，就在 ``report_design_json_inputs`` 里；
- 普通聊天消息、诊断记录与调试跟踪**不复制**完整数组 ——
  对外只给「已提交 JSON 数据」的摘要与引用；
- 这是**用户数据**，不属于模拟业务数值：ADR 0004 §5.1 的不落盘约束
  在这里明确豁免（ADR 0004 §8）。模拟业务数值仍然只在响应与内存里。

**版本语义**：同一会话再次粘贴 → 同一 ``inputId`` 的下一个版本，
旧版本**不删除**。生效组合只认已确认的那一版；候选不覆盖生效输入。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Sequence

from app.services.effective_combination import (
    combination_of_definition,
    normalize_combination,
)
from app.services.json_input_service import (
    MAX_SAMPLE_ROWS,
    JsonInputError,
    JsonValidationOutcome,
    build_model_context,
    validate_json_payload,
)
from app.services.report_log_safety import log_info, log_warning
from app.services.session_service import session_service
from app.storage.json_input_store import JsonInputRecord, SqliteJsonInputStore


logger = logging.getLogger(__name__)


#: 输入记录 ID 的前缀（契约表 §4.3：形如 ``inp_<uuid4hex[:12]>``）。
INPUT_ID_PREFIX = "inp_"


def new_input_id() -> str:
    """生成一个输入记录 ID。"""
    return f"{INPUT_ID_PREFIX}{uuid.uuid4().hex[:12]}"


class UserJsonService:
    """用户 JSON 输入的读写编排。"""

    def __init__(self, store: SqliteJsonInputStore | None = None) -> None:
        self._injected_store = store
        self._store: SqliteJsonInputStore | None = None
        self._store_source: Any = None

    # ---------- 依赖 ----------

    @property
    def store(self) -> SqliteJsonInputStore:
        """输入记录存储。

        连接与锁跟会话存储**同一条**、**同一把** —— 与设计存储一致，
        这样输入记录与设计对象的写入落在同一笔事务里。
        """
        if self._injected_store is not None:
            return self._injected_store
        session_repo = session_service.repository
        if self._store is None or self._store_source is not session_repo:
            self._store = SqliteJsonInputStore(
                connection=session_repo.conn, lock=session_repo.lock
            )
            self._store.initialize_schema()
            self._store_source = session_repo
        return self._store

    def reset_for_tests(self, *, store: SqliteJsonInputStore | None = None) -> None:
        """换掉依赖（测试用）。"""
        self._injected_store = store
        self._store = None
        self._store_source = None

    # ---------- 提交 ----------

    def submit(
        self,
        *,
        session_id: str,
        text: str,
        array_path: str | None = None,
    ) -> tuple[JsonInputRecord | None, JsonInputError | None]:
        """校验并保存一次 JSON 输入。

        校验不通过 → **不落库**，返回错误（错误里只有位置与计数，没有原文）。
        校验通过 → 存成**新版本**（旧版本不删除），返回那条记录。
        """
        outcome: JsonValidationOutcome = validate_json_payload(
            text, array_path=array_path
        )
        if not outcome.ok:
            error = outcome.error or JsonInputError(
                code="JSON_INPUT_INVALID", message="这份数据没通过校验。"
            )
            log_warning(
                logger,
                "[user_json] 校验未通过 session=%s code=%s rows=%d bytes=%d",
                session_id,
                error.code,
                outcome.recordCount,
                outcome.byteSize,
            )
            return None, error

        store = self.store
        previous = store.latest_for_session(session_id)
        input_id = previous.inputId if previous else new_input_id()
        version = store.next_version(session_id, input_id)
        record = JsonInputRecord(
            inputId=input_id,
            inputVersion=version,
            sessionId=session_id,
            arrayPath=outcome.arrayPath,
            rawJson=_dump_rows(outcome.rows),
            schema=outcome.schema,
            sample=outcome.sample,
            recordCount=outcome.recordCount,
            byteSize=outcome.byteSize,
            createdAt=_now(),
        )
        store.insert(record)
        log_info(
            logger,
            "[user_json] 已保存输入记录 session=%s inputId=%s version=%d rows=%d fields=%d bytes=%d",
            session_id,
            record.inputId,
            record.inputVersion,
            record.recordCount,
            outcome.schema.get("fieldCount", 0),
            record.byteSize,
        )
        return record, None

    # ---------- 读取 ----------

    def latest_for_session(self, session_id: str) -> JsonInputRecord | None:
        """会话里最新的一版输入记录。"""
        if not session_id:
            return None
        return self.store.latest_for_session(session_id)

    def get(self, input_id: str, input_version: int) -> JsonInputRecord | None:
        """按身份取一个版本。"""
        if not input_id:
            return None
        return self.store.get(input_id, input_version)

    def rows_of(self, record: JsonInputRecord | None) -> list[dict[str, Any]]:
        """取一条记录的**完整**已校验数据。"""
        if record is None:
            return []
        return _load_rows(record.rawJson)

    def effective_record(self, definition: Any) -> JsonInputRecord | None:
        """按**生效组合**取输入记录。

        生效组合里绑定了 ``inputId`` / ``inputVersion`` 就取那一版；
        没绑定 → 没有生效输入（**不拿最新候选冒充生效输入**，
        也不拿模拟数字冒充用户的真实数据）。
        """
        combination = normalize_combination(combination_of_definition(definition))
        input_id = combination.get("inputId")
        input_version = combination.get("inputVersion")
        if not input_id or input_version is None:
            return None
        return self.get(str(input_id), int(input_version))

    # ---------- 给模型与用户的摘要 ----------

    def model_context(
        self, record: JsonInputRecord | None, *, sample_limit: int = MAX_SAMPLE_ROWS
    ) -> str:
        """模型上下文（只有结构、缺失与类型统计、有上限的样本）。"""
        if record is None:
            return ""
        return build_model_context(
            schema=record.schema,
            sample=record.sample,
            input_id=record.inputId,
            input_version=record.inputVersion,
            sample_limit=sample_limit,
        )

    def safe_summary(self, record: JsonInputRecord | None) -> str:
        """**安全摘要** —— 给聊天消息、诊断与调试跟踪用。

        只有身份、规模与字段名，**没有一行业务数据**。
        """
        if record is None:
            return "（没有 JSON 输入记录）"
        fields = [
            str(item.get("key"))
            for item in (record.schema.get("fields") or [])
            if item.get("key")
        ]
        field_text = "、".join(fields[:12])
        if len(fields) > 12:
            field_text += f" 等 {len(fields)} 个字段"
        return (
            f"【已提交 JSON 数据】{record.recordCount} 行 / {len(fields)} 个字段"
            f"（{field_text}）；引用 {record.inputId} v{record.inputVersion}"
        )

    def reference(self, record: JsonInputRecord | None) -> dict[str, Any]:
        """**引用** —— 只有身份与规模，不带原文。"""
        if record is None:
            return {}
        return {
            "inputId": record.inputId,
            "inputVersion": record.inputVersion,
            "recordCount": record.recordCount,
            "fieldCount": record.schema.get("fieldCount", 0),
            "byteSize": record.byteSize,
        }

    def data_issues(
        self, record: JsonInputRecord | None
    ) -> list[str]:
        """这份数据的**问题清单**（类型混乱、缺失、null）。

        只说结构事实，不带业务数值。
        """
        if record is None:
            return []
        issues: list[str] = []
        for item in record.schema.get("fields") or []:
            key = str(item.get("key") or "")
            if item.get("mixed"):
                issues.append(
                    f"字段「{key}」里混了多种类型（{'、'.join(item.get('types') or [])}），"
                    "已按明细展示并保留原值。"
                )
            if item.get("missingCount"):
                issues.append(
                    f"字段「{key}」有 {item.get('missingCount')} 行缺失，"
                    "缺失按「（缺失）」标注，不当成 0。"
                )
            if item.get("nullCount"):
                issues.append(
                    f"字段「{key}」有 {item.get('nullCount')} 行是 null，"
                    "null 与 0 分开标注。"
                )
        return issues


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _dump_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    import json

    return json.dumps(list(rows), ensure_ascii=False, default=str)


def _load_rows(raw: str) -> list[dict[str, Any]]:
    import json

    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        log_warning(logger, "[user_json] 原始记录解析失败，按空数据处理")
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


#: 全局单例（与既有服务一致的风格）。
user_json_service = UserJsonService()
