"""Zero3 Pilot Commander Bridge.

Transport and control middleware between external AI commanders and Zero3.

    Zero3 Core Repo governs "what Zero3 is".
    Zero3 Pilot Commander Bridge Repo governs "how the outside controls Zero3".

This package is a replaceable transport adapter. It holds no business
authority: not Task, Worker, Scheduler, Execution Lease, Fencing Token,
Execution Contract, Skill Binding, or Artifact authority. Zero3 Central is the
sole authority for all of them.

It reaches Zero3 only over HTTPS through the Commander Gateway at
``/api/commander/...``, and it must never import Zero3 Core Python modules.
That boundary is enforced by ``tests/test_no_core_imports.py`` and in CI.
"""

from __future__ import annotations

from .models import BRIDGE_VERSION, TERMINAL_STATES

__all__ = ["BRIDGE_VERSION", "TERMINAL_STATES", "__version__"]

__version__ = BRIDGE_VERSION
