"""Result publication and the reconciliation fallback.

The governing rule: a result file existing is never what makes a result true.
It must parse, match its schema, carry the expected ``execution_id`` and
``task_id``, be marked terminal, and name a legal terminal state.
"""

from __future__ import annotations

import pytest

from zero3_pilot_commander_bridge.atomic_io import is_valid_document, read_json_strict
from zero3_pilot_commander_bridge.commander_client import CommanderError
from zero3_pilot_commander_bridge.github_client import BridgeRepository, LayoutError
from zero3_pilot_commander_bridge.models import ExecutionResult, StateMirror, utc_now
from zero3_pilot_commander_bridge.publisher import (
    Publisher,
    PublishError,
    ResultConflict,
)
from zero3_pilot_commander_bridge.reconciliation import Reconciler


@pytest.fixture
def repo(tmp_path):
    return BridgeRepository(tmp_path)


@pytest.fixture
def publisher(repo):
    return Publisher(repo)


def make_result(state="succeeded", task_id="task-1", execution_id="exec-1"):
    return ExecutionResult(
        execution_id=execution_id,
        task_id=task_id,
        state=state,
        completed_at="2026-08-28T10:00:00Z",
        event_sequence=9,
    )


class FakeClient:
    """Stands in for the Commander Gateway. Never touches the network."""

    def __init__(self, snapshots=None, error=None):
        self.snapshots = snapshots or {}
        self.error = error
        self.calls = []

    def execution_status(self, task_id):
        self.calls.append(task_id)
        if self.error is not None:
            raise self.error
        return self.snapshots[task_id]


# -- publishing results --------------------------------------------------


def test_valid_terminal_result_is_published(repo, publisher):
    assert publisher.publish_result(make_result()) is True
    document = read_json_strict(repo.result_path("exec-1"))
    assert document["state"] == "succeeded"
    assert document["terminal"] is True


@pytest.mark.parametrize(
    "state", ["succeeded", "failed", "cancelled", "outcome_unknown", "quarantined"]
)
def test_every_terminal_state_can_be_published(publisher, state):
    assert publisher.publish_result(make_result(state=state, execution_id=f"exec-{state}"))


def test_outcome_unknown_is_preserved_not_rewritten(repo, publisher):
    """An honest 'we do not know' must survive into the mirror unchanged."""
    publisher.publish_result(make_result(state="outcome_unknown"))
    assert read_json_strict(repo.result_path("exec-1"))["state"] == "outcome_unknown"


@pytest.mark.parametrize("state", ["running", "queued", "", "SUCCEEDED", "done"])
def test_non_terminal_state_is_never_published_as_a_result(repo, publisher, state):
    with pytest.raises(PublishError, match="non-terminal|invalid result"):
        publisher.publish_result(make_result(state=state))
    assert not repo.result_path("exec-1").exists()


def test_duplicate_identical_result_is_a_noop(repo, publisher):
    assert publisher.publish_result(make_result()) is True
    assert publisher.publish_result(make_result()) is False
    assert read_json_strict(repo.result_path("exec-1"))["state"] == "succeeded"


def test_conflicting_terminal_result_is_refused(repo, publisher):
    publisher.publish_result(make_result(state="succeeded"))

    # A task reaching two different terminal states is a real inconsistency.
    # It must stay visible rather than being resolved by last-write-wins.
    with pytest.raises(ResultConflict, match="refusing to overwrite"):
        publisher.publish_result(make_result(state="failed"))

    assert read_json_strict(repo.result_path("exec-1"))["state"] == "succeeded"


def test_inconsistent_task_id_is_refused(repo, publisher):
    with pytest.raises(PublishError, match="task_id"):
        publisher.publish_result(make_result(task_id="task-1"), expected_task_id="task-2")
    assert not repo.result_path("exec-1").exists()


