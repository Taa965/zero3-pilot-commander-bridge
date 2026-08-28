"""Transport data shapes.

These model the *envelope*, never the domain. Nothing here knows what a
storyboard, scene, beat, skill, or worker is, and nothing here may learn.
Payload bodies stay opaque dictionaries relayed verbatim between the external
agent and Zero3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "TERMINAL_STATES",
    "COMMAND_SCHEMAS",
    "STATE_SCHEMA",
    "EVENT_SCHEMA",
    "RESULT_SCHEMA",
    "ACCEPTED_SCHEMA",
    "REJECTED_SCHEMA",
    "HEALTH_SCHEMA",
    "CAPABILITIES_SCHEMA",
    "BRIDGE_VERSION",
    "CommandEnvelope",
    "StateMirror",
    "EventMirror",
    "ExecutionResult",
    "utc_now",
]

BRIDGE_VERSION = "0.1.0"

TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "outcome_unknown", "quarantined"}
)

STATE_SCHEMA = "zero3.commander.state/2.0"
EVENT_SCHEMA = "zero3.commander.event/2.0"
RESULT_SCHEMA = "zero3.commander.result/2.0"
ACCEPTED_SCHEMA = "zero3.commander.command-accepted/1.0"
REJECTED_SCHEMA = "zero3.commander.command-rejected/1.0"
HEALTH_SCHEMA = "zero3.commander.bridge-health/1.0"
CAPABILITIES_SCHEMA = "zero3.commander.capabilities/1.0"

COMMAND_SCHEMAS = {
    "execution.submit": "execution-submit.schema.json",
    "task.cancel": "task-cancel.schema.json",
    "task.retry": "task-retry.schema.json",
}


def utc_now() -> str:
    """Return an RFC 3339 timestamp in UTC, compatible with Python 3.10+."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CommandEnvelope:
    """An inbound request from an external agent. Nothing is decided yet."""

    schema: str
    command: str
    execution_id: str
    commander_id: str
    issued_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "CommandEnvelope":
        return cls(
            schema=str(document.get("schema", "")),
            command=str(document.get("command", "")),
            execution_id=str(document.get("execution_id", "")),
            commander_id=str(document.get("commander_id", "")),
            issued_at=str(document.get("issued_at", "")),
            payload=document.get("payload") or {},
            raw=document,
        )


@dataclass(frozen=True)
class StateMirror:
    """The latest observed state of one execution. Never authoritative."""

    execution_id: str
    task_id: str
    state: str
    stage: str
    progress: float
    event_sequence: int
    updated_at: str
    source: str = "publisher"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "state": self.state,
            "stage": self.stage,
            "progress": self.progress,
            "event_sequence": self.event_sequence,
            "terminal": self.terminal,
            "updated_at": self.updated_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class EventMirror:
    """One immutable point in an execution history."""

    execution_id: str
    event_sequence: int
    event_type: str
    occurred_at: str
    task_id: str = ""
    state: str = ""
    stage: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": EVENT_SCHEMA,
            "execution_id": self.execution_id,
            "event_sequence": self.event_sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "recorded_at": utc_now(),
            "detail": self.detail,
        }
        for key, value in (
            ("task_id", self.task_id),
            ("state", self.state),
            ("stage", self.stage),
        ):
            if value:
                document[key] = value
        return document


@dataclass(frozen=True)
class ExecutionResult:
    """A genuine terminal outcome, subject to full validation before publication."""

    execution_id: str
    task_id: str
    state: str
    completed_at: str
    event_sequence: int = 0
    error: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "state": self.state,
            "terminal": True,
            "event_sequence": self.event_sequence,
            "completed_at": self.completed_at,
            "recorded_at": utc_now(),
            "summary": self.summary,
        }
        if self.error is not None:
            document["error"] = self.error
        return document
