"""Projection of the real Commander execution-status response into mirrors.

The wire shape asserted here was captured from the live gateway
(`zero3.execution-status/1.0`), not invented: `task.executions[]` carries
`progress`, `stage`, and `attempt_generation`, and `task.events[]` carries
`event_sequence`, `event_id`, `event_type`, `payload`, `created_at`,
`actor_type`, and `actor_id`.

Core stores execution progress as a 0..1 fraction; the mirror publishes a
0..100 percentage. That conversion is the only arithmetic the transport is
allowed to do here. Nothing in this module or these tests may encode video
domain meaning.
"""

from __future__ import annotations

import pytest

from zero3_pilot_commander_bridge.github_client import BridgeRepository
from zero3_pilot_commander_bridge.models import TERMINAL_STATES
from zero3_pilot_commander_bridge.publisher import Publisher
from zero3_pilot_commander_bridge.status_projection import (
    StatusProjectionError,
    project_status,
)

EXECUTION_ID = "exec-2026-08-28-001"
TASK_ID = "0f9c1f4e-1f3a-4a1e-9c2b-8d5f0a1b2c3d"


def snapshot(**overrides):
    """A status response shaped like the real gateway answer."""
    document = {
        "schema": "zero3.execution-status/1.0",
        "task_id": TASK_ID,
        "state": "running",
        "terminal": False,
        "task": {
            "task_id": TASK_ID,
            "executions": [
                {
                    "execution_id": "core-exec-1",
                    "attempt_generation": 1,
                    "progress": 0.63,
                    "stage": "generate-video",
                    "state": "running",
                }
            ],
            "events": [],
        },
    }
    document.update(overrides)
    return document


def event(sequence: int, **overrides):
    row = {
        "event_sequence": sequence,
        "event_id": f"event-{sequence}",
        "event_type": "task.progress",
        "payload": {"detail": sequence},
        "created_at": "2026-08-28T10:00:00Z",
        "actor_type": "scheduler",
        "actor_id": "central-scheduler",
        "execution_id": "core-exec-1",
    }
    row.update(overrides)
    return row


@pytest.fixture
def repo(tmp_path):
    return BridgeRepository(tmp_path)


# -- progress conversion -------------------------------------------------


def test_core_progress_063_becomes_63_percent():
    """Core stores 0..1; the mirror publishes 0..100."""
    projection = project_status(snapshot(), execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert projection.progress == pytest.approx(63.0)


@pytest.mark.parametrize(
    ("core", "expected"),
    [(0.0, 0.0), (0.5, 50.0), (1.0, 100.0), (0.001, 0.1)],
)
def test_fraction_progress_conversion(core, expected):
    doc = snapshot()
    doc["task"]["executions"][0]["progress"] = core
    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert projection.progress == pytest.approx(expected)


def test_missing_progress_carries_the_previous_value_forward():
    """A status answer without progress must never reset a running task."""
    doc = snapshot()
    del doc["task"]["executions"][0]["progress"]
    projection = project_status(
        doc, execution_id=EXECUTION_ID, task_id=TASK_ID, previous_progress=80.0
    )
    assert projection.progress == pytest.approx(80.0)


def test_latest_attempt_generation_wins():
    """The live gateway returned 43 executions for one task; the newest wins."""
    doc = snapshot()
    doc["task"]["executions"] = [
        {"attempt_generation": 1, "progress": 0.10, "stage": "old"},
        {"attempt_generation": 43, "progress": 0.63, "stage": "generate-video"},
        {"attempt_generation": 7, "progress": 0.20, "stage": "middle"},
    ]
    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert projection.progress == pytest.approx(63.0)
    assert projection.stage == "generate-video"


# -- stage ---------------------------------------------------------------


def test_core_stage_reaches_the_state_mirror():
    projection = project_status(snapshot(), execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert projection.stage == "generate-video"


def test_missing_stage_carries_the_previous_value_forward():
    doc = snapshot()
    doc["task"]["executions"] = []
    projection = project_status(
        doc, execution_id=EXECUTION_ID, task_id=TASK_ID, previous_stage="render"
    )
    assert projection.stage == "render"


# -- event projection ----------------------------------------------------


def test_two_core_events_become_two_event_mirrors():
    doc = snapshot()
    doc["task"]["events"] = [event(100), event(101)]

    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)

    assert [e.event_sequence for e in projection.events] == [100, 101]
    first = projection.events[0]
    assert first.execution_id == EXECUTION_ID
    assert first.task_id == TASK_ID
    assert first.detail["event_id"] == "event-100"
    assert first.detail["actor_type"] == "scheduler"
    assert first.detail["actor_id"] == "central-scheduler"
    assert first.detail["payload"] == {"detail": 100}
    assert first.detail["core_execution_id"] == "core-exec-1"


def test_events_are_sorted_by_sequence():
    """The live gateway returned events newest-first; order must be restored."""
    doc = snapshot()
    doc["task"]["events"] = [event(101), event(99), event(100)]

    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert [e.event_sequence for e in projection.events] == [99, 100, 101]


def test_duplicate_sequence_in_one_response_is_collapsed():
    doc = snapshot()
    doc["task"]["events"] = [event(100), event(100, event_id="dup")]

    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert [e.event_sequence for e in projection.events] == [100]


def test_republishing_the_same_event_is_a_noop(repo):
    """Seeing an event again across passes must not rewrite history."""
    doc = snapshot()
    doc["task"]["events"] = [event(100)]
    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    publisher = Publisher(repo)

    assert publisher.publish_event(projection.events[0]) is True
    again = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert publisher.publish_event(again.events[0]) is False


def test_events_land_under_the_bridge_execution_id(repo):
    """events/<bridge_execution_id>/<event_sequence>.json, not the Core id."""
    doc = snapshot()
    doc["task"]["events"] = [event(100), event(101)]
    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)

    publisher = Publisher(repo)
    for mirrored in projection.events:
        publisher.publish_event(mirrored)

    assert repo.event_path(EXECUTION_ID, 100).is_file()
    assert repo.event_path(EXECUTION_ID, 101).is_file()
    assert repo.event_sequences(EXECUTION_ID) == [100, 101]


