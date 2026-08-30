# Zero3 Pilot Commander Bridge

Transport and control middleware between external AI commanders
(GPT web sessions, Codex, Claude, Hermes) and **Zero3 Pilot development**.

This repository keeps the established transport pattern — GitHub command
mailbox, atomic mirrors, transport-only validation, bounded Git sync — while
routing new work through the durable Zero3 Pilot H5 control plane.

> **`Taa965/zero3-pilot` governs "what Zero3 Pilot is".**
>
> **This repository governs "how outside intent reaches Zero3 Pilot".**

This repository is isolated from the Zero3 Pilot core repository: separate
repo, service account, systemd unit, deploy key, config/token, and working tree.
See `docs/architecture.md` for the authority boundary.

---

## Control flow

```text
External Agent (ChatGPT web session, Codex, Claude, Hermes)
      |
      v
GitHub / Zero3 Pilot Commander Bridge      <-- this repository
      |
      | HTTPS control transport
      v
Zero3 Pilot H5 apps/web                    <-- admission + durable task state
      |
      | /api/host/v1/* lease/fencing protocol
      v
Windows Zero3 Remote Host
      |
      v
Zero3CodexAppServer / pinned Codex         <-- execution authority
```

The Bridge does not call `/api/host/v1/*`. It reaches H5 only through
`/health` and `/api/control/v1/tasks...`. There is no direct database access,
no shared filesystem with the Remote Host, and no SSH control path from this
service.

---

## H5/Remote Host is the execution boundary

The Bridge does **not** own repository state, branch policy, test execution,
deploy authority, leases, fencing, or Codex turns. H5 owns durable remote-task
admission/control-plane state; the Windows Remote Host/Codex runtime owns
development execution.

The Bridge only transports commands and mirrors observations. If a GitHub
mirror disagrees with H5/Remote Host state, the mirror is stale. GitHub is a
durable command mailbox, state/event/result mirror, and audit transport —
never the system of record.

The former Pilot Dev Executor adapter remains available only as
`ZERO3_COMMANDER_ADAPTER=legacy-commander` for bounded rollback during cutover.
It is legacy and must not receive new capabilities.

---

## Hard isolation boundary

The bridge must never import Zero3 product/runtime Python modules or gain
execution privileges. `tests/test_no_core_imports.py` enforces this boundary
together with gates for committed credentials, hardcoded deployment addresses,
disabled TLS verification, and business-domain assertions in the transport
layer.

The Bridge and Zero3 Pilot keep separate repositories, permissions, deployment
lifecycles, versions, and failure domains. A total outage of this repository
must degrade the system to "no new external mailbox commands accepted", never
to "Zero3 Pilot executes incorrectly".

---

## Repository layout is a protocol

| Path | Writer | Meaning |
|---|---|---|
| `commands/pending/<execution_id>.json` | External agent | A request; nothing decided yet. |
| `commands/accepted/<execution_id>.json` | Bridge | H5/control endpoint accepted ownership. |
| `commands/rejected/<execution_id>.json` | Bridge | Authoritative refusal of this envelope. |
| `state/<execution_id>.json` | Bridge | Latest observed state, monotonic by sequence. |
| `events/<execution_id>/<event_sequence>.json` | Bridge | Immutable event mirror. |
| `results/<execution_id>.json` | Bridge | Validated terminal outcome. |
| `index/active.json` | Bridge | Non-terminal execution references. |
| `index/recent.json` | Bridge | Most recent 50 execution references. |
| `bridge/health.json` | Bridge | Observed transport health. |
| `bridge/capabilities.json` | Bridge | Capabilities backed by real endpoints. |

Terminal states are `succeeded`, `failed`, `cancelled`, `blocked`,
`outcome_unknown`, and `quarantined`. `blocked` is a genuine H5 terminal state
and is never rewritten to `failed`.

### A file existing is not a verdict or result

The bridge trusts protocol documents only when they parse and validate against
the expected schema and correlation identifiers.

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

The `execution.submit` payload remains opaque to the Bridge. With the default
H5 adapter it is relayed verbatim to `POST /api/control/v1/tasks`, where H5
validates the `zero3.pilot.remote-task.v1` contract. The Bridge must not infer
task objective, repository policy, worker selection, execution strategy, or
other domain/runtime meaning.

