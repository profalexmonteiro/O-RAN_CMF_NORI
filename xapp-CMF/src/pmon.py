# Performance Monitoring (PMon) component, feeding Implicit Conflict
# Detection (ImCD).
#
# Implements Section III-D of "Conflict Mitigation Framework and Conflict
# Detection in O-RAN Near-RT RIC" (Adamczyk & Kliks, IEEE ComMag 2023).
# Unlike DCD/ICD, implicit conflicts cannot be anticipated pre-action: PMon
# watches a RAN KPI (mean UE satisfaction per cell, computed by nori-cmf.cc
# and reported as the QoS.MeanUeSatisfactionPermille KPM item) and only finds
# out about a problem once it has already happened. When a significant drop
# is observed, ImCD correlates it against the CD Agent's own record of which
# xApps recently changed which parameters on that cell (reusing the same
# database DCD/ICD already maintain, exactly as the paper describes: "ImCD
# utilizes the data captured in the Database by DCD and ICD, so it does not
# need to monitor any control messages by itself"). A per-(cell, source-set)
# counter is incremented on every correlated occurrence, and once it breaches
# a threshold the implicit conflict is reported.

from typing import Callable, Dict, FrozenSet, Optional, Set

from .cd_agent import ConflictDetectionAgent


class PerformanceMonitor:
    def __init__(
        self,
        cd_agent: ConflictDetectionAgent,
        degradation_threshold: float = 0.02,
        occurrence_threshold: int = 3,
        report_callback: Optional[Callable[[int, Set[str]], None]] = None,
    ):
        """
        degradation_threshold: relative drop in the monitored KPI, compared to
            its previous sample for the same cell, that counts as "degraded".
        occurrence_threshold: number of correlated degradation occurrences
            for the same (cell, set-of-xApps) before an implicit conflict is
            actually reported - a single bad sample is noise, not a conflict.
        report_callback: called as report_callback(cell_id, sources) once the
            threshold is breached.
        """
        self.cd_agent = cd_agent
        self.degradation_threshold = degradation_threshold
        self.occurrence_threshold = occurrence_threshold
        self.report_callback = report_callback
        self._last_kpi: Dict[int, float] = {}
        self._occurrences: Dict[FrozenSet, int] = {}

    def observe(self, cell_id: int, kpi_value: float, timestamp: float) -> None:
        """
        Feeds one new KPI sample for a cell (mean UE satisfaction, in [0, 1]).
        """
        previous = self._last_kpi.get(cell_id)
        self._last_kpi[cell_id] = kpi_value
        if previous is None or previous <= 0.0:
            return

        drop = (previous - kpi_value) / previous
        if drop <= self.degradation_threshold:
            return

        # KPI degradation detected for this cell (ImCD1/PMon "Analyse PM data").
        records = self.cd_agent.active_records(cell_id, timestamp)
        sources = {r.source for r in records}
        if len(sources) < 2:
            return  # nothing to correlate the degradation with

        key = (cell_id, frozenset(sources))
        self._occurrences[key] = self._occurrences.get(key, 0) + 1
        if self._occurrences[key] >= self.occurrence_threshold:
            if self.report_callback is not None:
                self.report_callback(cell_id, sources)
            # "the CD Agent may remove data about RAN KPI degradation
            # occurrences ... related to the reported conflict from the
            # Database" once it has been reported.
            del self._occurrences[key]
