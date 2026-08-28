# Architecture

## Position in the system

```text
External Agent  (GPT web session, Codex, Claude, Hermes)
      |
      v
GitHub / Commander Bridge        <-- this repository
      |
      | HTTPS Commander Protocol
      v
Zero3 Commander Gateway          <-- Zero3 Core
      |
      v
Zero3 Central                    <-- Zero3 Core, sole authority
      |
      v
Scheduler / Runtime v3 / Worker  <-- Zero3 Core
```

This repository owns exactly one job: moving intent inward and observations
outward, durably and verifiably. It owns no business meaning.

## Two projects, two responsibilities

| | Zero3 Core (Project A) | Commander Bridge (Project B) |
|---|---|---|
| Repository | `zero-three-self-media-management-system` | `zero3-pilot-commander-bridge` |
| Question answered | "What is Zero3?" | "How does the outside control Zero3?" |
| Owns | Central, Scheduler, Runtime v3, Worker Protocol, Placement Scheduler, Compute Strategy, Production Policy, AIGate, UI, database | Command envelopes, mirrors, reconciliation, GitHub integration, Commander Protocol client |
| Data store | PostgreSQL / SQLite | Git objects only |
| Authority | Total | None |

Code, git history, CI/CD, permissions, deployment lifecycle, versioning, and
failure domains are isolated. A total outage of this repository must degrade
Zero3 to "no external commands accepted", never to "Zero3 is wrong".

## Components

### `config.py`

Loads and validates settings from the environment. Enforces that the base URL is
HTTPS, reads the machine token from a file path rather than an inline value, and
never renders the token in `repr`, logs, or exceptions.

### `commander_client.py`

The only component permitted to talk to Zero3. Standard-library HTTPS via
`urllib.request` with an explicit `ssl` context. Implements the three Stage 1
operations against the real gateway:

| Method | Endpoint |
|---|---|
| `health()` | `GET /api/commander/v1/health` |
| `submit_execution()` | `POST /api/commander/v1/execution-packages` |
| `execution_status()` | `GET /api/commander/v1/execution-packages/tasks/{task_id}` |

Auth is `Authorization: Bearer <token>` plus `X-Zero3-Commander-ID`.

### `github_client.py`

Owns the on-disk repository layout and git operations. Resolves paths for
commands, state, events, results, and index. It performs no network calls to
Zero3.

### `ingestor.py`

Inbound: `commands/pending/` to Zero3.

1. Read a pending envelope through the strict reader.
2. Validate it as *transport*: schema, ids, size, protocol version.
3. Submit to the Commander Gateway.
4. Write `commands/accepted/` or `commands/rejected/` atomically.

A rejection is a normal, recorded outcome, not a crash.

### `publisher.py`

Outbound: Zero3 to GitHub. Writes the latest state mirror with a monotonic
`event_sequence` guard, appends events, and writes results only when they pass
full terminal validation. This is the **authoritative** event path.

### `reconciliation.py`

A fallback that repairs drift when the publisher has missed something. It walks
`index/active.json`, asks the gateway for current status, and republishes. It is
explicitly not the primary production path.

### `atomic_io.py`

Serialize, write to a temp file in the destination directory, `flush`, `fsync`,
re-read and validate, then `os.replace` into place, then fsync the directory
where the platform supports it. Readers use a strict reader that rejects
missing, zero-byte, whitespace-only, malformed, and non-object documents.

This is the direct answer to the zero-byte `*.status.json` and `*.result.json`
files produced by the previous bridge.

### `validation.py`

Transport-level validation only, driven by the JSON Schemas in `schemas/`. It
also carries the negative rule: no domain assertion may be added here.

## Failure model

| Failure | Behaviour |
|---|---|
| Gateway unreachable | Command stays pending. Nothing is invented. Health degrades. |
| Gateway rejects | `commands/rejected/<id>.json` written with the reason. |
| Write interrupted | Temp file is discarded. The previous good document survives. |
| Out-of-order state | Older `event_sequence` is ignored. |
| Duplicate terminal result | Identical is a no-op. Conflicting is refused and surfaced. |
| Mirror disagrees with Central | Central wins. The mirror is stale by definition. |

## What this repository will never do

- Import Zero3 Core Python modules.
- Connect to PostgreSQL or any Zero3 database.
- Assign workers, leases, or fencing tokens.
- Decide whether a production package is creatively correct.
- Serve as the system of record for anything.
