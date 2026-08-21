# MQTT observation gate

`klove.northbound.mqtt` validates only the retained, ADR-0008-compliant
`mqtt-client-profile` facts for Grove revision
`cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. The slice retains TLS 1.3 with
`TLS_AES_256_GCM_SHA384` and no TLS-session reuse; MQTT 3.1.1 clean sessions;
a 30-second keepalive; and the generated client-ID shape, including decimal
printer and session fields. It also retains the observed subscriptions and
initial QoS-1 publishes.

One fault capture withheld the first PUBACK for 75 seconds. It observed no
connected-session QoS-1 retry, PINGREQ at 30,086 ms and 60,119 ms, and then
forced reconnect recovery: fresh initial publishes preceded DUP-marked
outstanding publishes, whose prior packet IDs and payload hashes were reused.
A second forced reconnect retained both earlier unacknowledged sets. The
fixture retains only those boolean equality/order facts, never an identifier,
payload, payload hash, or credential. The validator treats the profile as
hostile input: JSON is duplicate-key-safe, UTF-8 and size are bounded, and any
changed, omitted, duplicate, or additional fact is rejected.

The module has no socket, TLS context, credential lookup, MQTT codec, publish,
subscription, control, dispatch, or configuration wiring. Its only runtime
disposition is `mqtt_runtime_disabled`.

The retained profile does not establish a request payload upper bound or the
full request and report schemas. Those gaps, the still-proposed ADR 0009, and
the missing transport, authentication, authorization, replay, and control tests
continue to deny listener composition.
