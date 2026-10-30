"""用户 JSON 输入记录的持久化（工单 #64，契约表 §4.3）。

**这张表里存的是用户数据，不是模拟业务数值。**

ADR 0004 §5.1 规定「模拟业务数值不落盘」，§8 明确豁免：用户自己提供的
JSON 原始记录**可以保存一份**。本模块就是那一份的落点：

    report_design_json_inputs  一次输入的某一个版本

**只保存这一份。** 普通聊天消息、诊断记录与调试跟踪都不复制完整数组
（规格第 21 条）—— 那些地方只留「已提交 JSON 数据」的摘要与引用。

表结构（契约表 §4.3 逐列定名）：

    input_id       输入记录 ID，形如 ``inp_<uuid4hex[:12]>``
    input_version  输入版本，从 1 起递增
    session_id     归属会话
    array_path     JSON 数组在报文里的位置
    raw_json       **原始记录数组——全系统只保存这一份**
    schema_json    字段结构、缺失与类型统计
    sample_json    有上限的代表样本
    record_count   行数
    byte_size      字节数
    created_at     生成时间

**版本语义**：同一会话里再次粘贴产生**新版本**（``input_version`` + 1），
旧版本**不删除**（老数据不迁移、不删除）。生效组合只认已确认的那一版，
候选不覆盖生效输入（契约表 §4.3）。

连接与锁**由调用方注入** —— 与设计存储、会话存储同一条连接、同一把锁，
这样输入记录与设计对象的写入落在同一笔事务里。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_design_json_inputs (
    input_id      TEXT NOT NULL,
    input_version INTEGER NOT NULL,
    session_id    TEXT NOT NULL,
    array_path    TEXT NOT NULL DEFAULT '',
    raw_json      TEXT NOT NULL,
    schema_json   TEXT NOT NULL DEFAULT '{}',
    sample_json   TEXT NOT NULL DEFAULT '[]',
    record_count  INTEGER NOT NULL DEFAULT 0,
    byte_size     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (input_id, input_version)
);

CREATE INDEX IF NOT EXISTS idx_report_design_json_inputs_session
    ON report_design_json_inputs (session_id, input_id, input_version);
"""


@dataclass(frozen=True)
class JsonInputRecord:
    """一条输入记录（一个版本）。

    ``rawJson`` 是原始记录数组 —— **全系统只保存这一份**。别的消费方
    （模型上下文、渲染载荷）各自拿自己需要的那一份视图，不落第二份。
    """

    inputId: str
    inputVersion: int
    sessionId: str
    arrayPath: str
    rawJson: str
    schema: dict[str, Any]
    sample: list[dict[str, Any]]
    recordCount: int
    byteSize: int
    createdAt: str

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.inputId,
            self.inputVersion,
            self.sessionId,
            self.arrayPath,
            self.rawJson,
            json.dumps(self.schema, ensure_ascii=False),
            json.dumps(self.sample, ensure_ascii=False),
            self.recordCount,
            self.byteSize,
            self.createdAt,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "JsonInputRecord":
        return cls(
            inputId=row["input_id"],
            inputVersion=int(row["input_version"]),
            sessionId=row["session_id"],
            arrayPath=row["array_path"],
            rawJson=row["raw_json"],
            schema=_load_json(row["schema_json"], {}),
            sample=_load_json(row["sample_json"], []),
            recordCount=int(row["record_count"]),
            byteSize=int(row["byte_size"]),
            createdAt=row["created_at"],
        )


def _load_json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("[json_input_store] 记录里的 JSON 解析失败，按空值处理")
        return default


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SqliteJsonInputStore:
    """JSON 输入记录的 SQLite 实现。

    连接与锁由调用方注入（与设计存储、会话存储同一条），
    本类**不**自己建库、不自己造锁。
    """

    def __init__(self, *, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = connection
        self._lock = lock

    def initialize_schema(self) -> None:
        """建表（幂等）。只新增 ``report_design_`` 前缀的表，不动旧 schema。"""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- 写 ----------

    def insert(self, record: JsonInputRecord) -> JsonInputRecord:
        """写入一个输入版本。

        同一 ``(input_id, input_version)`` 重复写入按**幂等**处理：
        覆盖同一行，不新增（重试不会多出一份原始数据）。
        """
        with self._lock:
            self.initialize_schema()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO report_design_json_inputs (
                    input_id, input_version, session_id, array_path, raw_json,
                    schema_json, sample_json, record_count, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                record.to_row(),
            )
            self._conn.commit()
        return record

    # ---------- 读 ----------

    def get(self, input_id: str, input_version: int) -> JsonInputRecord | None:
        """按身份取一个版本。"""
        if not input_id:
            return None
        with self._lock:
            self.initialize_schema()
            row = self._conn.execute(
                """
                SELECT * FROM report_design_json_inputs
                 WHERE input_id = ? AND input_version = ?
                """,
                (input_id, int(input_version)),
            ).fetchone()
        return JsonInputRecord.from_row(row) if row else None

    def latest_for_session(self, session_id: str) -> JsonInputRecord | None:
        """会话里**最新**的那一版输入记录（候选或生效都取它）。

        这是「这个会话当前按哪份 JSON 输入工作」的解析口径：
        生效组合里的 ``inputId`` / ``inputVersion`` 优先，
        没有生效组合时才退到这里取最新版本。
        """
        if not session_id:
            return None
        with self._lock:
            self.initialize_schema()
            row = self._conn.execute(
                """
                SELECT * FROM report_design_json_inputs
                 WHERE session_id = ?
                 ORDER BY input_id DESC, input_version DESC
                 LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return JsonInputRecord.from_row(row) if row else None

    def list_for_session(self, session_id: str) -> list[JsonInputRecord]:
        """会话的全部输入版本（老版本不删除，按版本倒序）。"""
        if not session_id:
            return []
        with self._lock:
            self.initialize_schema()
            rows = self._conn.execute(
                """
                SELECT * FROM report_design_json_inputs
                 WHERE session_id = ?
                 ORDER BY input_id DESC, input_version DESC
                """,
                (session_id,),
            ).fetchall()
        return [JsonInputRecord.from_row(row) for row in rows]

    def next_version(self, session_id: str, input_id: str) -> int:
        """给定输入身份下的下一个版本号（从 1 起递增）。"""
        with self._lock:
            self.initialize_schema()
            row = self._conn.execute(
                """
                SELECT MAX(input_version) AS latest FROM report_design_json_inputs
                 WHERE input_id = ?
                """,
                (input_id,),
            ).fetchone()
        latest = row["latest"] if row else None
        return int(latest or 0) + 1
