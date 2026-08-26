# FTPS production-container contract

`scripts/test-ftps-container.sh` is an opt-in Docker check of the production
image. It generates disposable TLS and credential material and places Klove at
one run-unique static RFC1918 address on an internal bridge. Local execution
requires rootless Docker; rootful Docker is accepted only by explicit CI.

The check proves UID/GID 10001, a read-only root, no effective capabilities,
`no-new-privileges`, the namespaced low-port setting, no host port publication,
readiness changing from 503 to 200, authenticated Grove cleanup and upload
reply chains, TLS 1.3 on both control and passive data connections, the exact
advertised address and passive range, durable byte-count/SHA-256 staging
evidence, and absence of MQTT. Control and dispatch are disabled
in the generated configuration. This is container and protocol evidence only;
it is not Grove, Moonraker, RatOS, motion, or physical-printer acceptance.

The mounted `runtime.py` adds only a one-second test barrier immediately before
the production FTPS start call, making the existing 503-to-200 readiness order
deterministically observable. Before startup it creates one canonical active
printer and its generated compatibility secret through the production bootstrap
lifecycle; the fixture supplies only bounded identity evidence. It does not
replace a listener, protocol handler, staging store, or readiness code.
The one-shot secret preparer receives only `SETUID` and `SETGID` so it can read
owner-only host inputs, drop permanently to 10001, and create 0600 volume files;
it copies only the public certificate into the contract peer's narrow trust
volume. The Klove runtime and UID/GID 10001 private peer retain no capabilities.
