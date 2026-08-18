# Imports form local libraries
from .asn1_defs.e2sm_kpm_rc import E2SM_KPM_RC
from .asn1_defs.e2ap_2_3 import E2AP_PDU_Descriptions

# Imports from OSC libraries
from ricxappframe.xapp_frame import RMRXapp, rmr
from mdclogpy import Logger, Level
from ricxappframe import xapp_rest, xapp_subscribe
from ricxappframe.entities.rnib.nb_identity_pb2 import NbIdentity

# Imports from other libraries
from threading import Thread
import signal
import json
import math
import requests
from typing import Dict, List, Optional, Tuple
import datetime
from influxdb_client import InfluxDBClient, Point, WritePrecision


# ---------------------------------------------------------------------------
# MRO decision tables
#
# These mirror, value for value, the tables PingPongRatioToTtt() and
# RlfRatioToHysteresis() of the nori-cmf.cc ns-3 example
# (contrib/nori/examples/nori-cmf.cc), which in turn reproduce the tables
# published in https://github.com/czezy/O-RAN_CMF_CM2023. Keeping both
# implementations bit-for-bit identical is what lets this xApp and the
# built-in MRO emulation of nori-cmf.cc converge to the same decision for the
# same observed ratios, whichever one is actually driving a given cell.
# ---------------------------------------------------------------------------

# Ratio buckets are 1/15th wide; the last bucket (ratio == 100%) is the one
# extra step inferred from the reference CSVs, see nori-cmf.cc for details.
_TTT_TABLE = [
    0.08, 0.08, 0.08, 0.08,  # 0.00% - 26.67%
    0.1,                     # 26.67% - 33.33%
    0.128,                   # 33.33% - 40.00%
    0.16,                    # 40.00% - 46.67%
    0.256,                   # 46.67% - 53.33%
    0.32,                    # 53.33% - 60.00%
    0.48,                    # 60.00% - 66.67%
    0.512,                   # 66.67% - 73.33%
    0.64,                    # 73.33% - 80.00%
    1.024,                   # 80.00% - 86.67%
    1.28,                    # 86.67% - 93.33%
    2.56,                    # 93.33% - 100.00%
    5.12,                    # ratio == 100.00%
]


def pingpong_ratio_to_ttt(ratio: float) -> float:
    """Ping-pong-to-handover ratio -> Time-To-Trigger [s]."""
    idx = min(max(int(math.floor(ratio * 15.0)), 0), 15)
    return _TTT_TABLE[idx]


def rlf_ratio_to_hysteresis(ratio: float) -> float:
    """RLF-to-handover ratio -> hysteresis [dB]. 0-15% -> 1 dB, then +0.5 dB every 5%."""
    idx = min(max(int(math.floor(ratio * 20.0)), 0), 20)
    return 1.0 + 0.5 * max(0, idx - 2)


# RAN parameter IDs, matching the RicControlCallback() convention in nori-cmf.cc.
RAN_PARAM_CIO = 1  # HOMeasurementOffset, written by the MLB xApp (read-only here)
RAN_PARAM_HYSTERESIS = 2  # HOHysteresis, written by this xApp
RAN_PARAM_TTT = 3  # HOTimeToTrigger, written by this xApp

# ricRequestorID used for control requests issued by this xApp. Distinct from
# the TS/QoS/RAN_SLICING IDs (1001/1002/1003) already used by other nori
# control paths; not otherwise interpreted by nori-cmf.cc, which decodes the
# control message content regardless of this value.
MRO_REQUESTOR_ID = 1004

# Decisions are only re-sent when they differ from the last value reported by
# the cell by more than this tolerance, to avoid flooding RMR with no-op
# control requests every reporting period.
DECISION_EPSILON = 1e-6


