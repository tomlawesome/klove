# MQTT observation gate

`klove.northbound.mqtt` validates only the retained, ADR-0008-compliant
`mqtt-client-profile` facts for Grove revision
`cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. The slice retains TLS 1.3, a
30-second keepalive, the observed two QoS-0 subscriptions, one
server-to-client topic, and three initial command tokens. It treats the profile
as hostile input: JSON is duplicate-key-safe, UTF-8 and size are bounded, and
any changed, omitted, duplicate, or additional fact is rejected.

The module has no socket, TLS context, credential lookup, MQTT codec, publish,
subscription, control, dispatch, or configuration wiring. Its only runtime
disposition is `mqtt_runtime_disabled`.

The retained profile explicitly does not observe the MQTT `CONNECT` protocol
level, client identifier, clean-session behavior, access-code encoding, publish
QoS/retain behavior, acknowledgement ordering, or request payload bounds.
Those gaps continue to deny listener composition. A later accepted ADR 0009
profile and its full transport, authentication, authorization, replay, and
control tests are required before this boundary can change.
