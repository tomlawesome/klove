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

`mqtt-initial-request-schema` separately retains the complete initial outbound
request sequence from one bounded TLS MQTT session: `pushall`, `get_version`,
then `extrusion_cali_get`. Each request is QoS 1 on
`device/{serial}/request`, has its observed structural member schema and byte
count, and retains the observed first-PUBACK ordering. It contains neither
payload values nor packet identifiers, credentials, endpoint data, certificates,
or raw trace material.

`mqtt-control-request-schema` separately retains the complete bounded control
sequence after public controls: `pause`, `resume`, `stop`, then a separate
same-payload `pause` publish. Each request is QoS 1 on
`device/{serial}/request`, with the observed `print` member schema, occurrence
relation, flags, byte count, PUBACK ordering, and post-control quiescence. It
contains neither payload values nor packet identifiers, credentials, endpoint
data, certificates, or raw trace material. This observation is not evidence
that control is safe or authorised.

The separate report observer retains only one bounded persistent-session chain:
the generated `project_file` success acknowledgement, followed by
`push_status` `PREPARE` and the first strict `push_status` `FINISH`. Each
observed report in that chain is QoS 0, `dup=false`, and `retain=false`, and no
two observed report bodies in the chain are byte-identical. It closes immediately
after that first `FINISH`; it does not claim a quiet tail, any later report, or a
lasting session-wide replay property.

This is non-runtime evidence only. It grants no MQTT listener, request handler,
credential lookup, control, upload, or print authority. Any runtime use needs a
separate accepted decision, bounded implementation, and its own negative tests.
