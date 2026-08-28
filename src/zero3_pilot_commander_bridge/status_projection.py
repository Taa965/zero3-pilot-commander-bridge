"""Project the Commander v1 execution-status wire response into mirror fields.

This module interprets only the verified transport-safe status shape returned by
Zero3 Core. It does not make scheduler, worker, video, or business decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import EventMirror, TERMINAL_STATES

__all__ = ["StatusProjectionError", "StatusProjection", "project_status"]

STATUS_SCHEMA = "zero3.execution-status/1.0"


class StatusProjectionError(Exception):
    """The gateway status response cannot safely be correlated/projected."""


@dataclass(frozen=True)
class StatusProjection:
    state: str
    terminal: bool
    stage: str
    progress: float
    event_sequence: int
    events: tuple[EventMirror, ...]
    summary: dict[str, Any]


def _latest_execution(task: dict[str, Any]) -> dict[str, Any]:
    executions = task.get("executions")
    if not isinstance(executions, list):
        return {}
    rows = [row for row in executions if isinstance(row, dict)]
    if not rows:
        return {}

    def generation(row: dict[str, Any]) -> int:
        try:
            return int(row.get("attempt_generation") or 0)
        except (TypeError, ValueError):
            return 0

    return max(rows, key=generation)


def _progress_percent(value: object, fallback: float) -> float:
    """Convert Core execution_runs.progress (verified 0..1) to mirror percent."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    number = float(value)
    if 0.0 <= number <= 1.0:
        return number * 100.0
    # Fail soft for a future wire shape already expressed as percent.
    if 0.0 <= number <= 100.0:
        return number
    return fallback


def _project_events(
    task: dict[str, Any], *, bridge_execution_id: str, task_id: str
) -> tuple[EventMirror, ...]:
    rows = task.get("events")
    if not isinstance(rows, list):
        return ()

    events: list[EventMirror] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            sequence = int(row.get("event_sequence"))
        except (TypeError, ValueError):
            continue
        if sequence < 0 or sequence in seen:
            continue
        event_type = row.get("event_type")
        occurred_at = row.get("created_at")
        if not isinstance(event_type, str) or not event_type.strip():
            continue
        if not isinstance(occurred_at, str) or not occurred_at.strip():
            continue

        payload = row.get("payload")
        detail: dict[str, Any] = {
            "payload": payload if isinstance(payload, dict) else {},
        }
        for source, target in (
            ("event_id", "event_id"),
            ("execution_id", "core_execution_id"),
            ("actor_type", "actor_type"),
            ("actor_id", "actor_id"),
        ):
            value = row.get(source)
            if value is not None:
                detail[target] = value

        events.append(
            EventMirror(
                execution_id=bridge_execution_id,
                task_id=task_id,
                event_sequence=sequence,
                event_type=event_type.strip()[:128],
                occurred_at=occurred_at,
                detail=detail,
            )
        )
        seen.add(sequence)

    events.sort(key=lambda event: event.event_sequence)
    return tuple(events)


def project_status(
    snapshot: dict[str, Any],
    *,
    execution_id: str,
    task_id: str,
    previous_state: str = "",
    previous_stage: str = "",
    previous_progress: float = 0.0,
    previous_sequence: int = 0,
) -> StatusProjection:
    """Correlate a status response and project transport-safe mirror fields."""
    if not isinstance(snapshot, dict):
        raise StatusProjectionError("status response is not a JSON object")

    schema = snapshot.get("schema")
    if schema is not None and schema != STATUS_SCHEMA:
        raise StatusProjectionError(f"unexpected status schema {schema!r}")

    returned_task_id = snapshot.get("task_id")
    if returned_task_id is not None and str(returned_task_id) != task_id:
        raise StatusProjectionError(
            f"status task_id {returned_task_id!r} does not match expected {task_id!r}"
        )

    state = snapshot.get("state")
    if not isinstance(state, str) or not state.strip():
        raise StatusProjectionError("status response carries no state")
    state = state.strip()

    terminal = bool(snapshot.get("terminal"))
    if terminal and state not in TERMINAL_STATES:
        raise StatusProjectionError(
            f"status marks non-terminal state {state!r} as terminal"
        )
    if state in TERMINAL_STATES and snapshot.get("terminal") is False:
        raise StatusProjectionError(
            f"status marks terminal state {state!r} as non-terminal"
        )

    task = snapshot.get("task")
    task = task if isinstance(task, dict) else {}
    latest = _latest_execution(task)

    stage = previous_stage
    current_stage = latest.get("stage")
    if isinstance(current_stage, str):
        stage = current_stage[:128]

    progress = _progress_percent(latest.get("progress"), previous_progress)
    if state == "succeeded" and not latest:
        progress = max(progress, 100.0)

    events = _project_events(task, bridge_execution_id=execution_id, task_id=task_id)
    newest_sequence = max(
        [previous_sequence, *(event.event_sequence for event in events)],
        default=previous_sequence,
    )
    if state != previous_state and newest_sequence <= previous_sequence:
        newest_sequence = previous_sequence + 1

    return StatusProjection(
        state=state,
        terminal=terminal,
        stage=stage,
        progress=progress,
        event_sequence=newest_sequence,
        events=events,
        summary=task,
    )
