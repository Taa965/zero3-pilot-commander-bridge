# Architecture

## Position in the system

```text
External Agent
      |
      v
GitHub mailbox / Commander Bridge        <-- this repository
      |
      | HTTPS + control bearer token
      v
Zero3 Pilot H5 apps/web                  <-- admission + durable control plane
      |
      | /api/host/v1/* lease/fencing protocol
      v
Windows Zero3 Remote Host
      |
      v
Zero3CodexAppServer / pinned Codex       <-- sole development execution authority
```

The bridge owns transport only: durable command ingress, correlation, Git
synchronization, and monotonic/immutable mirrors. It never executes repository,
shell, filesystem, Scheduler, Agent, or Codex actions.

## H5 default adapter

The default client adapter is `h5`.

| Bridge method | H5 endpoint |
|---|---|
| `health()` | `GET /health` |
| `submit_execution()` | `POST /api/control/v1/tasks` |
| `execution_status()` | `GET /api/control/v1/tasks/{task_id}` |

`execution.submit` keeps its existing mailbox envelope. Its `payload` remains
opaque to the bridge and is sent verbatim to H5. H5 validates the
`zero3.pilot.remote-task.v1` contract and owns task identity, idempotency,
persistence, leases, fencing, accepted events, and terminal state.

H5 returns a durable `TaskRecord`. `commander_client.py` performs a
transport-only projection of that record into the bridge's stable
`zero3.execution-status/1.0` observation shape so the existing publisher and
reconciler do not gain control-plane authority.

## Legacy rollback adapter

`ZERO3_COMMANDER_ADAPTER=legacy-commander` explicitly selects the former Pilot
Dev Executor endpoints under `/api/commander/v1`. This adapter is legacy and is
retained only as a bounded rollback path during cutover. `h5` is the default.

The legacy adapter must not be extended with new capabilities. Removal is
expected after H5 deployment/pairing/end-to-end verification is complete.

## Authentication and TLS

The client still reads its bearer token from `ZERO3_COMMANDER_TOKEN_FILE` on
every request so rotation does not require a bridge restart. With H5, that
credential must correspond to the server-side `ZERO3_CONTROL_TOKEN_FILE`.
Deployment addresses and secrets are never committed.

TLS hostname and certificate verification are mandatory. A private CA may be
configured with `ZERO3_COMMANDER_CA_BUNDLE`; disabling verification is not
supported.

## Mailbox and mirror invariants

| Path | Meaning |
|---|---|
| `commands/pending/<execution_id>.json` | external request; no verdict yet |
| `commands/accepted/<execution_id>.json` | H5 accepted ownership |
| `commands/rejected/<execution_id>.json` | stable admission refusal |
| `state/<execution_id>.json` | latest observed monotonic state |
| `events/<execution_id>/<event_sequence>.json` | immutable accepted event mirror |
| `results/<execution_id>.json` | immutable validated terminal result |

H5's `blocked` state is terminal and is mirrored as `blocked`; it is never
rewritten to `failed`.

## Failure model

- Network/auth/protocol/infrastructure failures leave a pending command
  retryable unless the HTTP status is an established command-level refusal.
- A 2xx response is recorded once even if strong correlation is unavailable;
  blind resubmission could duplicate an already-admitted task.
- State observations never move backward by event sequence.
- Existing valid events/results are immutable; conflicting rewrites fail.
- Git push uses fetch/rebase/retry and never force-pushes over concurrent
  mailbox commits.
- H5/Remote Host are authoritative when a GitHub mirror is stale.

## Non-goals

This repository does not:

- acquire or renew H5 leases;
- create fencing tokens;
- register Remote Host nodes;
- call `/api/host/v1/*`;
- invoke Codex, shell, Git execution, filesystem mutation, or desktop IPC;
- plan work or select agents/workers;
- become a second Scheduler or durable execution history.
