from e2sm_kpm_rc import E2SM_KPM_RC

ueid = 0
control_style_type = 0
control_action_id = 0

control_header_message = {"value": [("controlHeader-Format1",
    {"value":[
        ("ueId",ueid),
        ("ric-ControlStyle-Type",control_style_type),
        ("ric-ControlAction-ID",control_action_id)
    ]}
)]}

header = E2SM_KPM_RC.E2SM_RC_ControlHeader
header.set_val(control_header_message)

print(header.get_val())

E2SM_KPM_RC.E2SM_RC_ControlMessage