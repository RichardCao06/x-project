"""Persistent, evidence-bound Wiki-v2 vertical slice runtime.

The runtime deliberately models *proof of a release*, not prose generation.
Agent-originated values are frozen artifacts and can only be admitted in the
three proposal/verdict/attestation stages.  No method accepts an agent command
or lets an agent choose a filesystem target.
"""

from .runtime import (
    AGENT_STAGES,
    STAGES,
    WikiRun,
    WikiRuntime,
    WikiRuntimeError,
    WikiStage,
    WikiStageConflict,
)

__all__ = [
    "AGENT_STAGES", "STAGES", "WikiRun", "WikiRuntime", "WikiRuntimeError",
    "WikiStage", "WikiStageConflict",
]
