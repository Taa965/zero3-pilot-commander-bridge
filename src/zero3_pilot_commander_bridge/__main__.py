"""Command line entry point: ``python -m zero3_pilot_commander_bridge``.

Subcommands map one-to-one onto the runtimes, so the same code path runs
whether a human is debugging or systemd is supervising:

    health      check the Commander Gateway and print the wire contract
    ingest      one pass of commands/pending/ -> Zero3
    publish     one pass of Zero3 -> the GitHub mirror
    run         both loops, forever, with health reporting
    reconcile   the drift-repair fallback
    audit       report unusable documents without changing anything

Configuration comes from the environment only. Nothing is accepted on the
command line that would put a credential in shell history or a process list.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .commander_client import CommanderClient, CommanderError
from .config import CommanderConfig, ConfigError
from .github_client import BridgeRepository
from .publisher import Publisher
from .reconciliation import Reconciler
from .runtime import DEFAULT_INTERVAL_SECONDS, BridgeRuntime, StopSignal


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _client() -> CommanderClient:
    return CommanderClient(CommanderConfig.from_env())


def cmd_health(args: argparse.Namespace) -> int:
    client = _client()
    payload = client.health()
    _emit(payload)
    return 0 if payload.get("ok") else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    repo = BridgeRepository(args.root)
    runtime = BridgeRuntime(repo, _client(), commit=args.commit, push=args.push)
    report = runtime.pass_once() if args.publish else _ingest_only(runtime)
    _emit(report.as_dict())
    return 0


def _ingest_only(runtime: BridgeRuntime):
    from .runtime import RuntimeReport

    report = RuntimeReport()
    runtime.ingest.pass_once(report)
    return report


def cmd_publish(args: argparse.Namespace) -> int:
    repo = BridgeRepository(args.root)
    runtime = BridgeRuntime(repo, _client(), commit=args.commit, push=args.push)

    from .runtime import RuntimeReport

    report = RuntimeReport()
    runtime.publish.pass_once(report)
    _emit(report.as_dict())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = BridgeRepository(args.root)
    runtime = BridgeRuntime(repo, _client(), commit=args.commit, push=args.push)
    runtime.run_forever(interval=args.interval, stop=StopSignal().install())
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    repo = BridgeRepository(args.root)
    publisher = Publisher(repo)
    report = Reconciler(repo, _client(), publisher).reconcile()
    _emit(report.as_dict())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Report unusable documents. Read-only: repairs nothing."""
    repo = BridgeRepository(args.root)
    publisher = Publisher(repo)
    broken = publisher.invalid_state_mirrors()
    _emit(
        {
            "root": str(repo.root),
            "pending_commands": len(repo.list_pending()),
            "invalid_state_mirrors": broken,
            "healthy": not broken,
        }
    )
    return 1 if broken else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zero3_pilot_commander_bridge")
    parser.add_argument("--root", default=".", help="bridge repository root (default: .)")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_write_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--commit", action="store_true", default=False, help="commit mirror changes")
        p.add_argument("--push", action="store_true", default=False, help="push after committing")

    p_health = sub.add_parser("health", help="check the Commander Gateway")
    p_health.set_defaults(func=cmd_health)

    p_ingest = sub.add_parser("ingest", help="one pass: pending commands -> Zero3")
    p_ingest.add_argument(
        "--publish", action="store_true", default=False, help="also run the publish pass"
    )
    add_write_flags(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    p_publish = sub.add_parser("publish", help="one pass: Zero3 -> the GitHub mirror")
    add_write_flags(p_publish)
    p_publish.set_defaults(func=cmd_publish)

    p_run = sub.add_parser("run", help="run both loops until stopped")
    p_run.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    add_write_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_rec = sub.add_parser("reconcile", help="drift-repair fallback")
    p_rec.set_defaults(func=cmd_reconcile)

    p_audit = sub.add_parser("audit", help="report unusable documents (read-only)")
    p_audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG
    except CommanderError as exc:
        print(f"commander error: {exc}", file=sys.stderr)
        return 69  # EX_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