def test_zero_byte_result_is_not_a_result(repo, publisher):
    path = repo.result_path("exec-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    assert path.exists()
    assert is_valid_document(path) is False

    # A verified result legitimately replaces an unreadable placeholder.
    assert publisher.publish_result(make_result()) is True
    assert read_json_strict(path)["state"] == "succeeded"


def test_execution_id_cannot_escape_the_layout(repo):
    with pytest.raises(LayoutError):
        repo.result_path("../../.github/workflows/ci")


# -- events --------------------------------------------------------------


def test_identical_event_republish_is_idempotent(publisher):
    from zero3_pilot_commander_bridge.models import EventMirror

    event = EventMirror(
        execution_id="exec-1",
        event_sequence=1,
        event_type="stage.started",
        occurred_at="2026-08-28T10:00:00Z",
    )
    assert publisher.publish_event(event) is True
    assert publisher.publish_event(event) is False


# -- indexes -------------------------------------------------------------


def test_active_index_holds_only_non_terminal_executions(repo, publisher):
    publisher.publish_state(
        StateMirror("exec-live", "task-a", "running", "render", 40, 3, utc_now())
    )
    publisher.publish_state(
        StateMirror("exec-done", "task-b", "succeeded", "complete", 100, 9, utc_now())
    )

    active_count, recent_count = publisher.refresh_indexes()
    assert active_count == 1
    assert recent_count == 2

    active = read_json_strict(repo.active_index)
    assert [entry["execution_id"] for entry in active["executions"]] == ["exec-live"]

    # References only: no execution payload is duplicated into the index.
    assert set(active["executions"][0]) == {
        "execution_id", "task_id", "state", "stage", "progress",
        "event_sequence", "updated_at",
    }


def test_corrupt_state_mirror_is_not_indexed(repo, publisher):
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    (repo.state_dir / "exec-broken.json").touch()

    active_count, recent_count = publisher.refresh_indexes()
    assert (active_count, recent_count) == (0, 0)


# -- reconciliation ------------------------------------------------------


def test_reconciliation_publishes_a_terminal_result(repo, publisher):
    publisher.publish_state(
        StateMirror("exec-1", "task-1", "running", "render", 50, 4, utc_now())
    )
    publisher.refresh_indexes()

    client = FakeClient(
        {"task-1": {"state": "succeeded", "terminal": True, "task": {"id": "task-1"}}}
    )
    report = Reconciler(repo, client, publisher).reconcile()

    assert report.checked == 1
    assert report.results_published == 1
    assert read_json_strict(repo.result_path("exec-1"))["state"] == "succeeded"


def test_reconciliation_ignores_terminal_executions(repo, publisher):
    publisher.publish_state(
        StateMirror("exec-done", "task-b", "succeeded", "complete", 100, 9, utc_now())
    )
    publisher.refresh_indexes()

    client = FakeClient()
    report = Reconciler(repo, client, publisher).reconcile()

    # Settled executions are never re-polled: this is a fallback, not a
    # permanent poll of all history.
    assert report.checked == 0
    assert client.calls == []


def test_unreachable_gateway_writes_nothing(repo, publisher):
    publisher.publish_state(
        StateMirror("exec-1", "task-1", "running", "render", 50, 4, utc_now())
    )
    publisher.refresh_indexes()

    client = FakeClient(error=CommanderError("connection refused"))
    report = Reconciler(repo, client, publisher).reconcile()

    assert report.unreachable == 1
    assert report.results_published == 0
    # Unreachable is not the same as finished, so no result is invented.
    assert not repo.result_path("exec-1").exists()


def test_reconciliation_does_not_regress_state(repo, publisher):
    publisher.publish_state(
        StateMirror("exec-1", "task-1", "running", "render", 50, 7, utc_now())
    )
    publisher.refresh_indexes()
    # Simulate a real event arriving ahead of the index snapshot.
    publisher.publish_state(
        StateMirror("exec-1", "task-1", "encoding", "encode", 80, 12, utc_now())
    )

    client = FakeClient({"task-1": {"state": "running", "terminal": False}})
    Reconciler(repo, client, publisher).reconcile()

    surviving = read_json_strict(repo.state_path("exec-1"))
    assert surviving["event_sequence"] == 12
    assert surviving["state"] == "encoding"


def test_gateway_terminal_flag_alone_is_not_enough(repo, publisher):
    """A bogus state is not published just because terminal is true."""
    publisher.publish_state(
        StateMirror("exec-1", "task-1", "running", "render", 50, 4, utc_now())
    )
    publisher.refresh_indexes()

    client = FakeClient({"task-1": {"state": "finished-ish", "terminal": True}})
    report = Reconciler(repo, client, publisher).reconcile()

    assert report.results_published == 0
    assert not repo.result_path("exec-1").exists()