---

## Configuration

All deployment-specific values come from the environment:

| Variable | Meaning |
|---|---|
| `ZERO3_COMMANDER_BASE_URL` | Zero3 Pilot `apps/web` origin; HTTPS required. |
| `ZERO3_COMMANDER_ADAPTER` | `h5` (default) or explicit rollback `legacy-commander`. |
| `ZERO3_COMMANDER_TOKEN_FILE` | File containing the client machine credential. |
| `ZERO3_COMMANDER_ID` | Transport audit identity; required by the legacy adapter. |
| `ZERO3_COMMANDER_CA_BUNDLE` | Optional private-CA bundle. |
| `ZERO3_COMMANDER_TIMEOUT` | Optional request timeout in seconds. |

For H5, the client token must match the server-side control-plane secret
configured by `ZERO3_CONTROL_TOKEN_FILE`.

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
| `health` | real selected-transport health request |
| `ingest` | one pass: GitHub pending commands -> H5 |
| `publish` | one pass: accepted H5 work -> state/event/result mirror |
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

## H5 observation model

H5 exposes:

```text
POST /api/control/v1/tasks
GET  /api/control/v1/tasks
GET  /api/control/v1/tasks/{task_id}
GET  /api/control/v1/nodes
```

The Bridge uses only task admission and per-task observation. H5 returns a
durable `TaskRecord` containing state, accepted events, lease/fencing metadata,
and terminal outcome. `commander_client.py` projects that wire record into the
Bridge's stable `zero3.execution-status/1.0` observation shape.

That projection is deliberately one-way and transport-only: the Bridge does
not acquire leases, renew leases, generate fencing tokens, register nodes, or
write host events/outcomes.

The runtime polls the per-task control endpoint at a short bounded interval and
mirrors state/events/results. `.github/workflows/bridge-reconcile.yml` remains
a much slower fallback for drift repair, not the primary production path.

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
uncorrelated**. H5 may already own the task, so blindly resubmitting would risk
duplicate execution.

---

## Current capabilities

Default H5 adapter:

| Command | Status | Endpoint |
|---|---|---|
| `execution.submit` | enabled | `POST /api/control/v1/tasks` |
| `execution.status` | enabled | `GET /api/control/v1/tasks/{task_id}` |
| `task.cancel` | disabled | no control endpoint yet |
| `task.retry` | disabled | no control endpoint yet |
| `task.pause` | disabled | no control endpoint yet |
| `task.resume` | disabled | no control endpoint yet |

Legacy rollback adapter:

```text
GET  /api/commander/v1/health
POST /api/commander/v1/execution-packages
GET  /api/commander/v1/execution-packages/tasks/{task_id}
```

The legacy adapter is not the default and is scheduled for removal only after
H5 deployment, pairing, and end-to-end verification.

---

## Development and tests

The package supports Python 3.10+ and does not require a Python interpreter
owned by Zero3 Pilot.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m compileall src tests
pytest -q
```

A production package install also includes the JSON Schemas under the venv's
`share/zero3-pilot-commander-bridge/schemas` path; schema validation does not
depend on an editable source checkout.

GitHub Actions is useful when account/runner capacity is available, but
successful test evidence is required before production deployment; a job that
has not acquired a runner is not test evidence.

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
configuration and repo-scoped Deploy Key, but does not receive Zero3 Pilot
source execution, database, Remote Host, Codex, or sudo authority.

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
- H5 `blocked` remains terminal `blocked`
- reconciliation refuses stale work items
- GitHub command commits and Bridge mirror commits survive push races
- quiet observations do not generate continuous Git churn
- Bridge never calls `/api/host/v1/*`

---

## Documentation

- `docs/architecture.md` — H5/Remote Host authority and transport flow
- `docs/protocol-boundary.md` — transport/domain boundary
- `docs/migration-from-core.md` — legacy in-Core Bridge migration

## Status

H5 adapter implementation is reviewable independently. Production cutover and
removal of the legacy Pilot Dev Executor adapter remain gated on H5 deployment,
pairing, Remote Host end-to-end verification, and the repository CI/deployment
gates.
