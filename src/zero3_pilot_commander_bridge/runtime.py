"""Production runtimes for the bridge.

Two loops, deliberately separate because they fail differently:

``IngestRuntime``
    GitHub ``commands/pending/`` -> Zero3.

``PublisherRuntime``
    Zero3 -> the GitHub mirror for executions already accepted by Zero3.

Commander v1.5 has no outbound stream. The publisher therefore asks the
transport-safe execution-status endpoint at a short bounded interval, while
projecting the *real* execution progress/stage and immutable task events already
returned in that status snapshot. The scheduled GitHub reconciliation workflow
remains a slow drift-repair fallback.

Crash safety is filesystem-first: events are written before the latest-state
pointer, verdicts/mirrors are atomic, durable local changes are recovered into
Git before remote sync, and concurrent GitHub command commits are rebased rather
than force-overwritten.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Any

from .atomic_io import atomic_write_json, read_json_or_none, read_validated_json_or_none
from .commander_client import CommanderClient, CommanderError
from .github_client import BridgeRepository, GitError
from .ingestor import Ingestor, IngestOutcome
from .models import BRIDGE_VERSION, TERMINAL_STATES, ExecutionResult, StateMirror, utc_now
from .publisher import Publisher, PublishError
from .status_projection import StatusProjectionError, project_status
from .validation import (
    validate_accepted_document,
    validate_against,
    validate_result_document,
    validate_state_document,
)

__all__ = [
    "RuntimeReport",
    "IngestRuntime",
    "PublisherRuntime",
    "BridgeRuntime",
    "StopSignal",
]

DEFAULT_INTERVAL_SECONDS = 15.0
HEALTH_HEARTBEAT_SECONDS = 300.0
COMMANDER_PROTOCOL = "zero3-pilot-commander-v1/1.0"
MIRROR_PATHS = ("commands", "state", "events", "results", "index", "bridge")


class StopSignal:
    """Cooperative shutdown flag wired to SIGTERM and SIGINT."""

    def __init__(self) -> None:
        self.stopping = False

    def install(self) -> "StopSignal":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass
        return self

    def _handle(self, _signum: int, _frame: Any) -> None:
        self.stopping = True


@dataclass
class RuntimeReport:
    """Outcome of one pass, for logs and ``bridge/health.json``."""

    ingested: list[IngestOutcome] = field(default_factory=list)
    state_updated: int = 0
    events_published: int = 0
    results_published: int = 0
    unreachable: int = 0
    problems: list[str] = field(default_factory=list)
    committed: bool = False
    ingress_synced: bool = True

    @property
    def accepted(self) -> int:
        return sum(1 for o in self.ingested if o.status == "accepted")

    @property
    def rejected(self) -> int:
        return sum(1 for o in self.ingested if o.status == "rejected")

    @property
    def deferred(self) -> int:
        return sum(1 for o in self.ingested if o.status == "deferred")

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "deferred": self.deferred,
            "state_updated": self.state_updated,
            "events_published": self.events_published,
            "results_published": self.results_published,
            "unreachable": self.unreachable,
            "committed": self.committed,
            "ingress_synced": self.ingress_synced,
            "problems": self.problems,
        }


@dataclass
class IngestRuntime:
    """``commands/pending/`` -> Zero3."""

    repo: BridgeRepository
    client: CommanderClient

    def pass_once(self, report: RuntimeReport) -> None:
        outcomes = Ingestor(self.repo, self.client).ingest_pending()
        report.ingested.extend(outcomes)
        for outcome in outcomes:
            if outcome.status == "deferred" and outcome.reason:
                report.problems.append(f"{outcome.execution_id}: {outcome.reason}")


@dataclass
class PublisherRuntime:
    """Zero3 -> GitHub mirrors for every valid accepted execution."""

    repo: BridgeRepository
    client: CommanderClient
    publisher: Publisher

    def pending_executions(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        accepted_dir = self.repo.accepted_dir
        if not accepted_dir.is_dir():
            return items

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
            if not task_id:
                continue

            result = read_validated_json_or_none(
                self.repo.result_path(execution_id),
                lambda doc, eid=execution_id, tid=task_id: validate_result_document(
                    doc, execution_id=eid, task_id=tid
                ),
            )
            if result is not None and result.get("state") in TERMINAL_STATES:
                continue

            items.append((execution_id, task_id))
        return items

    def pass_once(self, report: RuntimeReport) -> None:
        for execution_id, task_id in self.pending_executions():
            try:
                snapshot = self.client.execution_status(task_id)
            except CommanderError as exc:
                report.unreachable += 1
                report.problems.append(f"{execution_id}: {exc}")
                continue

            try:
                self._publish(execution_id, task_id, snapshot, report)
            except (PublishError, StatusProjectionError) as exc:
                report.problems.append(f"{execution_id}: {exc}")

        self.publisher.refresh_indexes()

    def _publish(
        self,
        execution_id: str,
        task_id: str,
        snapshot: dict[str, Any],
        report: RuntimeReport,
    ) -> None:
        mirror = read_validated_json_or_none(
            self.repo.state_path(execution_id), validate_state_document
        ) or {}
        try:
            previous_sequence = int(mirror.get("event_sequence") or 0)
        except (TypeError, ValueError):
            previous_sequence = 0
        try:
            previous_progress = float(mirror.get("progress") or 0)
        except (TypeError, ValueError):
            previous_progress = 0.0

        projection = project_status(
            snapshot,
            execution_id=execution_id,
            task_id=task_id,
            previous_state=str(mirror.get("state") or ""),
            previous_stage=str(mirror.get("stage") or ""),
            previous_progress=previous_progress,
            previous_sequence=previous_sequence,
        )

        # Publish immutable Core task events first. If the process dies before
        # state.json advances, the next pass safely retries these idempotently.
        for event in projection.events:
            if event.event_sequence <= previous_sequence:
                continue
            if self.publisher.publish_event(event):
                report.events_published += 1

        if self.publisher.publish_state(
            StateMirror(
                execution_id=execution_id,
                task_id=task_id,
                state=projection.state,
                stage=projection.stage,
                progress=projection.progress,
                event_sequence=projection.event_sequence,
                updated_at=utc_now(),
                source="publisher",
            )
        ):
            report.state_updated += 1

        if not projection.terminal:
            return

        result = ExecutionResult(
            execution_id=execution_id,
            task_id=task_id,
            state=projection.state,
            completed_at=utc_now(),
            event_sequence=projection.event_sequence,
            summary=projection.summary,
        )
        if self.publisher.publish_result(result, expected_task_id=task_id):
            report.results_published += 1


@dataclass
class BridgeRuntime:
    """Runs ingress, publication, health, and Git synchronization."""

    repo: BridgeRepository
    client: CommanderClient
    commit: bool = True
    push: bool = False
    git_branch: str = "main"

    def __post_init__(self) -> None:
        self.publisher = Publisher(self.repo)
        self.ingest = IngestRuntime(self.repo, self.client)
        self.publish = PublisherRuntime(self.repo, self.client, self.publisher)
        self._last_health_write_monotonic = 0.0

    def pass_once(self) -> RuntimeReport:
        report = RuntimeReport()
        started = time.monotonic()

        if self.commit:
            self._recover_and_sync(report)

        if report.ingress_synced:
            self.ingest.pass_once(report)
        else:
            report.problems.append("github ingress not synchronized; ingest skipped")

        self.publish.pass_once(report)
        self.write_health(report, lag_seconds=time.monotonic() - started)

        if self.commit:
            self._commit(report)
        return report

    def _recover_and_sync(self, report: RuntimeReport) -> None:
        """Commit crash leftovers, publish them if possible, then fetch commands."""
        try:
            if self.repo.commit_paths(MIRROR_PATHS, "bridge: recover interrupted mirror pass"):
                report.committed = True
            if self.push:
                self.repo.push_with_retry(branch=self.git_branch)
            self.repo.sync_from_remote(branch=self.git_branch)
        except GitError as exc:
            report.ingress_synced = False
            report.problems.append(f"git-sync: {exc}")

    def _commit(self, report: RuntimeReport) -> None:
        try:
            if self.repo.commit_paths(MIRROR_PATHS, "bridge: mirror Zero3 execution state"):
                report.committed = True
            if self.push:
                self.repo.push_with_retry(branch=self.git_branch)
        except GitError as exc:
            report.problems.append(f"git-egress: {exc}")

    def write_health(self, report: RuntimeReport, *, lag_seconds: float) -> bool:
        """Record observed transport health without generating commit storms."""
        commander = "healthy"
        central = "unknown"
        try:
            payload = self.client.health()
            commander = "healthy" if payload.get("ok") else "degraded"
            reported = payload.get("central")
            if isinstance(reported, dict):
                central = "healthy" if reported.get("healthy") else "unhealthy"
        except CommanderError:
            commander = "unhealthy"
            central = "unknown"

        backlog = len(self.repo.list_pending())
        previous = read_json_or_none(self.repo.health_path) or {}

        document = {
            "schema": "zero3.commander.bridge-health/1.0",
            "bridge_version": BRIDGE_VERSION,
            "commander_protocol": COMMANDER_PROTOCOL,
            "github_ingress": "healthy" if report.ingress_synced else "degraded",
            "github_egress": (
                "degraded"
                if any(p.startswith(("git-sync:", "git-egress:")) for p in report.problems)
                else "healthy"
            ),
            "zero3_commander": commander,
            "zero3_central": central,
            "last_command_received_at": (
                utc_now() if report.ingested else previous.get("last_command_received_at")
            ),
            "last_event_published_at": (
                utc_now()
                if report.events_published
                or report.state_updated
                or report.results_published
                else previous.get("last_event_published_at")
            ),
            "outbox_backlog": backlog,
            "publish_lag_seconds": round(lag_seconds, 3),
        }

        previous_semantic = {
            k: v
            for k, v in previous.items()
            if k not in {"updated_at", "publish_lag_seconds"}
        }
        semantic_changed = previous_semantic != {
            k: v for k, v in document.items() if k != "publish_lag_seconds"
        }
        now_monotonic = time.monotonic()
        heartbeat_due = (
            now_monotonic - self._last_health_write_monotonic >= HEALTH_HEARTBEAT_SECONDS
        )
        activity = bool(
            report.ingested
            or report.events_published
            or report.state_updated
            or report.results_published
            or report.problems
        )
        if previous and not semantic_changed and not heartbeat_due and not activity:
            return False

        document["updated_at"] = utc_now()
        atomic_write_json(
            self.repo.health_path,
            document,
            validator=lambda doc: validate_against(doc, "bridge-health.schema.json"),
        )
        self._last_health_write_monotonic = now_monotonic
        return True

    def run_forever(
        self,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        stop: StopSignal | None = None,
    ) -> None:
        """Run until asked to stop; recoverable pass failures never kill systemd."""
        stop = stop or StopSignal().install()
        while not stop.stopping:
            try:
                self.pass_once()
            except Exception as exc:  # noqa: BLE001 - service loop containment
                print(f"pass failed: {type(exc).__name__}: {exc}", flush=True)
            deadline = time.monotonic() + max(0.1, interval)
            while not stop.stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
