"""数据来源模式：**模拟示例** / **用户 JSON**（工单 #59 能力层）。

**为什么是一等契约**

「这份报表的数字从哪来」以前不是服务端记录的事实，而是从上下文里**猜**出来的：
看标题像不像示例、看板块名里有没有「模拟」、看提示词里有没有「用 JSON」。
那种猜法在「用户自己贴了一份 JSON」和「系统造的示例数据」同时存在时会直接说错，
所以本模块把来源模式变成**服务端记录的一等事实**：

    模拟示例（simulated）  —— 系统按确定性种子造的示例业务数据（ADR 0004）
    用户 JSON（user_json）  —— 用户自己提供的数据，未经业务核验

**判定只认服务端记录的值**，不认标题、板块名或提示词：

    - 记录在定义级 ``metadata.sourceMode``；
    - 记录在区域级 ``templateBinding.sourceMode``；
    - 记录在区域级 ``templateBinding.previewSpec.sourceMode``。

三处都没有 → 用**默认口径**（模拟示例，ADR 0004：模板态一律用模拟业务数据渲染），
并且 ``recorded=False`` —— 调用方据此知道「这是默认值，不是谁显式声明的」。

**与既有真实绑定分离**（规格决定 2）：来源模式是**并列的另一个维度**。
区域上的 ``metricRefs`` / ``dataStatus`` 表达的是**真实数据绑定**，
来源模式不覆盖它、也不被它覆盖：真实绑定该受目录约束还是受目录约束，
模拟模式只是说「屏上这一屏数字的来源是模拟示例」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


#: 模拟示例：系统按固定种子生成的示例业务数据（ADR 0004）。
SOURCE_MODE_SIMULATED = "simulated"

#: 用户 JSON：用户自己提供的数据，未经业务核验。
SOURCE_MODE_USER_JSON = "user_json"

#: 合法的来源模式取值。
SOURCE_MODE_VALUES = (SOURCE_MODE_SIMULATED, SOURCE_MODE_USER_JSON)

#: 默认来源模式。模板态一律用模拟业务数据渲染（ADR 0004 §2.1）。
DEFAULT_SOURCE_MODE = SOURCE_MODE_SIMULATED

#: 来源模式的**记录键**（定义级 metadata、区域级 templateBinding 与 previewSpec 共用）。
SOURCE_MODE_KEY = "sourceMode"

#: 来源模式的用户可见文案。界面必须把文案显示出来。
SOURCE_MODE_LABELS: dict[str, str] = {
    SOURCE_MODE_SIMULATED: "模拟示例数据",
    SOURCE_MODE_USER_JSON: "用户提供的 JSON 数据（未经业务核验）",
}

#: 区域级来源标记（载荷里的 ``dataSource``）与来源模式的对应关系。
#:
#: 模拟示例 → ``simulated``；用户 JSON → ``user_json``。
DATA_SOURCE_BY_MODE: dict[str, str] = {
    SOURCE_MODE_SIMULATED: "simulated",
    SOURCE_MODE_USER_JSON: "user_json",
}


@dataclass(frozen=True)
class SourceMode:
    """一个**已解析**的来源模式。

    ``recorded`` 表示这个值是服务端**显式记录**的（True）还是走了默认口径（False）。
    校验层与回复口径据此区分「谁声明的来源」与「系统兜底的来源」。
    """

    mode: str
    label: str
    recorded: bool

    @property
    def is_simulated(self) -> bool:
        """是不是模拟示例模式。"""
        return self.mode == SOURCE_MODE_SIMULATED

    @property
    def is_user_json(self) -> bool:
        """是不是用户 JSON 模式。"""
        return self.mode == SOURCE_MODE_USER_JSON

    @property
    def data_source(self) -> str:
        """区域级来源标记（载荷里的 ``dataSource``）。"""
        return DATA_SOURCE_BY_MODE.get(self.mode, DATA_SOURCE_BY_MODE[SOURCE_MODE_SIMULATED])


def normalize_source_mode(value: Any) -> str | None:
    """把任意输入归一成合法来源模式。

    认不出来返回 ``None`` —— **不猜**。来源模式要么是服务端记录的事实，
    要么就没有；凭标题或提示词猜出来一个模式，正是本模块要根除的做法。
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in SOURCE_MODE_VALUES:
            return text
    return None


def resolve_source_mode(recorded: Any = None) -> SourceMode:
    """把「服务端记录的值」解析成来源模式。

    没有记录（或记录了非法值）→ 默认口径（模拟示例），``recorded=False``。
    """
    mode = normalize_source_mode(recorded)
    if mode is None:
        return SourceMode(
            DEFAULT_SOURCE_MODE, SOURCE_MODE_LABELS[DEFAULT_SOURCE_MODE], False
        )
    return SourceMode(mode, SOURCE_MODE_LABELS[mode], True)


def source_mode_label(mode: Any) -> str:
    """来源模式的用户可见文案。"""
    return resolve_source_mode(mode).label


def _binding_of(region: Any) -> Mapping[str, Any]:
    if not isinstance(region, Mapping):
        return {}
    binding = region.get("templateBinding")
    return binding if isinstance(binding, Mapping) else {}


def source_mode_of_region(region: Any) -> SourceMode:
    """区域级来源模式。

    读取顺序（都来自服务端记录的结构，不看标题）：
    ``templateBinding.sourceMode`` → ``templateBinding.previewSpec.sourceMode``
    → ``region.sourceMode`` → 默认口径。
    """
    if not isinstance(region, Mapping):
        return resolve_source_mode(None)
    binding = _binding_of(region)
    for candidate in (
        binding.get(SOURCE_MODE_KEY),
        (binding.get("previewSpec") or {}).get(SOURCE_MODE_KEY)
        if isinstance(binding.get("previewSpec"), Mapping)
        else None,
        region.get(SOURCE_MODE_KEY),
    ):
        mode = normalize_source_mode(candidate)
        if mode:
            return SourceMode(mode, SOURCE_MODE_LABELS[mode], True)
    return resolve_source_mode(None)


def source_mode_of_definition(definition: Any) -> SourceMode:
    """定义级来源模式。

    读取顺序：``metadata.sourceMode`` → 任一区域的 ``templateBinding.sourceMode``
    → 默认口径。区域之间的模式**应当一致**（一份报表不混两种来源的数字，
    规格 US51）；不一致时以定义级记录为准。
    """
    if not isinstance(definition, Mapping):
        return resolve_source_mode(None)
    metadata = definition.get("metadata")
    if isinstance(metadata, Mapping):
        mode = normalize_source_mode(metadata.get(SOURCE_MODE_KEY))
        if mode:
            return SourceMode(mode, SOURCE_MODE_LABELS[mode], True)
    for region in definition.get("regions") or []:
        mode = normalize_source_mode(
            (_binding_of(region) or {}).get(SOURCE_MODE_KEY)
        )
        if mode:
            return SourceMode(mode, SOURCE_MODE_LABELS[mode], True)
    return resolve_source_mode(None)


def record_source_mode(target: dict[str, Any] | None, mode: Any) -> dict[str, Any]:
    """把来源模式**记录**进一个结构（定义 metadata / 区域 binding / previewSpec）。

    记录的是**归一后的合法取值**：非法值不落库（宁可没有记录，也不要一个
    认不出来的来源模式混进定义里）。
    """
    normalized = normalize_source_mode(mode)
    if target is None or normalized is None:
        return target if isinstance(target, dict) else {}
    target[SOURCE_MODE_KEY] = normalized
    return target
