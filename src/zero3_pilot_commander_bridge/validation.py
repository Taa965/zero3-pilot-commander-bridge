"""Transport validation.

The boundary rule, stated once and enforced everywhere:

    The bridge validates TRANSPORT. Zero3 Core validates MEANING.

Permitted here: JSON parseability, schema conformance, identifier shape and
consistency, payload size, protocol version support, content hash, terminal
state legality, sequence ordering.

Forbidden here, permanently: storyboard counts, scene counts, visual beat
counts, script content, Author Skill selection, Production Package contents,
Worker selection. Rules like ``storyboard == 17`` or ``beats == 107`` belong to
the Zero3 Core domain validator. Adding one here is an architectural
regression, however convenient it looks.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .models import COMMAND_SCHEMAS, TERMINAL_STATES

__all__ = [
    "ValidationError",
    "MAX_COMMAND_BYTES",
    "SUPPORTED_PROTOCOL_MAJOR",
    "schema_dir",
    "load_schema",
    "validate_against",
    "validate_command_envelope",
    "validate_state_document",
    "validate_event_document",
    "validate_result_document",
    "validate_accepted_document",
    "validate_rejected_document",
    "is_valid_identifier",
]


class ValidationError(Exception):
    """A document violates the transport contract."""


MAX_COMMAND_BYTES = 4 * 1024 * 1024
SUPPORTED_PROTOCOL_MAJOR = "1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def is_valid_identifier(value: object) -> bool:
    """Check identifier shape. Also blocks path traversal in file layout."""
    return isinstance(value, str) and bool(_IDENTIFIER.match(value))


@functools.cache
def schema_dir() -> Path:
    """Locate the protocol schema directory for source or installed runtimes."""
    override = os.environ.get("ZERO3_BRIDGE_SCHEMA_DIR")
    if override:
        candidate = Path(override)
        if (candidate / "result.schema.json").is_file():
            return candidate
        raise ValidationError(
            f"ZERO3_BRIDGE_SCHEMA_DIR does not contain schemas: {candidate}"
        )

    # Normal wheel/non-editable install created by setuptools data-files.
    installed = Path(sys.prefix) / "share" / "zero3-pilot-commander-bridge" / "schemas"
    if (installed / "result.schema.json").is_file():
        return installed

    # Source checkout / editable install.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "schemas"
        if (candidate / "result.schema.json").is_file():
            return candidate
    raise ValidationError("could not locate the schemas/ directory")


@functools.cache
def load_schema(name: str) -> dict[str, Any]:
    """Load and cache one JSON Schema by filename."""
    path = schema_dir() / name
    try:
        with path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
    except FileNotFoundError as exc:
        raise ValidationError(f"unknown schema: {name}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"schema {name} is not valid JSON: {exc}") from exc

    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValidationError(
            f"schema {name} is not a valid Draft 2020-12 schema: {exc}"
        ) from exc
    return schema


@functools.cache
def _validator(name: str) -> Draft202012Validator:
    # ``format`` is annotation-only unless a checker is supplied. Without
    # this, malformed timestamps such as "yesterday" pass a schema that says
    # ``format: date-time``.
    return Draft202012Validator(load_schema(name), format_checker=FormatChecker())


def validate_against(document: Any, schema_name: str) -> None:
    """Validate a document against a named schema, reporting every problem."""
    errors = sorted(_validator(schema_name).iter_errors(document), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(
            f"{'/'.join(str(p) for p in error.path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValidationError(f"{schema_name}: {detail}")


def validate_command_envelope(document: Any, *, max_bytes: int = MAX_COMMAND_BYTES) -> str:
    """Validate an inbound command envelope. Returns the command name.

    The ``payload`` is deliberately not inspected beyond being an object.
    """
    if not isinstance(document, dict):
        raise ValidationError("command envelope must be a JSON object")

    encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValidationError(f"command envelope is {len(encoded)} bytes, limit is {max_bytes}")

    command = document.get("command")
    if not isinstance(command, str) or command not in COMMAND_SCHEMAS:
        supported = ", ".join(sorted(COMMAND_SCHEMAS))
        raise ValidationError(f"unsupported command {command!r}; supported: {supported}")

    protocol = document.get("protocol_version")
    if protocol is not None:
        if not isinstance(protocol, str) or not protocol.startswith(
            f"{SUPPORTED_PROTOCOL_MAJOR}."
        ):
            raise ValidationError(
                f"unsupported protocol_version {protocol!r}; "
                f"this bridge speaks {SUPPORTED_PROTOCOL_MAJOR}.x"
            )

    validate_against(document, COMMAND_SCHEMAS[command])

    if not is_valid_identifier(document.get("execution_id")):
        raise ValidationError(f"malformed execution_id: {document.get('execution_id')!r}")

    expected_hash = document.get("content_hash")
    if isinstance(expected_hash, str):
        _verify_content_hash(document.get("payload"), expected_hash)

    return command


def _verify_content_hash(payload: Any, expected: str) -> None:
    """Verify payload integrity without interpreting the payload."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    actual = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if actual != expected:
        raise ValidationError("content_hash does not match payload")


def validate_state_document(document: Any) -> None:
    """Validate a latest-state mirror."""
    validate_against(document, "state-mirror.schema.json")


def validate_event_document(document: Any) -> None:
    """Validate one event mirror entry."""
    validate_against(document, "event-mirror.schema.json")


def validate_result_document(
    document: Any,
    *,
    execution_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Validate a terminal result."""
    if not isinstance(document, dict):
        raise ValidationError("result must be a JSON object")

    validate_against(document, "result.schema.json")

    if execution_id is not None and document.get("execution_id") != execution_id:
        raise ValidationError(
            f"result execution_id {document.get('execution_id')!r} does not match "
            f"expected {execution_id!r}"
        )

    if task_id is not None and document.get("task_id") != task_id:
        raise ValidationError(
            f"result task_id {document.get('task_id')!r} does not match expected {task_id!r}"
        )

    if document.get("terminal") is not True:
        raise ValidationError("result is not marked terminal")

    state = document.get("state")
    if state not in TERMINAL_STATES:
        legal = ", ".join(sorted(TERMINAL_STATES))
        raise ValidationError(f"illegal terminal state {state!r}; legal states: {legal}")


def validate_accepted_document(
    document: object, *, execution_id: str | None = None
) -> None:
    """Validate an acceptance verdict."""
    if not isinstance(document, dict):
        raise ValidationError("acceptance verdict must be a JSON object")
    validate_against(document, "command-accepted.schema.json")
    if execution_id is not None and document.get("execution_id") != execution_id:
        raise ValidationError(
            f"acceptance execution_id {document.get('execution_id')!r} does not match "
            f"expected {execution_id!r}"
        )
    if document.get("correlation") == "task_id" and not str(
        document.get("task_id") or ""
    ).strip():
        raise ValidationError("acceptance claims correlation but carries no task_id")


def validate_rejected_document(
    document: object, *, execution_id: str | None = None
) -> None:
    """Validate a rejection verdict."""
    if not isinstance(document, dict):
        raise ValidationError("rejection verdict must be a JSON object")
    validate_against(document, "command-rejected.schema.json")
    if execution_id is not None and document.get("execution_id") != execution_id:
        raise ValidationError(
            f"rejection execution_id {document.get('execution_id')!r} does not match "
            f"expected {execution_id!r}"
        )
