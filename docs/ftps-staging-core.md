# FTPS staging core

`FtpsStagingStore` is a private byte-store primitive. The bounded
`FtpsTlsServer` class now implements the accepted implicit-TLS listener
profile: one authenticated cleanup or protected passive upload, with strict
command order, bounded sessions and transfers, same-peer protected data, and
private staging only. It is not composed into application startup or readiness.

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
or control request. Production composition remains disabled pending startup
reconciliation and expiry, lifecycle-driven session revocation, and every
remaining ADR-0010 runtime gate. The listener class and its conformance tests
do not make issue #14 complete or establish Grove acceptance, dispatch
authority, or production readiness.
