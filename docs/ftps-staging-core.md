# FTPS staging core

This module is a private byte-store primitive for the future FTPS boundary. It
does not implement an FTP/FTPS listener, TLS, login, commands, filenames, data
connections, replies, retries, or MQTT correlation.

The only approved FTPS observation records an implicit TLS listener on port 990,
TLS 1.2, and a passive port range. It explicitly records that no client transfer
occurred. Therefore `klove.ftps.staging` has no API that accepts a client name
or a wire principal. A later accepted ADR 0010 profile must provide those facts
and bind a validated exact-printer principal to a generated `FtpsStageReservation`.

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
its missing clean-room transfer observations.
