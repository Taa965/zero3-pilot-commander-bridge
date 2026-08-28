# Zero3 Pilot Commander Bridge

Transport and control middleware between external AI commanders
(GPT web sessions, Codex, Claude, Hermes) and **Zero3 Pilot development**.

This is a 1:1 port of `Taa965/zero3-commander-bridge`'s transport pattern
(GitHub command mailbox, atomic mirrors, transport-only validation) onto a
completely independent target: **Zero3 Pilot development commands**
(`repo.*`/`file.*`/`git.*`/`test.*`/`ci.*`/`deploy.*`) instead of Zero3's
video-production execution packages. The wire protocol (envelope schemas,
mailbox layout, Commander Protocol HTTP shape) is unchanged; only what's on
the other end of the HTTPS call is new.

> **`Taa965/zero3-pilot` governs "what Zero3 Pilot is".**
>
> **This repository governs "how the outside controls Zero3 Pilot development".**

This repository is fully isolated from `Taa965/zero3-commander-bridge` and
from `Taa965/zero-three-self-media-management-system` — separate repo,
separate service account, separate systemd unit, separate deploy key,
separate config/token, separate working tree. See
`docs/architecture.md` for the isolation table.

---

## Control flow

```text
External Agent (ChatGPT web session, Codex, Claude, Hermes)
      |
      v
GitHub / Zero3 Pilot Commander Bridge      <-- this repository
      |
      | HTTPS Pilot Commander Protocol
      v
Zero3 Pilot Dev Executor                   <-- new, purpose-built for dev commands
      |
      v
/opt/zero3-pilot-dev (git worktree)
      |
      v
Taa965/zero3-pilot  ->  existing strict CI (test -> build -> deploy)
```

Every arrow crossing from this repository outward is an **HTTPS call to
`/api/commander/...`** (the exact same endpoint shape
`Taa965/zero3-commander-bridge` uses against Zero3's real Gateway — see
`docs/architecture.md`). There is no direct database access, no shared
filesystem with the executor's process, and no SSH control path from this
service itself.

---

## The Pilot Dev Executor is the only execution authority

The Bridge does **not** own repository state, branch policy, test
execution, or deploy authority. Those belong to the Zero3 Pilot Dev
Executor, which enforces a fixed capability allow-list and a fixed target
(`Taa965/zero3-pilot`, working tree `/opt/zero3-pilot-dev`). The bridge
only transports commands and mirrors observations — exactly the same
transport/domain boundary `zero3-commander-bridge` enforces (see
`docs/protocol-boundary.md`).

If this repository and the Executor disagree, **the Executor is right and
the mirror is stale.** GitHub is a durable command mailbox, state/event/
result mirror, and audit transport — never the system of record.

---

## Hard isolation boundary

The bridge must never import Zero3 Core Python modules. Forbidden executable
code includes:

```python
from app.cloud import ...
from app.runtime import ...
from app.services import ...
```

`tests/test_no_core_imports.py` enforces this boundary together with gates for
committed credentials, hardcoded deployment addresses, disabled TLS
verification, and business-domain assertions in the transport layer.

The two projects keep separate repositories, permissions, deployment
lifecycles, versions, and failure domains. They may run on the same host while
remaining separate services.

---

## Repository layout is a protocol

| Path | Writer | Meaning |
|---|---|---|
| `commands/pending/<execution_id>.json` | External agent | A request; nothing decided yet. |
| `commands/accepted/<execution_id>.json` | Bridge | Commander Gateway accepted ownership. |
| `commands/rejected/<execution_id>.json` | Bridge | Authoritative refusal of this envelope. |
| `state/<execution_id>.json` | Bridge | Latest observed state, monotonic by sequence. |
| `events/<execution_id>/<event_sequence>.json` | Bridge | Immutable event mirror when a native event source is available. |
| `results/<execution_id>.json` | Bridge | Validated terminal outcome. |
| `index/active.json` | Bridge | Non-terminal execution references. |
| `index/recent.json` | Bridge | Most recent 50 execution references. |
| `bridge/health.json` | Bridge | Observed transport health. |
| `bridge/capabilities.json` | Bridge | Capabilities backed by real Commander endpoints. |

Terminal states match Zero3 Core exactly: `succeeded`, `failed`, `cancelled`,
`outcome_unknown`, `quarantined`.

### A file existing is not a verdict or result

The legacy in-Core bridge trusted file presence, including zero-byte and
partial files. This bridge trusts protocol documents only when they parse and
validate against the expected schema and correlation identifiers.

Writes use temp file -> flush -> `fsync` -> read-back -> validate -> atomic
rename. Parseable-but-schema-invalid state, event, result, and verdict files are
not treated as authority and may be repaired by a later verified observation.

---

## Transport validation only

The bridge may validate:

- JSON/schema/envelope shape
- identifier and correlation consistency
- payload byte size
- content hash
- supported protocol version
- terminal-state legality
- sequence monotonicity

It must never validate storyboard/scene/beat counts, script contents, Author
Skill choice, Production Package meaning, Worker placement, or other business
rules. Those belong to Zero3 Core.

---

## Configuration

All deployment-specific values come from the environment:

| Variable | Meaning |
|---|---|
| `ZERO3_COMMANDER_BASE_URL` | Commander Gateway base URL; HTTPS required. |
| `ZERO3_COMMANDER_TOKEN_FILE` | File containing the machine credential. |
| `ZERO3_COMMANDER_ID` | Authenticated commander identity header. |
| `ZERO3_COMMANDER_CA_BUNDLE` | Optional private-CA bundle. |
| `ZERO3_COMMANDER_TIMEOUT` | Optional request timeout in seconds. |

