from asn1_defs.e2sm_kpm_rc import E2SM_KPM_RC
from asn1_defs.e2ap_2_3 import E2AP_PDU_Descriptions

# Encoding RIC Control Header
asn1_control_header = E2SM_KPM_RC.E2SM_RC_ControlHeader
ric_control_header = ('controlHeader-Format1', {
	"ueId":b'\x00\x01', 
	"ric-ControlStyle-Type": 1, 
	"ric-ControlAction-ID": 1, 
	"rrmPolicyList": [
		{
			"rrmPolicy": 
   			{
				"rrmPolicyMemberList": 
					[
						{
							"plmnIdentity": b'\x00\x01\x02',
							"sNSSAI": {
								"sST": b'\x00',
								"sD": b'\x00\x01\x03'
							}
						}
					]
			},
			"dedicatedPRBPolicyRatio": 20,
			"minPRBPolicyRatio": 40,
			"maxPRBPolicyRatio": 80,
		},
		{
			"rrmPolicy": 
   			{
				"rrmPolicyMemberList": 
					[
						{
							"plmnIdentity": b'\x00\x01\x02',
							"sNSSAI": {
								"sST": b'\x00',
								"sD": b'\x00\x01\x04'
							}
						}
					]
			},
			"dedicatedPRBPolicyRatio": 20,
			"minPRBPolicyRatio": 50,
			"maxPRBPolicyRatio": 80,
		},
	]
}
)
												
asn1_control_header.set_val(ric_control_header)
coded_control_header = asn1_control_header.to_aper()

# Encoding RIC Control Request
asn1_pdu = E2AP_PDU_Descriptions.E2AP_PDU
ric_request_msg = ('initiatingMessage', {
		'procedureCode': 4,
		'criticality': 'ignore',
		'value': ('RICcontrolRequest', {
				'protocolIEs': [
					{'id': 29, "criticality": 'reject', "value":('RICrequestID', {'ricRequestorID': 1, 'ricInstanceID': 1})},
					{'id': 5, "criticality": 'reject', "value":('RANfunctionID',200)},
					{'id': 20, "criticality": 'reject', "value":('RICcallProcessID', b'\x00\x01')},
					{'id': 22, "criticality": 'reject', "value":('RICcontrolHeader', coded_control_header)},
					{'id': 23, "criticality": 'reject', "value":('RICcontrolMessage', b'\x00\x01')},
				]
			}
		)
}
)
asn1_pdu.set_val(ric_request_msg)
coded_pdu = asn1_pdu.to_aper()

# Decoding RIC Control Request
asn1_pdu.from_aper(coded_pdu)
decoded_pdu = asn1_pdu.get_val()
print(f"\n\n\n################\nDecoded PDU: {decoded_pdu}")

# Decoding RIC Control Header
coded_control = decoded_pdu[1]["value"][1]["protocolIEs"][3]["value"][1]
asn1_control_header.from_aper(coded_control)
decoded_control = asn1_control_header.get_val()
print(f"\n\n\n################\nDecoded Control Header: {decoded_control}")
