"""Hardening tests added after the first production-runtime audit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from zero3_pilot_commander_bridge.atomic_io import read_json_strict
from zero3_pilot_commander_bridge.commander_client import (
    CommanderClient,
    CommanderError,
    CommanderHTTPError,
)
from zero3_pilot_commander_bridge.config import CommanderConfig
from zero3_pilot_commander_bridge.github_client import BridgeRepository
from zero3_pilot_commander_bridge.ingestor import IngestOutcome, Ingestor
from zero3_pilot_commander_bridge.models import ExecutionResult, StateMirror, utc_now
from zero3_pilot_commander_bridge.publisher import Publisher
from zero3_pilot_commander_bridge.validation import ValidationError, validate_state_document

EXECUTION_ID = "exec-hardening-001"
TASK_ID = "0f9c1f4e-1f3a-4a1e-9c2b-8d5f0a1b2c3d"


class FakeClient:
    def __init__(self, *, submit=None):
        self.submit = submit or {
            "schema": "zero3.execution-acceptance/1.0",
            "execution_id": EXECUTION_ID,
            "task_id": TASK_ID,
        }
        self.submits = 0

    def submit_execution(self, _package):
        self.submits += 1
        if isinstance(self.submit, Exception):
            raise self.submit
        return self.submit


def write_pending(repo: BridgeRepository, execution_id: str = EXECUTION_ID, **overrides):
    document = {
        "schema": "zero3.commander.command.execution-submit/1.0",
        "command": "execution.submit",
        "execution_id": execution_id,
        "commander_id": "gpt-web-session",
        "issued_at": "2026-08-28T10:00:00Z",
        "payload": {"execution_id": execution_id},
    }
    document.update(overrides)
    path = repo.pending_command(execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("status", [401, 403, 404, 405, 410, 429, 500, 503])
def test_infrastructure_and_protocol_statuses_stay_retryable(tmp_path, status):
    repo = BridgeRepository(tmp_path)
    pending = write_pending(repo)
    client = FakeClient(submit=CommanderHTTPError(status, "not a command verdict", "u"))

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "deferred"
    assert pending.exists()
    assert not repo.rejected_command(EXECUTION_ID).exists()


@pytest.mark.parametrize("status", [400, 409, 413, 422])
def test_authoritative_command_refusals_are_terminal(tmp_path, status):
    repo = BridgeRepository(tmp_path)
    pending = write_pending(repo)
    client = FakeClient(submit=CommanderHTTPError(status, "refused", "u"))

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "rejected"
    assert not pending.exists()


def test_commander_http_error_property_uses_same_classification():
    assert CommanderHTTPError(422, "", "u").rejected is True
    assert CommanderHTTPError(401, "", "u").rejected is False
    assert CommanderHTTPError(404, "", "u").rejected is False
    assert CommanderHTTPError(429, "", "u").rejected is False


def test_missing_rotated_token_is_a_commander_error_not_config_escape(tmp_path):
    config = CommanderConfig(
        base_url="https://example.com",
        token_file=tmp_path / "missing-token",
        commander_id="bridge-test",
    )
    with pytest.raises(CommanderError, match="credential unavailable"):
        CommanderClient(config).health()


def test_payload_execution_identity_cannot_contradict_mailbox_envelope(tmp_path):
    repo = BridgeRepository(tmp_path)
    pending = write_pending(repo, payload={"execution_id": "different-execution"})
    client = FakeClient()

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "rejected"
    assert client.submits == 0
    verdict = read_json_strict(repo.rejected_command(EXECUTION_ID))
    assert verdict["reason_code"] == "identifier-mismatch"


def test_wrong_acceptance_schema_is_accepted_but_not_strongly_correlated(tmp_path):
    repo = BridgeRepository(tmp_path)
    pending = write_pending(repo)
    client = FakeClient(
        submit={
            "schema": "unexpected.acceptance/9.9",
            "execution_id": EXECUTION_ID,
            "task_id": TASK_ID,
        }
    )

    outcome = Ingestor(repo, client).ingest_one(pending)

    assert outcome.status == "accepted"
    verdict = read_json_strict(repo.accepted_command(EXECUTION_ID))
    assert verdict["correlation"] == "unknown"
    assert "task_id" not in verdict


def test_one_unexpected_ingest_failure_does_not_block_later_commands(tmp_path, monkeypatch):
    repo = BridgeRepository(tmp_path)
    first = write_pending(repo, "exec-hardening-001")
    second = write_pending(repo, "exec-hardening-002")
    ingestor = Ingestor(repo, FakeClient())

    def fake_ingest(path: Path):
        if path == first:
            raise RuntimeError("boom")
        return IngestOutcome(path.stem, "accepted")

    monkeypatch.setattr(ingestor, "ingest_one", fake_ingest)
    outcomes = ingestor.ingest_pending()

    assert [item.execution_id for item in outcomes] == [first.stem, second.stem]
    assert outcomes[0].status == "deferred"
    assert outcomes[1].status == "accepted"


def test_parseable_but_schema_invalid_state_is_not_indexed(tmp_path):
    repo = BridgeRepository(tmp_path)
    repo.state_dir.mkdir(parents=True)
    bad = repo.state_path(EXECUTION_ID)
    bad.write_text(json.dumps({"schema": "wrong", "execution_id": EXECUTION_ID}), encoding="utf-8")

    publisher = Publisher(repo)
    assert publisher.invalid_state_mirrors() == [EXECUTION_ID]
    publisher.refresh_indexes()

    assert read_json_strict(repo.active_index)["executions"] == []


def test_parseable_but_invalid_result_is_repaired(tmp_path):
    repo = BridgeRepository(tmp_path)
    repo.results_dir.mkdir(parents=True)
    path = repo.result_path(EXECUTION_ID)
    path.write_text(json.dumps({"schema": "wrong", "state": "succeeded"}), encoding="utf-8")

    publisher = Publisher(repo)
    result = ExecutionResult(
        execution_id=EXECUTION_ID,
        task_id=TASK_ID,
        state="succeeded",
        completed_at=utc_now(),
        event_sequence=4,
    )

    assert publisher.publish_result(result, expected_task_id=TASK_ID) is True
    repaired = read_json_strict(path)
    assert repaired["execution_id"] == EXECUTION_ID
    assert repaired["task_id"] == TASK_ID


def test_quiet_state_observation_does_not_rewrite_updated_at(tmp_path):
    repo = BridgeRepository(tmp_path)
    publisher = Publisher(repo)
    first = StateMirror(EXECUTION_ID, TASK_ID, "running", "render", 20, 3, utc_now())
    assert publisher.publish_state(first) is True
    before = read_json_strict(repo.state_path(EXECUTION_ID))

    later = StateMirror(
        EXECUTION_ID,
        TASK_ID,
        "running",
        "render",
        20,
        3,
        "2026-08-28T12:00:00Z",
    )
    assert publisher.publish_state(later) is False
    assert read_json_strict(repo.state_path(EXECUTION_ID)) == before


def test_date_time_format_is_actually_enforced():
    document = {
        "schema": "zero3.commander.state/2.0",
        "execution_id": EXECUTION_ID,
        "task_id": TASK_ID,
        "state": "running",
        "stage": "render",
        "progress": 10,
        "event_sequence": 1,
        "terminal": False,
        "updated_at": "yesterday",
        "source": "publisher",
    }
    with pytest.raises(ValidationError):
        validate_state_document(document)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout


def _configure_identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "Bridge Test")
    _git(repo, "config", "user.email", "bridge-test@example.invalid")


def test_git_sync_sees_external_commands_and_push_retry_preserves_races(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    server = tmp_path / "server"
    external = tmp_path / "external"

    remote.mkdir()
    _git(remote, "init", "--bare")
    seed.mkdir()
    _git(seed, "init")
    _configure_identity(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(tmp_path, "clone", str(remote), str(server))
    _git(tmp_path, "clone", str(remote), str(external))
    _configure_identity(server)
    _configure_identity(external)

    ext_pending = external / "commands" / "pending"
    ext_pending.mkdir(parents=True)
    (ext_pending / "one.json").write_text("{}\n", encoding="utf-8")
    _git(external, "add", "commands")
    _git(external, "commit", "-m", "external command one")
    _git(external, "push", "origin", "main")

    bridge_repo = BridgeRepository(server)
    bridge_repo.sync_from_remote()
    assert (server / "commands" / "pending" / "one.json").is_file()

    state_dir = server / "state"
    state_dir.mkdir()
    (state_dir / "local.json").write_text("{}\n", encoding="utf-8")
    bridge_repo.commit_paths(["state"], "local mirror")

    (ext_pending / "two.json").write_text("{}\n", encoding="utf-8")
    _git(external, "add", "commands")
    _git(external, "commit", "-m", "external command two")
    _git(external, "push", "origin", "main")

    bridge_repo.push_with_retry()

    verify = tmp_path / "verify"
    _git(tmp_path, "clone", str(remote), str(verify))
    assert (verify / "commands" / "pending" / "one.json").is_file()
    assert (verify / "commands" / "pending" / "two.json").is_file()
    assert (verify / "state" / "local.json").is_file()
