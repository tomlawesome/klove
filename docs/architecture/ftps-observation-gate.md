# FTPS observation gate

`klove.ftps.profile` admits only the ADR-0008-compliant
`ftps-client-profile` capture for Grove revision
`cdf6b829ad5da200bd9eda5d3a4fcda5a7bba3e4`. It retains two implicit-FTPS TLS
1.3 sessions: `USER bblp`, then `PASS` mapped to the generated access code,
then `PBSZ 0` and `PROT P`. The first sends `DELE /observation.3mf` and quits. The second sends `PASV`,
`STOR /observation.3mf`, then quits; its one passive data connection is
protected with TLS 1.3, comes from the same control peer, and transfers exactly
1,684 bytes with the retained SHA-256.

The profile is hostile input: it is bounded, strict UTF-8, duplicate-key-safe,
and rejects any added, removed, reordered, or changed retained fact. Its only
runtime disposition is `ftps_runtime_disabled`. It has no listener, TLS
context, credential lookup, FTP parser, file access, data socket, reply, MQTT,
dispatch, or configuration wiring.

The supplied operational logs (30-second timeout, two-second retry delay, four
total attempts) are non-wire evidence. The version-1 observation manifest
schema has no operational-evidence category, so they are intentionally absent
from the wire fixture and cannot authorize retry behavior. FTP reply/error
semantics, PASV address/port/NAT behavior, canonical production filename
grammar, concurrent transfers, and wire retry/disconnect behavior remain
explicitly unobserved. ADR 0010 is still proposed; each of those gaps keeps
listener composition fail-closed.
