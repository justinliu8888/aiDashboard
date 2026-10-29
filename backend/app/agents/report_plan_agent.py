"""模型设计适配：把模型输出变成可交给领域层校验的方案候选。

这一层只做**设计意图**的转换，不管版本、不管落库、不管取数：

- 输入：用户这一轮的新建/修改需求、当前已提交方案（可为 null）、简短历史；
- 输出：``plan_candidate`` / ``clarification`` / ``failure`` 三选一。

边界（冻结契约）：

- 模型通过注入的 ``model_call(messages)`` 进来，外部客户端和超时由调用者包装；
  本模块**不建全局模型、不复制配置或凭证**。
- 候选**不含顶层 id/revision**，也不分配提交版本；方案 ID 与板块 ID 由领域层生成。
- 本模块消费**原始 JSON mapping**，不引入尚未交付的领域类型；领域校验由协调者接线时调用 A 完成。
- 不写库、不确认、不取数、不调用旧的完整报表 builder、不编真实数值。
- 普通只读提问由协调者挂回旧查询链路，本模块不取代事实问答。
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.schemas.report_plan import PlanValidationError
from app.services.report_plan_service import (
    COLUMNS_NOTE_PREFIX,
    PlanResolution,
    drop_unaccepted_suggestion_sections,
    normalize_column_format,
    resolve_plan,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "report_plan.md"

#: 结果种类（三选一）。
KIND_PLAN_CANDIDATE = "plan_candidate"
KIND_CLARIFICATION = "clarification"
KIND_FAILURE = "failure"

#: 失败码：调用方据此区分「模型不可用」「输出看不懂」「输出不安全」。
FAILURE_MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
FAILURE_MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
FAILURE_UNSAFE_CONTENT = "UNSAFE_CONTENT"

#: 注入的外部模型调用：接收 messages 数组，返回模型文本。
ModelCall = Callable[[list[dict[str, Any]]], Awaitable[str]]
#: 可选的进度回调。同步、异步都支持；回调失败不影响本轮结论。
Emit = Callable[[dict[str, Any]], Any]


class _InvalidOutput(Exception):
    """模型输出不是本模块能识别的结构。"""


class _UnsafeContent(Exception):
    """模型输出带了不该进方案的可执行内容或敏感内容。"""


# --------------------------------------------------------------------------
# 安全检查：方案里不允许出现的东西
# --------------------------------------------------------------------------

#: 一旦出现就说明模型在写可执行内容或数据绑定，而不是设计意图。
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "sql",
        "query",
        "querystatement",
        "script",
        "code",
        "command",
        "shell",
        "formula",
        "expression",
        "datasource",
        "datasourceref",
        "sourceref",
        "source",
        "metricref",
        "metricrefs",
        "dataref",
        "binding",
        "connection",
        "connectionstring",
        "jdbc",
        "credential",
        "credentials",
        "secret",
        "token",
        "apikey",
        "api_key",
        "password",
        "passwd",
        "pwd",
        "privatekey",
        "username",
        "schedule",
        "cron",
        "job",
        "jobid",
        "executionplan",
        "execution",
        "endpoint",
        "url",
        "host",
        "port",
    }
)

#: SQL 语句。
_RE_SQL = re.compile(
    r"(?i)\b(select\s+[^;]{0,120}?\s+from\b"
    r"|insert\s+into\b"
    r"|update\s+\w+\s+set\b"
    r"|delete\s+from\b"
    r"|drop\s+(table|view|database|index)\b"
    r"|create\s+table\b"
    r"|alter\s+table\b"
    r"|truncate\s+table\b"
    r"|union\s+select\b)"
)

#: 脚本 / 可执行代码。
_RE_SCRIPT = re.compile(
    r"(?i)(<\s*script\b"
    r"|<\s*/\s*script\b"
    r"|javascript:"
    r"|vbscript:"
    r"|eval\s*\("
    r"|os\.system\s*\("
    r"|subprocess\."
    r"|rm\s+-rf"
    r"|#!/usr/bin"
    r"|#!/bin/bash"
    r"|import\s+os\b"
    r"|from\s+os\s+import"
    r"|curl\s+https?://"
    r"|wget\s+https?://)"
)

#: 凭证 / 密钥。
_RE_CREDENTIAL = re.compile(
    r"(?i)(\bpassword\b|\bpasswd\b|\bapi[_\- ]?key\b|\bsecret[_\- ]?key\b"
    r"|\baccess[_\- ]?key\b|\bprivate[_\- ]?key\b|\bbearer\s+[A-Za-z0-9._\-]{8,}"
    r"|\bsk-[A-Za-z0-9]{8,}"
    r"|-----BEGIN\s+(RSA|PRIVATE)"
    r"|\bAKID[A-Za-z0-9]{10,}"
    r"|凭证|密钥|密码)"
)

#: 连接串 / 地址：方案不绑定任何数据源。
_RE_CONNECTION = re.compile(
    r"(?i)(jdbc:\w+://|mongodb(\+srv)?://|postgres(ql)?://|mysql://|redis://"
    r"|amqp://|ftps?://|https?://\S+)"
)

#: 数据目录编码：指标要用用户说的意图表达，不写目录里的编码。
_RE_CATALOG_CODE = re.compile(r"\b(?:catalog|metric)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*")

#: 推断出来的计算公式：未知口径只能标为待核实，不能自己补公式。
_RE_FORMULA_ARITHMETIC = re.compile(
    r"[一-龥A-Za-z][一-龥A-Za-z_ ]{0,20}\s*[=＝]\s*[^=，。；;\n]{0,40}[/*×÷+]"
)
_RE_FORMULA_NUMBER = re.compile(
    r"[一-龥A-Za-z][一-龥A-Za-z_ ]{0,20}\s*[=＝]\s*[-+]?\d+(?:\.\d+)?\s*[%％]?\s*$"
)


def _scan_text(text: str) -> None:
    """检查一个字符串值里有没有不该进方案的内容。"""
    if _RE_SQL.search(text):
        raise _UnsafeContent("出现了 SQL 语句")
    if _RE_SCRIPT.search(text):
        raise _UnsafeContent("出现了可执行脚本")
    if _RE_CREDENTIAL.search(text):
        raise _UnsafeContent("出现了凭证内容")
    if _RE_CONNECTION.search(text):
        raise _UnsafeContent("出现了数据源地址")
    if _RE_CATALOG_CODE.search(text):
        raise _UnsafeContent("出现了数据目录编码")
    if _RE_FORMULA_ARITHMETIC.search(text) or _RE_FORMULA_NUMBER.search(text):
        raise _UnsafeContent("出现了推断出来的计算公式")


def _is_version_number(path: tuple[str, ...], value: Any) -> bool:
    """版本号不算业务数值。

    参考模板的 ``version`` 和模型自带的提交 ``revision`` 都是正整数版本号；
    提交版本号随后会被丢弃，不进候选。
    """
    if not path:
        return False
    if path[-1].lower() not in {"version", "revision"}:
        return False
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_format_precision(path: tuple[str, ...], value: Any) -> bool:
    """列格式里的**小数位数**是格式参数，不是业务数值。

    ``columns[].format.precision`` 说明这一列保留几位小数 —— 它是**显示
    格式**，不是实测值。方案侧能声明 ``format``，表头才有单位标注
    （工单 03 的列格式契约）。
    """
    if len(path) < 2:
        return False
    if path[-1].lower() != "precision" or path[-2].lower() != "format":
        return False
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


#: 标识位白名单：**容器键** -> 该容器项里哪些键是**标识位**。
#:
#: 标识位上的数值是「序号式标识」（模型把「第 1 列」写成 ``1``），不是业务
#: 数值。模型给数字 id 很常见 —— 数值扫描器一刀切拒掉，等于让整轮方案作废、
#: 用户被无端重问。那是误伤，不是防护。
_IDENTIFIER_CONTAINERS: dict[str, frozenset[str]] = {
    # 栏目项：``id``（栏目身份）与 ``fieldRef``（取值键）都是标识位
    "columns": frozenset({"id", "fieldRef"}),
    # 板块项：``id`` 是标识位
    "sections": frozenset({"id"}),
}


def _normalize_identifiers(node: Any, container: str | None = None) -> None:
    """把**标识位**上的整数归一成字符串（就地改）。

    只动已知的标识位（栏目项的 ``id`` / ``fieldRef``、板块项的 ``id``）：

    - 归一后扫描器照样扫得到这些位置（值已经是字符串，不再是数值）；
    - 其它位置的数值照旧被拒 —— 真实业务数值仍然进不了方案。
    """
    if isinstance(node, dict):
        allowed = _IDENTIFIER_CONTAINERS.get(container or "", frozenset())
        for key, value in list(node.items()):
            name = str(key)
            if name in allowed and isinstance(value, int) and not isinstance(value, bool):
                node[key] = str(value)
            else:
                _normalize_identifiers(
                    value, name if name in _IDENTIFIER_CONTAINERS else None
                )
        return
    if isinstance(node, list):
        for item in node:
            _normalize_identifiers(item, container)


def _scan_node(node: Any, path: tuple[str, ...]) -> None:
    """递归检查模型输出：危险字段、真实数值、可执行内容。"""
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).strip().lower()
            if lowered in _FORBIDDEN_KEYS:
                raise _UnsafeContent(f"出现了不该有的字段 {key}")
            _scan_node(value, path + (str(key),))
        return

    if isinstance(node, list):
        for index, item in enumerate(node):
            _scan_node(item, path + (str(index),))
        return

    if isinstance(node, bool):
        return

    if isinstance(node, (int, float)):
        if _is_version_number(path, node):
            return
        if _is_format_precision(path, node):
            return
        raise _UnsafeContent("方案里出现了真实数值")

    if isinstance(node, str):
        _scan_text(node)


def _scan_unsafe_content(payload: Any) -> None:
    """扫描整个模型输出，命中就抛 ``_UnsafeContent``。"""
    _scan_node(payload, ())


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_messages(
    *,
    message: str,
    current_plan: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """拼出发给模型的 messages。

    顺序固定：系统提示 → 历史 → 当前方案 → 本轮需求。
    历史只保留有文本的条目，角色限制在 user / assistant。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": _load_prompt()}]

    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        content = str(turn.get("text") or turn.get("content") or "").strip()
        if not content:
            continue
        role = str(turn.get("role") or "user").strip().lower()
        if role not in {"user", "assistant"}:
            role = "user"
        messages.append({"role": role, "content": content})

    if current_plan:
        messages.append(
            {
                "role": "user",
                "content": "当前已提交方案（JSON）：\n"
                + json.dumps(current_plan, ensure_ascii=False, default=str),
            }
        )

    messages.append({"role": "user", "content": str(message or "")})
    return messages


