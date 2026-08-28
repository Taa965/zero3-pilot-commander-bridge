"""Production-runtime correctness for command loss, duplication, and drift."""

from __future__ import annotations

import json

import pytest

from zero3_pilot_commander_bridge.atomic_io import read_json_strict
from zero3_pilot_commander_bridge.commander_client import CommanderError, CommanderHTTPError
from zero3_pilot_commander_bridge.github_client import BridgeRepository
from zero3_pilot_commander_bridge.ingestor import PERMANENT_REJECTION_STATUSES, Ingestor
from zero3_pilot_commander_bridge.models import StateMirror, utc_now
from zero3_pilot_commander_bridge.publisher import Publisher
from zero3_pilot_commander_bridge.reconciliation import Reconciler
from zero3_pilot_commander_bridge.runtime import BridgeRuntime, PublisherRuntime

EXECUTION_ID = "exec-2026-08-28-001"
TASK_ID = "0f9c1f4e-1f3a-4a1e-9c2b-8d5f0a1b2c3d"


class FakeClient:
    """Commander fake. It never touches the network."""

    def __init__(self, *, submit=None, status=None, health=None):
        self._submit = submit if submit is not None else {"task_id": TASK_ID}
        self._status = status or {}
        self._health = health or {"ok": True, "central": {"healthy": True}}
        self.submits = 0
        self.status_calls: list[str] = []

    def submit_execution(self, _package):
        self.submits += 1
        if isinstance(self._submit, Exception):
            raise self._submit
        return self._submit

    def execution_status(self, task_id):
        self.status_calls.append(task_id)
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def health(self):
        if isinstance(self._health, Exception):
            raise self._health
        return self._health


@pytest.fixture
def repo(tmp_path):
    return BridgeRepository(tmp_path)