@pytest.mark.parametrize(
    "bad",
    [
        {"event_sequence": "x", "event_type": "t", "created_at": "2026-08-28T10:00:00Z"},
        {"event_sequence": -1, "event_type": "t", "created_at": "2026-08-28T10:00:00Z"},
        {"event_sequence": 1, "event_type": "", "created_at": "2026-08-28T10:00:00Z"},
        {"event_sequence": 1, "event_type": "t", "created_at": ""},
        {"event_sequence": 1, "event_type": "t"},
        "not-an-object",
    ],
)
def test_unusable_event_rows_are_skipped_not_guessed_at(bad):
    doc = snapshot()
    doc["task"]["events"] = [bad, event(100)]

    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert [e.event_sequence for e in projection.events] == [100]


# -- correlation and refusal --------------------------------------------


def test_task_id_mismatch_is_refused():
    with pytest.raises(StatusProjectionError, match="task_id"):
        project_status(
            snapshot(task_id="a-different-task"),
            execution_id=EXECUTION_ID,
            task_id=TASK_ID,
        )


def test_unexpected_status_schema_is_refused():
    with pytest.raises(StatusProjectionError, match="schema"):
        project_status(
            snapshot(schema="zero3.execution-status/9.9"),
            execution_id=EXECUTION_ID,
            task_id=TASK_ID,
        )


def test_terminal_true_with_running_state_is_refused():
    """A gateway answer that contradicts itself must not be projected."""
    with pytest.raises(StatusProjectionError, match="non-terminal state"):
        project_status(
            snapshot(state="running", terminal=True),
            execution_id=EXECUTION_ID,
            task_id=TASK_ID,
        )


def test_terminal_state_marked_non_terminal_is_refused():
    with pytest.raises(StatusProjectionError, match="non-terminal"):
        project_status(
            snapshot(state="succeeded", terminal=False),
            execution_id=EXECUTION_ID,
            task_id=TASK_ID,
        )


def test_missing_state_is_refused():
    with pytest.raises(StatusProjectionError, match="no state"):
        project_status(snapshot(state=""), execution_id=EXECUTION_ID, task_id=TASK_ID)


def test_non_object_response_is_refused():
    with pytest.raises(StatusProjectionError, match="JSON object"):
        project_status(["nope"], execution_id=EXECUTION_ID, task_id=TASK_ID)


# -- terminal success ----------------------------------------------------


def test_succeeded_with_terminal_true_projects_a_terminal_outcome():
    doc = snapshot(state="succeeded", terminal=True)
    doc["task"]["executions"][0]["progress"] = 1.0
    doc["task"]["executions"][0]["stage"] = "complete"

    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)

    assert projection.state == "succeeded"
    assert projection.terminal is True
    assert projection.state in TERMINAL_STATES
    assert projection.progress == pytest.approx(100.0)
    assert projection.stage == "complete"


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_every_terminal_state_projects(state):
    projection = project_status(
        snapshot(state=state, terminal=True), execution_id=EXECUTION_ID, task_id=TASK_ID
    )
    assert projection.terminal is True
    assert projection.state == state


# -- sequence advance ----------------------------------------------------


def test_state_change_without_events_still_advances_the_sequence():
    projection = project_status(
        snapshot(state="encoding"),
        execution_id=EXECUTION_ID,
        task_id=TASK_ID,
        previous_state="running",
        previous_sequence=7,
    )
    assert projection.event_sequence == 8


def test_quiet_poll_does_not_advance_the_sequence():
    projection = project_status(
        snapshot(state="running"),
        execution_id=EXECUTION_ID,
        task_id=TASK_ID,
        previous_state="running",
        previous_sequence=7,
    )
    assert projection.event_sequence == 7


def test_real_core_sequences_are_adopted_when_higher():
    doc = snapshot()
    doc["task"]["events"] = [event(199)]
    projection = project_status(
        doc,
        execution_id=EXECUTION_ID,
        task_id=TASK_ID,
        previous_state="running",
        previous_sequence=7,
    )
    assert projection.event_sequence == 199


# -- the boundary --------------------------------------------------------


def test_projection_carries_no_video_domain_rules():
    """Any payload projects. Meaning belongs to Zero3 Core, never here."""
    doc = snapshot()
    doc["task"]["events"] = [
        event(100, payload={"storyboard": {"scenes": 17}, "visual_beats": 107}),
        event(101, payload={"storyboard": {"scenes": 0}, "visual_beats": 0}),
    ]
    projection = project_status(doc, execution_id=EXECUTION_ID, task_id=TASK_ID)
    assert len(projection.events) == 2
