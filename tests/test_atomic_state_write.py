"""Durability of mirror writes.

These tests encode the failure the legacy bridge shipped: zero-byte
``*.status.json`` and ``*.result.json`` files that were trusted because they
existed. Every case below must keep failing loudly rather than silently
producing a document a reader would believe.
"""

from __future__ import annotations

import json
import os

import pytest

from zero3_pilot_commander_bridge.atomic_io import (
    CorruptDocument,
    SequenceRegression,
    atomic_write_json,
    atomic_write_state,
    is_valid_document,
    read_json_or_none,
    read_json_strict,
)


def state_document(sequence: int, state: str = "running") -> dict:
    return {
        "schema": "zero3.commander.state/2.0",
        "execution_id": "exec-1",
        "task_id": "task-1",
        "state": state,
        "stage": "render",
        "progress": 10,
        "event_sequence": sequence,
        "updated_at": "2026-08-28T10:00:00Z",
    }


# -- the reader refuses what the legacy bridge accepted -------------------


def test_zero_byte_file_is_not_a_document(tmp_path):
    path = tmp_path / "empty.json"
    path.touch()

    assert path.exists(), "precondition: the file exists"
    assert path.stat().st_size == 0

    # The legacy bridge would have accepted this purely because it exists.
    assert is_valid_document(path) is False
    with pytest.raises(CorruptDocument, match="zero-byte"):
        read_json_strict(path)


def test_whitespace_only_file_is_not_a_document(tmp_path):
    path = tmp_path / "blank.json"
    path.write_text("   \n\t\n", encoding="utf-8")
    with pytest.raises(CorruptDocument, match="whitespace-only"):
        read_json_strict(path)


def test_malformed_json_is_rejected(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"execution_id": "exec-1", "state":', encoding="utf-8")
    with pytest.raises(CorruptDocument, match="malformed JSON"):
        read_json_strict(path)
    assert is_valid_document(path) is False


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(CorruptDocument, match="missing"):
        read_json_strict(tmp_path / "nope.json")


def test_non_object_json_is_rejected(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(CorruptDocument, match="expected a JSON object"):
        read_json_strict(path)


def test_read_json_or_none_maps_corruption_to_none(tmp_path):
    path = tmp_path / "empty.json"
    path.touch()
    assert read_json_or_none(path) is None


# -- writing -------------------------------------------------------------


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, state_document(1))
    assert read_json_strict(path)["event_sequence"] == 1
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_write_creates_missing_directories(tmp_path):
    path = tmp_path / "events" / "exec-1" / "3.json"
    atomic_write_json(path, {"event_sequence": 3})
    assert read_json_strict(path)["event_sequence"] == 3


def test_interrupted_write_preserves_the_previous_document(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_write_json(path, state_document(1))

    def explode(src, dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="simulated crash"):
        atomic_write_json(path, state_document(2))

    # The old document survives intact; no partial write is visible.
    assert read_json_strict(path)["event_sequence"] == 1
    assert not list(tmp_path.glob(".tmp-*")), "temp file must be cleaned up"


def test_failed_validation_never_publishes_a_document(tmp_path):
    path = tmp_path / "state.json"

    def reject(_document):
        raise ValueError("validator says no")

    with pytest.raises(ValueError, match="validator says no"):
        atomic_write_json(path, state_document(1), validator=reject)

    assert not path.exists(), "an invalid document must never appear at the target path"
    assert not list(tmp_path.glob(".tmp-*"))


def test_keyboard_interrupt_during_write_leaves_no_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"

    def interrupt(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        atomic_write_json(path, state_document(1))

    assert not path.exists()
    assert not list(tmp_path.glob(".tmp-*"))


def test_non_object_document_is_refused(tmp_path):
    with pytest.raises(CorruptDocument):
        atomic_write_json(tmp_path / "x.json", ["not", "an", "object"])


# -- monotonic state -----------------------------------------------------


def test_newer_sequence_replaces_older(tmp_path):
    path = tmp_path / "state.json"
    assert atomic_write_state(path, state_document(1)) is True
    assert atomic_write_state(path, state_document(5)) is True
    assert read_json_strict(path)["event_sequence"] == 5


def test_old_sequence_never_overwrites_new(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_state(path, state_document(5, state="running"))

    # A late-arriving stale event must not resurrect an earlier state.
    assert atomic_write_state(path, state_document(2, state="queued")) is False

    surviving = read_json_strict(path)
    assert surviving["event_sequence"] == 5
    assert surviving["state"] == "running"


def test_equal_sequence_is_allowed_so_republish_is_idempotent(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_state(path, state_document(3))
    assert atomic_write_state(path, state_document(3)) is True


def test_strict_mode_raises_on_regression(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_state(path, state_document(9))
    with pytest.raises(SequenceRegression, match="refusing to overwrite"):
        atomic_write_state(path, state_document(4), strict=True)


def test_state_requires_integer_sequence(tmp_path):
    path = tmp_path / "state.json"
    with pytest.raises(CorruptDocument, match="integer event_sequence"):
        atomic_write_state(path, state_document(1) | {"event_sequence": "3"})


def test_corrupt_existing_mirror_is_replaced_not_trusted(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")  # the legacy zero-byte artefact

    # A verified document must win over an unreadable one.
    assert atomic_write_state(path, state_document(1)) is True
    assert read_json_strict(path)["event_sequence"] == 1


def test_written_documents_are_deterministic(tmp_path):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    document = state_document(1)
    atomic_write_json(first, document)
    atomic_write_json(second, dict(reversed(list(document.items()))))
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_written_document_is_valid_json_on_disk(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, state_document(1))
    json.loads(path.read_text(encoding="utf-8"))
