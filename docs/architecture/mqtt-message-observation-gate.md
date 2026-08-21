# MQTT message observation gate

`tests/fixtures/grove-observations/mqtt-message-schema` is a sanitized,
generated-data record from one bounded black-box MQTT 3.1.1 session. Its manifest
binds the record to the pinned Grove revision and the committed observation
lifecycle.

`klove.northbound.mqtt.messages` accepts only the byte-exact profile. It is
non-runtime evidence: it opens no listener, supplies no credentials, and grants
no printing or control authority.

The fixture's `observed_fields` labels mean presence in one successful message,
not a Grove client requirement. The capture observed idle/available
`print.push_status` reports and an `info` `get_version` reply. The bounded capture did not establish an
`extrusion_cali_get` reply, pause/resume/stop requests, or printing/paused report
schemas. Those remain unsupported until separately observed and reviewed.

A follow-up bound a generated virtual printer to a disposable target through the
public create/update API. Complete observed reports with only `gcode_state`
varied, and reduced two-field reports, left the public target state idle. The
public pause, resume, and stop endpoints each returned `200`, but the bounded
wildcard recorder saw no request-topic publish. Thus no outbound request schema
or minimum accepted idle/printing/paused report is retained; a success HTTP
status alone is not transport evidence.
