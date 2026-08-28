"""Reconciliation fallback.

Fallback, not the primary observation path. It repairs drift for non-terminal
work, accepted executions that never produced a mirror, and schema-invalid
mirrors. Nothing here invents progress or trusts file existence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .atomic_io import read_json_or_none, read_validated_json_or_none
from .commander_client import CommanderClient, CommanderError
from .github_client import BridgeRepository
from .models import TERMINAL_STATES, ExecutionResult, StateMirror, utc_now
from .publisher import Publisher, PublishError
from .validation import (
    validate_accepted_document,
    validate_result_document,
    validate_state_document,
)

__all__ = ["ReconcileReport", "Reconciler"]


@dataclass
class ReconcileReport:
    """What one reconciliation pass observed."""

    checked: int = 0
    state_updated: int = 0
    results_published: int = 0
    repaired: int = 0
    unreachable: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "state_updated": self.state_updated,
            "results_published": self.results_published,
            "repaired": self.repaired,
            "unreachable": self.unreachable,
            "problems": self.problems,
        }


@dataclass
class Reconciler:
    """Repairs mirror drift for non-terminal and broken executions."""

    repo: BridgeRepository
    client: CommanderClient
    publisher: Publisher

    def active_references(self) -> list[dict[str, Any]]:
        index = read_json_or_none(self.repo.active_index)
        if index is None:
            return []
        executions = index.get("executions")
        if not isinstance(executions, list):
            return []
        return [entry for entry in executions if isinstance(entry, dict)]

    def broken_references(self) -> list[dict[str, Any]]:
        """Recover correlation handles for schema-invalid state mirrors."""
        recovered: list[dict[str, Any]] = []
        for execution_id in self.publisher.invalid_state_mirrors():
            accepted = read_validated_json_or_none(
                self.repo.accepted_command(execution_id),
                lambda doc, eid=execution_id: validate_accepted_document(
                    doc, execution_id=eid
                ),
            )
            task_id = str((accepted or {}).get("task_id") or "")
            recovered.append(
                {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "stage": "",
                    "progress": 0,
                    "event_sequence": 0,
                    "repair": True,
                }
            )
        return recovered

    def accepted_references(self) -> list[dict[str, Any]]:
        """Accepted executions that may not yet have a state mirror."""
        pending: list[dict[str, Any]] = []
        accepted_dir = self.repo.accepted_dir
        if not accepted_dir.is_dir():
            return pending

        for path in sorted(accepted_dir.glob("*.json")):
            execution_id = path.stem
            record = read_validated_json_or_none(
                path,
                lambda doc, eid=execution_id: validate_accepted_document(
                    doc, execution_id=eid
                ),
            )
            if record is None:
                continue
            execution_id = str(record.get("execution_id") or execution_id)
            task_id = str(record.get("task_id") or "")

            settled = None
            if task_id:
                settled = read_validated_json_or_none(
                    self.repo.result_path(execution_id),
                    lambda doc, eid=execution_id, tid=task_id: validate_result_document(
                        doc, execution_id=eid, task_id=tid
                    ),
                )
            if settled is not None and settled.get("state") in TERMINAL_STATES:
                continue

            pending.append(
                {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "stage": "",
                    "progress": 0,
                    "event_sequence": 0,
                }
            )
        return pending

    def work_items(self) -> list[dict[str, Any]]:
        """Active executions, broken mirrors, and unreported acceptances."""
        items = {
            str(entry.get("execution_id")): entry
            for entry in self.active_references()
            if entry.get("execution_id")
        }
        for entry in self.broken_references():
            items.setdefault(str(entry["execution_id"]), entry)
        for entry in self.accepted_references():
            items.setdefault(str(entry["execution_id"]), entry)
        return list(items.values())

    def reconcile(self) -> ReconcileReport:
        report = ReconcileReport()

        for entry in self.work_items():
            execution_id = str(entry.get("execution_id") or "")
            task_id = str(entry.get("task_id") or "")
            if not execution_id:
                report.problems.append("skipping index entry without execution_id")
                continue
            if not task_id:
                report.problems.append(
                    f"{execution_id}: no task_id recorded, cannot reconcile "
                    "(acceptance may be uncorrelated)"
                )
                continue

            report.checked += 1
            try:
                snapshot = self.client.execution_status(task_id)
            except CommanderError as exc:
                report.unreachable += 1
                report.problems.append(f"{execution_id}: {exc}")
                continue

            try:
                self._apply(execution_id, task_id, entry, snapshot, report)
            except PublishError as exc:
                report.problems.append(f"{execution_id}: {exc}")

        self.publisher.refresh_indexes()
        return report

    def _apply(
        self,
        execution_id: str,
        task_id: str,
        entry: dict[str, Any],
        snapshot: dict[str, Any],
        report: ReconcileReport,
    ) -> None:
        state = str(snapshot.get("state") or "")
        if not state:
            report.problems.append(f"{execution_id}: gateway returned no state")
            return

        mirror = read_validated_json_or_none(
            self.repo.state_path(execution_id), validate_state_document
        ) or {}

        def carried(key: str, fallback: Any) -> Any:
            value = mirror.get(key)
            if value is None:
                value = entry.get(key)
            return fallback if value is None else value

        try:
            planned_sequence = int(entry.get("event_sequence") or 0)
        except (TypeError, ValueError):
            planned_sequence = 0
        try:
            mirror_sequence = int(mirror.get("event_sequence") or 0)
        except (TypeError, ValueError):
            mirror_sequence = 0

        # The work list is a snapshot. If the live mirror advanced after that
        # snapshot was built, this reconciliation item is stale; do not create
        # an equal-sequence write that could overwrite the fresher publisher.
        if mirror and mirror_sequence > planned_sequence:
            return

        try:
            progress = float(carried("progress", 0))
        except (TypeError, ValueError):
            progress = 0.0
        stage = str(carried("stage", "") or "")

        state_changed = not mirror or str(mirror.get("state") or "") != state
        event_sequence = planned_sequence + 1 if state_changed else planned_sequence
        was_broken = bool(entry.get("repair"))

        published = self.publisher.publish_state(
            StateMirror(
                execution_id=execution_id,
                task_id=task_id,
                state=state,
                stage=stage,
                progress=progress,
                event_sequence=event_sequence,
                updated_at=utc_now(),
                source="reconciliation",
            )
        )
        if published:
            report.state_updated += 1
            if was_broken:
                report.repaired += 1

        if not (bool(snapshot.get("terminal")) and state in TERMINAL_STATES):
            return

        result = ExecutionResult(
            execution_id=execution_id,
            task_id=task_id,
            state=state,
            completed_at=utc_now(),
            event_sequence=event_sequence,
            summary=snapshot.get("task") or {},
        )
        if self.publisher.publish_result(result, expected_task_id=task_id):
            report.results_published += 1
