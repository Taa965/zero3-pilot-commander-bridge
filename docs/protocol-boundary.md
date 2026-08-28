# Protocol boundary

The single rule:

> The bridge validates **transport**. Zero3 Core validates **meaning**.

A transport layer that starts understanding video production has stopped being a
transport layer. It becomes a second, unversioned, silently drifting copy of the
domain rules, and it will eventually reject work that Zero3 would have accepted.

## The bridge MAY validate

| Check | Why it is transport |
|---|---|
| JSON parses | Otherwise there is no message at all. |
| Matches the JSON Schema in `schemas/` | Envelope shape is the wire contract. |
| `execution_id` and `task_id` are well formed and consistent | Routing and correlation. |
| Payload size within limit | Resource protection. |
| `schema` / protocol version is supported | Compatibility negotiation. |
| Content hash or signature matches | Integrity and authenticity. |
| Terminal state is one of the five legal values | Lifecycle correctness. |
| `event_sequence` is monotonic | Ordering correctness. |

## The bridge MUST NOT validate

These are all **forbidden** in this repository:

- storyboard count, for example `storyboard == 17`
- scene count, for example `scenes == 17`
- visual beat count, for example `beats == 107`
- script or narration content
- Author Skill selection or binding
- Production Package contents
- Worker selection or capability matching
- shot lists, pacing, durations, aspect ratios
- whether a creative review was "good enough"

Any of these appearing here is an architectural regression, regardless of how
convenient it seems at the time.

### Why the counts are the canonical example

The previous GitHub workflows hardcoded assertions like `storyboard == 17` and
`beats == 107`. Those numbers are properties of one production template at one
moment. Encoding them in the transport meant every domain change required a
synchronized edit in a repository that has no idea what a beat is, and a
mismatch failed as a confusing transport error instead of a clear domain error.

## Where domain validation belongs

Zero3 Core, behind the Commander Gateway. When the gateway rejects a package it
returns a reason, and the bridge records it verbatim:

```text
commands/rejected/<execution_id>.json
```

The bridge does not interpret, translate, or second-guess the reason. It relays it.

## Authority table

| Concern | Owner |
|---|---|
| Task Authority | Zero3 Central |
| Worker Authority | Zero3 Central |
| Scheduler Authority | Zero3 Central |
| Execution Lease | Zero3 Central |
| Fencing Token | Zero3 Central |
| Execution Contract Authority | Zero3 Central |
| Skill Binding Authority | Zero3 Central |
| Artifact Authority | Zero3 Central |
| Envelope shape | Commander Bridge |
| Delivery durability | Commander Bridge |
| Mirror freshness | Commander Bridge |
| Audit trail transport | Commander Bridge |

## Result validity

A result file is valid only when **all** of the following hold. Existence is not
one of them.

1. The file parses as JSON.
2. It validates against `schemas/result.schema.json`.
3. `execution_id` matches the expected execution.
4. `task_id` matches the task recorded at acceptance.
5. `terminal` is `true`.
6. `state` is one of `succeeded`, `failed`, `cancelled`, `outcome_unknown`,
   `quarantined`.

`outcome_unknown` is a legitimate, honest terminal state. It means Zero3 could
not determine the outcome. The bridge must never convert it into `failed` or
`succeeded`, and must never quietly drop it.

## Transport communication rule

The bridge reaches Zero3 only through:

```text
HTTPS
-> Zero3 Commander Gateway
-> /api/commander/...
```

No direct database access, no SSH, no shared filesystem, no Python import of
Core modules. TLS verification stays enabled; a private CA is configured with
`ZERO3_COMMANDER_CA_BUNDLE`, never by disabling verification.
