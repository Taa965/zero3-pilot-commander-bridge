"""The GitHub side of the transport: repository layout and git operations.

GitHub is a durable command mailbox, a state/event/result mirror, and an audit
transport. It is not the Zero3 database. Everything here is replaceable: swapping
GitHub for another durable transport should not require touching Zero3 Core.

This module performs no network calls to Zero3.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from .validation import is_valid_identifier

__all__ = ["LayoutError", "GitError", "BridgeRepository"]


class LayoutError(Exception):
    """An identifier or path would escape the intended repository layout."""


class GitError(Exception):
    """A git command failed."""


class BridgeRepository:
    """Resolves the on-disk protocol layout and runs git against it."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    # -- layout ---------------------------------------------------------

    @staticmethod
    def _safe(execution_id: str) -> str:
        if not is_valid_identifier(execution_id):
            raise LayoutError(f"unsafe execution_id: {execution_id!r}")
        return execution_id

    @property
    def pending_dir(self) -> Path:
        return self.root / "commands" / "pending"

    @property
    def accepted_dir(self) -> Path:
        return self.root / "commands" / "accepted"

    @property
    def rejected_dir(self) -> Path:
        return self.root / "commands" / "rejected"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def index_dir(self) -> Path:
        return self.root / "index"

    @property
    def bridge_dir(self) -> Path:
        return self.root / "bridge"

    def pending_command(self, execution_id: str) -> Path:
        return self.pending_dir / f"{self._safe(execution_id)}.json"

    def accepted_command(self, execution_id: str) -> Path:
        return self.accepted_dir / f"{self._safe(execution_id)}.json"

    def rejected_command(self, execution_id: str) -> Path:
        return self.rejected_dir / f"{self._safe(execution_id)}.json"

    def state_path(self, execution_id: str) -> Path:
        return self.state_dir / f"{self._safe(execution_id)}.json"

    def event_path(self, execution_id: str, event_sequence: int) -> Path:
        if not isinstance(event_sequence, int) or isinstance(event_sequence, bool):
            raise LayoutError(f"event_sequence must be an integer, got {event_sequence!r}")
        if event_sequence < 0:
            raise LayoutError(f"event_sequence must not be negative: {event_sequence}")
        return self.events_dir / self._safe(execution_id) / f"{event_sequence}.json"

    def result_path(self, execution_id: str) -> Path:
        return self.results_dir / f"{self._safe(execution_id)}.json"

    @property
    def active_index(self) -> Path:
        return self.index_dir / "active.json"

    @property
    def recent_index(self) -> Path:
        return self.index_dir / "recent.json"

    @property
    def health_path(self) -> Path:
        return self.bridge_dir / "health.json"

    @property
    def capabilities_path(self) -> Path:
        return self.bridge_dir / "capabilities.json"

    def list_pending(self) -> list[Path]:
        if not self.pending_dir.is_dir():
            return []
        return sorted(p for p in self.pending_dir.glob("*.json") if p.is_file())

    def event_sequences(self, execution_id: str) -> list[int]:
        directory = self.events_dir / self._safe(execution_id)
        if not directory.is_dir():
            return []
        sequences = []
        for path in directory.glob("*.json"):
            try:
                sequences.append(int(path.stem))
            except ValueError:
                continue
        return sorted(sequences)

    # -- git ------------------------------------------------------------

    def git(self, *args: str, timeout: float = 60.0) -> str:
        """Run a bounded git command inside the repository."""
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
        except OSError as exc:
            raise GitError(f"cannot run git: {exc}") from exc
        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout

    def has_changes(self, paths: Sequence[str | Path] | None = None) -> bool:
        """Report whether the working tree differs from HEAD."""
        args = ["status", "--porcelain"]
        if paths:
            args.append("--")
            args.extend(str(p) for p in paths)
        return bool(self.git(*args).strip())

    def commit_paths(self, paths: Iterable[str | Path], message: str) -> bool:
        """Stage the given paths and commit; return False when nothing changed."""
        targets = [str(Path(p)) for p in paths]
        if not targets:
            return False

        self.git("add", "--", *targets)
        if not self.git("diff", "--cached", "--name-only").strip():
            return False

        self.git("commit", "-m", message)
        return True

    def sync_from_remote(self, *, remote: str = "origin", branch: str = "main") -> None:
        """Rebase local bridge commits onto the latest remote mailbox state.

        External agents create command commits on GitHub while this process
        creates mirror commits locally. A plain ``git pull`` or blind push can
        either miss commands or lose a race. Rebase preserves both histories.

        The working tree must be clean. Runtime crash recovery commits durable
        local mirror files before calling this method.
        """
        if self.has_changes():
            raise GitError("refusing to sync a dirty bridge working tree")

        self.git("fetch", "--prune", remote, branch)
        try:
            self.git("rebase", f"{remote}/{branch}")
        except GitError as exc:
            try:
                self.git("rebase", "--abort")
            except GitError:
                pass
            raise GitError(f"cannot rebase bridge state onto {remote}/{branch}: {exc}") from exc

    def push_with_retry(
        self,
        *,
        remote: str = "origin",
        branch: str = "main",
        attempts: int = 3,
    ) -> None:
        """Push without overwriting concurrent GitHub command commits.

        On a non-fast-forward race, fetch/rebase and retry. Conflicts are
        surfaced and never resolved with force-push or reset.
        """
        if attempts < 1:
            raise ValueError("attempts must be >= 1")

        last_error: GitError | None = None
        for _ in range(attempts):
            try:
                self.git("push", remote, f"HEAD:{branch}")
                return
            except GitError as exc:
                last_error = exc

            if self.has_changes():
                raise GitError("push failed and working tree is dirty; refusing automatic rebase")

            self.git("fetch", "--prune", remote, branch)
            try:
                self.git("rebase", f"{remote}/{branch}")
            except GitError as exc:
                try:
                    self.git("rebase", "--abort")
                except GitError:
                    pass
                raise GitError(f"push race produced a rebase conflict: {exc}") from exc

        assert last_error is not None
        raise GitError(f"push failed after {attempts} attempts: {last_error}")
