"""共享主题服务（P4，契约 0002 §8）。

职责边界：
    - **只管外观**：主题的 token / 默认样式白名单；不碰布局、统计口径、FormatSpec。
    - **服务端解析**：`resolve_theme` 产出结构固定的 ``resolvedTheme``，
      前端渲染器只消费它，不自己拼主题注册表。
    - **共享对象落库**：主题是**数据库里的共享对象**，不是进程内内存 ——
      默认实例跟着会话仓库走（``RepositoryThemeStorage``），
      保存一次，所有引用它的报表下次解析就拿到新版本（契约 §8.3 的联动）。

联动语义（很重要）：
    保存主题**只更新主题这一份共享值**，绝不改任何草稿的定义
    （不动数据、FormatSpec、布局）。「一起更新外观」是**解析期**的效果 ——
    草稿下次 resolve / open / refresh 时重新解析主题，拿到的是新版本。
    ``affected_drafts`` 只用于给用户一份「影响摘要」，不做批量改写。

主题域（被"应用主题"清掉的旧局部覆盖）就是契约 0002 §3.4 的 style 白名单：
``fontFamily`` / ``fontSize`` / ``fontWeight`` / ``textColor`` /
``backgroundColor`` / ``textAlign`` / ``verticalAlign`` / ``lineHeight`` /
``padding`` / ``margin`` / ``border`` / ``borderRadius`` / ``opacity``。
不在该白名单里的 ``region.style`` 键**保持不动**（扩展位）。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from app.schemas.report_definition import ReportDefinitionDraft


logger = logging.getLogger(__name__)


#: 预置主题资源文件（`app/resources/report_themes.json`）。
THEME_RESOURCE_PATH = Path(__file__).resolve().parent.parent / "resources" / "report_themes.json"

#: 兜底主题 id：定义里没有主题 / 主题找不到时回退到这里。
DEFAULT_THEME_ID = "theme_default"


# ---------- 白名单 ----------

#: 主题 token 白名单（契约 0002 §8.1「tokens（白名单）」）。
THEME_TOKEN_KEYS: tuple[str, ...] = (
    "colorBackground",
    "colorSurface",
    "colorText",
    "colorTextMuted",
    "colorPrimary",
    "colorBorder",
    "colorAccent",
    "fontFamily",
    "fontSizeBase",
    "lineHeightBase",
    "radiusBase",
    "gapBase",
    "spacingUnit",
)

#: StyleSpec 白名单（契约 0002 §3.4，键名与取值形状完全一致）。
STYLE_SPEC_KEYS: tuple[str, ...] = (
    "fontFamily",
    "fontSize",
    "fontWeight",
    "textColor",
    "backgroundColor",
    "textAlign",
    "verticalAlign",
    "lineHeight",
    "padding",
    "margin",
    "border",
    "borderRadius",
    "opacity",
)

#: **主题管理的域**：应用主题时，这批键上的旧局部覆盖会被清掉，
#: 让主题的 ``defaultStyle`` 重新生效；此后用户再做局部 ``SET_STYLE``
#: 就是新的局部覆盖。``region.style`` 里不在这里的键**不动**。
THEME_MANAGED_STYLE_KEYS: frozenset[str] = frozenset(STYLE_SPEC_KEYS)

_TOKEN_KEY_SET = frozenset(THEME_TOKEN_KEYS)
_STYLE_KEY_SET = frozenset(STYLE_SPEC_KEYS)


# ---------- 异常 ----------

class ThemeError(Exception):
    """主题相关错误基类；``code`` 供上层分类（前端 / P0 路由直接透传）。"""

    code = "theme_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ThemeValidationError(ThemeError):
    """token / defaultStyle 越界（未知键、取值不合法、含被拒字符串）。"""

    code = "theme_validation_failed"


class ThemeConflictError(ThemeError):
    """乐观锁冲突：``expected_revision`` 与当前版本不一致。"""

    code = "theme_revision_conflict"


class ThemeNotFoundError(ThemeError):
    """主题不存在。"""

    code = "theme_not_found"


# ---------- 校验 ----------

#: 契约 §3.4 明确拒绝的东西（大小写不敏感）。
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("<", ">", "url(", "expression(", "javascript:")

#: 颜色：十六进制（#RGB / #RRGGBB / #RRGGBBAA）或纯字母关键字（white / transparent ...）。
_COLOR_RE = re.compile(r"^(#[0-9A-Fa-f]{3,8}|[A-Za-z]+)$")

#: 字体族：字母 / 数字 / 汉字 / 空格 / 引号 / 逗号 / 连字符（不收 `;` `(` `)`）。
_FONT_FAMILY_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff ,'\"\-\+\.]+$")

_TEXT_ALIGN_VALUES = frozenset({"LEFT", "CENTER", "RIGHT"})
_VERTICAL_ALIGN_VALUES = frozenset({"TOP", "MIDDLE", "BOTTOM"})
_BORDER_STYLE_VALUES = frozenset({"SOLID", "DASHED", "DOTTED", "NONE"})


def _reject_unsafe_text(value: str, *, where: str) -> None:
    lowered = value.lower()
    for needle in _FORBIDDEN_SUBSTRINGS:
        if needle in lowered:
            raise ThemeValidationError(
                f"{where} 含被拒绝的内容：{needle}",
                details={"field": where, "rejected": needle},
            )


def _check_text(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThemeValidationError(f"{where} 必须是非空字符串", details={"field": where})
    _reject_unsafe_text(value, where=where)
    return value


def _check_color(value: Any, *, where: str) -> str:
    text = _check_text(value, where=where)
    if not _COLOR_RE.match(text):
        raise ThemeValidationError(
            f"{where} 不是合法颜色（只接受十六进制或颜色关键字）",
            details={"field": where},
        )
    return text


def _check_number(value: Any, *, where: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ThemeValidationError(f"{where} 必须是数字", details={"field": where})
    if minimum is not None and value < minimum:
        raise ThemeValidationError(f"{where} 不能小于 {minimum}", details={"field": where})
    if maximum is not None and value > maximum:
        raise ThemeValidationError(f"{where} 不能大于 {maximum}", details={"field": where})
    return value


def _check_int(value: Any, *, where: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ThemeValidationError(f"{where} 必须是整数", details={"field": where})
    if minimum is not None and value < minimum:
        raise ThemeValidationError(f"{where} 不能小于 {minimum}", details={"field": where})
    if maximum is not None and value > maximum:
        raise ThemeValidationError(f"{where} 不能大于 {maximum}", details={"field": where})
    return value


def _check_length(value: Any, *, where: str, units: frozenset[str], number: type = int) -> dict[str, Any]:
    """校验 ``{value, unit}`` 形状（契约 §3.4）。"""
    if not isinstance(value, Mapping):
        raise ThemeValidationError(f"{where} 必须是 {{value, unit}} 对象", details={"field": where})
    extra = set(value.keys()) - {"value", "unit"}
    if extra:
        raise ThemeValidationError(
            f"{where} 含未知键：{sorted(extra)}",
            details={"field": where, "unknownKeys": sorted(extra)},
        )
    if "value" not in value or "unit" not in value:
        raise ThemeValidationError(f"{where} 缺 value / unit", details={"field": where})
    unit = value["unit"]
    if unit not in units:
        raise ThemeValidationError(
            f"{where}.unit 只能是 {sorted(units)}",
            details={"field": f"{where}.unit"},
        )
    raw = value["value"]
    if number is int:
        _check_int(raw, where=f"{where}.value")
    else:
        _check_number(raw, where=f"{where}.value")
    return {"value": raw, "unit": unit}


def _validate_token_value(key: str, value: Any) -> Any:
    where = f"tokens.{key}"
    if key.startswith("color"):
        return _check_color(value, where=where)
    if key == "fontFamily":
        text = _check_text(value, where=where)
        if not _FONT_FAMILY_RE.match(text):
            raise ThemeValidationError(
                f"{where} 含不允许的字符",
                details={"field": where},
            )
        return text
    if key in ("fontSizeBase", "radiusBase", "gapBase", "spacingUnit"):
        return _check_number(value, where=where, minimum=0)
    if key == "lineHeightBase":
        return _check_number(value, where=where, minimum=0)
    raise ThemeValidationError(f"未知 token：{key}", details={"field": where, "unknownKey": key})


def _validate_style_value(key: str, value: Any) -> Any:
    where = f"style.{key}"
    if key == "fontFamily":
        text = _check_text(value, where=where)
        if not _FONT_FAMILY_RE.match(text):
            raise ThemeValidationError(f"{where} 含不允许的字符", details={"field": where})
        return text
    if key == "fontSize":
        return _check_length(value, where=where, units=frozenset({"PX"}), number=int)
    if key == "fontWeight":
        return _check_int(value, where=where, minimum=100, maximum=900)
    if key in ("textColor", "backgroundColor"):
        return _check_color(value, where=where)
    if key == "textAlign":
        text = _check_text(value, where=where).upper()
        if text not in _TEXT_ALIGN_VALUES:
            raise ThemeValidationError(
                f"{where} 只能是 {sorted(_TEXT_ALIGN_VALUES)}",
                details={"field": where},
            )
        return text
    if key == "verticalAlign":
        text = _check_text(value, where=where).upper()
        if text not in _VERTICAL_ALIGN_VALUES:
            raise ThemeValidationError(
                f"{where} 只能是 {sorted(_VERTICAL_ALIGN_VALUES)}",
                details={"field": where},
            )
        return text
    if key == "lineHeight":
        return _check_length(value, where=where, units=frozenset({"PX", "PERCENT"}), number=float)
    if key in ("padding", "margin"):
        return _check_length(value, where=where, units=frozenset({"PX"}), number=int)
    if key == "border":
        if not isinstance(value, Mapping):
            raise ThemeValidationError(f"{where} 必须是对象", details={"field": where})
        extra = set(value.keys()) - {"width", "style", "color"}
        if extra:
            raise ThemeValidationError(
                f"{where} 含未知键：{sorted(extra)}",
                details={"field": where, "unknownKeys": sorted(extra)},
            )
        missing = {"width", "style", "color"} - set(value.keys())
        if missing:
            raise ThemeValidationError(
                f"{where} 缺 {sorted(missing)}",
                details={"field": where},
            )
        border_style = _check_text(value["style"], where=f"{where}.style").upper()
        if border_style not in _BORDER_STYLE_VALUES:
            raise ThemeValidationError(
                f"{where}.style 只能是 {sorted(_BORDER_STYLE_VALUES)}",
                details={"field": f"{where}.style"},
            )
        return {
            "width": _check_int(value["width"], where=f"{where}.width", minimum=0),
            "style": border_style,
            "color": _check_color(value["color"], where=f"{where}.color"),
        }
    if key == "borderRadius":
        return _check_int(value, where=where, minimum=0)
    if key == "opacity":
        return _check_number(value, where=where, minimum=0.0, maximum=1.0)
    raise ThemeValidationError(f"未知 style 键：{key}", details={"field": where, "unknownKey": key})


def validate_tokens(tokens: Any) -> dict[str, Any]:
    """校验并规范化 token 集合；未知键 / 非法取值直接抛 :class:`ThemeValidationError`。"""
    if tokens is None:
        return {}
    if not isinstance(tokens, Mapping):
        raise ThemeValidationError("tokens 必须是对象", details={"field": "tokens"})
    unknown = sorted(set(tokens.keys()) - _TOKEN_KEY_SET)
    if unknown:
        raise ThemeValidationError(
            f"未知 token：{unknown}",
            details={"field": "tokens", "unknownKeys": unknown},
        )
    return {key: _validate_token_value(key, tokens[key]) for key in tokens}


def validate_style(style: Any) -> dict[str, Any]:
    """校验并规范化 StyleSpec（契约 §3.4 白名单）。"""
    if style is None:
        return {}
    if not isinstance(style, Mapping):
        raise ThemeValidationError("defaultStyle 必须是对象", details={"field": "defaultStyle"})
    unknown = sorted(set(style.keys()) - _STYLE_KEY_SET)
    if unknown:
        raise ThemeValidationError(
            f"未知 style 键：{unknown}",
            details={"field": "defaultStyle", "unknownKeys": unknown},
        )
    return {key: _validate_style_value(key, style[key]) for key in style}


# ---------- 持久化 Adapter ----------

@runtime_checkable
class ThemeStorage(Protocol):
    """主题持久化 Adapter 接口（P2 接 SQLite 时实现这份契约）。

    默认实现是内存版（进程内字典），**不建表、不连数据库**。
    """

    def list_theme_ids(self) -> list[str]:
        """返回存储里全部主题 id（含自定义；是否含预置由实现自决）。"""

    def load_theme(self, theme_id: str) -> dict | None:
        """按 id 读一个完整主题对象；没有则返回 ``None``。"""

    def save_theme(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: dict[str, Any],
        default_style: dict[str, Any],
        theme_revision: int,
    ) -> dict[str, Any]:
        """原子写入一个新版本并返回完整主题对象。"""

    def affected_drafts(self, theme_id: str) -> list[dict[str, Any]]:
        """返回正在用这个主题的草稿清单（``[{"draftId", "name"}]``）。"""


class InMemoryThemeStorage:
    """默认的内存实现：进程内字典存自定义主题与版本。

    主题注册表**不硬编码在代码里**：预置主题从
    ``app/resources/report_themes.json`` 读；本类只负责**自定义主题**。
    """

    def __init__(self) -> None:
        self._themes: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def list_theme_ids(self) -> list[str]:
        with self._lock:
            return list(self._themes.keys())

    def load_theme(self, theme_id: str) -> dict[str, Any] | None:
        with self._lock:
            theme = self._themes.get(theme_id)
            return copy.deepcopy(theme) if theme is not None else None

    def save_theme(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: dict[str, Any],
        default_style: dict[str, Any],
        theme_revision: int,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            existing = self._themes.get(theme_id) or {}
            theme = {
                "themeId": theme_id,
                "name": name,
                "builtin": False,
                "themeRevision": int(theme_revision),
                "tokens": copy.deepcopy(tokens),
                "defaultStyle": copy.deepcopy(default_style),
                "createdAt": existing.get("createdAt") or now,
                "updatedAt": now,
            }
            self._themes[theme_id] = theme
            return copy.deepcopy(theme)

    def affected_drafts(self, theme_id: str) -> list[dict[str, Any]]:
        # 内存版没有草稿表，影响清单恒为空；接上 SQLite 后由那边给出。
        return []


class RepositoryThemeStorage:
    """跟着**会话仓库**走的主题存储（**按调用时**解析）。

    为什么按调用时解析（和 ``draft_workspace._follow_session_repo`` 同一套做法）：
    测试会把 ``session_service._repo`` 换成临时库，主题必须跟着换 ——
    否则**测试会把主题写进用户的库**（``backend/data/haewu.db``）。
    生产上会话库与草稿表本来就是同一个文件，跟着走即可。

    这里用**函数体内延迟 import** 拿 ``draft_workspace.adapter``：
    ``app.services.draft_workspace`` 本身会拉起一串服务层依赖，
    模块顶层互相 import 容易成环；延迟 import 也保证
    **模块导入时不会顺手打开数据库**（导入不该有写库的副作用）。
    """

    def _adapter(self) -> Any:
        """当前生效的草稿存储适配器（跟随会话仓库）。"""
        from app.services.draft_workspace import draft_workspace

        return draft_workspace.adapter

    def list_theme_ids(self) -> list[str]:
        return list(self._adapter().list_theme_ids())

    def load_theme(self, theme_id: str) -> dict[str, Any] | None:
        return self._adapter().load_theme(theme_id)

    def save_theme(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: dict[str, Any],
        default_style: dict[str, Any],
        theme_revision: int,
    ) -> dict[str, Any]:
        return self._adapter().save_theme(
            theme_id,
            name=name,
            tokens=tokens,
            default_style=default_style,
            theme_revision=theme_revision,
        )

    def affected_drafts(self, theme_id: str) -> list[dict[str, Any]]:
        return list(self._adapter().affected_drafts(theme_id))


# ---------- 工具 ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_theme_id() -> str:
    return f"theme_custom_{uuid.uuid4().hex[:8]}"


def _iter_regions(
    regions: Sequence[Any] | None,
    ancestors: tuple[dict[str, Any], ...] = (),
) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """递归遍历区域树（含容器），产出 ``(region, ancestors)``。

    兼容两种形状：顶层扁平列表，以及容器区域自己的 ``children``。
    """
    for region in regions or []:
        if not isinstance(region, Mapping):
            continue
        chain = list(ancestors)
        # 产出**原对象**（不是拷贝）：调用方要能就地改 region（应用主题要清 style）。
        yield region, chain
        children = region.get("children")
        if isinstance(children, list):
            yield from _iter_regions(children, tuple(chain + [region]))


def _find_region(
    regions: Sequence[Any] | None,
    region_id: str,
    ancestors: tuple[dict[str, Any], ...] = (),
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    for region, chain in _iter_regions(regions, ancestors):
        if region.get("id") == region_id:
            return region, chain
    return None, []


def _to_draft(definition: Any) -> ReportDefinitionDraft:
    """把入参转成**独立副本**的 ``ReportDefinitionDraft``（不改入参）。"""
    if isinstance(definition, ReportDefinitionDraft):
        return definition.model_copy(deep=True)
    if isinstance(definition, Mapping):
        return ReportDefinitionDraft.model_validate(copy.deepcopy(dict(definition)))
    raise ThemeValidationError(
        "definition 必须是 ReportDefinitionDraft 或 dict",
        details={"field": "definition"},
    )


def _theme_ref_of(definition: Any) -> str:
    """读定义里的主题引用（``theme.themeRef``，兼容 ``theme.themeId``）。"""
    if isinstance(definition, ReportDefinitionDraft):
        theme = definition.theme
    elif isinstance(definition, Mapping):
        theme = definition.get("theme") or {}
    else:
        theme = {}
    if not isinstance(theme, Mapping):
        return ""
    ref = theme.get("themeRef") or theme.get("themeId") or ""
    return str(ref or "").strip()


def _merge_style(base: dict[str, Any], extra: Any) -> None:
    """按 key 覆盖式合并（值是标量或 ``{value, unit}`` 小对象，整键替换）。"""
    if not isinstance(extra, Mapping):
        return
    for key, value in extra.items():
        base[key] = copy.deepcopy(value)


# ---------- 区域上的「实际样式层」 ----------

def _style_payload_of(source: Any) -> dict[str, Any] | None:
    """样式源里**真正持有样式字典**的那一层。

    与前端 ``workbench_presentation.js`` 的 ``applyStyleInto`` **同一口径**：
    ``{"style": {...}}`` 这种形态取内层的 ``style``，否则就是它自己。

    这两种写法在契约 0002 §3 的样式层里都出现过 —— 服务端要是按「ref 本身」
    去清，``{"style": {"backgroundColor": ...}}`` 这种就会**清不到内层**，
    旧颜色仍然盖在新主题上（浏览器侧实测过）。
    """
    if not isinstance(source, dict):
        return None
    inner = source.get("style")
    if isinstance(inner, dict):
        return inner
    return source


def theme_style_layers(region: Any) -> list[tuple[str, Any, Any]]:
    """区域上的**实际样式层**：``(路径, 容器, 键)``。

    前端渲染器（``frontend/pages/workbench_presentation.js`` 的
    ``nodeStyleSources()`` / ``nodeComponentStyleSources()``）会应用这四层：

    1. ``region.styleRefs``（**数组或对象**两种形态都可能出现）、
    2. ``region.style``、
    3. ``region.component.style``、
    4. ``region.component.styleHints[].style``。

    **只清一层**的话，其余层里压着的旧颜色还盖在新主题上 ——
    换主题看起来没生效。

    容器是**真正持有**这个样式字典的地方，键是它在那里的键名或下标 ——
    调用方要**换掉**这一层时用得上。
    """
    layers: list[tuple[str, Any, Any]] = []
    if not isinstance(region, dict):
        return layers
    if isinstance(region.get("style"), dict):
        layers.append(("/style", region, "style"))
    refs = region.get("styleRefs")
    if isinstance(refs, dict):
        payload = _style_payload_of(refs)
        if payload is not None:
            if payload is refs:
                layers.append(("/styleRefs", region, "styleRefs"))
            else:
                layers.append(("/styleRefs/style", refs, "style"))
    elif isinstance(refs, list):
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                continue
            payload = _style_payload_of(ref)
            if payload is None:
                continue
            if payload is ref:
                layers.append((f"/styleRefs/{index}", refs, index))
            else:
                layers.append((f"/styleRefs/{index}/style", ref, "style"))
    component = region.get("component")
    if isinstance(component, dict):
        if isinstance(component.get("style"), dict):
            layers.append(("/component/style", component, "style"))
        hints = component.get("styleHints")
        if isinstance(hints, list):
            for index, hint in enumerate(hints):
                if isinstance(hint, dict) and isinstance(hint.get("style"), dict):
                    layers.append((f"/component/styleHints/{index}/style", hint, "style"))
        elif isinstance(hints, dict) and isinstance(hints.get("style"), dict):
            # 服务端 ``resolve_region_style`` 也认这一形态（``styleHints`` 是对象）。
            layers.append(("/component/styleHints/style", hints, "style"))
    return layers


def clear_theme_managed_style_layers(
    region: Any,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """把区域上**各样式层**里属于主题域的键清掉。

    - 只删 :data:`THEME_MANAGED_STYLE_KEYS` 里的键 —— 扩展位（不在这里的键）
      以及**格式 / 布局 / 数据**一个字节都不动；
    - **不就地改**样式字典：每一层换上一份清过的副本 ——
      **共享样式源**（同一份字典被别处引用）因此**不被改动**；
    - 清空的 ``region.style`` 按既有约定 pop 掉；其它层保持原位
      （不发明新语义：那一层本来就是空的样式）。

    返回 ``[(路径, 清之前, 清之后), ...]``（只含有改动的那几层）。
    """
    changes: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for path, container, key in theme_style_layers(region):
        current = container[key]
        if not any(name in THEME_MANAGED_STYLE_KEYS for name in current):
            continue
        before = copy.deepcopy(dict(current))
        cleaned = {
            name: value
            for name, value in current.items()
            if name not in THEME_MANAGED_STYLE_KEYS
        }
        if path == "/style" and not cleaned:
            # 既有约定：``region.style`` 清空就 pop 掉。
            container.pop(key, None)
        else:
            container[key] = cleaned
        changes.append((path, before, copy.deepcopy(cleaned)))
    return changes


# ---------- 主题服务 ----------

class ThemeModule:
    """共享主题模块（契约 0002 §8.2）。

    ``ThemeModule`` 是**纯逻辑**的：解析、应用、校验、影响摘要；
    真正的落库交给注入进来的 :class:`ThemeStorage`。
    """

    def __init__(self, storage: ThemeStorage | None = None) -> None:
        self._storage: ThemeStorage = storage if storage is not None else RepositoryThemeStorage()
        self._builtin: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load_builtin_themes()

    # ---------- 存储注入 ----------

    @property
    def storage(self) -> ThemeStorage:
        return self._storage

    def set_storage(self, storage: ThemeStorage) -> None:
        """换掉持久化实现（测试里塞内存实现、或换别的库时用这个）。"""
        with self._lock:
            self._storage = storage if storage is not None else RepositoryThemeStorage()

    # ---------- 预置主题 ----------

    def _load_builtin_themes(self) -> None:
        """从资源文件读预置主题；**文件缺失 / 损坏时降级为内置最小主题，不抛异常**。

        这里是**回落层**：读主题时**先查 storage**，storage 里没有才回落这里
        （见 :meth:`get_theme`）。所以这个方法**不碰数据库** ——
        模块导入时不该顺手打开用户的库。
        """
        raw: Any = None
        try:
            if THEME_RESOURCE_PATH.is_file():
                raw = json.loads(THEME_RESOURCE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 —— 资源文件坏了也不能让 import 失败
            logger.warning("读取预置主题资源失败，降级为内置最小主题：%s", exc)
            raw = None

        themes: list[Any] = []
        if isinstance(raw, Mapping):
            candidate = raw.get("themes")
            if isinstance(candidate, list):
                themes = candidate
        elif isinstance(raw, list):
            themes = raw

        loaded: dict[str, dict[str, Any]] = {}
        for item in themes:
            if not isinstance(item, Mapping):
                continue
            theme_id = str(item.get("themeId") or "").strip()
            if not theme_id:
                continue
            tokens = validate_tokens(item.get("tokens") or {})
            default_style = validate_style(item.get("defaultStyle") or {})
            loaded[theme_id] = {
                "themeId": theme_id,
                "name": str(item.get("name") or theme_id),
                "builtin": True,
                "themeRevision": int(item.get("themeRevision") or 1),
                "tokens": tokens,
                "defaultStyle": default_style,
                "createdAt": str(item.get("createdAt") or item.get("updatedAt") or _now()),
                "updatedAt": str(item.get("updatedAt") or _now()),
            }

        if not loaded:
            loaded = copy.deepcopy(_FALLBACK_BUILTIN_THEMES)
            logger.warning("预置主题资源为空或不可用，使用内置最小主题集合")

        with self._lock:
            self._builtin = loaded

    def builtin_theme_ids(self) -> list[str]:
        with self._lock:
            return list(self._builtin.keys())

    # ---------- 读 ----------

    def list_themes(self) -> list[dict[str, Any]]:
        """预置 + 自定义，返回 ``[{themeId, name, builtin, themeRevision, updatedAt}]``。"""
        summaries: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self._lock:
            builtin_ids = list(self._builtin.keys())
        for theme_id in builtin_ids:
            seen.add(theme_id)
            # 走 get_theme：库里存了新版本就用库里的（联动的前提）。
            theme = self.get_theme(theme_id)
            if theme:
                summaries.append(_theme_summary(theme))
        for theme_id in self._storage.list_theme_ids():
            if theme_id in seen:
                continue
            theme = self._storage.load_theme(theme_id)
            if not theme:
                continue
            seen.add(theme_id)
            summaries.append(_theme_summary(theme))
        summaries.sort(key=lambda item: (not item.get("builtin", False), str(item.get("themeId"))))
        return summaries

    def get_theme(self, theme_id: str) -> dict[str, Any] | None:
        """返回完整主题对象（**先查 storage，再回落预置资源**）；没有则 ``None``。

        库里存了新版本就用库里的 —— 这是联动的地基：
        预置主题被改过版本后**仍然算预置主题**（``builtin=True``），
        但 name / tokens / defaultStyle / themeRevision 用库里那份。
        """
        key = str(theme_id or "").strip()
        if not key:
            return None
        stored = self._storage.load_theme(key)
        with self._lock:
            builtin = copy.deepcopy(self._builtin.get(key))
        if stored is None:
            return builtin
        theme = copy.deepcopy(stored)
        if builtin is not None:
            theme["builtin"] = True
        return theme

    def resolve_theme(self, definition: Any, *, theme_id: str | None = None) -> dict[str, Any]:
        """服务端解析主题，返回 ``resolvedTheme``。

        ``theme_id`` 缺省时从 ``definition.theme.themeRef`` 读；读不到
        （没有主题 / 主题不存在）回退 ``theme_default``。
        """
        resolved_id = str(theme_id or "").strip() or _theme_ref_of(definition)
        theme = self.get_theme(resolved_id) if resolved_id else None
        if theme is None:
            theme = self.get_theme(DEFAULT_THEME_ID)
        if theme is None:
            theme = copy.deepcopy(_FALLBACK_BUILTIN_THEMES[DEFAULT_THEME_ID])
        return {
            "themeId": str(theme.get("themeId") or DEFAULT_THEME_ID),
            "themeRevision": int(theme.get("themeRevision") or 1),
            "name": str(theme.get("name") or DEFAULT_THEME_ID),
            "tokens": copy.deepcopy(theme.get("tokens") or {}),
            "defaultStyle": copy.deepcopy(theme.get("defaultStyle") or {}),
        }

    # ---------- 应用 ----------

    def apply_theme(self, definition: Any, theme_id: str) -> ReportDefinitionDraft:
        """应用主题：清掉区域上的**旧主题域外观覆盖**，让主题重新生效。

        - **清掉区域上各样式层**里属于主题域的键（§3.4 白名单）：
          ``region.style``、``region.styleRefs``（数组或对象）、
          ``region.component.style``、``region.component.styleHints[].style``
          —— 前端渲染器会应用这四层，只清一层的话旧颜色还压在新主题上；
        - **不动**各层里其它键（扩展位）；
        - **不动** 布局 / 统计 / FormatSpec（``layout`` / ``component.arrangement`` /
          ``data`` / ``binding`` / ``format`` / ``component.metric.format`` /
          ``component.columns[].format``）；
        - 返回**新**定义，不改入参。
        """
        resolved = self.resolve_theme(definition, theme_id=theme_id)
        draft = _to_draft(definition)

        draft.theme = {
            **(draft.theme or {}),
            "themeRef": resolved["themeId"],
            "version": f"{resolved['themeRevision']}.0",
        }

        for region, _ancestors in _iter_regions(draft.regions):
            clear_theme_managed_style_layers(region)
        return draft

    # ---------- 样式继承（服务端算一份，供前端对比 / 落库用） ----------

    def resolve_region_style(
        self,
        definition: Any,
        region_id: str,
        *,
        resolved_theme: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """算出一个区域的**最终 style**。

        合并顺序（契约 §5.3）：主题 ``defaultStyle`` → 祖先 ``region.style``
        → 区域自身 ``region.style`` → 组件字段样式
        （``component.style`` 与 ``component.styleHints[].style`` 中的 StyleSpec 对象；
        行级条件样式的字符串值如 ``"RED"`` 不参与合并）。
        """
        regions = _definition_regions(definition)
        region, ancestors = _find_region(regions, region_id)
        if region is None:
            return {}

        theme = resolved_theme or self.resolve_theme(definition)
        style: dict[str, Any] = {}
        _merge_style(style, theme.get("defaultStyle") or {})
        for ancestor in ancestors:
            _merge_style(style, ancestor.get("style"))
        _merge_style(style, region.get("style"))
        _merge_style(style, _component_style(region.get("component")))
        return style

    # ---------- 写 ----------

    def _resolve_default_style(
        self,
        default_style: Mapping[str, Any] | None,
        current: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """默认样式：给了就校验后用；**没给（None）就沿用当前主题的默认样式**。

        沿用而不是清空：保存预置主题的新版本时，**预置的 defaultStyle 不能丢**。
        想清空就显式传空字典。
        """
        if default_style is not None:
            return validate_style(default_style)
        if current:
            existing = current.get("defaultStyle")
            if isinstance(existing, Mapping):
                return copy.deepcopy(dict(existing))
        return {}

    def save_theme(
        self,
        name: str,
        tokens: Mapping[str, Any],
        *,
        theme_id: str | None = None,
        default_style: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """**只算影响摘要，不落库**。

        返回 ``{themeId, themeRevision, nextRevision, affectedDrafts,
        affectedCount, requiresConfirm}``；确认后调
        :meth:`commit_theme_save` 才真正写入。
        """
        theme_name = _check_text(name, where="name")
        validated_tokens = validate_tokens(tokens)
        target_id = str(theme_id or "").strip() or _new_theme_id()
        current = self.get_theme(target_id)
        validated_style = self._resolve_default_style(default_style, current)
        current_revision = int(current.get("themeRevision") or 0) if current else 0
        affected = self._storage.affected_drafts(target_id) or []
        affected = [
            {"draftId": str(item.get("draftId") or ""), "name": str(item.get("name") or "")}
            for item in affected
            if isinstance(item, Mapping)
        ]
        return {
            "themeId": target_id,
            "themeRevision": current_revision,
            "nextRevision": current_revision + 1,
            "name": theme_name,
            "tokens": validated_tokens,
            "defaultStyle": validated_style,
            "affectedDrafts": affected,
            "affectedCount": len(affected),
            "requiresConfirm": True,
        }

    def commit_theme_save(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: Mapping[str, Any],
        default_style: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """真正落库：版本 +1，原子提交。

        ``expected_revision`` 与当前版本不一致 → :class:`ThemeConflictError`。

        只写**主题这一份共享值**：不改任何草稿的定义（联动是解析期的事）。
        """
        target_id = str(theme_id or "").strip()
        if not target_id:
            raise ThemeValidationError("theme_id 不能为空", details={"field": "themeId"})
        theme_name = _check_text(name, where="name")
        validated_tokens = validate_tokens(tokens)

        current = self.get_theme(target_id)
        validated_style = self._resolve_default_style(default_style, current)
        current_revision = int(current.get("themeRevision") or 0) if current else 0
        if expected_revision is not None:
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise ThemeValidationError(
                    "expected_revision 必须是整数",
                    details={"field": "expectedRevision"},
                ) from exc
            if expected != current_revision:
                raise ThemeConflictError(
                    f"主题 {target_id} 当前版本为 {current_revision}，与期望版本 {expected} 不一致",
                    details={
                        "themeId": target_id,
                        "currentRevision": current_revision,
                        "expectedRevision": expected,
                    },
                )

        next_revision = current_revision + 1
        saved = self._storage.save_theme(
            target_id,
            name=theme_name,
            tokens=validated_tokens,
            default_style=validated_style,
            theme_revision=next_revision,
        )
        return copy.deepcopy(saved)

    def theme_impact(self, theme_id: str) -> dict[str, Any]:
        """返回 ``{themeId, themeRevision, affectedDrafts, affectedCount}``。"""
        target_id = str(theme_id or "").strip()
        theme = self.get_theme(target_id)
        if theme is None:
            raise ThemeNotFoundError(f"主题不存在：{target_id}", details={"themeId": target_id})
        affected = self._storage.affected_drafts(target_id) or []
        affected = [
            {"draftId": str(item.get("draftId") or ""), "name": str(item.get("name") or "")}
            for item in affected
            if isinstance(item, Mapping)
        ]
        return {
            "themeId": target_id,
            "themeRevision": int(theme.get("themeRevision") or 1),
            "affectedDrafts": affected,
            "affectedCount": len(affected),
        }


def _definition_regions(definition: Any) -> list[Any]:
    if isinstance(definition, ReportDefinitionDraft):
        return list(definition.regions or [])
    if isinstance(definition, Mapping):
        return list(definition.get("regions") or [])
    return []


def _component_style(component: Any) -> dict[str, Any]:
    """取组件上的样式字段（StyleSpec 对象形态；行级条件样式字符串忽略）。"""
    if not isinstance(component, Mapping):
        return {}
    merged: dict[str, Any] = {}
    direct = component.get("style")
    if isinstance(direct, Mapping):
        _merge_style(merged, direct)
    hints = component.get("styleHints")
    if isinstance(hints, Mapping):
        hint_style = hints.get("style")
        if isinstance(hint_style, Mapping):
            _merge_style(merged, hint_style)
    elif isinstance(hints, list):
        for hint in hints:
            if not isinstance(hint, Mapping):
                continue
            hint_style = hint.get("style")
            if isinstance(hint_style, Mapping):
                _merge_style(merged, hint_style)
    return merged


def _theme_summary(theme: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "themeId": str(theme.get("themeId") or ""),
        "name": str(theme.get("name") or ""),
        "builtin": bool(theme.get("builtin", False)),
        "themeRevision": int(theme.get("themeRevision") or 1),
        "updatedAt": str(theme.get("updatedAt") or ""),
    }


#: 资源文件缺失时的**最小兜底主题**（保证 import 永远安全）。
_FALLBACK_BUILTIN_THEMES: dict[str, dict[str, Any]] = {
    DEFAULT_THEME_ID: {
        "themeId": DEFAULT_THEME_ID,
        "name": "默认主题",
        "builtin": True,
        "themeRevision": 1,
        "tokens": {
            "colorBackground": "#FFFFFF",
            "colorSurface": "#F7F9FC",
            "colorText": "#1F2329",
            "colorTextMuted": "#646A73",
            "colorPrimary": "#2F6BFF",
            "colorBorder": "#E3E6EB",
            "colorAccent": "#00B7A3",
            "fontFamily": "PingFang SC, Microsoft YaHei, sans-serif",
            "fontSizeBase": 14,
            "lineHeightBase": 1.6,
            "radiusBase": 8,
            "gapBase": 12,
            "spacingUnit": 8,
        },
        "defaultStyle": {
            "fontFamily": "PingFang SC, Microsoft YaHei, sans-serif",
            "fontSize": {"value": 14, "unit": "PX"},
            "fontWeight": 400,
            "textColor": "#1F2329",
            "backgroundColor": "#FFFFFF",
            "textAlign": "LEFT",
            "verticalAlign": "MIDDLE",
            "lineHeight": {"value": 1.6, "unit": "PERCENT"},
            "padding": {"value": 12, "unit": "PX"},
            "margin": {"value": 0, "unit": "PX"},
            "border": {"width": 1, "style": "SOLID", "color": "#E3E6EB"},
            "borderRadius": 8,
            "opacity": 1,
        },
        "createdAt": _now(),
        "updatedAt": _now(),
    }
}


#: 模块级单例：P0 / P2 直接用它。
#: 存储走 ``RepositoryThemeStorage`` —— **按调用时**跟随会话仓库，
#: 所以生产环境落用户库、测试环境落临时库（不污染用户库）。
theme_service = ThemeModule()

#: 兼容别名（契约里两种叫法都出现过）。
theme_module = theme_service


def set_theme_storage(storage: ThemeStorage) -> None:
    """注入主题持久化 Adapter（默认已经是 SQLite Adapter，这里是给测试换实现用的）。"""
    theme_service.set_storage(storage)


def get_theme_storage() -> ThemeStorage:
    """当前生效的持久化 Adapter。"""
    return theme_service.storage


__all__ = [
    "DEFAULT_THEME_ID",
    "InMemoryThemeStorage",
    "RepositoryThemeStorage",
    "STYLE_SPEC_KEYS",
    "THEME_MANAGED_STYLE_KEYS",
    "THEME_RESOURCE_PATH",
    "THEME_TOKEN_KEYS",
    "ThemeConflictError",
    "ThemeError",
    "ThemeModule",
    "ThemeNotFoundError",
    "ThemeStorage",
    "ThemeValidationError",
    "clear_theme_managed_style_layers",
    "get_theme_storage",
    "set_theme_storage",
    "theme_module",
    "theme_service",
    "theme_style_layers",
    "validate_style",
    "validate_tokens",
]
