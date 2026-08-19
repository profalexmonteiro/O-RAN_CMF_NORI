# Conflict Resolution (CR) Agent.
#
# Implements the resolution half of the framework described in
# "Conflict Mitigation Framework and Conflict Detection in O-RAN Near-RT RIC"
# (Adamczyk & Kliks, IEEE ComMag 2023). The paper leaves the CR Agent's exact
# resolution logic open ("it is assumed that it can resolve any conflicts
# detected by the CD Agent") and evaluates, in its simulation, the simplest
# possible scheme: static xApp prioritization. That is what is implemented
# here, matching the three modes the paper's own evaluation compares
# ("CMF disabled", "prioritize MRO", "prioritize MLB").

from enum import Enum
from typing import List, Optional

from .cd_agent import Conflict


class CrMode(str, Enum):
    NONE = "none"  # conflicts are only logged, never mitigated
    PRIO_MRO = "prioMRO"  # MRO's decisions always take effect
    PRIO_MLB = "prioMLB"  # MLB's decisions always take effect


class Resolution:
    def __init__(self, allowed: bool, reason: Optional[str] = None):
        self.allowed = allowed
        self.reason = reason


class ConflictResolutionAgent:
    """
    Decides, for a proposal that the CD Agent flagged as conflicting with one
    or more currently active decisions, whether it is allowed to take effect.
    """

    def __init__(self, mode: CrMode = CrMode.NONE):
        self.mode = mode

    def resolve(self, conflicts: List[Conflict]) -> Resolution:
        if not conflicts:
            return Resolution(allowed=True)

        if self.mode == CrMode.NONE:
            # "If a given xApp is prioritized..." - with none prioritized,
            # every decision takes effect in the order it is provided; the
            # conflict is only reported for the record.
            return Resolution(allowed=True)

        prioritized = "MRO" if self.mode == CrMode.PRIO_MRO else "MLB"

        proposal_source = conflicts[0].proposal.source
        if proposal_source == prioritized:
            # The proposal itself is from the prioritized xApp: it always
            # takes effect, regardless of what it conflicts with.
            return Resolution(allowed=True)

        if any(c.existing.source == prioritized for c in conflicts):
            # The proposal conflicts with a decision from the prioritized
            # xApp still in effect: the proposal is rejected.
            c = next(c for c in conflicts if c.existing.source == prioritized)
            return Resolution(
                allowed=False,
                reason=(
                    f"{c.detector} {c.conflict_type} conflict with {prioritized}'s "
                    f"'{c.existing.parameter_name()}'={c.existing.value} on cell "
                    f"{c.proposal.cell_id} (prioritized: {prioritized})"
                ),
            )

        # Conflicts with a third, non-prioritized source (not expected with
        # only MRO/MLB deployed, but handled defensively): allow by default
        # rather than blocking RAN control on an unrecognised source.
        return Resolution(allowed=True)
