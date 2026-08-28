"""Durable JSON document IO.

The previous bridge treated a file existing as a result being valid::

    [[ ! -e "$out" ]] || continue

That accepted zero-byte and partially written documents as authoritative. This
module makes that impossible. Every write follows:

    serialize -> temp file -> flush -> fsync -> re-read -> validate -> rename

and every read goes through :func:`read_json_strict`, which refuses missing,
zero-byte, whitespace-only, malformed, and non-object documents.

A reader therefore never observes a half-written document: until ``os.replace``
runs, the previous good document is still in place, and ``os.replace`` is
atomic on both POSIX and Windows.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "AtomicIOError",
    "CorruptDocument",
    "SequenceRegression",
    "atomic_write_json",
    "atomic_write_state",
    "is_valid_document",
    "read_json_strict",
    "read_json_or_none",
    "read_validated_json_or_none",
]

Validator = Callable[[dict[str, Any]], None]


class AtomicIOError(Exception):
    """Base class for durable IO failures."""


class CorruptDocument(AtomicIOError):
    """A document is missing, empty, malformed, or not a JSON object."""


class SequenceRegression(AtomicIOError):
    """A write would move a mirror backwards in time."""


def _serialize(document: dict[str, Any]) -> str:
    """Render a document deterministically so diffs stay reviewable."""
    if not isinstance(document, dict):
        raise CorruptDocument(f"document must be a JSON object, got {type(document).__name__}")
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def read_json_strict(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a JSON object, refusing anything that is not clearly valid."""
    target = Path(path)

    try:
        stat = target.stat()
    except FileNotFoundError as exc:
        raise CorruptDocument(f"missing document: {target}") from exc
    except OSError as exc:
        raise CorruptDocument(f"unreadable document: {target}: {exc}") from exc

    if not target.is_file():
        raise CorruptDocument(f"not a regular file: {target}")
    if stat.st_size == 0:
        raise CorruptDocument(f"zero-byte document: {target}")

    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CorruptDocument(f"unreadable document: {target}: {exc}") from exc

    if not raw.strip():
        raise CorruptDocument(f"whitespace-only document: {target}")

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptDocument(f"malformed JSON in {target}: {exc}") from exc

    if not isinstance(document, dict):
        raise CorruptDocument(
            f"expected a JSON object in {target}, got {type(document).__name__}"
        )

    return document


def read_json_or_none(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return a parseable JSON object, or ``None`` when absent/corrupt.

    This helper checks JSON structure only. Protocol mirrors and verdicts
    should normally use :func:`read_validated_json_or_none` instead.
    """
    try:
        return read_json_strict(path)
    except CorruptDocument:
        return None


def read_validated_json_or_none(
    path: str | os.PathLike[str], validator: Validator
) -> dict[str, Any] | None:
    """Return a schema-valid document, or ``None`` when it is unusable.

    A syntactically valid object with the wrong schema is not a real state,
    result, event, or verdict. Treating it as authoritative is the parseable
    version of the legacy zero-byte-file bug.
    """
    try:
        document = read_json_strict(path)
        validator(document)
        return document
    except Exception:
        return None


def is_valid_document(
    path: str | os.PathLike[str], validator: Validator | None = None
) -> bool:
    """Report whether a document is genuinely usable."""
    try:
        document = read_json_strict(path)
    except CorruptDocument:
        return False
    if validator is not None:
        try:
            validator(document)
        except Exception:
            return False
    return True


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry so the rename itself survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(
    path: str | os.PathLike[str],
    document: dict[str, Any],
    *,
    validator: Validator | None = None,
) -> None:
    """Publish a document atomically, or leave the previous one untouched."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    text = _serialize(document)
    if validator is not None:
        validator(document)

    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".tmp-{target.name}-", suffix=".partial"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        written = read_json_strict(tmp_path)
        if validator is not None:
            validator(written)

        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    _fsync_directory(target.parent)


def atomic_write_state(
    path: str | os.PathLike[str],
    document: dict[str, Any],
    *,
    validator: Validator | None = None,
    strict: bool = False,
) -> bool:
    """Write a state mirror only when it does not move backwards.

    Returns ``True`` when the mirror was updated and ``False`` when a stale
    document was ignored. A parseable but schema-invalid existing state is
    treated as absent by callers that perform protocol validation before this
    function; this function itself enforces sequence monotonicity for any
    parseable existing document.
    """
    target = Path(path)

    incoming = document.get("event_sequence")
    if not isinstance(incoming, int) or isinstance(incoming, bool):
        raise CorruptDocument("state document requires an integer event_sequence")

    existing = read_json_or_none(target)
    if existing is not None:
        current = existing.get("event_sequence")
        if isinstance(current, int) and not isinstance(current, bool) and incoming < current:
            if strict:
                raise SequenceRegression(
                    f"refusing to overwrite event_sequence {current} with {incoming} at {target}"
                )
            return False

    atomic_write_json(target, document, validator=validator)
    return True
