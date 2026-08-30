"""H5 control-plane adapter contract tests."""

from __future__ import annotations

import pytest

from zero3_pilot_commander_bridge.commander_client import CommanderClient
from zero3_pilot_commander_bridge.config import (
    H5_ADAPTER,
    LEGACY_COMMANDER_ADAPTER,
    CommanderConfig,
    ConfigError,
)
from zero3_pilot_commander_bridge.ingestor import Ingestor
from zero3_pilot_commander_bridge.status_projection import project_status

TASK_ID = "task-h5-001"
EXECUTION_ID = "exec-h5-001"


def config(tmp_path, *, adapter=H5_ADAPTER):
    token = tmp_path / "token"
    token.write_text("test-token\n", encoding="utf-8")
    return CommanderConfig(
        base_url="https://pilot.example.invalid",
        token_file=token,
        commander_id="bridge-test",
        adapter=adapter,
    )


def h5_record(*, state="running"):
    return {
        "task": {
            "protocol": "zero3.pilot.remote-task.v1",
            "task_id": TASK_ID,
            "execution_id": EXECUTION_ID,
            "objective": "transport fixture",
            "target": {"workspace": "fixture"},
        },
        "task_fingerprint": "{}",
        "state": state,
        "sticky_node_id": "node-a",
        "fencing_token": 2,
        "active_lease": None,
        "last_event_sequence": 7,
        "events": [
            {
                "delivery_id": "delivery-7",
                "execution_id": EXECUTION_ID,
                "lease_id": "lease-2",
                "fencing_token": 2,
                "event_sequence": 7,
                "event_type": "turn.completed",
                "created_at": "2026-08-30T12:00:00Z",
                "payload": {"ok": True},
            }
        ],
        "terminal": (
            {
                "delivery_id": "terminal-1",
                "execution_id": EXECUTION_ID,
                "lease_id": "lease-2",
                "fencing_token": 2,
                "created_at": "2026-08-30T12:01:00Z",
                "state": state,
                "result": {"reason": "needs-input"},
            }
            if state == "blocked"
            else None
        ),
        "created_at": "2026-08-30T11:59:00Z",
        "updated_at": "2026-08-30T12:01:00Z",
    }


def test_config_defaults_to_h5(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-token\n", encoding="utf-8")
    cfg = CommanderConfig.from_env(
        {
            "ZERO3_COMMANDER_BASE_URL": "https://pilot.example.invalid/",
            "ZERO3_COMMANDER_TOKEN_FILE": str(token),
        }
    )
    assert cfg.adapter == H5_ADAPTER
    assert cfg.root_url_for("health") == "https://pilot.example.invalid/health"
    assert cfg.control_url_for("tasks") == "https://pilot.example.invalid/api/control/v1/tasks"


def test_config_rejects_inline_credentials_query_and_fragment(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-token\n", encoding="utf-8")
    for base_url in (
        "https://user:secret@pilot.example.invalid",
        "https://pilot.example.invalid?token=secret",
        "https://pilot.example.invalid#fragment",
    ):
        with pytest.raises(ConfigError):
            CommanderConfig.from_env(
                {
                    "ZERO3_COMMANDER_BASE_URL": base_url,
                    "ZERO3_COMMANDER_TOKEN_FILE": str(token),
                }
            )


def test_legacy_adapter_requires_explicit_opt_in(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-token\n", encoding="utf-8")
    cfg = CommanderConfig.from_env(
        {
            "ZERO3_COMMANDER_BASE_URL": "https://pilot.example.invalid",
            "ZERO3_COMMANDER_TOKEN_FILE": str(token),
            "ZERO3_COMMANDER_ADAPTER": "legacy-commander",
        }
    )
    assert cfg.adapter == LEGACY_COMMANDER_ADAPTER
    assert cfg.url_for("health").endswith("/api/commander/v1/health")


def test_h5_submit_relays_payload_verbatim_to_control_tasks(tmp_path, monkeypatch):
    client = CommanderClient(config(tmp_path))
    captured = {}

    def fake_request(method, url, payload=None):
        captured.update(method=method, url=url, payload=payload)
        return h5_record()

    monkeypatch.setattr(client, "_request_url", fake_request)
    payload = {
        "protocol": "zero3.pilot.remote-task.v1",
        "task_id": TASK_ID,
        "execution_id": EXECUTION_ID,
        "objective": "do the requested task",
        "target": {"workspace": "repo"},
    }

    response = client.submit_execution(payload)

    assert response["task"]["task_id"] == TASK_ID
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/control/v1/tasks")
    assert captured["payload"] is payload


def test_h5_task_record_is_normalized_without_taking_execution_authority():
    snapshot = CommanderClient._normalize_h5_task_record(
        h5_record(state="blocked"),
        expected_task_id=TASK_ID,
    )

    assert snapshot["schema"] == "zero3.execution-status/1.0"
    assert snapshot["state"] == "blocked"
    assert snapshot["terminal"] is True
    assert snapshot["task"]["control_plane"]["fencing_token"] == 2
    assert snapshot["task"]["terminal_record"]["result"] == {"reason": "needs-input"}

    projection = project_status(
        snapshot,
        execution_id=EXECUTION_ID,
        task_id=TASK_ID,
        previous_sequence=6,
    )
    assert projection.state == "blocked"
    assert projection.terminal is True
    assert [event.event_sequence for event in projection.events] == [7]
    assert projection.events[0].detail["payload"] == {"ok": True}


def test_h5_create_response_correlates_through_nested_task():
    task_id, problem = Ingestor.correlate(h5_record(), EXECUTION_ID)
    assert task_id == TASK_ID
    assert problem == ""