def write_pending(repo, execution_id=EXECUTION_ID, **overrides):
    envelope = {
        "schema": "zero3.commander.command.execution-submit/1.0",
        "command": "execution.submit",
        "execution_id": execution_id,
        "commander_id": "gpt-web-session",
        "issued_at": "2026-08-28T10:00:00Z",
        "payload": {"anything": "opaque"},
    }
    envelope.update(overrides)
    path = repo.pending_command(execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


# -- A verdict file existing is not a verdict ----------------------------


def test_zero_byte_accepted_file_does_not_discard_command(repo):
    pending = write_pending(repo)
    accepted = repo.accepted_command(EXECUTION_ID)
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.touch()

    client = FakeClient()
    outcome = Ingestor(repo, client).ingest_one(pending)

    assert client.submits == 1
    assert outcome.status == "accepted"
    assert read_json_strict(accepted)["task_id"] == TASK_ID


def test_malformed_accepted_file_does_not_discard_command(repo):
    pending = write_pending(repo)
    accepted = repo.accepted_command(EXECUTION_ID)
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text('{"schema": "truncated', encoding="utf-8")

    client = FakeClient()
    assert Ingestor(repo, client).ingest_one(pending).status == "accepted"
    assert client.submits == 1


def test_mis_correlated_accepted_file_is_not_trusted(repo):
    pending = write_pending(repo)
    accepted = repo.accepted_command(EXECUTION_ID)
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text(
        json.dumps(
            {
                "schema": "zero3.commander.command-accepted/1.0",
                "execution_id": "some-other-execution",
                "command": "execution.submit",
                "correlation": "task_id",
                "task_id": TASK_ID,
                "accepted_at": utc_now(),
            }
        ),
        encoding="utf-8",
    )

    client = FakeClient()
    assert Ingestor(repo, client).ingest_one(pending).status == "accepted"
    assert client.submits == 1


def test_valid_verdict_remains_idempotent(repo):
    pending = write_pending(repo)
    client = FakeClient()
    Ingestor(repo, client).ingest_one(pending)

    pending_again = write_pending(repo)
    outcome = Ingestor(repo, client).ingest_one(pending_again)

    assert outcome.status == "accepted"
    assert outcome.reason == "already decided"
    assert client.submits == 1


# -- HTTP refusal classification -----------------------------------------


@pytest.mark.parametrize("status", [400, 409, 413, 422])
def test_authoritative_refusals_are_rejections(repo, status):
    pending = write_pending(repo)
    client = FakeClient(submit=CommanderHTTPError(status, "refused", "u"))

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "rejected"
    assert status in PERMANENT_REJECTION_STATUSES
    assert read_json_strict(repo.rejected_command(EXECUTION_ID))["commander_status"] == status


@pytest.mark.parametrize("status", [401, 403, 404, 405, 408, 410, 425, 429, 500, 502, 503])
def test_auth_protocol_throttle_and_infra_faults_are_deferred(repo, status):
    pending = write_pending(repo)
    client = FakeClient(submit=CommanderHTTPError(status, "not a verdict", "u"))

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "deferred"
    assert status not in PERMANENT_REJECTION_STATUSES
    assert pending.exists()
    assert not repo.rejected_command(EXECUTION_ID).exists()


def test_unreachable_gateway_keeps_pending(repo):
    pending = write_pending(repo)
    outcome = Ingestor(repo, FakeClient(submit=CommanderError("offline"))).ingest_one(pending)
    assert outcome.status == "deferred"
    assert pending.exists()


# -- Acceptance correlation ---------------------------------------------


def test_acceptance_without_task_id_is_recorded_but_never_resubmitted(repo):
    pending = write_pending(repo)
    client = FakeClient(submit={"accepted": True})

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "accepted"
    record = read_json_strict(repo.accepted_command(EXECUTION_ID))
    assert record["correlation"] == "unknown"
    assert "task_id" not in record
    assert record["correlation_problem"]
    assert not pending.exists()


def test_acceptance_naming_other_execution_is_uncorrelated(repo):
    pending = write_pending(repo)
    client = FakeClient(submit={"execution_id": "someone-else", "task_id": TASK_ID})
    Ingestor(repo, client).ingest_one(pending)
    assert read_json_strict(repo.accepted_command(EXECUTION_ID))["correlation"] == "unknown"


def test_nested_task_id_can_be_correlated_for_legacy_wire_shape(repo):
    pending = write_pending(repo)
    client = FakeClient(submit={"task": {"task_id": TASK_ID}})
    Ingestor(repo, client).ingest_one(pending)
    record = read_json_strict(repo.accepted_command(EXECUTION_ID))
    assert record["correlation"] == "task_id"
    assert record["task_id"] == TASK_ID


def test_uncorrelated_acceptance_is_surfaced_by_reconciliation(repo):
    write_pending(repo)
    client = FakeClient(submit={"accepted": True})
    Ingestor(repo, client).ingest_pending()

    publisher = Publisher(repo)
    assert PublisherRuntime(repo, client, publisher).pending_executions() == []
    report = Reconciler(repo, client, publisher).reconcile()
    assert any("uncorrelated" in problem for problem in report.problems)


# -- Progress and sequence monotonicity ----------------------------------


def test_reconciliation_does_not_reset_progress(repo):
    publisher = Publisher(repo)
    publisher.publish_state(
        StateMirror(EXECUTION_ID, TASK_ID, "running", "render", 80, 5, utc_now())
    )
    publisher.refresh_indexes()

    Reconciler(
        repo,
        FakeClient(status={"state": "running", "terminal": False}),
        publisher,
    ).reconcile()

    surviving = read_json_strict(repo.state_path(EXECUTION_ID))
    assert surviving["progress"] == 80
    assert surviving["stage"] == "render"


def test_index_carries_progress(repo):
    publisher = Publisher(repo)
    publisher.publish_state(
        StateMirror(EXECUTION_ID, TASK_ID, "running", "render", 42, 3, utc_now())
    )
    publisher.refresh_indexes()
    assert read_json_strict(repo.active_index)["executions"][0]["progress"] == 42


def test_publisher_clamps_nonterminal_progress(repo):
    publisher = Publisher(repo)
    publisher.publish_state(
        StateMirror(EXECUTION_ID, TASK_ID, "running", "render", 80, 5, utc_now())
    )
    publisher.publish_state(
        StateMirror(EXECUTION_ID, TASK_ID, "running", "render", 0, 6, utc_now())
    )
    assert read_json_strict(repo.state_path(EXECUTION_ID))["progress"] == 80


def test_terminal_state_can_record_real_lower_progress(repo):
    publisher = Publisher(repo)
    publisher.publish_state(
        StateMirror(EXECUTION_ID, TASK_ID, "running", "render", 80, 5, utc_now())
    )
    publisher.publish_state(
        StateMirror(EXECUTION_ID, TASK_ID, "failed", "render", 12, 6, utc_now())
    )
    surviving = read_json_strict(repo.state_path(EXECUTION_ID))
    assert surviving["state"] == "failed"
    assert surviving["progress"] == 12


# -- Broken mirrors remain repairable -----------------------------------


def test_corrupt_state_mirror_is_detected(repo):
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    repo.state_path(EXECUTION_ID).touch()
    assert Publisher(repo).invalid_state_mirrors() == [EXECUTION_ID]


def test_corrupt_mirror_is_rebuilt_from_recorded_acceptance(repo):
    write_pending(repo)
    client = FakeClient()
    Ingestor(repo, client).ingest_pending()

    publisher = Publisher(repo)
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    repo.state_path(EXECUTION_ID).write_text("", encoding="utf-8")
    publisher.refresh_indexes()
    assert read_json_strict(repo.active_index)["executions"] == []

    client._status = {"state": "running", "terminal": False}
    report = Reconciler(repo, client, publisher).reconcile()

    assert report.repaired == 1
    rebuilt = read_json_strict(repo.state_path(EXECUTION_ID))
    assert rebuilt["state"] == "running"
    assert rebuilt["task_id"] == TASK_ID


# -- Long-running runtime paths ------------------------------------------


def test_runtime_ingests_pending_command(repo):
    write_pending(repo)
    client = FakeClient(status={"state": "queued", "terminal": False})

    report = BridgeRuntime(repo, client, commit=False).pass_once()

    assert report.accepted == 1
    assert read_json_strict(repo.accepted_command(EXECUTION_ID))["task_id"] == TASK_ID


def test_publisher_tracks_acceptance_before_state_exists(repo):
    write_pending(repo)
    client = FakeClient()
    Ingestor(repo, client).ingest_pending()
    assert PublisherRuntime(repo, client, Publisher(repo)).pending_executions() == [
        (EXECUTION_ID, TASK_ID)
    ]


def test_runtime_publishes_terminal_result(repo):
    write_pending(repo)
    client = FakeClient(
        status={"state": "succeeded", "terminal": True, "task": {"id": TASK_ID}}
    )

    report = BridgeRuntime(repo, client, commit=False).pass_once()

    assert report.results_published == 1
    assert read_json_strict(repo.result_path(EXECUTION_ID))["state"] == "succeeded"


def test_settled_execution_is_not_polled_again(repo):
    write_pending(repo)
    client = FakeClient(status={"state": "succeeded", "terminal": True})
    runtime = BridgeRuntime(repo, client, commit=False)
    runtime.pass_once()
    first = len(client.status_calls)
    runtime.pass_once()
    assert len(client.status_calls) == first


def test_state_transition_advances_sequence_but_quiet_poll_does_not(repo):
    write_pending(repo)
    client = FakeClient(status={"state": "running", "terminal": False})
    runtime = BridgeRuntime(repo, client, commit=False)

    runtime.pass_once()
    first = read_json_strict(repo.state_path(EXECUTION_ID))["event_sequence"]
    runtime.pass_once()
    second = read_json_strict(repo.state_path(EXECUTION_ID))["event_sequence"]
    assert second == first

    client._status = {"state": "encoding", "terminal": False}
    runtime.pass_once()
    assert read_json_strict(repo.state_path(EXECUTION_ID))["event_sequence"] == first + 1


def test_health_document_is_written_and_valid(repo):
    write_pending(repo)
    client = FakeClient(status={"state": "running", "terminal": False})
    BridgeRuntime(repo, client, commit=False).pass_once()

    health = read_json_strict(repo.health_path)
    assert health["zero3_commander"] == "healthy"
    assert health["zero3_central"] == "healthy"
    assert health["github_ingress"] == "healthy"
