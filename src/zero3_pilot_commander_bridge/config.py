"""Runtime configuration for the transport-only Commander Bridge.

The H5 Zero3 Pilot control plane is the default transport adapter. The former
Pilot Dev Executor Commander API remains available only as an explicit legacy
rollback path while the H5 cutover is completed.

Deployment-specific values still come only from the environment. Credentials
are referenced by file path and read on demand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "ConfigError",
    "CommanderConfig",
    "H5_ADAPTER",
    "LEGACY_COMMANDER_ADAPTER",
]

DEFAULT_COMMANDER_ID = "github-bridge"
DEFAULT_TIMEOUT_SECONDS = 30.0
H5_ADAPTER = "h5"
LEGACY_COMMANDER_ADAPTER = "legacy-commander"
DEFAULT_ADAPTER = H5_ADAPTER

H5_CONTROL_API_PREFIX = "/api/control/v1"
LEGACY_COMMANDER_API_PREFIX = "/api/commander/v1"
# Compatibility alias for callers/tests that still import the old constant.
API_PREFIX = LEGACY_COMMANDER_API_PREFIX


class ConfigError(Exception):
    """Configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class CommanderConfig:
    """Connection settings for the Zero3 Pilot control transport."""

    base_url: str
    token_file: Path
    commander_id: str = DEFAULT_COMMANDER_ID
    ca_bundle: Path | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    adapter: str = DEFAULT_ADAPTER

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CommanderConfig:
        """Build configuration from the process environment."""
        source = os.environ if env is None else env

        base_url = (source.get("ZERO3_COMMANDER_BASE_URL") or "").strip().rstrip("/")
        if not base_url:
            raise ConfigError("ZERO3_COMMANDER_BASE_URL is not set")

        parsed = urlparse(base_url)
        if parsed.scheme != "https":
            raise ConfigError(
                f"ZERO3_COMMANDER_BASE_URL must use https, got {parsed.scheme or 'no scheme'!r}"
            )
        if not parsed.netloc:
            raise ConfigError("ZERO3_COMMANDER_BASE_URL has no host")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError("ZERO3_COMMANDER_BASE_URL must not contain inline credentials")
        if parsed.query or parsed.fragment:
            raise ConfigError("ZERO3_COMMANDER_BASE_URL must not contain query or fragment components")

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

        adapter = (source.get("ZERO3_COMMANDER_ADAPTER") or DEFAULT_ADAPTER).strip().lower()
        if adapter not in {H5_ADAPTER, LEGACY_COMMANDER_ADAPTER}:
            raise ConfigError(
                "ZERO3_COMMANDER_ADAPTER must be 'h5' or 'legacy-commander'"
            )

        return cls(
            base_url=base_url,
            token_file=Path(raw_token_file),
            commander_id=commander_id,
            ca_bundle=ca_bundle,
            timeout=timeout,
            adapter=adapter,
        )

    def url_for(self, path: str) -> str:
        """Build a legacy Pilot Dev Executor Commander API URL."""
        return f"{self.base_url}{LEGACY_COMMANDER_API_PREFIX}/{path.lstrip('/')}"

    def control_url_for(self, path: str) -> str:
        """Build an H5 control-plane URL."""
        return f"{self.base_url}{H5_CONTROL_API_PREFIX}/{path.lstrip('/')}"

    def root_url_for(self, path: str) -> str:
        """Build a URL rooted directly below the configured service origin."""
        return f"{self.base_url}/{path.lstrip('/')}"

    def read_token(self) -> str:
        """Read the machine token from disk without retaining it on this object."""
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ConfigError(f"commander token file not found: {self.token_file}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot read commander token file {self.token_file}: {exc}") from exc

        if not token:
            raise ConfigError(f"commander token file is empty: {self.token_file}")
        return token
