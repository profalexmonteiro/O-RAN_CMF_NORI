# Conflict Detection (CD) Agent.
#
# Implements the Direct Conflict Detection (DCD) and Indirect Conflict
# Detection (ICD) components described in Section III-B/III-C of
# "Conflict Mitigation Framework and Conflict Detection in O-RAN Near-RT RIC"
# (Adamczyk & Kliks, IEEE ComMag 2023, https://arxiv.org/abs/2305.07117).
#
# Both work "pre-action": every proposed RAN control message is evaluated
# against the Near-RT RIC's Database of currently-in-effect control decisions
# *before* it is allowed to reach the RAN, so a real conflict can be
# prevented rather than just observed after the fact. Implicit Conflict
# Detection (ImCD) cannot work this way (see pmon.py) and lives separately.

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# RAN parameter IDs and their O-RAN names, matching the convention decoded by
# RicControlCallback() in nori-cmf.cc.
PARAMETER_NAMES = {
    1: "HOMeasurementOffset",  # CIO, written by MLB
    2: "HOHysteresis",  # written by MRO
    3: "HOTimeToTrigger",  # written by MRO
}

# Parameter Groups (PGs), configured here the same way the paper describes
# ("can be configured manually by the MNO, predefined in the standards, or
# learned dynamically"): CIO, Hysteresis and TTT all influence where the
# handover boundary of a cell sits, so a change to any of them can offset a
# change to any other.
PARAMETER_GROUPS = {
    "CellAffectHandoverBoundary": {1, 2, 3},
}


def group_of(parameter_id: int) -> Optional[str]:
    for name, members in PARAMETER_GROUPS.items():
        if parameter_id in members:
            return name
    return None


@dataclass
class ControlRecord:
    """
    One entry of the Near-RT RIC's "Recently changed parameters" /
    "Recently changed Parameter Groups" database (Fig. 2 of the paper).
    """

    source: str  # originating xApp ("MRO" or "MLB")
    cell_id: int  # control target
    parameter_id: int
    value: float
    timestamp: float  # wall-clock time.time() the decision was logged
    control_timespan_s: float = 0.5  # how long the decision stays "in effect"

    def expired(self, now: float) -> bool:
        return now - self.timestamp > self.control_timespan_s

    def parameter_name(self) -> str:
        return PARAMETER_NAMES.get(self.parameter_id, "Unknown")


@dataclass
class Conflict:
    conflict_type: str  # "direct" | "indirect"
    detector: str  # "DCD" | "ICD"
    group_name: Optional[str]
    proposal: ControlRecord
    existing: ControlRecord


class ConflictDetectionAgent:
    """
    Tracks, per cell, every control decision currently in effect and detects
    direct and indirect conflicts against a newly proposed one.
    """

    def __init__(self):
        # "Database" / SDL stand-in: cell_id -> list of currently-in-effect records.
        self._records: Dict[int, List[ControlRecord]] = {}

    def _prune(self, cell_id: int, now: float) -> None:
        records = self._records.get(cell_id, [])
        self._records[cell_id] = [r for r in records if not r.expired(now)]

    def active_records(self, cell_id: int, now: Optional[float] = None) -> List[ControlRecord]:
        """Returns the currently valid (non-expired) records for a cell."""
        now = time.time() if now is None else now
        self._prune(cell_id, now)
        return list(self._records.get(cell_id, []))

    def evaluate(self, proposal: ControlRecord) -> List[Conflict]:
        """
        DCD + ICD: compares `proposal` against every other xApp's currently
        active decision on the same cell. Returns every conflict found (there
        can be more than one, e.g. against two different existing records).
        The proposal is recorded into the database regardless of the outcome
        -so later proposals can be checked against it too- matching the
        paper's DCD2 "store data about control message" step, which happens
        unconditionally, before the comparison.
        """
        now = proposal.timestamp
        existing = self.active_records(proposal.cell_id, now)

        conflicts: List[Conflict] = []
        proposal_group = group_of(proposal.parameter_id)
        for record in existing:
            if record.source == proposal.source:
                continue  # a conflict is between *different* xApps

            if record.parameter_id == proposal.parameter_id:
                # DCD: same parameter of the same cell, contradicting values.
                conflicts.append(
                    Conflict("direct", "DCD", None, proposal, record)
                )
            else:
                record_group = group_of(record.parameter_id)
                if proposal_group is not None and proposal_group == record_group:
                    # ICD: different parameters of the same functional group.
                    conflicts.append(
                        Conflict("indirect", "ICD", proposal_group, proposal, record)
                    )

        self._records.setdefault(proposal.cell_id, []).append(proposal)
        return conflicts
