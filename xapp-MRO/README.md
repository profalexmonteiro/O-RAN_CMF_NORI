# xapp-MRO

The xApp-MRO is an OSC xApp that implements **Mobility Robustness Optimization (MRO)** for gNBs simulated by the NORI NS-3 module, in particular the [`nori-cmf.cc`](../ns-3-dev/contrib/nori/examples/nori-cmf.cc) scenario (see [`contrib/nori/docs/nori-cmf.md`](../ns-3-dev/contrib/nori/docs/nori-cmf.md) for the full scenario description).

It is built from the same base as [`xapp-nori`](../xapp-nori), stripped of the RL/network-slicing logic and replaced with a closed-loop MRO controller.

<<<<<<< HEAD
> Deploy [`xapp-CMF`](../xapp-CMF) alongside this xApp and [`xapp-MLB`](../xapp-MLB) if you want conflicts between the two actually mitigated, not just detected/logged after the fact — see [Conflicts with MLB, and the CMF](#conflicts-with-mlb-and-the-cmf) below.

=======
>>>>>>> 8d6507985e9043f9dd0d376a634f6354e7703e38
## What it does

The xApp subscribes to RAN Function ID 200 (KPM) on every registered E2 node. Each RIC Indication received from a cell carries, among other measurements:

- `HO.TotNbrOut` — handovers originated by this cell in the last statistics window;
- `HO.PingPongNbrOut` — of those, how many were ping-pongs;
- `RRC.ReEstabAtt.RLF` — how many radio link failures happened on this cell in the same window;
- `MRO.HysteresisMilliDb` / `MRO.TimeToTriggerMs` — the handover Hysteresis and Time-To-Trigger **currently active** on the cell;
- `MLB.CioMilliDb` — the Cell Individual Offset currently active (read-only here — that parameter belongs to the MLB xApp).

On every indication, the xApp:

1. Computes the ping-pong ratio (`HO.PingPongNbrOut / HO.TotNbrOut`) and the RLF ratio (`RRC.ReEstabAtt.RLF / HO.TotNbrOut`).
2. Maps each ratio to a target value using the same step tables published in [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023) and reproduced in `nori-cmf.cc`:
   - ping-pong ratio → Time-To-Trigger (fewer ping-pongs need less patience; more ping-pongs need a longer TTT so a handover only fires on a sustained advantage);
   - RLF ratio → Hysteresis (more radio link failures need a *smaller* margin, so a handover can fire sooner, before the link degrades further).
3. Compares the result against the cell's currently active values (also read from the same indication).
4. If either differs, sends a RIC Control Request back to that same cell — and only that cell — carrying the new value(s).

The xApp is purely reactive and stateless across indications: every decision is recomputed from scratch from what the cell reports *right now*. This means it self-corrects automatically if a previous decision was overridden or dropped elsewhere in the loop (see "Conflicts with MLB" below) — it will simply keep proposing the same value every period until it is accepted.

**The xApp never writes `HOMeasurementOffset` (CIO)** — that parameter is the responsibility of a separate MLB (Mobility Load Balancing) xApp. Running both against the same cells is what exercises the Conflict Mitigation Framework described below.

## Conflicts with MLB, and the CMF

`HOMeasurementOffset` (CIO), `HOHysteresis` and `HOTimeToTrigger` jointly decide when a handover fires (they all enter the same A3-event inequality). If an MLB xApp is also adjusting CIO on the same cells this xApp is adjusting Hysteresis/TTT on, the two are structurally in an **indirect conflict** per the taxonomy of [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023): different parameters, same functional group (`CellAffectHandoverBoundary`).

<<<<<<< HEAD
Conflict detection and resolution are **not** done by this xApp, and not by `nori-cmf.cc` either: they are the job of a third xApp, [`xapp-CMF`](../xapp-CMF), which implements the Conflict Detection (DCD/ICD/ImCD) and Conflict Resolution Agents of [Adamczyk & Kliks, IEEE ComMag 2023](https://arxiv.org/abs/2305.07117). Before this xApp sends a RIC Control Request for a new Hysteresis/TTT value, it first submits the proposal to `xapp-CMF`'s `POST /ric/v1/cmf/evaluate` and only proceeds if the answer is `{"allowed": true}` — see [`xapp-CMF`'s README](../xapp-CMF/README.md) for the full protocol and the resolution modes (`none`, `prioMRO`, `prioMLB`). If `xapp-CMF` is unreachable, this call fails open (a warning is logged, and the decision proceeds unmitigated) rather than freezing RAN control on a missing dependency.

Every detected conflict is logged by `xapp-CMF` to `/tmp/conflicts.json` inside its own pod, in the same JSON format documented in the reference repository.
=======
When both xApps run against `nori-cmf.cc`, its built-in Conflict Mitigation Framework detects this on every control round and, depending on `--cmMode`:

- `none`: both decisions are applied — this is the "no CM" baseline of the reference paper;
- `prioMRO`: this xApp's decision wins, the MLB xApp's CIO update for that round is dropped;
- `prioMLB`: the MLB xApp's decision wins, this xApp's update for that round is dropped.

Every detected conflict is logged by the simulator to `conflicts.json` in the scenario's output directory, in the same JSON format documented in the reference repository. See [`nori-cmf.md`](../ns-3-dev/contrib/nori/docs/nori-cmf.md#10-o-conflict-mitigation-framework-cmf) for the full CMF description.
>>>>>>> 8d6507985e9043f9dd0d376a634f6354e7703e38

## Wire format

There is no standardized E2SM-RC payload for handover-boundary parameters, so this xApp uses the same convention `nori-cmf.cc`'s `RicControlCallback()` decodes:

- RIC Control Request targets **RAN Function ID 300** (RIC Control), the function ID `nori-cmf.cc` registers for control messages.
- The control message is a standard `E2SM-RC-ControlMessage-Format1` whose `ranParameters-List` carries one `RANParameter-Item` per changed parameter:

  | `ranParameterItem-ID` | Parameter | Written by |
  |---|---|---|
  | 1 | `HOMeasurementOffset` (CIO) | MLB xApp (not this one) |
  | 2 | `HOHysteresis` | **this xApp** |
  | 3 | `HOTimeToTrigger` | **this xApp** |

- Each item's value is an `INTEGER` (`valueInt`) in **milli-units** — milli-dB for CIO/Hysteresis, milliseconds for TTT — since the underlying ASN.1 type has no native decimal representation.
- The RIC Control Header (`E2SM-RC-ControlHeader-Format1`) is filled with placeholder `ueId`/style/action values (this control is cell-scoped, not UE-scoped) and omits the optional `rrmPolicyList`, which is specific to RAN-slicing control and unrelated to MRO.

See `build_ric_control_pdu()` in [`src/custom_xapp.py`](src/custom_xapp.py) for the exact encoding.

> **Note on the underlying nori module**: making this control path actually work end-to-end required three small fixes to `contrib/nori/model/{asn1c-types,ric-control-message}.cc` in the ns-3 `nori` module — the generic `ranParameters-List` decoder was previously stubbed out to always return an empty list, `RANParameterItem` had a double-free bug when copied (the parameter ID was unreachable from outside the class as a result), and the optional `rrmPolicyList` field of the control header was dereferenced unconditionally. All three are fixed in the `nori` module this xApp was developed against; if you see decoded control messages coming back empty, or a crash when a control request without slicing policies arrives, check that your `nori` checkout includes those fixes.

## Requirements

All commands assume:

- You are running an [OpenRAN@Brasil Blueprint v1](https://github.com/LABORA-INF-UFG/openran-br-blueprint/wiki/OpenRAN@Brasil-Blueprint-v1) VM, with `kubectl`, `docker` and `dms_cli` available and a local image registry reachable at `127.0.0.1:5001`
- A Near-RT RIC platform (`ricplt` namespace) is deployed and healthy
- You are inside the repository folder `xapp-MRO/`

## Deploying to the Near-RT RIC, step by step

This walks through everything from "the RIC is up" to "seeing the MRO control loop actually move a cell's Hysteresis/TTT". It was written from, and verified against, a real onboarding session on a live cluster — including the rough edges.

### 1. Confirm the RIC platform is healthy

```bash
kubectl get pods -n ricplt
```

Every pod should show `1/1` or `2/2` under `READY`. If not, see [Troubleshooting the platform](#troubleshooting-the-platform) below before continuing — onboarding an xApp against a half-started RIC will onboard fine but subscriptions will fail.

### 2. Build, push and install the xApp

```bash
bash update_xapp.sh
```

This one script does everything: it (a) onboards the xApp chart (`dms_cli onboard init/config-file.json init/schema.json`), (b) removes any previous `xappmro` install/image, (c) `docker build`s the image and pushes it to `127.0.0.1:5001/xappmro:1.0.0`, and (d) `dms_cli install`s it into the `ricxapp` namespace. A first build downloads and compiles `rmr`, clones `ric-plt-xapp-frame-py` and installs the Python dependencies — expect it to take several minutes the first time; later runs reuse Docker's layer cache and are much faster (the actual xApp code is only the last couple of layers).

The script waits for the pod to reach `1/1` on its own and tails its first log lines. If instead you see `CrashLoopBackOff`, jump to [Troubleshooting the xApp](#troubleshooting-the-xapp).

### 3. Confirm the pod is running

```bash
kubectl get pods -n ricxapp
```

You should see `ricxapp-xappmro-...` at `1/1 Running`. At this point the xApp has already tried, once, to subscribe to every E2 node the RIC currently knows about (`RANFunctionID: 200`, i.e. KPM). Check what happened:

```bash
bash log_xapp.sh
```

Look for lines like `Subscription response from <gNB>: status = 201, reason = Created` (success) versus `Failed to subscribe to node <gNB>. Status code: 503` (the E2 node isn't actually reachable, or `submgr` wasn't ready yet — very common right after either the RIC or the xApp just started, see below).

### 4. Start (or confirm) `nori-cmf` is connected

If it isn't running yet, find the `e2term` pod's IP and start the scenario in E2 mode from the `ns-3-dev` tree:

```bash
E2TERM_IP=$(kubectl get pods -n ricplt -o wide | grep e2term-alpha | awk '{print $6}')
cd ~/ns-3-dev
./ns3 run "nori-cmf --useE2=1 --ipE2TermRic=$E2TERM_IP"
```

Watch for `[INFO ] [SCTP] Sent E2-SETUP-REQUEST` for all 19 cells in the simulator's own output, and cross-check on the RIC side:

```bash
kubectl logs deployment-ricplt-e2mgr-* -n ricplt --tail=50 | grep CONNECTED
```

### 5. Resubscribe

Because `nori-cmf` registers its 19 cells *after* the xApp pod already started (step 2/3 happened first), the xApp's initial subscription attempt in step 3 almost certainly missed them. Trigger a resubscribe:

```bash
bash resubscribe.sh
```

This call returns immediately (`xApp ACKs resubscription request, resubscribing in the background`) — the actual unsubscribe-then-subscribe cycle runs in a background thread inside the pod, not in the HTTP request. This matters because **a cluster that has accumulated many stale E2 node registrations over time can take a while to cycle through** (each stale/unreachable node still costs up to ~10 s before its subscribe attempt gives up) — don't assume "no immediate log line" means it failed; tail the logs and give it a minute or two on an old/heavily-used cluster:

```bash
bash log_xapp.sh
```

You want to see `201, reason = Created` for the gNBs your current `nori-cmf` run actually registered (their names look like `gnb_<mcc>_<mnc>_<...>`; cross-check against `kubectl get pods -n ricplt` → e2mgr logs, or just try the next step and see if data shows up).

### 6. Verify the control loop is actually closing

Once subscribed to a live cell, the xApp logs one line per cell per indication *only when it decides to send a new Hysteresis/TTT* (silent when its decision matches what's already active — which is most of the time once the loop stabilises):

```bash
bash log_xapp.sh | grep "Cell NRCellDU"
```

A healthy loop looks like this — note how each new line's `(was hysteresis=... ttt=...)` matches the `-> hysteresis=... ttt=...` the *previous* line for that same cell sent, confirming the simulator actually applied it:

```text
Cell NRCellDU_12: ho=11 pp_ratio=27.3% rlf_ratio=9.1% -> hysteresis=1.0dB ttt=0.1s (was hysteresis=1.0 ttt=0.08)
Cell NRCellDU_12: ho=12 pp_ratio=25.0% rlf_ratio=8.3% -> hysteresis=1.0dB ttt=0.08s (was hysteresis=1.0 ttt=0.1)
```

You can cross-check the same values directly in the simulator's own output, in `<outputDir>/bs-12.csv` (the `hyst`/`ttt` columns) — they should match exactly.

### 7. Tear down

```bash
dms_cli uninstall xappmro ricxapp
```

---

### Troubleshooting the platform

If the xApp says no gNBs are registered and `kubectl get pods -n ricplt` shows something unhealthy, try redeploying the Near-RT RIC:

```bash
bash redeploy_ric.sh
watch kubectl get pods -n ricplt   # wait for every pod to reach 1/1 or 2/2
```

**Known issue — `e2term` can crash on simulation teardown.** When `nori-cmf` exits, all 19 SCTP connections to `e2term` close in quick succession; this can trigger a pre-existing crash in the RIC's own `e2term` (`free(): invalid pointer`), unrelated to this xApp or to `nori-cmf.cc`. It happens intermittently (observed on roughly 2 out of 3 teardowns during development), and Kubernetes restarts the pod automatically — it takes about 1–2 minutes (the readiness probe has a startup delay) to come back to `1/1`:

```bash
watch kubectl get pods -n ricplt deployment-ricplt-e2term-alpha-*
```

Don't start a new `nori-cmf` run against it until it's back to `1/1`.

### Troubleshooting the xApp

- **`CrashLoopBackOff` right after install**: `kubectl logs -n ricxapp <pod> --previous` almost always shows the Python traceback. If it's an `ImportError`, the image build likely failed partway (check `docker images | grep xappmro` and re-run `update_xapp.sh`).
- **Pod restarts with exit code 137 (`kubectl describe pod ... | grep -A3 "Last State"`)**: this is a Kubernetes-issued `SIGKILL`, almost always because the liveness probe (`GET /ric/v1/health/alive`, 1 s timeout) went unanswered for too long. The subscribe/unsubscribe/resubscribe paths use a per-request timeout and dispatch the resubscribe cycle to a background thread specifically to avoid this; if you still see it, it likely means the pod is under genuine resource pressure (check `kubectl top pod -n ricxapp`, and the node's overall memory with `free -h` — a small VM building Docker images and running the RIC platform at the same time can get tight).
- **Sometimes gNBs just don't register even though the platform looks healthy**: this can happen while some Near-RT RIC components are still finishing their own startup routines right after `redeploy_ric.sh`. Stop and restart `nori-cmf`, or call `bash resubscribe.sh` again once the E2 nodes show up in `kubectl logs` on `e2mgr`.

## Observability

If `self.save_influx = True` (the default, see `src/custom_xapp.py`), every indication is written to InfluxDB in two measurements:

- `cell_metrics` — the raw KPM dump (same shape as `xapp-nori`'s), one field per `pmType` reported by the cell;
- `mro_control` — one point per cell per indication with `ho_total`, `pp_total`, `rlf_total`, `pp_ratio`, `rlf_ratio`, `current_hysteresis_db`, `current_ttt_s`, `new_hysteresis_db`, `new_ttt_s` and (when reported) `current_cio_db` — everything needed to plot the MRO control loop over time on Grafana.
