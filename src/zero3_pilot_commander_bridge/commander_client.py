"""HTTPS client for the Zero3 Commander Gateway.

This is the *only* module permitted to reach Zero3, and it reaches it only over
HTTPS at ``/api/commander/v1``. There is no database connection, no SSH, no
shared filesystem, and no import of Zero3 Core Python modules anywhere in this
repository.

Stage 1 implements the three operations the gateway actually exposes today:

===================  ====================================================
``health``           ``GET  /api/commander/v1/health``
``execution.submit`` ``POST /api/commander/v1/execution-packages``
``execution.status`` ``GET  /api/commander/v1/execution-packages/tasks/{id}``
===================  ====================================================

Capabilities not backed by a real endpoint stay disabled in
``bridge/capabilities.json`` rather than being stubbed out here.

TLS verification is unconditional. There is no switch to disable it and no
``curl -k`` equivalent. A private CA is supported by pointing
``ZERO3_COMMANDER_CA_BUNDLE`` at a bundle, which is the correct way to trust a
non-public issuer without weakening verification.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .config import CommanderConfig, ConfigError
from .http_semantics import is_permanent_rejection

__all__ = ["CommanderError", "CommanderHTTPError", "CommanderClient"]


class CommanderError(Exception):
    """The Commander Gateway could not be reached or gave an unusable answer."""


class CommanderHTTPError(CommanderError):
    """The gateway answered with a non-success status."""

    def __init__(self, status: int, body: str, url: str) -> None:
        # The body may echo request detail, so keep it bounded in the message.
        super().__init__(f"commander returned HTTP {status} for {url}: {body[:512]}")
        self.status = status
        self.body = body
        self.url = url

    @property
    def rejected(self) -> bool:
        """Whether this status is an authoritative refusal of this command.

        Authentication failures, throttling, route/protocol mismatches, and
        infrastructure failures are not command verdicts. They must leave a
        pending command retryable.
        """
        return is_permanent_rejection(self.status)


class CommanderClient:
    """A thin, auditable HTTPS client. Standard library only."""

    def __init__(self, config: CommanderConfig) -> None:
        self._config = config
        self._ssl_context = self._build_ssl_context(config)

    @staticmethod
    def _build_ssl_context(config: CommanderConfig) -> ssl.SSLContext:
        cafile = str(config.ca_bundle) if config.ca_bundle else None
        context = ssl.create_default_context(cafile=cafile)
        # Explicit rather than implied, so a future edit that weakens these is
        # visible in review.
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def __repr__(self) -> str:
        # No token is held on this object; the path is safe to show.
        return (
            f"CommanderClient(base_url={self._config.base_url!r}, "
            f"commander_id={self._config.commander_id!r})"
        )

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = self._config.url_for(path)
        body = None

        # Token reads happen on every request so rotation takes effect without
        # restarting the service. Convert configuration/permission failures to
        # CommanderError so a single bad credential read cannot escape the
        # transport boundary and abort an entire ingest batch.
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
            # Covers DNS, connection, and TLS verification failures. A TLS
            # failure is a hard error: it is never downgraded or retried
            # without verification.
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

    def health(self) -> dict[str, Any]:
        """Ask the gateway for its health.

        Central health arrives here as a gateway *observation*. The bridge never
        queries PostgreSQL or any Zero3 database to form its own opinion.
        """
        return self._request("GET", "health")

    def submit_execution(self, package: dict[str, Any]) -> dict[str, Any]:
        """Hand one execution package to Zero3.

        The package is relayed verbatim. The bridge does not inspect, enrich,
        rewrite, or domain-validate it; Zero3 Core owns that judgement and
        answers 4xx when it declines.
        """
        return self._request("POST", "execution-packages", payload=package)

    def execution_status(self, task_id: str) -> dict[str, Any]:
        """Fetch a transport-safe task snapshot."""
        if not task_id or "/" in task_id or "\\" in task_id:
            raise CommanderError(f"malformed task_id: {task_id!r}")
        return self._request("GET", f"execution-packages/tasks/{task_id}")
