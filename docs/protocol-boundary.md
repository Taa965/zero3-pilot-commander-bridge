# Protocol boundary

The single rule:

> The Bridge validates **transport**. Zero3 Pilot H5 and the Remote Host/Codex runtime validate and execute **meaning**.

A transport layer that starts understanding development or production meaning has stopped being a transport layer. It becomes a second, silently drifting copy of control/runtime rules and will eventually disagree with Zero3 Pilot.

## The Bridge MAY validate

| Check | Why it is transport |
|---|---|
| JSON parses | Otherwise there is no message at all. |
| Mailbox envelope matches the JSON Schema in `schemas/` | Envelope shape is this repository's wire contract. |
| `execution_id` and `task_id` are well formed and consistent | Routing and correlation. |
| Mailbox payload size is within the Bridge limit | Resource protection. |
| Envelope schema/protocol version is supported | Compatibility negotiation. |
| Content hash matches | Integrity. |
| Mirrored terminal state is legal | Lifecycle projection. |
| `event_sequence` is monotonic | Ordering correctness. |

The mailbox envelope may be larger than the H5 admission body limit. H5 is authoritative for `zero3.pilot.remote-task.v1` admission and may return HTTP 413; the Bridge records that stable refusal rather than reinterpreting the task.

## The Bridge MUST NOT validate or decide

These are forbidden in this repository:

- task objective or whether the requested change is sensible;
- repository policy, branch strategy, or acceptance criteria semantics;
- Agent, worker, Scheduler, or Codex selection;
- shell, Git, filesystem, test, build, or deployment actions;
- Remote Host lease acquisition/renewal;
- fencing-token generation or node registration;
- production/business-domain rules such as storyboard, scene, beat, skill, pacing, duration, or creative-review rules.

Any of these appearing as Bridge decision logic is an architectural regression.

## Where validation and execution belong

With the default adapter, `execution.submit` keeps the existing GitHub mailbox envelope but its `payload` is opaque to the Bridge. The Bridge sends that object verbatim to:

```text
POST /api/control/v1/tasks
```

Zero3 Pilot H5 validates `zero3.pilot.remote-task.v1`, owns durable admission/idempotency, and stores control-plane state. The Windows Remote Host then uses the separate `/api/host/v1/*` lease/fencing protocol and delegates development execution to the pinned Codex runtime.

The Bridge never calls `/api/host/v1/*` and never manufactures a lease, fencing token, execution event, or terminal outcome on behalf of a host.

When H5 refuses admission with a stable command-level HTTP status, the Bridge records the refusal under:

```text
commands/rejected/<execution_id>.json
```

The Bridge does not translate a task into a different RemoteTask to make it pass validation.

## Authority table

| Concern | Owner |
|---|---|
| RemoteTask admission/idempotency | Zero3 Pilot H5 |
| Durable task/control-plane state | Zero3 Pilot H5 |
| Lease and fencing state | Zero3 Pilot H5 + Remote Host protocol |
| Host execution | Windows Zero3 Remote Host |
| Codex development execution | Zero3CodexAppServer / pinned Codex |
| Mailbox envelope shape | Commander Bridge |
| GitHub delivery durability | Commander Bridge |
| State/event/result mirror freshness | Commander Bridge |
| Audit transport | Commander Bridge |

## Result validity

A result file is valid only when all of the following hold. Existence is not one of them.

1. The file parses as JSON.
2. It validates against `schemas/result.schema.json`.
3. `execution_id` matches the expected execution.
4. `task_id` matches the task recorded at acceptance.
5. `terminal` is `true`.
6. `state` is one of `succeeded`, `failed`, `cancelled`, `blocked`, `outcome_unknown`, or `quarantined`.

`blocked` and `outcome_unknown` are honest terminal states. The Bridge must preserve them instead of rewriting them to `failed` or `succeeded`.

## Transport communication rule

Default path:

```text
External Agent
-> GitHub mailbox
-> Commander Bridge
-> HTTPS Zero3 Pilot H5 /api/control/v1/tasks
-> Windows Remote Host
-> Zero3CodexAppServer / Codex
```

The former Pilot Dev Executor `/api/commander/v1` adapter is available only when explicitly selected with:

```text
ZERO3_COMMANDER_ADAPTER=legacy-commander
```

It is a bounded rollback adapter and must not receive new capabilities.

No direct database access, no SSH control path, no shared filesystem with the Remote Host, and no Python import of Zero3 Pilot runtime modules are allowed. TLS verification stays enabled; a private CA is configured with `ZERO3_COMMANDER_CA_BUNDLE`, never by disabling verification.
