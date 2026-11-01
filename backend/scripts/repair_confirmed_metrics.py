"""Explicit repair of one unedited draft; run with the backend stopped."""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.agent import AgentQuestion, AgentResponse, AgentResponseDomainObjects
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.preview_service import build_report_preview_payload, resolve_preview_context
from app.services.session_service import session_service
from app.services.template_definition_service import build_template_definition


def repair(session_id: str, backup_path: Path, *, apply: bool = False) -> None:
    repo = session_service.repository
    with repo.lock:
        repo.conn.execute("BEGIN IMMEDIATE")
        try:
            record = repo.get_session(session_id)
            if record is None:
                raise ValueError("Session not found")
            snapshot = record.snapshot or {}
            original = snapshot.get("definition") or {}
            metadata = original.get("metadata") or {}
            plan = snapshot.get("reportPlan") or {}
            identity = metadata.get("templateBinding") or {}
            if metadata.get("sourceMode") != "simulated" or int(metadata.get("revision") or 0) != 0:
                raise ValueError("Only unedited simulated drafts can be repaired")
            if (identity.get("planId"), identity.get("planRevision")) != (plan.get("id"), plan.get("revision")):
                raise ValueError("Draft and confirmed plan do not have the same identity")
            if record.pendingEditPlan or snapshot.get("pendingPlan"):
                raise ValueError("Session has a pending edit; finish it before repair")
            rebuilt = build_template_definition(
                snapshot["templateDesign"], plan, (snapshot.get("scope") or {})["subjectId"]
            )
            by_id = {r["id"]: r for r in rebuilt.regions}
            updated = deepcopy(original)
            repaired = []
            for region in updated["regions"]:
                if region.get("component", {}).get("type") != "METRIC_CARD":
                    continue
                binding = region.get("templateBinding") or {}
                expected = by_id[region["id"]]["templateBinding"]["previewSpec"]
                old_labels = [m["label"] for m in binding.get("previewSpec", {}).get("metrics", [])]
                new_labels = [m["label"] for m in expected["metrics"]]
                if old_labels == new_labels:
                    continue
                binding["previewSpec"] = deepcopy(expected)
                repaired.append({"region": region["id"], "before": old_labels, "after": new_labels})
            print(repaired)
            if not repaired or not apply:
                repo.conn.rollback()
                return
            updated["metadata"]["revision"] = 1
            definition = ReportDefinitionDraft(**updated)
            context = resolve_preview_context(updated, plan=plan, existing=snapshot.get("previewContext"))
            payload = build_report_preview_payload(updated, context, plan=plan)
            pending = record.pendingChoice or {}
            response = AgentResponse(
                conversationId=f"repair_{uuid4().hex}", state=record.state,
                reply="已按已确认文字方案补齐核心指标展示，保留原布局和时间范围，数据均为模拟数据。",
                domainObjects=AgentResponseDomainObjects(reportDefinitionDraft=definition),
                questions=[AgentQuestion(**pending["question"])] if pending.get("question") else [],
                reportPreviewPayload=payload,
            )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.touch(mode=0o600, exist_ok=False)
            # Independent read connection snapshots the committed DB before this repair.
            with sqlite3.connect(repo.db_path) as source, sqlite3.connect(backup_path) as backup:
                source.backup(backup)
            session_service.write_turn_inline(
                repo.conn, session_id=session_id,
                user_text="修复已确认方案的指标展示", response=response,
                client_msg_id=f"repair_{uuid4().hex}",
            )
            repo.conn.commit()
        except BaseException:
            repo.conn.rollback()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    repair(args.session_id, args.backup, apply=args.apply)
