"""可见对象解析：编辑态（USER_EDITING_TEMPLATE）专用。

给第二波集成方调用的**纯函数**模块：不改草稿、不调模型、不依赖编辑引擎。

核心口径（来自 20261022 template-editing react 架构改进方案）：

* 「第三个」按**客户可见的正文卡片顺序**定位，不把后台可能包含的隐藏标题区
  （`role == "REPORT_TITLE"`）算进去 —— 这与前端 `workbench.js` 的
  `isTitleRegion()` 判定一致。
* 可见顺序以**前端回传的正文卡片 ID 顺序**为准（用户实际看到的顺序）；
  前端没给时，退回「草稿 region 顺序去掉隐藏标题区」这一兜底口径。
* 定位不到唯一对象时，输出**需要追问的候选**，而不是猜一个。

模糊的按名字匹配（如「那个耗电图」）仍由 `TemplateEditEngine` 的既有规则负责，
本模块只做「位置序数」和「指代词」这两类确定性解析，避免与既有规则抢活。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------- 状态 ----------

STATUS_RESOLVED = "RESOLVED"
STATUS_NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
STATUS_NOT_FOUND = "NOT_FOUND"


# 隐藏区：不单独渲染成正文卡片（与前端 workbench.js 的 isTitleRegion 判定一致）。
HIDDEN_REGION_ROLES = frozenset({"REPORT_TITLE"})

# 序数词
_ORDINAL_WORDS: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 「第三个」「第3个」「第三张」「第三个卡片」
_ORDINAL_PATTERN = re.compile(
    r"(?P<reverse>倒数)?第\s*"
    r"(?P<num>\d{1,3}|[一二两三四五六七八九十]{1,3})\s*"
    r"(?:个|张|块|条|幅|项|组)?"
)

# 「最后一个」「最后一张」
_LAST_PATTERN = re.compile(r"最后\s*(?:一)?\s*(?:个|张|块|条|幅|项|组)?")

# 「第一」「上一个」中的前一个
_PREV_PATTERN = re.compile(r"上\s*一\s*(?:个|张|块|条|幅|项|组)?")

# 指代词：这个 / 那个 / 它 / 这张 / 那张 / 这块 / 那块
_DEMONSTRATIVE_PATTERN = re.compile(
    r"(这|那)(?:个|张|块|条|幅|项|组|里|块儿)|它"
)

# 指代词后面常跟的“卡片/图/表”限定，用于判断它指的是正文卡片
_CARD_HINT_PATTERN = re.compile(r"卡片|图表|图|表|区域|区块|面板|模块")


# ---------- 输出结构 ----------

@dataclass(frozen=True)
class VisibleCard:
    """一张客户可见的正文卡片。"""

    regionId: str
    label: str
    index: int          # 可见顺序，从 1 开始
    role: str = ""


@dataclass(frozen=True)
class ReferenceOption:
    """一个可选目标（用于追问）。"""

    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class ReferenceResolution:
    """可见对象解析结果。"""

    status: str
    regionId: str = ""
    label: str = ""
    matchedText: str = ""
    # 序数定位时的可见序号（1 起）
    visibleIndex: int | None = None
    visibleCount: int = 0
    options: list[ReferenceOption] = field(default_factory=list)
    question: str = ""
    reason: str = ""
    # 可见顺序里存在、但当前草稿里已不存在的 ID：草稿变过，需要重新核对
    staleRegionIds: list[str] = field(default_factory=list)
    # 可见顺序来源：前端回传 / 草稿兜底
    orderSource: str = "draft"


# ---------- 工具 ----------

def _match_exact_label(
    raw: str, cards: list[VisibleCard]
) -> VisibleCard | None:
    """按卡片名称精确匹配原文，返回唯一命中的卡片。

    「设备耗电排名这个 section」这类话里，用户已经把卡片名说出来了，
    精确名称比「这个 / 那个」这类泛化指代更可信，所以先试精确名。

    命中规则：卡片名是原文的子串；多个卡片同时命中时取名字最长的那个，
    最长名仍并列（不同卡片同名）则视为没命中，交给上层追问。
    """
    best: VisibleCard | None = None
    best_len = 0
    tied = False
    for card in cards:
        label = (card.label or "").strip()
        if len(label) < 2 or label not in raw:
            continue
        if len(label) > best_len:
            best, best_len, tied = card, len(label), False
        elif len(label) == best_len and best is not None and card.regionId != best.regionId:
            tied = True
    if best is None or tied:
        return None
    return best


def _region_label(region: dict[str, Any]) -> str:
    """取卡片的可读名称。"""
    component = region.get("component") if isinstance(region.get("component"), dict) else {}
    metric = component.get("metric") if isinstance(component.get("metric"), dict) else {}
    return str(
        region.get("name")
        or component.get("title")
        or metric.get("label")
        or region.get("id")
        or "未命名 section"
    )


def _is_hidden(region: dict[str, Any]) -> bool:
    return str(region.get("role") or "") in HIDDEN_REGION_ROLES


def _parse_ordinal(text: str) -> tuple[int, bool, str] | None:
    """解析序数，返回 (序号, 是否倒数, 命中原文)。"""
    match = _LAST_PATTERN.search(text)
    if match:
        return -1, False, match.group(0)

    match = _PREV_PATTERN.search(text)
    if match:
        return -2, False, match.group(0)

    match = _ORDINAL_PATTERN.search(text)
    if not match:
        return None
    raw = match.group("num")
    if raw.isdigit():
        number = int(raw)
    else:
        number = _cn_number(raw)
        if number is None:
            return None
    return number, bool(match.group("reverse")), match.group(0)


def _cn_number(text: str) -> int | None:
    """把「三」「十二」「二十三」转成整数。"""
    if "十" not in text:
        total = 0
        for ch in text:
            digit = _ORDINAL_WORDS.get(ch)
            if digit is None:
                return None
            total = total * 10 + digit
        return total
    head, _, tail = text.partition("十")
    tens = 1 if head == "" else _ORDINAL_WORDS.get(head)
    ones = 0 if tail == "" else _ORDINAL_WORDS.get(tail)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


# ---------- 对外接口 ----------

def list_visible_cards(
    regions: list[dict[str, Any]] | None,
    visible_region_ids: list[str] | None = None,
) -> tuple[list[VisibleCard], list[str], str]:
    """列出客户可见的正文卡片。

    返回 `(可见卡片列表, 草稿里已不存在的可见 ID, 顺序来源)`。

    * `visible_region_ids` 由前端回传，代表用户实际看到的正文卡片顺序；
    * 没传时退回「草稿 region 顺序去掉隐藏标题区」的兜底口径。
    """
    regions = regions or []
    by_id = {
        str(region.get("id")): region
        for region in regions
        if region.get("id") and not _is_hidden(region)
    }

    if visible_region_ids:
        ordered: list[dict[str, Any]] = []
        stale: list[str] = []
        for region_id in visible_region_ids:
            region = by_id.get(str(region_id))
            if region is None:
                stale.append(str(region_id))
                continue
            ordered.append(region)
        # 草稿里有、但前端没回传的可见卡片：草稿比页面新，需要重新核对
        seen = {str(region.get("id")) for region in ordered}
        for region_id, region in by_id.items():
            if region_id not in seen:
                stale.append(region_id)
        source = "frontend"
    else:
        ordered = [region for region in regions if not _is_hidden(region)]
        stale = []
        source = "draft"

    cards = [
        VisibleCard(
            regionId=str(region.get("id")),
            label=_region_label(region),
            index=index,
            role=str(region.get("role") or ""),
        )
        for index, region in enumerate(ordered, start=1)
    ]
    return cards, stale, source


def resolve_visible_reference(
    text: str,
    regions: list[dict[str, Any]] | None = None,
    *,
    visible_region_ids: list[str] | None = None,
    selected_region_id: str | None = None,
) -> ReferenceResolution:
    """解析「第三个」「最后一张」「这个」这类对可见对象的指代。

    返回三态之一：

    * `RESOLVED` —— 唯一命中，`regionId` / `label` 有值；
    * `NEEDS_CLARIFICATION` —— 命中不唯一或序号越界，`options` 给出候选；
    * `NOT_FOUND` —— 这句话里没有指向可见对象的指代。

    参数：

    * `text`：用户原话；
    * `regions`：当前草稿的 regions；
    * `visible_region_ids`：前端回传的正文卡片 ID 顺序（可选）；
    * `selected_region_id`：当前选中的 region（用于解析「这个」）。
    """
    raw = (text or "").strip()
    if not raw:
        return ReferenceResolution(status=STATUS_NOT_FOUND, reason="empty_text")

    cards, stale, order_source = list_visible_cards(regions, visible_region_ids)
    if not cards:
        return ReferenceResolution(
            status=STATUS_NEEDS_CLARIFICATION,
            visibleCount=0,
            options=[],
            question="当前草稿里没有可定位的正文卡片，请说明要改哪一块。",
            reason="no_visible_cards",
            staleRegionIds=stale,
            orderSource=order_source,
        )

    options = [
        ReferenceOption(
            value=card.regionId,
            label=f"第 {card.index} 个：{card.label}",
            description=f"{card.role or 'REGION'} / {card.regionId}",
        )
        for card in cards
    ]

    # 定位优先级：序数 → 精确名称 → 指代词。
    #
    # 序数最优先：用户说「第三个」时，指的就是可见顺序里的第三张，
    # 即使用户话里同时出现了卡片名（例如「第三个 设备耗电排名」），
    # 也应以序数为准 —— 否则卡片名会盖掉用户真正说的位置。
    ordinal = _parse_ordinal(raw)
    if ordinal is not None:
        number, is_reverse, matched = ordinal
        if number == -1:  # 最后一个
            target = cards[-1]
        elif number == -2:  # 上一个
            return ReferenceResolution(
                status=STATUS_NEEDS_CLARIFICATION,
                matchedText=matched,
                visibleCount=len(cards),
                options=options,
                question="「上一个」需要结合操作历史判断，这次要改当前可见的第几个卡片？",
                reason="relative_previous",
                staleRegionIds=stale,
                orderSource=order_source,
            )
        else:
            position = len(cards) - number + 1 if is_reverse else number
            if position < 1 or position > len(cards):
                return ReferenceResolution(
                    status=STATUS_NEEDS_CLARIFICATION,
                    matchedText=matched,
                    visibleCount=len(cards),
                    options=options,
                    question=(
                        f"当前页面只有 {len(cards)} 个正文卡片，没有第 {number} 个，"
                        f"您指的是哪一个？"
                    ),
                    reason="ordinal_out_of_range",
                    staleRegionIds=stale,
                    orderSource=order_source,
                )
            target = cards[position - 1]
        return ReferenceResolution(
            status=STATUS_RESOLVED,
            regionId=target.regionId,
            label=target.label,
            matchedText=matched,
            visibleIndex=target.index,
            visibleCount=len(cards),
            options=options,
            reason="ordinal",
            staleRegionIds=stale,
            orderSource=order_source,
        )

    # 精确名称其次：用户已经把卡片名说出来了，就不该再问「您指哪一个」。
    exact = _match_exact_label(raw, cards)
    if exact is not None:
        return ReferenceResolution(
            status=STATUS_RESOLVED,
            regionId=exact.regionId,
            label=exact.label,
            matchedText=exact.label,
            visibleIndex=exact.index,
            visibleCount=len(cards),
            options=options,
            reason="exact_label",
            staleRegionIds=stale,
            orderSource=order_source,
        )

    if _DEMONSTRATIVE_PATTERN.search(raw):
        if selected_region_id:
            for card in cards:
                if card.regionId == str(selected_region_id):
                    return ReferenceResolution(
                        status=STATUS_RESOLVED,
                        regionId=card.regionId,
                        label=card.label,
                        matchedText=selected_region_id,
                        visibleIndex=card.index,
                        visibleCount=len(cards),
                        options=options,
                        reason="selected_region",
                        staleRegionIds=stale,
                        orderSource=order_source,
                    )
        return ReferenceResolution(
            status=STATUS_NEEDS_CLARIFICATION,
            visibleCount=len(cards),
            options=options,
            question="您指的是哪一个卡片？当前可见的有：" + "、".join(
                f"第 {card.index} 个 {card.label}" for card in cards
            ) + "。",
            reason="demonstrative_without_selection",
            staleRegionIds=stale,
            orderSource=order_source,
        )

    return ReferenceResolution(
        status=STATUS_NOT_FOUND,
        visibleCount=len(cards),
        options=options,
        reason="no_visible_reference",
        staleRegionIds=stale,
        orderSource=order_source,
    )


def build_reference_options(cards: list[VisibleCard]) -> list[ReferenceOption]:
    """把可见卡片列表转成可下拉的选项（前端用 value / label 结构）。"""
    return [
        ReferenceOption(
            value=card.regionId,
            label=f"第 {card.index} 个：{card.label}",
            description=f"{card.role or 'REGION'} / {card.regionId}",
        )
        for card in cards
    ]
