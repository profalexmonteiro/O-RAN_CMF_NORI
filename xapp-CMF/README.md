# xapp-CMF

The xApp-CMF is an OSC xApp implementing the **Conflict Mitigation Framework (CMF)** proposed by Adamczyk & Kliks in *"Conflict Mitigation Framework and Conflict Detection in O-RAN Near-RT RIC"* (IEEE ComMag 2023, [arXiv:2305.07117](https://arxiv.org/abs/2305.07117)), as a standalone xApp on the Near-RT RIC — matching where the paper itself places it, rather than embedding it in the [`nori-cmf.cc`](../ns-3-dev/contrib/nori/examples/nori-cmf.cc) ns-3 scenario.

It is the third xApp of this project, alongside [`xapp-MRO`](../xapp-MRO) and [`xapp-MLB`](../xapp-MLB): those two propose handover-boundary changes, and this one is what decides whether a proposal that conflicts with another xApp's decision is actually allowed to reach the RAN.

## Why a separate xApp

`nori-cmf.cc` used to include its own conflict detection/mitigation logic, directly inside the ns-3 process. That is not how the reference paper defines the CMF: it is explicitly a component of the Near-RT RIC's Conflict Mitigation (CM) entity (Fig. 2 of the paper), independent of any particular RAN or E2 node implementation, and meant to mitigate conflicts between **any** xApps deployed on that RIC — not just the ones a given simulator happens to know about. `nori-cmf.cc` is now *only* the RAN + E2 node scenario: it applies whatever RIC Control Request it receives, from whichever xApp sent it, with no arbitration of its own. All conflict detection and resolution logic now lives here.

## The three components

Following Section III of the paper exactly:

| Module | File | Role |
|---|---|---|
| **CD Agent** (Conflict Detection) | [`src/cd_agent.py`](src/cd_agent.py) | Implements **DCD** (Direct Conflict Detection) and **ICD** (Indirect Conflict Detection). Both are *pre-action*: every proposed decision is checked against a database of currently-in-effect decisions before it is allowed to take effect. |
| **CR Agent** (Conflict Resolution) | [`src/cr_agent.py`](src/cr_agent.py) | Decides, for a proposal the CD Agent flagged as conflicting, whether it is allowed. Implements the same simple prioritization scheme the paper evaluates: `none` (detect only), `prioMRO`, `prioMLB`. |
| **PMon** (Performance Monitoring) | [`src/pmon.py`](src/pmon.py) | Feeds **ImCD** (Implicit Conflict Detection), which is inherently *post-action*: it watches a RAN KPI (mean UE satisfaction per cell) and, when it drops significantly, correlates the drop against the CD Agent's own record of which xApps recently touched that cell. |

### DCD — Direct Conflict Detection

Two xApps writing the **same parameter** of the same cell with different values. In the current deployment (only MRO and MLB, writing disjoint parameters) this should not occur — it is implemented for completeness and as the fail-safe the paper describes it as ("in case of human error leading to deployment of directly conflicting xApps"), and will trigger correctly if a second xApp ever targets `HOHysteresis`, `HOTimeToTrigger` or `HOMeasurementOffset`.

### ICD — Indirect Conflict Detection

Two xApps writing **different parameters of the same functional group**. `HOMeasurementOffset` (CIO), `HOHysteresis` and `HOTimeToTrigger` are all registered under the group `CellAffectHandoverBoundary` (see `PARAMETER_GROUPS` in `cd_agent.py`) because together they decide when a handover fires. This is the conflict type that structurally occurs between MRO and MLB in this deployment, and the one exercised on essentially every control round both are active.

### ImCD — Implicit Conflict Detection

Cannot prevent a conflicting decision from taking effect — by the time it is detected, it already has. PMon tracks `QoS.MeanUeSatisfactionPermille` per cell (reported by `nori-cmf.cc` in every KPM indication); a relative drop past a threshold is checked against the CD Agent's database for that cell, and if more than one xApp had a decision in effect there, a correlated-occurrence counter is incremented. Only after a few correlated occurrences (not a single noisy sample) is the implicit conflict actually reported.

## How MRO and MLB talk to this xApp

There is no practical way to have this xApp transparently intercept a `RIC_CONTROL_REQ` RMR message addressed to `e2term`: MRO and MLB target a *specific* E2 node/cell by replying (`rmr_rts`) along the same connection their KPM indication arrived on, and that reply-route is only usable by the process that received the original message — a third process cannot forward it on their behalf without losing the routing context.

Instead, this xApp exposes a synchronous HTTP endpoint, and MRO/MLB call it **before** ever building or sending a RIC Control Request:

```
POST /ric/v1/cmf/evaluate
{"source": "MRO" | "MLB", "cellId": <int>, "parameterId": 1 | 2 | 3, "value": <float>}

-> 200 {"allowed": true}
-> 200 {"allowed": false, "reason": "ICD indirect conflict with MLB's 'HOMeasurementOffset'=3.0 on cell 12 (prioritized: MLB)"}
```

`parameterId` follows the same convention already decoded by `RicControlCallback()` in `nori-cmf.cc`: `1` = `HOMeasurementOffset` (CIO), `2` = `HOHysteresis`, `3` = `HOTimeToTrigger`. If a proposal is rejected, MRO/MLB simply do not send that parameter's RIC Control Request this round — exactly as if the CMF had silently dropped it, matching the old embedded behaviour, except the decision is now blocked **before** ever reaching the RAN, not after.

This call has a short timeout and **fails open**: if the CMF xApp is unreachable (not deployed, still starting, or crashed), MRO/MLB proceed as if conflict mitigation were disabled, rather than freezing RAN control entirely on a missing dependency. A warning is logged every time this happens.

Independently of the evaluate endpoint, this xApp also subscribes to KPM (RAN Function 200) on every registered E2 node, purely to feed PMon — it never writes any RAN parameter itself.

## Configuring the resolution mode

Set in `src/custom_xapp.py`:

```python
CM_MODE = CrMode.PRIO_MRO  # or CrMode.PRIO_MLB, or CrMode.NONE
```

matching the three modes the paper's own evaluation compares (`CMF disabled`, `prioritize MRO`, `prioritize MLB`).

## Requirements

Same as [`xapp-MRO`](../xapp-MRO/README.md#requirements) and [`xapp-MLB`](../xapp-MLB/README.md#requirements) — an OpenRAN@Brasil Blueprint VM with a healthy Near-RT RIC and a local registry at `127.0.0.1:5001`.

## Deploying to the Near-RT RIC

Follow the same step-by-step as [`xapp-MRO`'s README](../xapp-MRO/README.md#deploying-to-the-near-rt-ric-step-by-step) (build/install via `bash update_xapp.sh`, confirm the pod, resubscribe once `nori-cmf` is connected), replacing every `xappmro` with `xappcmf`. **Deploy this xApp before, or at the same time as, MRO/MLB** — while it is missing, MRO/MLB still work (fail-open), but no conflict is ever actually mitigated, only whatever the individual xApp decided on its own.

## Observability

- `kubectl logs` shows one line per evaluated proposal that resulted in a conflict, e.g. `Cell 12: MLB proposes HOMeasurementOffset=3.0 -> REJECTED (ICD indirect conflict with MRO's 'HOHysteresis'=1.5 on cell 12 (prioritized: MRO))`.
- Every detected conflict (DCD, ICD or ImCD) is appended as one JSON line to `/tmp/conflicts.json` inside the pod, in the same message shape as `json_messages/{DCD,ICD,ImCD}/*signal conflict.json` in the reference repository ([`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023)).
- If `self.save_influx = True` (the default), every evaluation is written to InfluxDB in measurement `cmf_evaluations` (`cellId`, `source`, `parameter`, `value`, `conflicts`, `allowed`), and every implicit conflict in `cmf_conflicts`.
