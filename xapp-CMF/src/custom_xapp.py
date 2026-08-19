# Imports form local libraries
from .asn1_defs.e2sm_kpm_rc import E2SM_KPM_RC
from .asn1_defs.e2ap_2_3 import E2AP_PDU_Descriptions
from .cd_agent import ConflictDetectionAgent, ControlRecord
from .cr_agent import ConflictResolutionAgent, CrMode
from .pmon import PerformanceMonitor

# Imports from OSC libraries
from ricxappframe.xapp_frame import RMRXapp, rmr
from mdclogpy import Logger, Level
from ricxappframe import xapp_rest, xapp_subscribe
from ricxappframe.entities.rnib.nb_identity_pb2 import NbIdentity

# Imports from other libraries
from threading import Thread
import signal
import json
import time
import requests
from typing import Dict, Set
import datetime
from influxdb_client import InfluxDBClient, Point, WritePrecision


class XappCmf:
    """
    Conflict Mitigation Framework (CMF) xApp for the NORI ns-3 module,
    implementing the framework of Adamczyk & Kliks, "Conflict Mitigation
    Framework and Conflict Detection in O-RAN Near-RT RIC" (IEEE ComMag 2023,
    https://arxiv.org/abs/2305.07117), as a standalone xApp on the Near-RT
    RIC rather than embedded in the ns-3 scenario.

    It is made of the three components the paper places in the Near-RT RIC's
    Conflict Mitigation (CM) entity:
      - the Conflict Detection (CD) Agent (cd_agent.py), implementing Direct
        (DCD) and Indirect (ICD) Conflict Detection, both pre-action;
      - the Conflict Resolution (CR) Agent (cr_agent.py), which decides
        whether a conflicting proposal is allowed to take effect;
      - the Performance Monitoring (PMon) component (pmon.py), feeding
        Implicit Conflict Detection (ImCD), which is inherently post-action.

    Unlike a plain KPM/RIC-Control xApp, this one sits *in front of* the
    MRO and MLB xApps' control path: before either of them sends a RIC
    Control Request to a cell, it POSTs the proposed decision to this xApp's
    HTTP endpoint (/ric/v1/cmf/evaluate). This xApp is the only place a
    decision can be blocked from ever reaching the RAN - exactly what the
    paper's Fig. 2 depicts ("Message Infrastructure... redirect all control
    messages from xApps into the CM component"). A real RMR-level interposition
    (rerouting message type RIC_CONTROL_REQ itself) is not practical with the
    reply-to-sender pattern used to target a specific E2 node (see the
    project README for the reasoning); a synchronous HTTP call achieves the
    same net effect - the RAN is never reached before this xApp is asked -
    without fighting RMR's static routing tables.

    Independently, this xApp also subscribes to the KPM RAN function (200)
    on every E2 node, purely to feed PMon with the per-cell satisfaction KPI
    it needs for ImCD.
    """

    # Conflict resolution policy applied by the CR Agent. One of CrMode.NONE
    # (detect only, matching the paper's "CM disabled" baseline), CrMode.PRIO_MRO
    # or CrMode.PRIO_MLB (matching the paper's two prioritization modes).
    CM_MODE = CrMode.PRIO_MRO

    def __init__(self):
        """
        Initializes the custom xApp instance and instatiates the xApp framework object.
        """

        self.save_influx = True  # If true it saves the data in InfluxDB

        self.logger = Logger(name="XappCmf", level=Level.DEBUG)
        self.logger.info(f"Initializing the CMF xApp (CR mode: {self.CM_MODE.value}).")

        self._shutdown = False
        self._ready = False
        self.subscription_responses: Dict[int, Dict] = {}
        self.sub_id_to_node: Dict[str, str] = {}

        # --- the three CMF components ---
        self.cd_agent = ConflictDetectionAgent()
        self.cr_agent = ConflictResolutionAgent(mode=self.CM_MODE)
        self.pmon = PerformanceMonitor(
            cd_agent=self.cd_agent,
            report_callback=self._on_implicit_conflict,
        )

        self._conflict_log_path = "/tmp/conflicts.json"
        self._conflict_log = open(self._conflict_log_path, "a", buffering=1)

        self._rmrxapp = RMRXapp(
            default_handler=self.default_rmr_handler,
            config_handler=self.config_change_handler,
            post_init=self.post_init,
            rmr_port=4560,
            rmr_wait_for_ready=True,
            use_fake_sdl=False,
        )

        self._rmrxapp.register_callback(
            handler=self.ric_indication_handler, message_type=12050
        )

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGQUIT, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

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
        # The one endpoint that actually makes this a Conflict Mitigation
        # Framework rather than just another KPM xApp: MRO/MLB call this
        # *before* sending a RIC Control Request, and only proceed if allowed.
        self.http_server.handler.add_handler(
            self.http_server.handler,
            method="POST",
            name="cmf_evaluate",
            uri="/ric/v1/cmf/evaluate",
            callback=self.cmf_evaluate_handler,
        )
        self.logger.info("Starting HTTP server.")
        self.http_server.start()

        self._ready = True
        self.logger.info("xApp is ready.")

        if self.save_influx:
            url = "http://influxdb-influxdb2.influxdb.svc.cluster.local:8086"
            token = "admin"
            org = "openranbr"
            self.bucket = "openranbr"
            self.client = InfluxDBClient(url=url, token=token, org=org)
            self.write_api = self.client.write_api()

    # ------------------ START AND STOP

    def start(self):
        self.log_gnbs()
        Thread(target=self.subscribe_to_e2_nodes).start()
        self._rmrxapp.run()

    def stop(self):
        self._shutdown = True
        self.unsubscribe_from_e2_nodes()
        self.logger.info(
            "Calling framework termination to unregister the xApp from AppMgr."
        )
        self._rmrxapp.stop()
        self.http_server.stop()
        self._conflict_log.close()

    # ------------------ RMRXAPP INTERNAL FUNCTIONS

    def config_change_handler(self, rmrxapp: RMRXapp, json_cfg: dict):
        self.logger.info("Detected a config change event.")
        rmrxapp._config_data = json_cfg

    def post_init(self, rmrxapp: RMRXapp):
        self.logger.info("Post initialization called.")

    # ------------------ E2 NODES (KPM subscription, feeding PMon only)

    def log_gnbs(self):
        nbid_list = self._rmrxapp.GetListNodebIds()
        if len(nbid_list) == 0:
            self.logger.info("No gNBs registered.")
            return
        for nbid in nbid_list:
            self.logger.info(
                f"Registered gNB: {nbid.inventory_name}, connection status: {nbid.connection_status}"
            )

    def subscribe_to_e2_nodes(self):
        e2_nodes = self._rmrxapp.GetListNodebIds()
        sub_trs_id = self._rmrxapp.sdl_get(
            namespace="xappcmf", key="subscription_transaction_id"
        )
        if sub_trs_id is None:
            sub_trs_id = 54321
        for node in e2_nodes:
            self.logger.info(f"Subscribing to node {node.inventory_name}")
            subscription_req = self.generate_subscription_request(
                node.inventory_name, sub_trs_id
            )
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
            if int(resp.status_code / 100) != 2:
                self.logger.error(
                    f"Failed to subscribe to node {node.inventory_name}. Status code: {resp.status_code}, reason: {resp.reason}"
                )
                continue
            data = resp.json()
            self.sub_id_to_node[data["SubscriptionId"]] = node.inventory_name
            self.subscription_responses[node.inventory_name] = data
            self._rmrxapp.sdl_set(
                namespace="xappcmf",
                key="subscription_transaction_id",
                value=sub_trs_id + 1,
            )

    def unsubscribe_from_e2_nodes(self):
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
        self.sub_id_to_node.clear()
        self.subscription_responses.clear()

    def resubscribe_to_e2_nodes(self):
        self.unsubscribe_from_e2_nodes()
        self.subscribe_to_e2_nodes()

    # ------------------ SIGNAL HANDLERS

    def _handle_signal(self, signum: int, frame):
        self.logger.info(
            "Received signal {} to stop the xApp.".format(signal.Signals(signum).name)
        )
        self.stop()

    # ------------------ RMR MESSAGE HANDLERS

    def default_rmr_handler(self, rmrxapp: RMRXapp, summary: dict, sbuf):
        self.logger.info("Received RMR message with summary: {}.".format(summary))
        rmrxapp.rmr_free(sbuf)

    def ric_indication_handler(self, rmrxapp: RMRXapp, summary: dict, sbuf):
        """
        The only thing this xApp does with a KPM indication is feed PMon: it
        never writes any RAN parameter itself.
        """
        msg = summary["payload"]

        pdu = E2AP_PDU_Descriptions.E2AP_PDU
        pdu.from_aper(msg)
        decoded_pdu = pdu.get_val()

        ric_indication_header_msg = decoded_pdu[1]["value"][1]["protocolIEs"][5]["value"][1]
        ric_indication_message_msg = decoded_pdu[1]["value"][1]["protocolIEs"][6]["value"][1]
        ric_indication_message = E2SM_KPM_RC.E2SM_KPM_IndicationMessage
        ric_indication_message.from_aper(ric_indication_message_msg)
        decoded_ric_indication_message = ric_indication_message.get_val()

        self._feed_pmon(decoded_ric_indication_message)

        rmrxapp.rmr_free(sbuf)

    def _feed_pmon(self, indication_message: tuple) -> None:
        msg = indication_message[1]
        cell_object_id = msg.get("cellObjectID", "")
        cell_id = _parse_cell_id(cell_object_id)
        if cell_id is None:
            return

        satisfaction_permille = None
        for pm_info in msg.get("list-of-PM-Information", []):
            if pm_info["pmType"][1] == "QoS.MeanUeSatisfactionPermille":
                _, satisfaction_permille = pm_info["pmVal"]
                break
        if satisfaction_permille is None:
            return

        self.pmon.observe(cell_id, satisfaction_permille / 1000.0, time.time())

    # ------------------ CMF: the actual conflict mitigation endpoint

    def cmf_evaluate_handler(self, name: str, path: str, data: bytes, ctype: str):
        """
        Handler for POST /ric/v1/cmf/evaluate. Body:
            {"source": "MRO"|"MLB", "cellId": <int>, "parameterId": 1|2|3, "value": <float>}
        Response:
            {"allowed": true}
            {"allowed": false, "reason": "..."}
        """
        try:
            body = json.loads(data.decode())
            record = ControlRecord(
                source=str(body["source"]),
                cell_id=int(body["cellId"]),
                parameter_id=int(body["parameterId"]),
                value=float(body["value"]),
                timestamp=time.time(),
            )
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.warning(f"Malformed /ric/v1/cmf/evaluate request: {exc}")
            response = xapp_rest.initResponse(status=400, response="Bad Request")
            response["payload"] = json.dumps({"allowed": False, "reason": "malformed request"})
            return response

        conflicts = self.cd_agent.evaluate(record)
        resolution = self.cr_agent.resolve(conflicts)

        for c in conflicts:
            self._log_conflict(c)

        if self.save_influx:
            try:
                self._send_evaluation_point(record, conflicts, resolution)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Failed to write CMF point to InfluxDB: {exc}")

        if conflicts:
            self.logger.info(
                f"Cell {record.cell_id}: {record.source} proposes "
                f"{record.parameter_name()}={record.value} -> "
                f"{'ALLOWED' if resolution.allowed else 'REJECTED'}"
                + (f" ({resolution.reason})" if resolution.reason else "")
            )

        response = xapp_rest.initResponse(status=200, response="Evaluated")
        payload = {"allowed": resolution.allowed}
        if resolution.reason:
            payload["reason"] = resolution.reason
        response["payload"] = json.dumps(payload)
        return response

    def _on_implicit_conflict(self, cell_id: int, sources: Set[str]) -> None:
        self.logger.info(
            f"ImCD: implicit conflict on cell {cell_id} between {sorted(sources)} "
            f"(correlated with a sustained drop in QoS.MeanUeSatisfactionPermille)"
        )
        line = {
            "source": "ImCD",
            "command": "Notify",
            "conflictType": "implicit",
            "timestamp": _iso_now(),
            "degradKPI": "QoS.MeanUeSatisfactionPermille",
            "targetCell": cell_id,
            "involvedSources": sorted(sources),
        }
        self._conflict_log.write(json.dumps(line) + "\n")
        if self.save_influx:
            try:
                p = (
                    Point("cmf_conflicts")
                    .tag("cellId", str(cell_id))
                    .tag("detector", "ImCD")
                    .field("conflictType", "implicit")
                    .field("sources", ",".join(sorted(sources)))
                    .time(datetime.datetime.now(datetime.timezone.utc), WritePrecision.NS)
                )
                self.write_api.write(bucket=self.bucket, record=[p])
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Failed to write ImCD point to InfluxDB: {exc}")

    def _log_conflict(self, conflict) -> None:
        # Same shape as json_messages/{DCD,ICD}/*signal conflict.json in the
        # reference repository (czezy/O-RAN_CMF_CM2023).
        line = {
            "source": conflict.detector,
            "command": "Notify",
            "conflictType": conflict.conflict_type,
            "timestamp": _iso_now(),
            "decisions": [
                {
                    "content": {
                        "source": conflict.proposal.source,
                        "command": "Modify",
                        "targetParameter": conflict.proposal.parameter_name(),
                        "targetValue": str(conflict.proposal.value),
                        "targetCell": conflict.proposal.cell_id,
                    }
                },
                {
                    "content": {
                        "source": conflict.existing.source,
                        "command": "Modify",
                        "targetParameter": conflict.existing.parameter_name(),
                        "targetValue": str(conflict.existing.value),
                        "targetCell": conflict.existing.cell_id,
                    }
                },
            ],
        }
        if conflict.group_name:
            line["groupName"] = conflict.group_name
        self._conflict_log.write(json.dumps(line) + "\n")

    def _send_evaluation_point(self, record: ControlRecord, conflicts, resolution) -> None:
        p = (
            Point("cmf_evaluations")
            .tag("cellId", str(record.cell_id))
            .tag("source", record.source)
            .tag("parameter", record.parameter_name())
            .field("value", record.value)
            .field("conflicts", len(conflicts))
            .field("allowed", resolution.allowed)
            .time(datetime.datetime.now(datetime.timezone.utc), WritePrecision.NS)
        )
        self.write_api.write(bucket=self.bucket, record=[p])

    # ------------------ HTTP HANDLERS (framework plumbing)

    def resubscribe_handler(self, name: str, path: str, data: bytes, ctype: str):
        self.logger.info(
            "Received GET /ric/v1/resubscribe request, resubscribing to E2 Nodes"
        )
        Thread(target=self.resubscribe_to_e2_nodes, daemon=True).start()
        response = xapp_rest.initResponse(
            status=200,
            response="xApp ACKs resubscription request, resubscribing in the background",
        )
        return response

    def subscription_response_handler(self, name: str, path: str, data: bytes, ctype: str):
        sub_resp = json.loads(data.decode())
        self.logger.info(
            "Received POST /ric/v1/subscriptions/response request with data {}.".format(sub_resp)
        )
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
            status=200, response="xApp ACKs the subscription response"
        )
        return response

    def config_handler(self, name: str, path: str, data: bytes, ctype: str):
        response = xapp_rest.initResponse(status=200, response="Config data")
        response["payload"] = json.dumps(self._rmrxapp._config_data)
        return response

    def liveness_handler(self, name: str, path: str, data: bytes, ctype: str):
        response = xapp_rest.initResponse(status=200, response="Liveness")
        response["payload"] = json.dumps({"status": "Healthy"})
        return response

    def readiness_handler(self, name: str, path: str, data: bytes, ctype: str):
        if self._ready:
            response = xapp_rest.initResponse(status=200, response="Readiness")
            response["payload"] = json.dumps({"status": "Ready"})
        else:
            response = xapp_rest.initResponse(status=503, response="Readiness")
            response["payload"] = json.dumps({"status": "Not ready"})
        return response

    # ------------------ SUBSCRIPTION REQUEST JSON

    def generate_subscription_request(self, inventory_name, subscription_transaction_id):
        return {
            "SubscriptionId": "",
            "ClientEndpoint": {
                "Host": "service-ricxapp-xappcmf-http.ricxapp",
                "HTTPPort": 8080,
                "RMRPort": 4560,
            },
            "Meid": inventory_name,
            "RANFunctionID": 200,
            "E2SubscriptionDirectives": {
                "E2TimeoutTimerValue": 2,
                "E2RetryCount": 2,
                "RMRRoutingNeeded": True,
            },
            "SubscriptionDetails": [
                {
                    "XappEventInstanceId": subscription_transaction_id,
                    "EventTriggers": [],
                    "ActionToBeSetupList": [
                        {
                            "ActionID": 0,
                            "ActionType": "report",
                            "ActionDefinition": [],
                            "SubsequentAction": {
                                "SubsequentActionType": "continue",
                                "TimeToWait": "w10ms",
                            },
                        }
                    ],
                }
            ],
        }


def _parse_cell_id(cell_object_id: str):
    # nori-cmf.cc names cells "NRCellDU_<id>".
    try:
        return int(cell_object_id.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d %H:%M:%S.%f")[:-3] + " " + str(
        datetime.datetime.now(datetime.timezone.utc).year
    )
