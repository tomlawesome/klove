# FTPS staging core

This module is a private byte-store primitive for the future FTPS boundary. It
does not implement an FTP/FTPS listener, TLS, login, commands, filenames, data
connections, replies, retries, or MQTT correlation.

The retained Grove-client observation now proves two implicit-FTPS TLS 1.3
sessions and one protected passive upload, including the generated access-code
mapping, ordered command tokens, exact generated test path, same-peer data
connection, byte count, and SHA-256. `klove.ftps.profile` validates that exact
sanitized evidence but keeps the listener disabled. Reply/error semantics,
PASV deployment behavior, production filename grammar, concurrency, and wire
retry/disconnect behavior remain unobserved. Therefore `klove.ftps.staging`
still has no API that accepts a client name or wire principal. An accepted ADR
0010 profile must close those gaps and bind a validated exact-printer principal
to a generated `FtpsStageReservation`.

`FtpsStagingStore` requires a deployment-created owner-only directory. It writes
one generated staging identity at most once, streams only `bytes`, caps it at
`ArtifactLimits.max_archive_compressed_bytes`, hashes it, fsyncs the immutable
source and a non-secret receipt, and uses no client-controlled filesystem path.
The receipt contains only its version, opaque staging ID, canonical printer UUID,
byte count, and SHA-256. `inspect` requires the same exact reservation and
re-verifies the retained bytes; it exposes no staged data.

This is file-only and non-actuating. It neither validates an archive nor creates
an artifact target approval, dispatch operation, upload, print start, or control
request. Retention, expiry, consumed-state, crash reconciliation, lifecycle-gate
integration, and all listener behavior remain gated on ADR 0010 acceptance and
the remaining clean-room fault observations.
