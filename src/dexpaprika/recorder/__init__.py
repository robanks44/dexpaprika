"""Recorder: reusable single-cycle capture + long-running service (S12a).

``run_cycle`` is the pure, testable recording cycle extracted from the CLI
``snapshot`` command; ``RecorderService`` loops it per-source cadence with
honest staleness, per-source backoff, and a cooperative stop. Storage is RAW
only — derived metrics are a read-time concern (S12b).
"""

from __future__ import annotations

from dexpaprika.recorder.cycle import (
    CycleResult,
    RecorderClients,
    SourceStamp,
    build_clients,
    run_cycle,
)
from dexpaprika.recorder.service import RecorderService, ServiceStatus

__all__ = [
    "CycleResult",
    "RecorderClients",
    "RecorderService",
    "ServiceStatus",
    "SourceStamp",
    "build_clients",
    "run_cycle",
]
