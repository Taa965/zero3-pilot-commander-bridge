"""Malformed transport envelopes must be removable from the mailbox."""

from __future__ import annotations

import json

from zero3_pilot_commander_bridge.atomic_io import read_json_strict
from zero3_pilot_commander_bridge.github_client import BridgeRepository
from zero3_pilot_commander_bridge.ingestor import Ingestor

EXECUTION_ID = "exec-malformed-envelope-001"


class NeverCalledClient:
    def submit_execution(self, _package):
        raise AssertionError("invalid envelope must not reach Zero3")


def test_invalid_typed_metadata_can_still_be_written_as_rejection(tmp_path):
    repo = BridgeRepository(tmp_path)
    pending = repo.pending_command(EXECUTION_ID)
    pending.parent.mkdir(parents=True)
    pending.write_text(
        json.dumps(
            {
                "schema": "zero3.commander.command.execution-submit/1.0",
                "command": {"not": "a string"},
                "execution_id": EXECUTION_ID,
                "commander_id": ["not", "a", "string"],
                "issued_at": "2026-08-28T10:00:00Z",
                "payload": {},
            }
        ),
        encoding="utf-8",
    )

    outcome = Ingestor(repo, NeverCalledClient()).ingest_one(pending)

    assert outcome.status == "rejected"
    assert not pending.exists()
    verdict = read_json_strict(repo.rejected_command(EXECUTION_ID))
    assert verdict["reason_code"] == "invalid-envelope"
    assert verdict["command"] is None
    assert verdict["commander_id"] is None
