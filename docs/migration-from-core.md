# Migration from Zero3 Core

**Status: plan only. Nothing in this document has been executed.**

Stage 1 created this standalone repository. Zero3 Core
(`Taa965/zero-three-self-media-management-system`) was **not modified**: no
files deleted, no workflows changed, no history rewritten, no gateway, runtime,
Central, production server, DNS, or secret touched.

Each removal below happens later, in its own reviewed pull request against Zero3
Core, only after the replacement path here is proven in production.

---

## Sequencing

Migration is only safe in this order. Skipping a step risks losing in-flight
executions.

1. **Stage 1 (done).** Standalone repository exists, tests green, no Core changes.
2. **Stage 2.** Configure `ZERO3_COMMANDER_BASE_URL`, `ZERO3_COMMANDER_TOKEN_FILE`,
   and `ZERO3_COMMANDER_ID` for this bridge. Verify `health()` against the real
   Commander Gateway.
3. **Stage 3.** Run both bridges in parallel, new one read-only, and compare
   mirrors. The legacy bridge stays authoritative.
4. **Stage 4.** Cut submission over to this repository. Legacy workflows are
   disabled but not yet deleted, so rollback is one toggle.
5. **Stage 5.** Drain: confirm no execution in the legacy `.zero3-bridge/running/`
   is still non-terminal.
6. **Stage 6.** Delete legacy components from Core in the PRs listed below.

Rollback for stages 2 through 5 is re-enabling the legacy workflows. After stage
6 rollback means reverting the deletion PR.

---

## To migrate out of Zero3 Core, later

### Runtime mailbox directories

```text
.zero3-bridge/**
```

Comprising `inbox/`, `outbox/`, `running/`, `poll/`, and `diagnostics/`.

These become, respectively, `commands/pending/`, `results/`, `state/` plus
`events/`, the reconciliation fallback, and out-of-band operator tooling.

**Do not delete before stage 5.** These directories contain the only record of
in-flight executions during the transition. Archive them with the deletion PR
rather than dropping them outright.

```text
.zero3-command-bus/**
```

The older `commands/` and `results/` command bus. Superseded by the four-way
command / state / event / result split.

### Workflows

```text
.github/workflows/zero3-github-bridge-submit.yml
.github/workflows/zero3-github-bridge-results.yml
.github/workflows/zero3-github-command-bus.yml
```

`zero3-github-bridge-results.yml` is the specific source of the reliability bug
this repository was built to fix. At line 60 it reads:

```bash
[[ ! -e "$out" ]] || continue
```

which treats *a file existing* as *a valid result*, so a zero-byte or partially
written `*.result.json` was accepted as authoritative and never retried. The
replacement path validates parse, schema, `execution_id`, `task_id`,
`terminal`, and terminal-state legality, and writes only through
`atomic_io`.

`zero3-github-bridge-submit.yml` additionally hardcodes a public production IP
in its `env:` block. The replacement reads `ZERO3_COMMANDER_BASE_URL` from the
environment; no address is committed to this repository.

### Documentation

```text
docs/Zero3-GitHub-Bridge-v1.md
```

Superseded by `docs/architecture.md` and `docs/protocol-boundary.md` here.
Replace the Core copy with a short pointer to this repository rather than a
silent deletion, so operators following an old link are not stranded.

---

## Stays in Zero3 Core, never migrates

These are business and execution authority. They are **not** transport:

```text
app/cloud/v43_commander_api.py
Central
Scheduler
Worker
Runtime v3
PlacementScheduler
ComputeStrategy
Production UI
```

`app/cloud/v43_commander_api.py` is the Commander Gateway itself. It is the
server side of the contract this repository consumes. It must remain in Core.
This repository is one of its external clients.

---

## Suggested rename inside Core, not part of any migration

```text
app/services/execution_package_bridge.py  ->  app/services/execution_package_ingestor.py
```

Despite the name, this module is not transport. It imports `app.cloud.models`,
`app.cloud.v43_m13_video_api`, and `app.services.execution_contract_v43`, and it
freezes and verifies execution contracts. That is Zero3 Core execution package
**ingest**, and it belongs in Core permanently. Only the name is misleading: it
invites the assumption that it is part of the GitHub bridge and therefore a
migration candidate. It is not.

**This rename was deliberately not performed.** It touches Core, and Stage 1
changes nothing in Core. It should be a separate, isolated PR whose only content
is the rename plus import updates, so it never mixes with a behavioural change.

---

## Deletion checklist for the future Core PR

- [ ] Stage 5 drain confirmed: no non-terminal execution left in `.zero3-bridge/running/`
- [ ] Legacy mailbox contents archived
- [ ] New bridge has served production submissions for an agreed soak period
- [ ] `bridge/health.json` reporting healthy ingress and egress
- [ ] Secrets used only by the legacy workflows identified and rotated or removed
- [ ] Core documentation points at this repository
- [ ] Deletion PR contains deletions only, no behavioural change
