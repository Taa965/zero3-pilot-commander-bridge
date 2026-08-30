# Migration history and H5 cutover

This file records two different migrations. Keeping them separate prevents an old Zero3 Core cleanup plan from being mistaken for the current Zero3 Pilot control-plane contract.

## Current migration: Pilot Dev Executor -> Zero3 Pilot H5

Current target path:

```text
External Agent
-> GitHub mailbox
-> zero3-pilot-commander-bridge
-> Zero3 Pilot H5 /api/control/v1/tasks
-> Windows Zero3 Remote Host
-> Zero3CodexAppServer / Codex
```

The Bridge implementation defaults to the `h5` transport adapter. The former Pilot Dev Executor `/api/commander/v1` adapter is retained only as an explicit rollback path:

```text
ZERO3_COMMANDER_ADAPTER=legacy-commander
```

### Cutover sequence

1. **Implementation/review.** Land the H5 adapter in this repository without changing `Taa965/zero3-pilot` and without giving the Bridge execution authority.
2. **Deploy H5.** Configure the Zero3 Pilot `apps/web` control plane and its `ZERO3_CONTROL_TOKEN_FILE` on the target environment.
3. **Configure Bridge.** Point `ZERO3_COMMANDER_BASE_URL` at H5, leave `ZERO3_COMMANDER_ADAPTER=h5`, and configure the matching client token file outside the repository.
4. **Pair Remote Host.** Verify the Windows Remote Host is registered/paired and can use H5 `/api/host/v1/*` lease/fencing routes. The Bridge never calls those routes.
5. **End-to-end proof.** Submit a real `zero3.pilot.remote-task.v1` through the GitHub mailbox and verify H5 admission, host lease, Codex execution, monotonic event/state mirror, and immutable terminal result.
6. **Soak.** Keep the legacy adapter available but unused for a bounded observation period.
7. **Retire legacy.** Remove the old Pilot Dev Executor adapter only after the H5 path is proven and rollback is no longer required.

Do not interpret a healthy root `/health` response as full cutover proof. Production readiness also depends on control-token configuration, Remote Host pairing, and an end-to-end RemoteTask execution.

## Rollback during current cutover

Before legacy retirement, rollback is explicit adapter selection:

```text
ZERO3_COMMANDER_ADAPTER=legacy-commander
```

Rollback changes transport routing only. It does not grant the Bridge Agent, Scheduler, lease, fencing, shell, Git, filesystem, or Codex authority.

---

## Historical migration: legacy Zero3 Core GitHub bridge -> standalone repository

The remainder of the original plan concerned an older repository and an older in-Core GitHub bridge. It is historical context, not the current Zero3 Pilot H5 contract.

That migration established the standalone transport repository and the following invariants, which still apply:

- GitHub command ingress is durable and actively synchronized.
- A file existing is not the same as a valid verdict or result.
- State/event/result writes are atomic and validated.
- Event sequence is monotonic and valid terminal results are immutable.
- Deployment addresses and credentials remain outside the repository.
- TLS verification cannot be disabled.
- Business/runtime validation stays outside the transport repository.

Historical in-Core paths such as `.zero3-bridge/**`, `.zero3-command-bus/**`, old GitHub workflows, and the former `app/cloud/v43_commander_api.py` belonged to that earlier system. They must not be used as documentation for the current `Taa965/zero3-pilot` H5 API.

For the current contract, use:

- `docs/architecture.md`
- `docs/protocol-boundary.md`
- `bridge/capabilities.json`
- the Zero3 Pilot H5 remote-control documentation in `Taa965/zero3-pilot`

## Current deletion gate

The legacy Pilot Dev Executor adapter may be removed only when all of these are true:

- [ ] H5 `apps/web` is deployed on the intended environment.
- [ ] H5 control authentication is configured and verified.
- [ ] Windows Remote Host pairing is healthy.
- [ ] A GitHub-mailbox -> Bridge -> H5 -> Remote Host -> Codex execution succeeds end to end.
- [ ] State/event/result mirrors are verified for monotonicity and terminal immutability on the real path.
- [ ] The rollback window/soak period is complete.
- [ ] No production process still depends on `/api/commander/v1`.

Until those gates are satisfied, the H5 implementation is reviewable and deployable for controlled verification, but legacy removal is intentionally deferred.
