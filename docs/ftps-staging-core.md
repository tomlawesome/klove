# FTPS staging core

`FtpsStagingStore` is a private byte-store primitive. The bounded
`FtpsTlsServer` class now implements the accepted implicit-TLS listener
profile: one authenticated cleanup or protected passive upload, with strict
command order, bounded sessions and transfers, same-peer protected data, and
private staging only. The explicit Grove bridge gate composes this FTPS boundary
into application startup and readiness.

The retained Grove-client observation now proves two implicit-FTPS TLS 1.3
sessions and one protected passive upload, including the generated access-code
mapping, ordered command tokens, exact generated test path, same-peer data
connection, byte count, and SHA-256. `klove.ftps.profile` validates that exact
sanitized evidence. The listener class accepts its principal only through the
supplied compatibility authenticator and creates a generated
`FtpsStageReservation`; it exposes no client-selected server path. Alternate
reply/error flows, NAT or host-published PASV deployment, concurrency beyond the
accepted bound, and wire retry/disconnect behavior remain unsupported.

`FtpsStagingStore` requires a deployment-created owner-only directory. It writes
one generated staging identity at most once, streams only `bytes`, caps it at
`ArtifactLimits.max_archive_compressed_bytes`, hashes it, fsyncs the immutable
source and a non-secret receipt, and uses no client-controlled filesystem path.
The receipt contains only its version, opaque staging ID, canonical printer UUID,
byte count, and SHA-256. `inspect` requires the same exact reservation and
re-verifies the retained bytes; it exposes no staged data.

This is file-only and non-actuating. It neither validates an archive nor creates
an artifact target approval, dispatch operation, Moonraker upload, print start,
or control request. Enabled startup first reconciles the owner-only staging
store, seeds session revocation from the complete canonical registry snapshot,
and only then starts the exact FTPS listener set and opens readiness. Shutdown
closes FTPS admission twice before fencing sessions and withdrawing runtime
routes, so ambiguous listener release receives one explicit retry. This does
not establish Grove acceptance or grant dispatch authority.

`FtpsBindSetDiagnostic` is a non-actuating, diagnostic-only startup helper. It
binds the configured control and passive set for direct loopback
or RFC1918 IPv4 configuration: either the advertised address is the exact bind
address, or the listener uses the accepted wildcard container bind with an
RFC1918 advertised address. Validated enabled bridge configuration rejects every
other topology before the diagnostic runs; forged configuration objects are
independently denied by the diagnostic. It returns
fixed non-secret codes for unsupported topology, control-port conflict,
passive-port conflict, or probe-release failure. It attempts every probe close;
a release failure makes no release or availability claim. It does not prove that an
advertised wildcard address is locally usable, reserve a later listener, or make
a readiness claim. It accepts no connection and performs no TLS, authentication,
routing, staging, or dispatch work. Application composition does not use this
diagnostic as readiness evidence.

`FtpsTlsServer.start` independently revalidates that exact private topology and
atomically acquires the configured control port plus every configured passive
port. It constructs all listeners dormant, activates the control listener only
after every passive listener activates, and retains those exact sockets until a
bounded, idempotent shutdown proves each close. Passive transfers lease one
already-owned port; they never scan for or fall through to another port and
never release the underlying listener between sessions. Each accepted passive
connection is bound to the immutable lease present before its TLS 1.3
handshake, so a delayed connection cannot enter a successor lease. Startup,
transfer, cancellation, and shutdown ambiguity fail closed and retain uncertain
handles for an explicit close retry. Grove acceptance and actuation remain
separate boundaries.