TLS hostname and certificate verification are always enabled. There is no
`verify=False`, `CERT_NONE`, or `curl -k` equivalent.

The token is read from its file for every request, so rotation does not require
a Bridge restart and the credential is never stored on the long-lived config
object.

---

## Runtime

The production entry point is:

```bash
python -m zero3_pilot_commander_bridge --root <repo> run --interval 15 --commit --push
```

Subcommands:

| Command | Purpose |
|---|---|
| `health` | real Commander health request |
| `ingest` | one pass: GitHub pending commands -> Zero3 |
| `publish` | one pass: accepted Zero3 work -> state/result mirror |
| `run` | long-running ingress + publication loop |
| `reconcile` | slow drift-repair fallback |
| `audit` | report unusable state mirrors without mutation |

### GitHub command ingress is actively synchronized

A local clone is not a command bus by itself. Before making production command
decisions, the runtime fetches/rebases the latest remote `main`, so commands
created by GPT or another external agent on GitHub actually reach the running
Bridge.

Mirror pushes use bounded push/fetch/rebase/retry. They never force-push or
reset away concurrent external command commits. A rebase conflict is surfaced
and production command ingest pauses rather than acting on a stale mailbox.

### Quiet work does not create a Git commit every 15 seconds

Unchanged state observations are no-ops; index timestamps move only when index
contents change; health writes use a bounded heartbeat. This keeps GitHub as an
audit mirror rather than a synthetic heartbeat log.

---

## Current event-delivery limitation

Commander Gateway v1.5 currently exposes health, execution submit, and task
status, but **does not expose an outbound event stream/webhook/SSE endpoint**.
Therefore the first production runtime observes accepted executions through the
status endpoint at a short bounded interval and mirrors state/results.

This is transitional transport behavior, not a claim that polling is the ideal
architecture. `.github/workflows/bridge-reconcile.yml` remains a much slower
fallback for drift repair, not the primary production path.

When Zero3 Core exposes a generic Commander event stream, the subscriber can
feed the existing immutable `events/`, `state/`, and `results/` contracts
without moving scheduler or business authority into this repository.

---

## Command-outcome classification

Only stable command-level refusals become `commands/rejected/` today:

- HTTP 400
- HTTP 409
- HTTP 413
- HTTP 422

Authentication/identity failures, route/protocol mismatch, throttling,
timeouts, and infrastructure failures remain pending/retryable. In particular,
401/403/404/405/410/429 and 5xx must never destroy a valid pending command.

A 2xx response that cannot be strongly correlated is recorded as **accepted but
uncorrelated**. Zero3 may already own the execution, so blindly resubmitting
would risk duplicate production work.

---

## Current capabilities

| Command | Status | Gateway endpoint |
|---|---|---|
| `execution.submit` | enabled | `POST /api/commander/v1/execution-packages` |
| `execution.status` | enabled | `GET /api/commander/v1/execution-packages/tasks/{task_id}` |
| `task.cancel` | disabled | no endpoint yet |
| `task.retry` | disabled | no endpoint yet |
| `task.pause` | disabled | no endpoint yet |
| `task.resume` | disabled | no endpoint yet |

---

## Development and tests

The package supports Python 3.10+ and does not require a Python interpreter
owned by Zero3 Core.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m compileall src tests
pytest -q
```

A production package install also includes the JSON Schemas under the venv's
`share/zero3-pilot-commander-bridge/schemas` path; schema validation does not depend
on an editable source checkout.

GitHub Actions is useful when account quota is available, but successful local
or server-side test evidence is required before production deployment; an
Actions job that fails before executing steps is not test evidence.

---

## Production deployment

Repository code and production runtime are separate concerns:

```text
/opt/zero3-pilot-commander-bridge      Git checkout + command/mirror data
/opt/zero3-pilot-bridge-runtime/venv   independent Bridge Python environment
/etc/zero3-pilot-bridge/bridge.env     non-secret runtime configuration
/etc/zero3-pilot-bridge/...            machine credential referenced by file path
zero3-pilot-bridge.service             long-running Bridge process
```

`deploy/install.sh`:

- requires a clean `main` checkout
- uses the host's independent Python 3.10+ interpreter
- installs a non-editable production package into the Bridge venv
- verifies packaged schemas
- never prints or copies the token value
- installs/enables the hardened systemd unit
- performs a real `CommanderClient.health()` before starting
- starts/restarts the service only when called with `--start` (or when upgrading
  an already-running Bridge)

The systemd service can write only the Bridge Git checkout. It can read its
configuration and repo-scoped Deploy Key, but does not receive Core source,
database, Scheduler, Worker, or sudo authority.

---

## Correctness invariants

- verdict existence is not verdict validity
- one malformed command cannot block the rest of the mailbox
- transient/auth/protocol failures never consume pending commands
- envelope/payload/acceptance correlation cannot silently cross executions
- accepted-but-uncorrelated work is never blindly resubmitted
- state sequence and non-terminal progress never regress
- parseable-but-invalid mirrors are repairable
- valid conflicting terminal results are never overwritten
- reconciliation refuses stale work items
- GitHub command commits and Bridge mirror commits survive push races
- quiet observations do not generate continuous Git churn

---

## Documentation

- `docs/architecture.md` — components and data flow
- `docs/protocol-boundary.md` — transport/domain boundary
- `docs/migration-from-core.md` — legacy in-Core Bridge migration

## Status

`bridge-v2-production-runtime` is the production-runtime development branch and
PR #1 is intentionally kept in review until local tests, clean-package tests,
and the real AWS service/canary evidence are complete. The legacy in-Core
Bridge is not removed by this repository and remains a separate later migration
step.
