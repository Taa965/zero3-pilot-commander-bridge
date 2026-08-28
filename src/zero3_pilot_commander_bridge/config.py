"""Runtime configuration, sourced entirely from the environment.

Nothing about a deployment is committed to this repository: no IP addresses, no
hostnames, no tokens, no secrets, no home directories, no cloud credentials.

The token is referenced by *path* and read on demand rather than held on the
config object, so it cannot leak through a ``repr``, a log line, a traceback,
or a crash dump of long-lived state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

__all__ = ["ConfigError", "CommanderConfig"]

DEFAULT_COMMANDER_ID = "github-bridge"
DEFAULT_TIMEOUT_SECONDS = 30.0
API_PREFIX = "/api/commander/v1"


class ConfigError(Exception):
    """Configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class CommanderConfig:
    """Connection settings for the Zero3 Commander Gateway."""

    base_url: str
    token_file: Path
    commander_id: str = DEFAULT_COMMANDER_ID
    ca_bundle: Path | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CommanderConfig:
        """Build configuration from the process environment."""
        source = os.environ if env is None else env

        base_url = (source.get("ZERO3_COMMANDER_BASE_URL") or "").strip().rstrip("/")
        if not base_url:
            raise ConfigError("ZERO3_COMMANDER_BASE_URL is not set")

        parsed = urlparse(base_url)
        # HTTPS is mandatory. The commander token is a bearer credential; over
        # plain HTTP it is readable by anything on the path.
        if parsed.scheme != "https":
            raise ConfigError(
                f"ZERO3_COMMANDER_BASE_URL must use https, got {parsed.scheme or 'no scheme'!r}"
            )
        if not parsed.netloc:
            raise ConfigError("ZERO3_COMMANDER_BASE_URL has no host")

        raw_token_file = (source.get("ZERO3_COMMANDER_TOKEN_FILE") or "").strip()
        if not raw_token_file:
            raise ConfigError("ZERO3_COMMANDER_TOKEN_FILE is not set")

        raw_ca = (source.get("ZERO3_COMMANDER_CA_BUNDLE") or "").strip()
        ca_bundle = Path(raw_ca) if raw_ca else None
        if ca_bundle is not None and not ca_bundle.is_file():
            raise ConfigError(f"ZERO3_COMMANDER_CA_BUNDLE does not exist: {ca_bundle}")

        raw_timeout = (source.get("ZERO3_COMMANDER_TIMEOUT") or "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        except ValueError as exc:
            raise ConfigError(f"ZERO3_COMMANDER_TIMEOUT is not a number: {raw_timeout!r}") from exc
        if timeout <= 0:
            raise ConfigError("ZERO3_COMMANDER_TIMEOUT must be positive")

        commander_id = (source.get("ZERO3_COMMANDER_ID") or DEFAULT_COMMANDER_ID).strip()
        if not commander_id:
            raise ConfigError("ZERO3_COMMANDER_ID must not be empty")

        return cls(
            base_url=base_url,
            token_file=Path(raw_token_file),
            commander_id=commander_id,
            ca_bundle=ca_bundle,
            timeout=timeout,
        )

    def url_for(self, path: str) -> str:
        """Build an absolute Commander Gateway URL."""
        return f"{self.base_url}{API_PREFIX}/{path.lstrip('/')}"

    def read_token(self) -> str:
        """Read the machine token from disk.

        Errors intentionally name the path and never the contents.
        """
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ConfigError(f"commander token file not found: {self.token_file}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot read commander token file {self.token_file}: {exc}") from exc

        if not token:
            raise ConfigError(f"commander token file is empty: {self.token_file}")
        return token
