# MQTT observation gate

`klove.northbound.mqtt` validates only the retained, ADR-0008-compliant
`mqtt-client-profile` facts for Grove revision
`cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. The slice retains TLS 1.3, a
MQTT 3.1.1, clean sessions, a 30-second keepalive, the generated client-ID
shape, `bblp` username and access-code password mapping, the observed two QoS-0
subscriptions, one server-to-client topic, and QoS-1 non-retained initial
publishes. The capture also proves that Grove can send another packet before
the first publish is acknowledged. The validator treats the profile as hostile
input: JSON is duplicate-key-safe, UTF-8 and size are bounded, and any changed,
omitted, duplicate, or additional fact is rejected.

The module has no socket, TLS context, credential lookup, MQTT codec, publish,
subscription, control, dispatch, or configuration wiring. Its only runtime
disposition is `mqtt_runtime_disabled`.

The retained profile does not establish the request payload upper bound or QoS-1
retransmission timing after a longer withheld acknowledgement. Those gaps, the
still-proposed ADR 0009, and the missing transport, authentication,
authorization, replay, and control tests continue to deny listener composition.
