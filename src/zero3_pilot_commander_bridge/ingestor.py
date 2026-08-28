"""Inbound path: ``commands/pending/`` into Zero3.

The mailbox is a state machine with exactly three resting places:

``commands/pending/``
    An external agent has asked for something. Nothing is decided.
``commands/accepted/``
    The Commander Gateway took ownership. Zero3 now owns the outcome.
``commands/rejected/``
    The Commander Gateway declined. Terminal for this envelope.

Rules here are transport correctness, never Zero3 domain policy. A malformed
transport envelope can be rejected here; storyboard, scene, skill, worker, or
production-package meaning belongs exclusively to Zero3 Core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic_io import CorruptDocument, atomic_write_json, is_valid_document, read_json_strict
from .commander_client import CommanderClient, CommanderError, CommanderHTTPError
from .github_client import BridgeRepository, LayoutError
from .http_semantics import PERMANENT_REJECTION_STATUSES
from .models import ACCEPTED_SCHEMA, REJECTED_SCHEMA, utc_now
from .validation import (
    ValidationError,
    is_valid_identifier,
    validate_accepted_document,
    validate_command_envelope,
    validate_rejected_document,
)

__all__ = [
    "IngestOutcome",
    "Ingestor",
    "PERMANENT_REJECTION_STATUSES",
    "ENABLED_COMMANDS",
]

ENABLED_COMMANDS = frozenset({"execution.submit"})
EXPECTED_ACCEPTANCE_SCHEMA = "zero3.execution-acceptance/1.0"


@dataclass(frozen=True)
class IngestOutcome:
    """What happened to one pending command."""

    execution_id: str
    status: str  # accepted | rejected | deferred
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Ingestor:
    """Moves pending commands to Zero3 and records the verdict."""

    repo: BridgeRepository
    client: CommanderClient

    def ingest_pending(self) -> list[IngestOutcome]:
        """Process every pending command; one unexpected failure never blocks the rest."""
        outcomes: list[IngestOutcome] = []
        for path in self.repo.list_pending():
            try:
                outcomes.append(self.ingest_one(path))
            except Exception as exc:  # noqa: BLE001 - batch isolation is deliberate
                outcomes.append(
                    IngestOutcome(
                        path.stem,
                        "deferred",
                        f"unexpected {type(exc).__name__}; command stays pending",
                    )
                )
        return outcomes

    def existing_verdict(self, execution_id: str) -> str | None:
        """Return a previously recorded *valid* verdict, or ``None``."""
        accepted = self.repo.accepted_command(execution_id)
        if is_valid_document(
            accepted, lambda doc: validate_accepted_document(doc, execution_id=execution_id)
        ):
            return "accepted"

        rejected = self.repo.rejected_command(execution_id)
        if is_valid_document(
            rejected, lambda doc: validate_rejected_document(doc, execution_id=execution_id)
        ):
            return "rejected"

        return None

    def ingest_one(self, path: Path) -> IngestOutcome:
        execution_id = path.stem

        if not is_valid_identifier(execution_id):
            return IngestOutcome(execution_id, "deferred", "unsafe command filename")

        decided = self.existing_verdict(execution_id)
        if decided is not None:
            path.unlink(missing_ok=True)
            return IngestOutcome(execution_id, decided, "already decided")

        try:
            document = read_json_strict(path)
        except CorruptDocument as exc:
            return self._reject(execution_id, path, "corrupt-envelope", str(exc), {})

        try:
            command = validate_command_envelope(document)
        except ValidationError as exc:
            return self._reject(execution_id, path, "invalid-envelope", str(exc), document)

        if document.get("execution_id") != execution_id:
            return self._reject(
                execution_id,
                path,
                "identifier-mismatch",
                f"filename {execution_id!r} does not match execution_id "
                f"{document.get('execution_id')!r}",
                document,
            )

        payload = document.get("payload") or {}
        payload_execution_id = payload.get("execution_id") if isinstance(payload, dict) else None
        if payload_execution_id is not None and payload_execution_id != execution_id:
            return self._reject(
                execution_id,
                path,
                "identifier-mismatch",
                f"payload execution_id {payload_execution_id!r} does not match "
                f"envelope execution_id {execution_id!r}",
                document,
            )

        if command not in ENABLED_COMMANDS:
            return self._reject(
                execution_id,
                path,
                "capability-disabled",
                f"{command} is not backed by a Commander Gateway endpoint yet; "
                "see bridge/capabilities.json",
                document,
            )

        try:
            response = self.client.submit_execution(payload)
        except CommanderHTTPError as exc:
            if exc.rejected:
                return self._reject(
                    execution_id,
                    path,
                    "commander-rejected",
                    exc.body[:4096],
                    document,
                    status=exc.status,
                )
            return IngestOutcome(
                execution_id,
                "deferred",
                f"HTTP {exc.status} is not a command verdict; command stays pending",
            )
        except CommanderError as exc:
            return IngestOutcome(execution_id, "deferred", f"commander unavailable: {exc}")

        return self._accept(execution_id, path, document, response)

    @staticmethod
    def correlate(response: dict[str, Any], execution_id: str) -> tuple[str | None, str]:
        """Extract a trustworthy task correlation from a 2xx response.

        A 2xx that cannot be correlated is still recorded as accepted: Zero3
        may already be executing it, so resubmission would risk duplication.
        """
        if not isinstance(response, dict):
            return None, f"gateway returned {type(response).__name__}, expected an object"

        schema = response.get("schema")
        if schema is not None and schema != EXPECTED_ACCEPTANCE_SCHEMA:
            return None, f"unexpected acceptance schema {schema!r}"

        echoed = response.get("execution_id")
        if isinstance(echoed, str) and echoed and echoed != execution_id:
            return None, (
                f"gateway response names execution_id {echoed!r}, expected {execution_id!r}"
            )
        if schema == EXPECTED_ACCEPTANCE_SCHEMA and echoed != execution_id:
            return None, "acceptance response omitted the expected execution_id"

        candidate = response.get("task_id")
        if not isinstance(candidate, str) or not candidate.strip():
            task = response.get("task")
            if isinstance(task, dict):
                candidate = task.get("task_id")

        if not isinstance(candidate, str) or not candidate.strip():
            return None, "gateway response carries no task_id"

        candidate = candidate.strip()
        if not is_valid_identifier(candidate):
            return None, f"gateway returned malformed task_id {candidate!r}"

        return candidate, ""

    def _accept(
        self,
        execution_id: str,
        pending_path: Path,
        envelope: dict[str, Any],
        response: dict[str, Any],
    ) -> IngestOutcome:
        task_id, problem = self.correlate(response, execution_id)

        record: dict[str, Any] = {
            "schema": ACCEPTED_SCHEMA,
            "execution_id": execution_id,
            "command": str(envelope.get("command") or ""),
            "commander_id": envelope.get("commander_id"),
            "accepted_at": utc_now(),
            "commander_response": response,
        }
        if task_id is not None:
            record["correlation"] = "task_id"
            record["task_id"] = task_id
        else:
            record["correlation"] = "unknown"
            record["correlation_problem"] = problem[:2048]

        try:
            atomic_write_json(
                self.repo.accepted_command(execution_id),
                record,
                validator=lambda doc: validate_accepted_document(doc, execution_id=execution_id),
            )
        except (LayoutError, ValidationError) as exc:
            return IngestOutcome(execution_id, "deferred", f"cannot record acceptance: {exc}")

        pending_path.unlink(missing_ok=True)
        reason = "" if task_id is not None else f"accepted but uncorrelated: {problem}"
        return IngestOutcome(execution_id, "accepted", reason, detail=record)

    @staticmethod
    def _safe_optional_text(value: object, *, max_length: int) -> str | None:
        """Keep malformed envelope fields from making the rejection unwriteable."""
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or len(value) > max_length:
            return None
        return value

    def _reject(
        self,
        execution_id: str,
        pending_path: Path,
        reason_code: str,
        reason: str,
        envelope: dict[str, Any],
        *,
        status: int | None = None,
    ) -> IngestOutcome:
        # The envelope may have failed schema validation precisely because these
        # fields are malformed. Never copy an arbitrary object into the verdict
        # and then fail to write the rejection that would clear the poison
        # command from the mailbox.
        record: dict[str, Any] = {
            "schema": REJECTED_SCHEMA,
            "execution_id": execution_id,
            "command": self._safe_optional_text(envelope.get("command"), max_length=64),
            "commander_id": self._safe_optional_text(
                envelope.get("commander_id"), max_length=128
            ),
            "reason_code": reason_code,
            "reason": str(reason)[:8192],
            "rejected_at": utc_now(),
        }
        if status is not None:
            record["commander_status"] = status

        try:
            atomic_write_json(
                self.repo.rejected_command(execution_id),
                record,
                validator=lambda doc: validate_rejected_document(doc, execution_id=execution_id),
            )
        except (LayoutError, ValidationError) as exc:
            return IngestOutcome(execution_id, "deferred", f"cannot record rejection: {exc}")

        pending_path.unlink(missing_ok=True)
        return IngestOutcome(execution_id, "rejected", str(reason), detail=record)
