"""HTTP outcome classification for the Commander boundary.

A transport adapter must distinguish an authoritative refusal of one command
from a failure of the bridge, its credentials, or the protocol route. Only the
former may consume a pending command and produce a rejection verdict.
"""

from __future__ import annotations

__all__ = ["PERMANENT_REJECTION_STATUSES", "is_permanent_rejection"]

# These statuses are stable command-level refusals for the current Commander
# contract:
#
# 400  malformed/invalid request submitted to the endpoint
# 409  authoritative conflict/idempotency refusal
# 413  command is too large for the endpoint
# 422  execution package/schema/domain refusal
#
# Deliberately NOT here:
# 401/403  bridge credential/identity failure
# 404/405  route/protocol/deployment mismatch
# 408/425  transient transport semantics
# 429      throttling
# 5xx      Commander/Central infrastructure failure
#
# 410 is also treated as protocol/deployment failure rather than a business
# verdict: an endpoint disappearing must not destroy a valid pending command.
PERMANENT_REJECTION_STATUSES = frozenset({400, 409, 413, 422})


def is_permanent_rejection(status: int) -> bool:
    """Return whether *status* is an authoritative verdict on the command."""
    return status in PERMANENT_REJECTION_STATUSES
