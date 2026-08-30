"""HTTPS transport client for Zero3 Pilot remote control.

H5 is the default adapter:

===================  ===============================================
``health``           ``GET  /health``
``execution.submit`` ``POST /api/control/v1/tasks``
``execution.status`` ``GET  /api/control/v1/tasks/{task_id}``
===================  ===============================================

The former Pilot Dev Executor ``/api/commander/v1`` adapter is retained only as
an explicit ``legacy-commander`` rollback path.  Neither adapter grants this
repository execution authority: payloads are relayed as transport documents,
and task execution remains on the Zero3 Pilot Remote Host/Codex runtime.

TLS verification is unconditional.  A private CA is supported through
``ZERO3_COMMANDER_CA_BUNDLE``; verification cannot be disabled.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .config import (
    H5_ADAPTER,
    LEGACY_COMMANDER_ADAPTER,
    CommanderConfig,
    ConfigError,
)
from .http_semantics import is_permanent_rejection

__all__ = ["CommanderError", "CommanderHTTPError", "CommanderClient"]

H5_TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "blocked", "outcome_unknown", "quarantined"}
)
NORMALIZED_STATUS_SCHEMA = "zero3.execution-status/1.0"


class CommanderError(Exception):
    """The configured transport endpoint could not provide a usable answer."""


class CommanderHTTPError(CommanderError):
    """The transport endpoint answered with a non-success status."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"commander returned HTTP {status} for {url}: {body[:512]}")
        self.status = status
        self.body = body
        self.url = url

    @property
    def rejected(self) -> bool:
        """Whether this status is an authoritative refusal of this command."""
        return is_permanent_rejection(self.status)


class CommanderClient:
    """A thin, auditable HTTPS client with H5 as the default adapter."""

    def __init__(self, config: CommanderConfig) -> None:
        self._config = config
        self._ssl_context = self._build_ssl_context(config)

    @staticmethod
    def _build_ssl_context(config: CommanderConfig) -> ssl.SSLContext:
        cafile = str(config.ca_bundle) if config.ca_bundle else None
        context = ssl.create_default_context(cafile=cafile)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def __repr__(self) -> str:
        return (
            f"CommanderClient(base_url={self._config.base_url!r}, "
            f"commander_id={self._config.commander_id!r}, "
            f"adapter={self._config.adapter!r})"
        )

    def _request_url(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None

        try:
            token = self._config.read_token()
        except ConfigError as exc:
            raise CommanderError(f"commander credential unavailable: {exc}") from exc

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Zero3-Commander-ID": self._config.commander_id,
            "Accept": "application/json",
            "User-Agent": "zero3-pilot-commander-bridge",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(
                request, timeout=self._config.timeout, context=self._ssl_context
            ) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CommanderHTTPError(exc.code, detail, url) from exc
        except urllib.error.URLError as exc:
            raise CommanderError(f"cannot reach commander at {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CommanderError(
                f"commander request timed out after {self._config.timeout}s"
            ) from exc

        if not raw.strip():
            raise CommanderError(
                f"commander returned an empty body for {url} (HTTP {status})"
            )

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommanderError(
                f"commander returned malformed JSON for {url}: {exc}"
            ) from exc

        if not isinstance(document, dict):
            raise CommanderError(
                f"commander returned {type(document).__name__}, expected an object, for {url}"
            )
        return document

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Legacy Commander API request helper kept for rollback compatibility."""
        return self._request_url(method, self._config.url_for(path), payload)

    def health(self) -> dict[str, Any]:
        """Ask the selected transport endpoint for health."""
        if self._config.adapter == LEGACY_COMMANDER_ADAPTER:
            return self._request("GET", "health")

        document = self._request_url("GET", self._config.root_url_for("health"))
        normalized = dict(document)
        normalized["ok"] = str(document.get("status") or "").lower() == "ok"
        normalized["adapter"] = H5_ADAPTER
        return normalized

    def submit_execution(self, package: dict[str, Any]) -> dict[str, Any]:
        """Relay one transport payload without domain inspection or rewriting."""
        if self._config.adapter == LEGACY_COMMANDER_ADAPTER:
            return self._request("POST", "execution-packages", payload=package)
        return self._request_url(
            "POST",
            self._config.control_url_for("tasks"),
            payload=package,
        )

    def execution_status(self, task_id: str) -> dict[str, Any]:
        """Fetch and normalize a transport-safe task snapshot."""
        if not task_id or "/" in task_id or "\\" in task_id:
            raise CommanderError(f"malformed task_id: {task_id!r}")

        if self._config.adapter == LEGACY_COMMANDER_ADAPTER:
            return self._request("GET", f"execution-packages/tasks/{task_id}")

        record = self._request_url(
            "GET", self._config.control_url_for(f"tasks/{task_id}")
        )
        return self._normalize_h5_task_record(record, expected_task_id=task_id)

    @staticmethod
    def _normalize_h5_task_record(
        record: dict[str, Any], *, expected_task_id: str
    ) -> dict[str, Any]:
        """Project an H5 ``TaskRecord`` into the bridge's stable status shape.

        This is protocol projection only.  H5 remains authoritative for task
        admission, persistence, leases, fencing and terminal state.
        """
        if not isinstance(record, dict):
            raise CommanderError("H5 task response is not an object")

        task = record.get("task")
        if not isinstance(task, dict):
            raise CommanderError("H5 task response carries no task object")

        returned_task_id = task.get("task_id")
        if not isinstance(returned_task_id, str) or not returned_task_id:
            raise CommanderError("H5 task response carries no task_id")
        if returned_task_id != expected_task_id:
            raise CommanderError(
                f"H5 task_id {returned_task_id!r} does not match expected {expected_task_id!r}"
            )

        state = record.get("state")
        if not isinstance(state, str) or not state.strip():
            raise CommanderError("H5 task response carries no state")
        state = state.strip()

        events = record.get("events")
        if events is None:
            events = []
        if not isinstance(events, list):
            raise CommanderError("H5 task response carries malformed events")

        summary = dict(task)
        summary["events"] = events
        summary["control_plane"] = {
            key: record[key]
            for key in (
                "sticky_node_id",
                "fencing_token",
                "active_lease",
                "last_event_sequence",
                "created_at",
                "updated_at",
            )
            if key in record
        }
        terminal_record = record.get("terminal")
        if isinstance(terminal_record, dict):
            summary["terminal_record"] = terminal_record

        return {
            "schema": NORMALIZED_STATUS_SCHEMA,
            "task_id": returned_task_id,
            "execution_id": task.get("execution_id"),
            "state": state,
            "terminal": state in H5_TERMINAL_STATES,
            "task": summary,
        }
