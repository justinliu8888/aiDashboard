"""文字方案的领域服务：把模型给的候选变成可提交的对象。

契约来源：`05. 并行分发/00. 协调与冻结契约.md` 的「A：领域与方案类型」。

    apply_candidate(candidate, previous=None, *, plan_id=None, expected_revision=None)
        -> ReportPlan

规则：

    - **新建**：调用方给 ``plan_id``，``expected_revision`` 为 null，
      revision 从 1 开始。
    - **修改**：基于 ``previous``，必须核对 ``expected_revision``；
      方案 ID 沿用上一版，已有板块 ID 尽量保留，revision 递增。
    - 原对象（上一版方案、候选）都不就地修改。
    - 结果只是一个**可提交对象**：不写库、不调用模型、不生成报表，
      也不决定一次提交是否最终成功。

ID 与版本的归属：模型不分配提交版本，也不改已有板块 ID；
板块 ID 由本模块生成或沿用，所以 ID 不会因为模型换了措辞而漂移。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.schemas.report_plan import (
    PlanCandidate,
    PlanSectionCandidate,
    PlanRevisionConflictError,
    PlanSection,
    PlanValidationError,
    ReportPlan,
)


# 板块 ID 的序号后缀：``section_3`` -> 3。用于继续往后发号，
# 而不是把删掉的板块 ID 回收给别的板块。
_SECTION_ID_PATTERN = re.compile(r"^section_(\d+)$")


def _coerce_candidate(candidate: Any) -> PlanCandidate:
    """把候选变成校验过的 ``PlanCandidate``。"""
    if isinstance(candidate, PlanCandidate):
        return candidate
    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()
    if not isinstance(candidate, Mapping):
        raise PlanValidationError(
            "候选方案必须是 PlanCandidate 或映射",
            code="PLAN_CANDIDATE_INVALID",
        )
    return PlanCandidate.model_validate(dict(candidate))


def _coerce_previous(previous: Any) -> ReportPlan | None:
    """把上一版方案变成校验过的 ``ReportPlan``。"""
    if previous is None:
        return None
    if isinstance(previous, ReportPlan):
        return previous
    if isinstance(previous, BaseModel):
        previous = previous.model_dump()
    if not isinstance(previous, Mapping):
        raise PlanValidationError(
            "上一版方案必须是 ReportPlan 或映射",
            code="PLAN_PREVIOUS_INVALID",
        )
    return ReportPlan.model_validate(dict(previous))


def _next_section_index(section_ids: list[str]) -> int:
    """下一个可用板块序号：延续已有编号，不回收删掉的编号。"""
    highest = 0
    for section_id in section_ids:
        match = _SECTION_ID_PATTERN.match(section_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _resolve_section_id(
    candidate_section: PlanSectionCandidate,
    *,
    previous_ids: list[str],
    previous_ids_by_title: dict[str, list[str]],
    used_ids: set[str],
    counter: int,
) -> tuple[str, int]:
    """决定一个板块最终用哪个 ID。

    顺序：显式且已存在的 ID > 标题唯一匹配的旧板块 > 新生成的 ID。
    """
    resolved: str | None = None

    if candidate_section.id and candidate_section.id in previous_ids:
        resolved = candidate_section.id
    elif not candidate_section.id:
        matches = previous_ids_by_title.get(candidate_section.title) or []
        if len(matches) == 1 and matches[0] not in used_ids:
            resolved = matches[0]

    if resolved is not None:
        if resolved in used_ids:
            raise PlanValidationError(
                f"板块 ID 重复：{resolved}",
                code="PLAN_DUPLICATE_SECTION_ID",
            )
        return resolved, counter

    while f"section_{counter}" in used_ids or f"section_{counter}" in previous_ids:
        counter += 1
    new_id = f"section_{counter}"
    return new_id, counter + 1


def _build_sections(
    candidate_sections: list[PlanSectionCandidate],
    previous_sections: list[PlanSection],
) -> list[PlanSection]:
    """把候选板块变成带稳定 ID 的板块列表。"""
    previous_ids = [section.id for section in previous_sections]
    previous_ids_by_title: dict[str, list[str]] = {}
    for section in previous_sections:
        previous_ids_by_title.setdefault(section.title, []).append(section.id)

    used_ids: set[str] = set()
    counter = _next_section_index(previous_ids)
    sections: list[PlanSection] = []

    for candidate_section in candidate_sections:
        section_id, counter = _resolve_section_id(
            candidate_section,
            previous_ids=previous_ids,
            previous_ids_by_title=previous_ids_by_title,
            used_ids=used_ids,
            counter=counter,
        )
        used_ids.add(section_id)
        sections.append(
            PlanSection(
                id=section_id,
                title=candidate_section.title,
                metricIntents=list(candidate_section.metricIntents),
                componentType=candidate_section.componentType,
            )
        )
    return sections


def _build_requirements(candidate: PlanCandidate):
    """复制需求块，避免结果与候选共享同一份对象。"""
    return candidate.requirements.model_copy(deep=True)


def apply_candidate(
    candidate: PlanCandidate | Mapping[str, Any],
    previous: ReportPlan | Mapping[str, Any] | None = None,
    *,
    plan_id: str | None = None,
    expected_revision: int | None = None,
) -> ReportPlan:
    """把候选方案变成一份可提交的 ``ReportPlan``。

    新建（``previous`` 为 None）：
        调用方必须给 ``plan_id``，``expected_revision`` 必须为 null，
        revision 从 1 开始，板块 ID 全部由本模块生成。

    修改（``previous`` 不为 None）：
        ``expected_revision`` 必须与上一版的 revision 一致，否则抛版本冲突；
        方案 ID 沿用上一版，已有板块 ID 保留，revision 递增。

    异常：
        - ``PlanValidationError``：候选结构非法，或调用方式不对。
        - ``PlanContentRejectedError``：候选里混进了执行性内容。
        - ``PlanRevisionConflictError``：基础版本与当前方案不一致。
    """
    candidate_plan = _coerce_candidate(candidate)
    previous_plan = _coerce_previous(previous)

    if previous_plan is None:
        if expected_revision is not None:
            raise PlanValidationError(
                "新建方案时 expected_revision 必须为 null",
                code="PLAN_EXPECTED_REVISION_INVALID",
            )
        if plan_id is None or not str(plan_id).strip():
            raise PlanValidationError(
                "新建方案必须由调用方提供 plan_id",
                code="PLAN_ID_REQUIRED",
            )
        sections = _build_sections(candidate_plan.sections, [])
        return ReportPlan(
            id=str(plan_id).strip(),
            revision=1,
            topic=candidate_plan.topic,
            requirements=_build_requirements(candidate_plan),
            sections=sections,
            assumptions=list(candidate_plan.assumptions),
            suggestions=list(candidate_plan.suggestions),
            referenceTemplates=[
                template.model_copy(deep=True)
                for template in candidate_plan.referenceTemplates
            ],
        )

    if expected_revision is None:
        raise PlanValidationError(
            "修改方案必须提供 expected_revision",
            code="PLAN_EXPECTED_REVISION_REQUIRED",
        )
    if expected_revision != previous_plan.revision:
        raise PlanRevisionConflictError(
            f"方案版本冲突：期望 revision {expected_revision}，"
            f"当前方案是 revision {previous_plan.revision}",
            expected_revision=expected_revision,
            actual_revision=previous_plan.revision,
        )
    if plan_id is not None and str(plan_id).strip() != previous_plan.id:
        raise PlanValidationError(
            f"修改方案时 plan_id 必须沿用原方案：{previous_plan.id}",
            code="PLAN_ID_MISMATCH",
        )

    sections = _build_sections(candidate_plan.sections, previous_plan.sections)
    return ReportPlan(
        id=previous_plan.id,
        revision=previous_plan.revision + 1,
        topic=candidate_plan.topic,
        requirements=_build_requirements(candidate_plan),
        sections=sections,
        assumptions=list(candidate_plan.assumptions),
        suggestions=list(candidate_plan.suggestions),
        referenceTemplates=[
            template.model_copy(deep=True)
            for template in candidate_plan.referenceTemplates
        ],
    )