def _extract_json_object(text: str) -> dict[str, Any]:
    """从模型文本里取出 JSON 对象。

    依次尝试：整体解析 → 去掉 markdown 代码块 → 扫描第一个配平的 ``{}``。
    """
    raw = (text or "").strip()
    if not raw:
        raise _InvalidOutput("模型没有返回内容")

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    fenced = _strip_code_fence(raw)
    if fenced != raw:
        try:
            parsed = json.loads(fenced)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    scanned = _scan_json_object(raw)
    if scanned is not None:
        return scanned

    raise _InvalidOutput("模型输出不是 JSON 对象")


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹，保留里面的内容。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _scan_json_object(text: str) -> dict[str, Any] | None:
    """在自由文本里扫描第一个配平的 JSON 对象。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


#: 候选里不允许出现的顶层字段：提交 ID 和版本由服务端分配，模型不能自己给。
_CANDIDATE_RESERVED_KEYS: tuple[str, ...] = ("id", "revision")

#: 板块可用的组件类型（冻结契约）。
COMPONENT_TYPES: tuple[str, ...] = (
    "metric_card",
    "line_chart",
    "bar_chart",
    "pie_chart",
    "table",
    "text",
)

#: 板块可用的展示目的：用户想看什么（语义候选，最终组件类型由服务端定）。
DISPLAY_INTENTS: tuple[str, ...] = (
    "total",
    "trend",
    "comparison",
    "composition",
    "records",
)

#: 时间粒度（既有粒度枚举）。
GRAIN_UNITS: tuple[str, ...] = ("MINUTE", "HOUR", "DAY", "WEEK", "MONTH")

#: 表格板块的组件类型：只有它用得上栏目声明。
_TABLE_COMPONENT_TYPE = "table"

#: 需求里必须出现的字段：指标和排除项被省略，就等于把用户明确的要求悄悄丢掉。
_REQUIRED_REQUIREMENT_KEYS: tuple[str, ...] = ("metrics", "exclusions")
#: 需求里可以为空的字段：用户没给就是 null，不算错。
_OPTIONAL_REQUIREMENT_KEYS: tuple[str, ...] = ("subject", "timeRange")
#: 板块里必须出现的字段。
_REQUIRED_SECTION_KEYS: tuple[str, ...] = ("title", "metricIntents", "componentType")
#: 参考模板条目里必须出现的字段。
_REQUIRED_REFERENCE_KEYS: tuple[str, ...] = ("assetId", "version", "name", "reason")


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _InvalidOutput(f"{field} 必须是对象")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _InvalidOutput(f"{field} 必须是非空字符串")
    return value


def _require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _InvalidOutput(f"{field} 必须是字符串数组")
    return list(value)


def _require_text_or_none(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidOutput(f"{field} 必须是字符串或 null")
    return value


def _validate_requirements(raw: Any) -> dict[str, Any]:
    """校验需求块。指标和排除项必须出现，避免范围被省略。"""
    requirements = _require_object(raw, "requirements")
    for key in _REQUIRED_REQUIREMENT_KEYS:
        if key not in requirements:
            raise _InvalidOutput(f"requirements 缺少 {key}")
    validated: dict[str, Any] = {
        "metrics": _require_string_list(
            requirements["metrics"], "requirements.metrics"
        ),
    }
    # 用户没给的对象和时间范围就是 null，不算错。
    for key in _OPTIONAL_REQUIREMENT_KEYS:
        validated[key] = _require_text_or_none(
            requirements.get(key), f"requirements.{key}"
        )
    validated["exclusions"] = _require_string_list(
        requirements["exclusions"], "requirements.exclusions"
    )
    # 时间粒度：与时间范围单位不是一回事；用户没说就由服务端推定。
    grain_unit = requirements.get("grainUnit")
    if isinstance(grain_unit, str) and grain_unit.strip():
        unit = grain_unit.strip().upper()
        if unit in GRAIN_UNITS:
            validated["grainUnit"] = unit
        else:
            logger.info("模型给的 grainUnit 非法，已忽略：%s", grain_unit)
    return validated


def _validate_columns(raw: Any, label: str) -> list[dict[str, Any]]:
    """表格栏目：模型可以只给显示名。

    结构完全不可用时（不是数组）不在这里判死：交给服务端的栏目解析，
    由它决定「补齐 → 追问 → 失败」哪条路。
    """
    if not isinstance(raw, list):
        logger.info("%s的 columns 不是数组，交给服务端解析", label)
        return []
    columns: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            logger.info("%s的 columns 里有非对象项，已忽略", label)
            continue
        text = str(item.get("label") or "").strip()
        if not text:
            logger.info("%s的 columns 里有空显示名，已忽略", label)
            continue
        column: dict[str, Any] = {"label": text}
        for key in ("id", "fieldRef"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                column[key] = value.strip()
        # 列的展示格式（精度、单位标注）：模型给了就带着走，表头才有单位。
        column_format = normalize_column_format(item.get("format"))
        if column_format:
            column["format"] = column_format
        columns.append(column)
    return columns


def _validate_sections(raw: Any) -> list[dict[str, Any]]:
    """校验板块数组。componentType 必须是约定枚举里的一个。"""
    if not isinstance(raw, list):
        raise _InvalidOutput("sections 必须是数组")
    sections: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        section = _require_object(item, "section")
        # 报错要**点名是哪个板块**（#39）：只说「componentType 不在允许的取值里」
        # 的话，用户根本不知道要改哪一块。
        label = f"板块 {index + 1}「{section.get('title') or '未命名'}」"
        for key in _REQUIRED_SECTION_KEYS:
            if key not in section:
                raise _InvalidOutput(f"{label}缺少 {key}")
        component_type = _require_string(section["componentType"], "componentType")
        if component_type not in COMPONENT_TYPES:
            raise _InvalidOutput(
                f"{label}的 componentType {component_type!r} 不在允许的取值里"
            )
        validated: dict[str, Any] = {
            "title": _require_string(section["title"], "title"),
            "metricIntents": _require_string_list(
                section["metricIntents"], "metricIntents"
            ),
            "componentType": component_type,
        }
        # 板块 id 只在引用已有板块时出现；新板块不编号，由领域层生成。
        if section.get("id") is not None:
            validated["id"] = _require_string(section["id"], "板块 id")
        # 展示目的：模型的语义候选。非法就丢掉，由服务端按请求推定。
        intent = section.get("displayIntent")
        if isinstance(intent, str) and intent.strip():
            if intent.strip() in DISPLAY_INTENTS:
                validated["displayIntent"] = intent.strip()
            else:
                logger.info("%s的 displayIntent 非法，已忽略：%s", label, intent)
        # 表格板块的栏目声明：只有表格板块用得上。
        if component_type == _TABLE_COMPONENT_TYPE and section.get("columns") is not None:
            validated["columns"] = _validate_columns(section["columns"], label)
        sections.append(validated)
    return sections


def _validate_reference_templates(raw: Any) -> list[dict[str, Any]]:
    """校验参考模板数组。首波没有模板库，这里应该是空数组。"""
    if not isinstance(raw, list):
        raise _InvalidOutput("referenceTemplates 必须是数组")
    templates: list[dict[str, Any]] = []
    for item in raw:
        entry = _require_object(item, "referenceTemplate")
        for key in _REQUIRED_REFERENCE_KEYS:
            if key not in entry:
                raise _InvalidOutput(f"参考模板缺少 {key}")
        version = entry["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise _InvalidOutput("参考模板 version 必须是正整数")
        templates.append(
            {
                "assetId": _require_string(entry["assetId"], "assetId"),
                "version": version,
                "name": _require_string(entry["name"], "name"),
                "reason": _require_string(entry["reason"], "reason"),
            }
        )
    return templates


def _validate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """按冻结契约校验候选结构，并归一成契约里的字段集合。

    只做**结构**判断：必填字段、类型、枚举。版本一致性和板块 ID 归属
    属于领域语义，交给 A 在协调者接线时校验。
    """
    if any(key in candidate for key in _CANDIDATE_RESERVED_KEYS):
        # 模型想自己给提交 ID / 版本：记一笔，候选里不留。
        logger.info("模型自带提交 ID/版本，已从候选中丢弃")
    if "topic" not in candidate:
        raise _InvalidOutput("候选缺少 topic")
    if "requirements" not in candidate:
        raise _InvalidOutput("候选缺少 requirements")
    if "sections" not in candidate:
        raise _InvalidOutput("候选缺少 sections")
    return {
        "topic": _require_string(candidate["topic"], "topic"),
        "requirements": _validate_requirements(candidate["requirements"]),
        "sections": _validate_sections(candidate["sections"]),
        "assumptions": _require_string_list(
            candidate.get("assumptions") or [], "assumptions"
        ),
        "suggestions": _require_string_list(
            candidate.get("suggestions") or [], "suggestions"
        ),
        "referenceTemplates": _validate_reference_templates(
            candidate.get("referenceTemplates") or []
        ),
    }


def _apply_resolution(candidate: dict[str, Any], resolution: PlanResolution) -> None:
    """把服务端定稿的栏目写回候选，并把补齐说明记进假设。

    系统补的栏目必须**出现在方案里**（用户看得见、能改），不能只在
    渲染时按标题偷偷补列。
    """
    sections = candidate.get("sections") or []
    assumptions = candidate.setdefault("assumptions", [])
    for index, section in enumerate(sections):
        if index not in resolution.columns:
            continue
        columns: list[dict[str, Any]] = []
        for spec in resolution.columns[index]:
            column: dict[str, Any] = {"label": spec.label}
            if spec.id:
                column["id"] = spec.id
            if spec.fieldRef:
                column["fieldRef"] = spec.fieldRef
            columns.append(column)
        section["columns"] = columns
        note = resolution.column_notes.get(index)
        if note:
            entry = f"{COLUMNS_NOTE_PREFIX}{note}"
            if entry not in assumptions:
                assumptions.append(entry)


def _failure(code: str, message: str) -> dict[str, Any]:
    """安全失败：只说可辨识的类别，不回显模型原文。"""
    return {"kind": KIND_FAILURE, "code": code, "message": message}


async def _fail(
    emit: Emit | None, code: str, message: str
) -> dict[str, Any]:
    """返回安全失败，并通知进度回调本轮结束于失败。"""
    await _notify(emit, {"event": "plan_design_failed", "code": code})
    return _failure(code, message)


async def _notify(emit: Emit | None, event: dict[str, Any]) -> None:
    """进度回调。回调自身出错不影响本轮结论。"""
    if emit is None:
        return
    try:
        result = emit(event)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("方案设计进度回调失败：%s", exc)


async def generate_candidate(
    *,
    message: str,
    current_plan: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
    model_call: ModelCall,
    emit: Emit | None = None,
) -> dict[str, Any]:
    """把一轮新建/修改需求变成方案候选、澄清问题或安全失败。

    参数：
        message：用户这一轮说的需求或修改意见。
        current_plan：当前已提交方案，新建时为 null。
        history：有角色和文本的简短上下文。
        model_call：注入的模型调用，接收 messages，返回模型文本。
        emit：可选的进度回调。

    返回：
        ``{"kind": "plan_candidate", "candidate": {...}}`` /
        ``{"kind": "clarification", "question": "..."}`` /
        ``{"kind": "failure", "code": "...", "message": "..."}``。
    """
    messages = build_messages(
        message=message, current_plan=current_plan, history=history
    )

    await _notify(emit, {"event": "plan_design_started"})

    try:
        text = await model_call(messages)
    except Exception as exc:  # noqa: BLE001
        # 只记异常类型：上游报错文本可能带凭证，不进日志也不进返回值。
        logger.warning("方案设计模型调用失败：%s", type(exc).__name__)
        return await _fail(
            emit, FAILURE_MODEL_CALL_FAILED, "模型调用失败，请稍后重试"
        )

    if not isinstance(text, str):
        logger.info("方案设计模型返回的不是文本：%s", type(text).__name__)
        return await _fail(
            emit, FAILURE_MODEL_OUTPUT_INVALID, "模型输出无法解析，请重试"
        )

    await _notify(emit, {"event": "plan_design_finished"})

    try:
        payload = _extract_json_object(text)
    except _InvalidOutput as exc:
        logger.info("方案设计输出无法解析：%s", exc)
        return await _fail(
            emit, FAILURE_MODEL_OUTPUT_INVALID, "模型输出无法解析，请重试"
        )

    try:
        # 标识位上的整数先归一成字符串：模型给数字 id 很常见，那是序号式
        # 标识，不是业务数值，不能让整轮方案因为它作废。
        _normalize_identifiers(payload)
        _scan_unsafe_content(payload)
    except _UnsafeContent as exc:
        logger.info("方案设计输出含不安全内容：%s", exc)
        return await _fail(
            emit,
            FAILURE_UNSAFE_CONTENT,
            f"模型输出包含不该进入方案的内容（{exc}）",
        )

    kind = payload.get("kind")
    if kind == KIND_PLAN_CANDIDATE:
        candidate = payload.get("candidate")
        if not isinstance(candidate, dict):
            return await _fail(
                emit, FAILURE_MODEL_OUTPUT_INVALID, "模型输出缺少方案内容"
            )
        try:
            validated = _validate_candidate(candidate)
        except _InvalidOutput as exc:
            logger.info("方案候选结构不合法：%s", exc)
            return await _fail(
                emit,
                FAILURE_MODEL_OUTPUT_INVALID,
                f"模型输出的方案结构不合法（{exc}）",
            )

        # ---- 选型与栏目：服务端定稿，模型说了不算 ----
        # 模型只给语义候选；组件类型、栏目身份、取值键都由服务端按优先级定。
        # 未接受的建议先剔掉：追问与补齐都只针对真正要做的板块。
        try:
            kept_sections = drop_unaccepted_suggestion_sections(
                list(candidate.get("sections") or []),
                list(candidate.get("suggestions") or []),
            )
            validated["sections"] = drop_unaccepted_suggestion_sections(
                list(validated.get("sections") or []),
                list(validated.get("suggestions") or []),
            )
        except PlanValidationError as exc:
            logger.info("候选里的板块都只是未接受的建议：%s", exc)
            return await _fail(
                emit,
                getattr(exc, "code", None) or FAILURE_MODEL_OUTPUT_INVALID,
                f"模型输出的方案结构不合法（{exc}）",
            )

        resolution = resolve_plan(
            kept_sections,
            request_text=str(message or ""),
            previous_sections=(current_plan or {}).get("sections") or [],
        )
        if resolution.question:
            # 猜不出来、且猜错会影响这块的用途 → 走澄清追问通道，
            # 问题出现在左侧对话区。「没有真实数据」不是追问的理由。
            logger.info("方案需要澄清：%s", resolution.question)
            return {"kind": KIND_CLARIFICATION, "question": resolution.question}
        if resolution.failure_code:
            # 修正仍失败：明确报失败并允许重试，不给空表。
            return await _fail(
                emit,
                resolution.failure_code,
                resolution.failure_message or "方案生成失败，请重试。",
            )

        _apply_resolution(validated, resolution)
        return {"kind": KIND_PLAN_CANDIDATE, "candidate": validated}

    if kind == KIND_CLARIFICATION:
        question = str(payload.get("question") or "").strip()
        if not question:
            return await _fail(
                emit, FAILURE_MODEL_OUTPUT_INVALID, "模型输出缺少澄清问题"
            )
        return {"kind": KIND_CLARIFICATION, "question": question}

    return await _fail(
        emit, FAILURE_MODEL_OUTPUT_INVALID, "模型输出无法解析，请重试"
    )