class XappMro:
    """
    MRO (Mobility Robustness Optimization) xApp for the NORI ns-3 module.

    Subscribes to the KPM RAN function (200) of every registered E2 node. On
    each RIC Indication, it reads the per-cell handover/ping-pong/RLF counters
    exposed by the nori-cmf.cc scenario, computes the same Hysteresis and
    Time-To-Trigger decisions the scenario's built-in MRO emulation would
    compute, and-when they differ from what the cell currently has-writes them
    back as a RIC Control Request on the RAN Control function (300).

    This xApp only ever touches HOHysteresis and HOTimeToTrigger. It never
    writes the Cell Individual Offset (HOMeasurementOffset): that parameter is
    the MLB xApp's responsibility. Because both parameters live in the same
    "CellAffectHandoverBoundary" group, running this xApp alongside an MLB
    xApp against nori-cmf.cc exercises the Conflict Mitigation Framework (CMF)
    exactly as described in https://github.com/czezy/O-RAN_CMF_CM2023 and in
    contrib/nori/docs/nori-cmf.md.
    """

    def __init__(self):
        """
        Initializes the custom xApp instance and instatiates the xApp framework object.
        """

        self.save_influx = True  # If true it saves the data in InfluxDB

        # Initializing a logger for the custom xApp instance in Debug level (logs everything)
        self.logger = Logger(
            name="XappMro", level=Level.DEBUG
        )  # The name is included in each log entry, Levels: DEBUG < INFO < WARNING < ERROR
        self.logger.info("Initializing the MRO xApp.")

        # Initializing custom control variables
        self._shutdown = False  # Stops the xApp loop if True
        self._ready = False  # True when the xApp is ready to start
        self.subscription_responses: Dict[int, Dict] = (
            {}
        )  # Stores the subscription responses for each E2 node inventory name
        self.sub_id_to_node: Dict[str, str] = (
            {}
        )  # Maps subscription IDs to E2 node inventory names

        # Instatiating the xApp framework object
        self._rmrxapp = RMRXapp(
            default_handler=self.default_rmr_handler,  # Called when no specific handler is found for an RMR message
            config_handler=self.config_change_handler,  # Called when a config change event is detected by inotify
            post_init=self.post_init,  # Called during the RMRXapp initialization, right after _BaseXapp is initialized
            rmr_port=4560,  # Port for RMR data
            rmr_wait_for_ready=True,  # Block xApp initiation until RMR is ready
            use_fake_sdl=False,  # Use a fake in-memory SDL
        )

        # Registering RMR message handlers
        self._rmrxapp.register_callback(
            handler=self.ric_indication_handler, message_type=12050
        )

        # Registering a handler for terminating the xApp after TERMINATE, QUIT, or INTERRUPT signals
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGQUIT, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Starting a threaded HTTP server listening to any host at port 8080
        self.http_server = xapp_rest.ThreadedHTTPServer("0.0.0.0", 8080)
        self.http_server.handler.add_handler(
            self.http_server.handler,
            method="GET",
            name="config",
            uri="/ric/v1/config",
            callback=self.config_handler,
        )
        self.http_server.handler.add_handler(
            self.http_server.handler,
            method="GET",
            name="liveness",
            uri="/ric/v1/health/alive",
            callback=self.liveness_handler,
        )
        self.http_server.handler.add_handler(
            self.http_server.handler,
            method="GET",
            name="readiness",
            uri="/ric/v1/health/ready",
            callback=self.readiness_handler,
        )
        self.http_server.handler.add_handler(
            self.http_server.handler,
            method="POST",
            name="sub_resp",
            uri="/ric/v1/subscriptions/response",
            callback=self.subscription_response_handler,
        )
        self.http_server.handler.add_handler(
            self.http_server.handler,
            method="GET",
            name="resubscribe",
            uri="/ric/v1/resubscribe",
            callback=self.resubscribe_handler,
        )
        self.logger.info("Starting HTTP server.")
        self.http_server.start()

        # xApp is ready to start
        self._ready = True
        self.logger.info("xApp is ready.")

        # InfluxDB client
        if self.save_influx:
            url = "http://influxdb-influxdb2.influxdb.svc.cluster.local:8086"  # URL of your InfluxDB instance
            token = "admin"  # your InfluxDB token
            org = "openranbr"  # your InfluxDB organization name
            self.bucket = "openranbr"  # your InfluxDB bucket name
            self.client = InfluxDBClient(url=url, token=token, org=org)
            self.write_api = self.client.write_api()

    # ------------------ START AND STOP

    def start(self):
        """
        Starts the xApp loop.
        """

        self.log_gnbs()
        Thread(target=self.subscribe_to_e2_nodes).start()
        self._rmrxapp.run()

    def stop(self):
        """
        Terminates the xApp. Can only be called if the xApp is running in threaded mode.
        """
        self._shutdown = True
        self.unsubscribe_from_e2_nodes()
        self.logger.info(
            "Calling framework termination to unregister the xApp from AppMgr."
        )
        self._rmrxapp.stop()
        self.http_server.stop()

    # ------------------ RMRXAPP INTERNAL FUNCTIONS

    def config_change_handler(self, rmrxapp: RMRXapp, json: dict):
        """
        Handler for the config change event.
        """
        self.logger.info("Detected a config change event.")

        rmrxapp._config_data = json
        self.logger.debug("New config data: {}.".format(json))

    def post_init(self, rmrxapp: RMRXapp):
        """
        Post initialization function.
        """
        self.logger.info("Post initialization called.")

    # ------------------ E2 NODES

    def log_gnbs(self):
        """
        Logs which gNBs are currently registered at the Near-RT RIC.
        """

        nbid_list = self._rmrxapp.GetListNodebIds()
        if len(nbid_list) == 0:
            self.logger.info("No gNBs registered.")
            return
        for nbid in nbid_list:
            self.logger.info(
                f"Registered gNB: {nbid.inventory_name}, connection status: {nbid.connection_status}"
            )

    def subscribe_to_e2_nodes(self):
        """
        Subscribes to all available E2 nodes.
        """
        e2_nodes = self._rmrxapp.GetListNodebIds()
        sub_trs_id = self._rmrxapp.sdl_get(
            namespace="xappmro", key="subscription_transaction_id"
        )
        if sub_trs_id is None:
            sub_trs_id = 54321
        for node in e2_nodes:
            self.logger.info(
                f"Subscribing to node {node.inventory_name}"
            )  # We use the inventory name as the node ID

            # Sending the subscription request
            subscription_req = self.generate_subscription_request(
                node.inventory_name, sub_trs_id
            )
            self.logger.debug(f"Subscription request: {subscription_req}")
            # A bounded timeout keeps one slow/unreachable E2 node from
            # stalling this whole loop (and, with it, the HTTP thread serving
            # the liveness probe) for an unbounded time.
            try:
                resp = requests.post(
                    "http://service-ricplt-submgr-http.ricplt.svc.cluster.local:8088/ric/v1/subscriptions",
                    json=subscription_req,
                    timeout=10,
                )
            except requests.exceptions.RequestException as exc:
                self.logger.warning(
                    f"Subscribe request to node {node.inventory_name} failed: {exc}"
                )
                continue
            status = resp.status_code
            reason = resp.reason

            # Handling the subscription response
            if int(resp.status_code / 100) != 2:
                self.logger.error(
                    f"Failed to subscribe to node {node.inventory_name}. Status code: {resp.status_code}, reason: {resp.reason}"
                )
                continue
            data = (
                resp.json()
            )  # {"SubscriptionId": "my_string_id", "SubscriptionInstances": null}
            self.sub_id_to_node[data["SubscriptionId"]] = node.inventory_name
            self.subscription_responses[node.inventory_name] = data
            self.logger.debug(
                f"Subscription response from {node.inventory_name}: status = {status}, reason = {reason}, data = {data}"
            )
            self._rmrxapp.sdl_set(
                namespace="xappmro",
                key="subscription_transaction_id",
                value=sub_trs_id + 1,
            )  # Update sub_trs_id on SDL

    def unsubscribe_from_e2_nodes(self):
        """
        Unsubscribes from all subscribed E2 nodes (stored in the self.subscription_responses dict).
        """

        for sub_id in list(self.sub_id_to_node.keys()):
            try:
                resp = requests.delete(
                    f"http://service-ricplt-submgr-http.ricplt.svc.cluster.local:8088/ric/v1/subscriptions/{sub_id}",
                    timeout=10,
                )
                self.logger.info(
                    f"Unsubscribe from sub id {sub_id}: status = {resp.status_code}, reason = {resp.reason}"
                )
            except requests.exceptions.RequestException as exc:
                self.logger.warning(f"Unsubscribe from sub id {sub_id} failed: {exc}")

        # Drop bookkeeping for every subscription just torn down, otherwise it
        # accumulates unbounded across repeated resubscribe_to_e2_nodes() calls
        # and a late async response for a since-forgotten id can still be
        # looked up against a stale entry.
        self.sub_id_to_node.clear()
        self.subscription_responses.clear()

    def resubscribe_to_e2_nodes(self):
        """
        Resubscribes to all E2 nodes.
        """
        self.unsubscribe_from_e2_nodes()
        self.subscribe_to_e2_nodes()

    # ------------------ SIGNAL HANDLERS

    def _handle_signal(self, signum: int, frame):
        """
        Function called when a Kubernetes signal is received to stop the xApp execution.
        """
        self.logger.info(
            "Received signal {} to stop the xApp.".format(signal.Signals(signum).name)
        )
        self.stop()  # Custom xApp termination routine

    # ------------------ RMR MESSAGE HANDLERS

    def default_rmr_handler(self, rmrxapp: RMRXapp, summary: dict, sbuf):
        """
        Default RMR message handler.
        """
        self.logger.info("Received RMR message with summary: {}.".format(summary))
        rmrxapp.rmr_free(sbuf)  # Freeing the RMR message buffer

    def ric_indication_handler(self, rmrxapp: RMRXapp, summary: dict, sbuf):
        """
        Handler for RIC indication messages. Runs the MRO decision loop for the
        cell that sent this indication and, if the outcome differs from the
        cell's current parameters, replies with a RIC Control Request on the
        same route (rmr_rts), targeting that same cell.
        """

        msg = summary["payload"]

        # Decoding the E2AP PDU data
        pdu = E2AP_PDU_Descriptions.E2AP_PDU
        pdu.from_aper(msg)
        decoded_pdu = pdu.get_val()
        e2pdu_data = {
            "ricRequestorID": decoded_pdu[1]["value"][1]["protocolIEs"][0]["value"][1][
                "ricRequestorID"
            ],
            "ricInstanceID": decoded_pdu[1]["value"][1]["protocolIEs"][0]["value"][1][
                "ricInstanceID"
            ],
            "RANfunctionID": decoded_pdu[1]["value"][1]["protocolIEs"][1]["value"][1],
            "RICactionID": decoded_pdu[1]["value"][1]["protocolIEs"][2]["value"][1],
            "RICindicationSN": decoded_pdu[1]["value"][1]["protocolIEs"][3]["value"][1],
            "RICindicationType": decoded_pdu[1]["value"][1]["protocolIEs"][4]["value"][
                1
            ],
            "RICcallProcessID": decoded_pdu[1]["value"][1]["protocolIEs"][7]["value"][
                1
            ],
        }

        # Decoding the E2SM RIC Indication data
        ric_indication_header_msg = decoded_pdu[1]["value"][1]["protocolIEs"][5][
            "value"
        ][1]
        ric_indication_message_msg = decoded_pdu[1]["value"][1]["protocolIEs"][6][
            "value"
        ][1]
        ric_indication_header = E2SM_KPM_RC.E2SM_KPM_IndicationHeader
        ric_indication_message = E2SM_KPM_RC.E2SM_KPM_IndicationMessage
        ric_indication_header.from_aper(ric_indication_header_msg)
        ric_indication_message.from_aper(ric_indication_message_msg)
        decoded_ric_indication_header = ric_indication_header.get_val()
        decoded_ric_indication_message = ric_indication_message.get_val()
        ric_indication_data = {
            "indicationHeader": decoded_ric_indication_header,
            "indicationMessage": decoded_ric_indication_message,
        }

        # Send information to InfluxDB (best-effort, does not affect the control loop)
        if self.save_influx:
            try:
                self.send_influxdb_data(ric_indication_data)
            except Exception as exc:  # noqa: BLE001 - never let telemetry break control
                self.logger.warning(f"Failed to write to InfluxDB: {exc}")

        # ---------------- MRO decision loop ----------------
        self.run_mro_step(ric_indication_data, rmrxapp, sbuf)

        rmrxapp.rmr_free(sbuf)

    # ------------------ MRO DECISION LOOP

    def _extract_cell_pm(self, ric_indication_data: dict) -> Tuple[str, Dict[str, object]]:
        """
        Returns (cellObjectID, {pmType: pmVal}) from a decoded KPM indication.
        """
        msg = ric_indication_data["indicationMessage"][1]
        cell_object_id = msg.get("cellObjectID", "")
        pm: Dict[str, object] = {}
        for pm_info in msg.get("list-of-PM-Information", []):
            pm_type = pm_info["pmType"][1]
            _, pm_val = pm_info["pmVal"]
            pm[pm_type] = pm_val
        return cell_object_id, pm

    def run_mro_step(self, ric_indication_data: dict, rmrxapp: RMRXapp, sbuf):
        """
        Reads the handover/ping-pong/RLF counters and the currently active
        Hysteresis/TTT reported by the cell, computes the MRO decision for
        this control period, and sends a RIC Control Request back to the same
        cell only when the decision differs from what is already active.
        """
        cell_object_id, pm = self._extract_cell_pm(ric_indication_data)
        if not pm:
            return  # Not a cell-level KPM indication (e.g. a CU-UP/CU-CP report)

        ho_total = int(pm.get("HO.TotNbrOut", 0) or 0)
        pp_total = int(pm.get("HO.PingPongNbrOut", 0) or 0)
        rlf_total = int(pm.get("RRC.ReEstabAtt.RLF", 0) or 0)

        hyst_milli = pm.get("MRO.HysteresisMilliDb")
        ttt_ms = pm.get("MRO.TimeToTriggerMs")
        current_hyst = hyst_milli / 1000.0 if hyst_milli is not None else None
        current_ttt = ttt_ms / 1000.0 if ttt_ms is not None else None

        pp_ratio = min(1.0, pp_total / ho_total) if ho_total > 0 else 0.0
        rlf_ratio = min(1.0, rlf_total / ho_total) if ho_total > 0 else 0.0

        new_hyst = rlf_ratio_to_hysteresis(rlf_ratio)
        new_ttt = pingpong_ratio_to_ttt(pp_ratio)

        decisions: List[Tuple[int, float]] = []
        if current_hyst is None or abs(new_hyst - current_hyst) > DECISION_EPSILON:
            decisions.append((RAN_PARAM_HYSTERESIS, new_hyst))
        if current_ttt is None or abs(new_ttt - current_ttt) > DECISION_EPSILON:
            decisions.append((RAN_PARAM_TTT, new_ttt))

        if self.save_influx:
            try:
                self.send_mro_influxdb_point(
                    cell_object_id,
                    ho_total,
                    pp_total,
                    rlf_total,
                    pp_ratio,
                    rlf_ratio,
                    current_hyst,
                    current_ttt,
                    new_hyst,
                    new_ttt,
                    pm.get("MLB.CioMilliDb"),
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Failed to write MRO point to InfluxDB: {exc}")

        if not decisions:
            return

        self.logger.info(
            f"Cell {cell_object_id}: ho={ho_total} pp_ratio={pp_ratio:.1%} "
            f"rlf_ratio={rlf_ratio:.1%} -> hysteresis={new_hyst}dB ttt={new_ttt}s "
            f"(was hysteresis={current_hyst} ttt={current_ttt})"
        )

        coded_pdu = self.build_ric_control_pdu(decisions)
        rmrxapp.rmr_rts(sbuf, new_payload=coded_pdu, new_mtype=12040)  # RIC Control Request

    def build_ric_control_pdu(self, decisions: List[Tuple[int, float]]) -> bytes:
        """
        Encodes an E2AP RIC Control Request PDU carrying an
        E2SM-RC-ControlMessage-Format1 whose ranParameters-List holds one
        RANParameter-Item per (parameter_id, value) decision. Values are sent
        in milli-units (milli-dB for hysteresis, milliseconds for TTT), matching
        the convention decoded by RicControlCallback() in nori-cmf.cc.
        """
        # RIC Control Header: mandatory but not consumed by nori-cmf.cc for
        # this parameter path, so ueId/style/action are harmless placeholders.
        # rrmPolicyList is intentionally omitted (it is OPTIONAL and only used
        # by the RAN-slicing control path).
        asn1_control_header = E2SM_KPM_RC.E2SM_RC_ControlHeader
        control_header = (
            "controlHeader-Format1",
            {
                "ueId": b"\x00\x00",
                "ric-ControlStyle-Type": 1,
                "ric-ControlAction-ID": 1,
            },
        )
        asn1_control_header.set_val(control_header)
        coded_control_header = asn1_control_header.to_aper()

        # RIC Control Message: one RANParameter-Item per decision.
        ran_parameters = [
            {
                "ranParameterItem-ID": param_id,
                "ranParameterItem-valueType": (
                    "ranParameter-Element",
                    {
                        "keyFlag": False,
                        "ranParameter-Value": ("valueInt", int(round(value * 1000.0))),
                    },
                ),
            }
            for param_id, value in decisions
        ]
        asn1_control_message = E2SM_KPM_RC.E2SM_RC_ControlMessage
        asn1_control_message.set_val(
            ("controlMessage-Format1", {"ranParameters-List": ran_parameters})
        )
        coded_control_message = asn1_control_message.to_aper()

        asn1_pdu = E2AP_PDU_Descriptions.E2AP_PDU
        ric_request_msg = (
            "initiatingMessage",
            {
                "procedureCode": 4,
                "criticality": "ignore",
                "value": (
                    "RICcontrolRequest",
                    {
                        "protocolIEs": [
                            {
                                "id": 29,
                                "criticality": "reject",
                                "value": (
                                    "RICrequestID",
                                    {
                                        "ricRequestorID": MRO_REQUESTOR_ID,
                                        "ricInstanceID": 1,
                                    },
                                ),
                            },
                            {
                                "id": 5,
                                "criticality": "reject",
                                "value": ("RANfunctionID", 300),
                            },
                            {
                                "id": 20,
                                "criticality": "reject",
                                "value": ("RICcallProcessID", b"\x00\x01"),
                            },
                            {
                                "id": 22,
                                "criticality": "reject",
                                "value": ("RICcontrolHeader", coded_control_header),
                            },
                            {
                                "id": 23,
                                "criticality": "reject",
                                "value": ("RICcontrolMessage", coded_control_message),
                            },
                        ]
                    },
                ),
            },
        )
        asn1_pdu.set_val(ric_request_msg)
        return asn1_pdu.to_aper()

    # ------------------ INFLUXDB

    def send_influxdb_data(self, data: dict):
        points = []

        # Common tags
        collection_start_time = datetime.datetime.now(datetime.timezone.utc)

        # Extract cellObjectID
        cell_object_id = data["indicationMessage"][1]["cellObjectID"]

        # Cell-level PM info
        if "list-of-PM-Information" in data["indicationMessage"][1]:
            for pm_info in data["indicationMessage"][1]["list-of-PM-Information"]:
                pm_type = pm_info["pmType"][1]
                pm_val_type, pm_val = pm_info["pmVal"]

                p = (
                    Point("cell_metrics")
                    .tag("cellObjectID", cell_object_id)
                    .field(pm_type, pm_val)
                    .time(collection_start_time, WritePrecision.NS)
                )
                points.append(p)

        if points:
            self.write_api.write(bucket=self.bucket, record=points)

    def send_mro_influxdb_point(
        self,
        cell_object_id: str,
        ho_total: int,
        pp_total: int,
        rlf_total: int,
        pp_ratio: float,
        rlf_ratio: float,
        current_hyst: Optional[float],
        current_ttt: Optional[float],
        new_hyst: float,
        new_ttt: float,
        cio_milli: Optional[int],
    ):
        """
        Records one MRO decision-loop sample per cell per indication, so the
        control loop can be inspected on Grafana independently of the raw KPM
        dump written by send_influxdb_data().
        """
        p = (
            Point("mro_control")
            .tag("cellObjectID", cell_object_id)
            .field("ho_total", ho_total)
            .field("pp_total", pp_total)
            .field("rlf_total", rlf_total)
            .field("pp_ratio", pp_ratio)
            .field("rlf_ratio", rlf_ratio)
            .field("new_hysteresis_db", new_hyst)
            .field("new_ttt_s", new_ttt)
        )
        if current_hyst is not None:
            p = p.field("current_hysteresis_db", current_hyst)
        if current_ttt is not None:
            p = p.field("current_ttt_s", current_ttt)
        if cio_milli is not None:
            p = p.field("current_cio_db", cio_milli / 1000.0)
        p = p.time(datetime.datetime.now(datetime.timezone.utc), WritePrecision.NS)
        self.write_api.write(bucket=self.bucket, record=[p])

    # ------------------ HTTP HANDLERS

    def resubscribe_handler(self, name: str, path: str, data: bytes, ctype: str):
        """
        Handler for the HTTP GET /ric/v1/resubscribe request.
        """
        self.logger.info(
            "Received GET /ric/v1/resubscribe request, resubscribing to E2 Nodes"
        )
        # Dispatched in a background thread, the same way the initial
        # subscribe_to_e2_nodes() call is in start(): with many stale/
        # unresponsive E2 nodes registered at the RIC, a full unsubscribe+
        # subscribe pass can take well over a minute even with a bounded
        # per-request timeout, which would otherwise stall this very HTTP
        # handler thread long enough for the liveness probe to time out and
        # get the pod killed.
        Thread(target=self.resubscribe_to_e2_nodes, daemon=True).start()
        response = xapp_rest.initResponse(
            status=200,  # Status = 200 OK
            response="xApp ACKs resubscription request, resubscribing in the background",
        )  # Initiating HTTP response
        return response

    def subscription_response_handler(
        self, name: str, path: str, data: bytes, ctype: str
    ):
        """
        Handler for the HTTP POST /ric/v1/subscriptions/response request.
        """
        sub_resp = json.loads(data.decode())
        self.logger.info(
            "Received POST /ric/v1/subscriptions/response request with data {}.".format(
                sub_resp
            )
        )
        # submgr retries/resends this callback (e.g. while an E2 node is slow
        # to answer); a retry can arrive after resubscribe_to_e2_nodes() has
        # already rebuilt sub_id_to_node, so the SubscriptionId may no longer
        # be known here. Still ACK it (submgr would otherwise keep retrying
        # forever), just skip recording it.
        nodeb = self.sub_id_to_node.get(sub_resp["SubscriptionId"])
        if nodeb is None:
            self.logger.warning(
                f"Subscription response for unknown/stale SubscriptionId {sub_resp['SubscriptionId']}, ignoring."
            )
        else:
            self.subscription_responses[nodeb]["SubscriptionInstances"] = sub_resp[
                "SubscriptionInstances"
            ]
        response = xapp_rest.initResponse(
            status=200,  # Status = 200 OK
            response="xApp ACKs the subscription response",
        )  # Initiating HTTP response
        return response

    def config_handler(self, name: str, path: str, data: bytes, ctype: str):
        """
        Handler for the HTTP GET /ric/v1/config request.
        """
        response = xapp_rest.initResponse(
            status=200, response="Config data"  # Status = 200 OK
        )  # Initiating HTTP response
        response["payload"] = json.dumps(
            self._rmrxapp._config_data
        )  # Payload = the xApp config-file
        return response

    def liveness_handler(self, name: str, path: str, data: bytes, ctype: str):
        """
        Handler for the HTTP GET /ric/v1/health/alive request.
        """
        response = xapp_rest.initResponse(
            status=200, response="Liveness"  # Status = 200 OK
        )  # Initiating HTTP response
        response["payload"] = json.dumps(
            {"status": "Healthy"}
        )  # Payload = status: Healthy
        return response

    def readiness_handler(self, name: str, path: str, data: bytes, ctype: str):
        """
        Handler for the HTTP GET /ric/v1/health/ready request.
        """
        if self._ready:
            response = xapp_rest.initResponse(
                status=200, response="Readiness"  # Status = 200 OK
            )  # Initiating HTTP response
            response["payload"] = json.dumps(
                {"status": "Ready"}
            )  # Payload = status: Healthy
        else:
            response = xapp_rest.initResponse(
                status=503, response="Readiness"  # Status = 503 Service Unavailable
            )
            response["payload"] = json.dumps({"status": "Not ready"})
        return response

    # ------------------ SUBSCRIPTION REQUEST JSON

    # Hard coded as workaround for the wrong keys in the SubscriptionParams object
    def generate_subscription_request(
        self, inventory_name, subscription_transaction_id
    ):
        return {
            "SubscriptionId": "",
            "ClientEndpoint": {
                "Host": "service-ricxapp-xappmro-http.ricxapp",
                "HTTPPort": 8080,
                "RMRPort": 4560,
            },
            "Meid": inventory_name,  # nobe B inventory_name
            "RANFunctionID": 200,
            "E2SubscriptionDirectives": {  # Optional
                "E2TimeoutTimerValue": 2,  # Default = 2
                "E2RetryCount": 2,  # Default = 2
                "RMRRoutingNeeded": True,  # Default = True
            },
            "SubscriptionDetails": [  # Can make multiple subscriptions
                {
                    "XappEventInstanceId": subscription_transaction_id,  # "Transaction id"
                    "EventTriggers": [],
                    "ActionToBeSetupList": [
                        {
                            "ActionID": 0,
                            "ActionType": "report",
                            "ActionDefinition": [],
                            "SubsequentAction": {
                                "SubsequentActionType": "continue",
                                "TimeToWait": "w10ms",  # Default = "zero"
                            },
                        }
                    ],
                }
            ],
        }
