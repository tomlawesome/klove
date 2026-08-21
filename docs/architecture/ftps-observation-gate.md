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

The separate `ftps-server-response-profile` capture drives Grove through its
public queue API and establishes the successful reply sequences: cleanup uses
`220/331/230/200/200/250/221`; upload uses
`220/331/230/200/200/227/150/226/221`. The PASV address equals the control-local
address, its port is an open listener, the protected data peer equals the
control peer, and Grove accepts `226` after closing the complete data payload.
Both channels use TLS 1.3.

The supplied operational logs (30-second timeout, two-second retry delay, four
total attempts) are non-wire evidence and cannot authorize retry behavior.
Alternate reply codes, wire failure/retry behavior, host-published or NAT
passive deployment, and concurrent transfers remain explicitly unobserved and
unsupported. ADR 0010 accepts only the exact successful private-network flow;
the runtime remains disabled until its implementation and composition gates
pass.
