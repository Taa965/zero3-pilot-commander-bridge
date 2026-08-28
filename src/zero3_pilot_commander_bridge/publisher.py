"""Outbound path: Zero3 observations into the GitHub mirror.

Three invariants hold here:

1. State is monotonic. A lower ``event_sequence`` never overwrites a higher one.
2. Events are immutable. A recorded sequence is never rewritten with different
   valid content.
3. A result is published only when it is genuinely terminal and fully valid.

File existence and JSON parseability are never sufficient authority: protocol
mirrors are trusted only after schema validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .atomic_io import (
    atomic_write_json,
    atomic_write_state,
    is_valid_document,
    read_json_or_none,
    read_validated_json_or_none,
)
from .github_client import BridgeRepository
from .models import TERMINAL_STATES, EventMirror, ExecutionResult, StateMirror, utc_now
from .validation import (
    ValidationError,
    validate_event_document,
    validate_result_document,
    validate_state_document,
)

__all__ = ["PublishError", "ResultConflict", "EventConflict", "Publisher"]

RECENT_LIMIT = 50


class PublishError(Exception):
    """A mirror update could not be completed."""


class ResultConflict(PublishError):
    """A different valid terminal result already exists for this execution."""


class EventConflict(PublishError):
    """A different valid event is already recorded at this sequence number."""


@dataclass
class Publisher:
    """Writes the state, event, result, and index mirrors."""

    repo: BridgeRepository

    # -- state ----------------------------------------------------------

    def publish_state(self, state: StateMirror) -> bool:
        """Update the latest-state mirror; return whether the file changed.

        Parseable-but-invalid existing documents are treated as broken and may
        be replaced by a verified observation. Progress is monotonic for
        non-terminal work. A semantically unchanged observation is a no-op so a
        15-second status check does not create a Git commit every 15 seconds.
        """
        document = state.to_document()
        path = self.repo.state_path(state.execution_id)
        existing = read_validated_json_or_none(path, validate_state_document)

        if existing is not None and not state.terminal:
            previous = existing.get("progress")
            incoming = document.get("progress")
            if (
                isinstance(previous, int | float)
                and isinstance(incoming, int | float)
                and incoming < previous
            ):
                document["progress"] = previous

        if existing is not None and _same_state(existing, document):
            return False

        return atomic_write_state(path, document, validator=validate_state_document)

    # -- events ---------------------------------------------------------

    def publish_event(self, event: EventMirror) -> bool:
        """Append one immutable event; invalid occupants may be repaired."""
        document = event.to_document()
        path = self.repo.event_path(event.execution_id, event.event_sequence)

        existing = read_validated_json_or_none(path, validate_event_document)
        if existing is not None:
            if _same_event(existing, document):
                return False
            raise EventConflict(
                f"event {event.event_sequence} for {event.execution_id} already exists "
                "with different valid content"
            )

        atomic_write_json(path, document, validator=validate_event_document)
        return True

    # -- results --------------------------------------------------------

    def publish_result(
        self, result: ExecutionResult, *, expected_task_id: str | None = None
    ) -> bool:
        """Publish a validated terminal result.

        A parseable but schema-invalid existing file is repaired. A different
        *valid* terminal result is a conflict and is never overwritten.
        """
        if result.state not in TERMINAL_STATES:
            legal = ", ".join(sorted(TERMINAL_STATES))
            raise PublishError(
                f"refusing to publish non-terminal state {result.state!r} as a result; "
                f"legal terminal states: {legal}"
            )

        document = result.to_document()
        task_id = expected_task_id if expected_task_id is not None else result.task_id

        try:
            validate_result_document(
                document, execution_id=result.execution_id, task_id=task_id
            )
        except ValidationError as exc:
            raise PublishError(f"refusing to publish invalid result: {exc}") from exc

        path = self.repo.result_path(result.execution_id)
        existing = read_validated_json_or_none(
            path,
            lambda doc: validate_result_document(
                doc, execution_id=result.execution_id, task_id=task_id
            ),
        )
        if existing is not None:
            if _same_result(existing, document):
                return False
            raise ResultConflict(
                f"result for {result.execution_id} already recorded as "
                f"{existing.get('state')!r}/{existing.get('task_id')!r}; "
                f"refusing to overwrite with {document.get('state')!r}/{document.get('task_id')!r}"
            )

        atomic_write_json(
            path,
            document,
            validator=lambda doc: validate_result_document(
                doc, execution_id=result.execution_id, task_id=task_id
            ),
        )
        return True

    # -- indexes --------------------------------------------------------

    def invalid_state_mirrors(self) -> list[str]:
        """Return execution ids whose state mirror is unusable."""
        broken: list[str] = []
        state_dir = self.repo.state_dir
        if not state_dir.is_dir():
            return broken
        for path in sorted(state_dir.glob("*.json")):
            if not is_valid_document(path, validate_state_document):
                broken.append(path.stem)
        return broken

    def refresh_indexes(self) -> tuple[int, int]:
        """Rebuild active/recent indexes from schema-valid state mirrors.

        ``updated_at`` changes only when the semantic index changes. This keeps
        a quiet bridge quiet in Git instead of manufacturing heartbeat commits.
        """
        references: list[dict[str, Any]] = []
        state_dir = self.repo.state_dir
        if state_dir.is_dir():
            for path in sorted(state_dir.glob("*.json")):
                document = read_validated_json_or_none(path, validate_state_document)
                if document is None:
                    continue
                references.append(
                    {
                        "execution_id": document.get("execution_id", path.stem),
                        "task_id": document.get("task_id", ""),
                        "state": document.get("state", ""),
                        "stage": document.get("stage", ""),
                        "progress": document.get("progress", 0),
                        "event_sequence": document.get("event_sequence", 0),
                        "updated_at": document.get("updated_at", ""),
                    }
                )

        active = [ref for ref in references if ref.get("state") not in TERMINAL_STATES]
        recent = sorted(references, key=lambda ref: str(ref.get("updated_at")), reverse=True)
        recent = recent[:RECENT_LIMIT]

        active_document = {
            "schema": "zero3.commander.index.active/1.0",
            "description": (
                "Non-terminal executions only. Lets an external agent resume context "
                "without a repository-wide search. Holds references, never full "
                "execution payloads."
            ),
            "executions": active,
        }
        recent_document = {
            "schema": "zero3.commander.index.recent/1.0",
            "description": (
                "Most recent execution references, newest first, capped at "
                f"{RECENT_LIMIT}. Holds references, never full execution payloads."
            ),
            "limit": RECENT_LIMIT,
            "executions": recent,
        }

        self._write_index_if_changed(self.repo.active_index, active_document)
        self._write_index_if_changed(self.repo.recent_index, recent_document)
        return len(active), len(recent)

    @staticmethod
    def _write_index_if_changed(path, document: dict[str, Any]) -> bool:
        existing = read_json_or_none(path)
        if existing is not None:
            previous = {k: v for k, v in existing.items() if k != "updated_at"}
            if previous == document:
                return False
        output = dict(document)
        output["updated_at"] = utc_now()
        atomic_write_json(path, output)
        return True


def _comparable(document: dict[str, Any], ignore: set[str]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in ignore}


def _same_state(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Observation time and source do not make a new state by themselves.
    ignore = {"updated_at", "source", "observed_by"}
    return _comparable(left, ignore) == _comparable(right, ignore)


def _same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _comparable(left, {"recorded_at"}) == _comparable(right, {"recorded_at"})


def _same_result(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # completed_at is the bridge's observation time today, not a timestamp
    # supplied by Central, so retries must not conflict only because it moved.
    ignore = {"recorded_at", "completed_at"}
    return _comparable(left, ignore) == _comparable(right, ignore)
