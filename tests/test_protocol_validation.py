"""Transport validation, including the negative guarantee.

The most important test here is :func:`test_transport_ignores_domain_content`,
which asserts that the bridge does *not* judge domain content. If someone later
adds a storyboard or beat-count rule to the transport, that test fails.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from jsonschema import Draft202012Validator

from zero3_pilot_commander_bridge.models import COMMAND_SCHEMAS, TERMINAL_STATES
from zero3_pilot_commander_bridge.validation import (
    MAX_COMMAND_BYTES,
    ValidationError,
    is_valid_identifier,
    load_schema,
    schema_dir,
    validate_command_envelope,
    validate_result_document,
    validate_state_document,
)


def make_envelope(**overrides):
    envelope = {
        "schema": "zero3.commander.command.execution-submit/1.0",
        "command": "execution.submit",
        "protocol_version": "1.0",
        "execution_id": "exec-2026-08-28-001",
        "commander_id": "gpt-web-session",
        "issued_at": "2026-08-28T10:00:00Z",
        "payload": {"anything": "opaque to the bridge"},
    }
    envelope.update(overrides)
    return envelope


# -- schema hygiene ------------------------------------------------------


def test_every_schema_file_is_a_valid_json_schema():
    files = sorted(schema_dir().glob("*.schema.json"))
    assert len(files) == 10, f"expected 10 schemas, found {[f.name for f in files]}"
    for path in files:
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_result_schema_terminal_states_match_core():
    schema = load_schema("result.schema.json")
    assert set(schema["properties"]["state"]["enum"]) == set(TERMINAL_STATES)


def test_every_supported_command_has_a_schema():
    for name in COMMAND_SCHEMAS.values():
        assert (schema_dir() / name).is_file()


# -- envelope validation -------------------------------------------------


def test_valid_envelope_is_accepted():
    assert validate_command_envelope(make_envelope()) == "execution.submit"


@pytest.mark.parametrize("missing", ["schema", "command", "execution_id", "commander_id", "issued_at"])
def test_missing_required_field_is_rejected(missing):
    envelope = make_envelope()
    del envelope[missing]
    with pytest.raises(ValidationError):
        validate_command_envelope(envelope)


def test_unknown_command_is_rejected():
    with pytest.raises(ValidationError, match="unsupported command"):
        validate_command_envelope(make_envelope(command="execution.obliterate"))


def test_unsupported_protocol_version_is_rejected():
    with pytest.raises(ValidationError, match="unsupported protocol_version"):
        validate_command_envelope(make_envelope(protocol_version="2.0"))


def test_unknown_envelope_field_is_rejected():
    with pytest.raises(ValidationError):
        validate_command_envelope(make_envelope(surprise="extra"))


def test_oversized_envelope_is_rejected():
    envelope = make_envelope(payload={"blob": "x" * 2048})
    with pytest.raises(ValidationError, match="limit is"):
        validate_command_envelope(envelope, max_bytes=1024)


def test_default_size_limit_is_generous_enough_for_real_packages():
    assert MAX_COMMAND_BYTES >= 1024 * 1024


@pytest.mark.parametrize(
    "bad_id",
    ["", "../../etc/passwd", "has space", "has/slash", "x" * 200, ".leading-dot"],
)
def test_malformed_execution_id_is_rejected(bad_id):
    assert not is_valid_identifier(bad_id)


def test_content_hash_must_match_payload():
    payload = {"a": 1, "b": [2, 3]}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    validate_command_envelope(make_envelope(payload=payload, content_hash=digest))

    with pytest.raises(ValidationError, match="content_hash"):
        validate_command_envelope(
            make_envelope(payload={"a": 999}, content_hash=digest)
        )


# -- the boundary --------------------------------------------------------


def test_transport_ignores_domain_content():
    """The bridge must never judge video production meaning.

    Every payload below would be nonsense to a domain validator. The transport
    accepts all of them, because storyboard counts, scene counts, beat counts,
    skill selection, and worker choice belong to Zero3 Core.
    """
    domain_payloads = [
        {"storyboard": {"scenes": 3}, "visual_beats": 7},
        {"storyboard": {"scenes": 17}, "visual_beats": 107},
        {"storyboard": {"scenes": 0}, "visual_beats": 0},
        {"author_skill": "anything-at-all", "worker": "whichever"},
        {"production_package": {"totally": "unvalidated"}},
    ]
    for payload in domain_payloads:
        assert validate_command_envelope(make_envelope(payload=payload)) == "execution.submit"


def test_payload_shape_is_the_only_payload_rule():
    """Payload must be an object, and that is the entire constraint."""
    validate_command_envelope(make_envelope(payload={}))
    with pytest.raises(ValidationError):
        validate_command_envelope(make_envelope(payload=[1, 2, 3]))


# -- mirrors -------------------------------------------------------------


def test_state_document_requires_the_agreed_fields():
    document = {
        "schema": "zero3.commander.state/2.0",
        "execution_id": "exec-1",
        "task_id": "0f9c1f4e-1f3a-4a1e-9c2b-8d5f0a1b2c3d",
        "state": "running",
        "stage": "render",
        "progress": 42,
        "event_sequence": 7,
        "updated_at": "2026-08-28T10:00:00Z",
    }
    validate_state_document(document)

    for field in ("execution_id", "task_id", "state", "stage", "progress", "event_sequence"):
        broken = dict(document)
        del broken[field]
        with pytest.raises(ValidationError):
            validate_state_document(broken)


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_all_five_terminal_states_are_legal(state):
    validate_result_document(
        {
            "schema": "zero3.commander.result/2.0",
            "execution_id": "exec-1",
            "task_id": "task-1",
            "state": state,
            "terminal": True,
            "completed_at": "2026-08-28T10:00:00Z",
        }
    )


def test_running_is_not_a_result():
    with pytest.raises(ValidationError):
        validate_result_document(
            {
                "schema": "zero3.commander.result/2.0",
                "execution_id": "exec-1",
                "task_id": "task-1",
                "state": "running",
                "terminal": True,
                "completed_at": "2026-08-28T10:00:00Z",
            }
        )
