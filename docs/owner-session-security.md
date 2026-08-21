# Owner-session security contract

Issue #71 — owner session implements the independent authentication and
request-security substrate required by ADR 0006. It does not expose a lifecycle
route or HTML surface. Issue #72 — lifecycle routes is the only next slice
allowed to consume this boundary.

## Configuration and authentication

Onboarding is disabled by default. Enabling it requires a separate absolute
owner-credential file and at least one exact Grove origin. The credential is a
bounded single visible-ASCII token, is compared in constant time, and is not
shared with the native API, Grove, Moonraker, a URL, browser storage, or logs.

Configured origins are canonical origins, not URL prefixes. HTTPS is required.
An explicit development flag may admit literal loopback HTTP only; it cannot
admit a remote cleartext origin. Wildcards, credentials, paths, queries,
fragments, default ports, non-canonical host spellings, IPv4-mapped IPv6, and
more than 32 origins are rejected.

## Session evidence

Successful owner authentication may issue one in-memory session bound to:

- one exact configured origin;
- one exact lifecycle operation;
- independent 256-bit session-cookie, CSRF, and flow-nonce values;
- at most 15 minutes of inactivity and 30 minutes of absolute lifetime.

The cookie is `HttpOnly`, path-scoped to `/v1/onboarding`, and
`SameSite=Strict`. It is `Secure` except in the explicit loopback-HTTP
development mode. Server state retains only domain-separated SHA-256 digests
of the cookie and CSRF values. Process restart therefore invalidates every
session.

Every protected request must carry exactly one raw Cookie header containing
one `klove_setup` cookie, exactly one `X-Klove-CSRF` header, exactly one Origin,
the bound flow nonce, and the bound typed operation. Missing, duplicate,
malformed, expired, cross-origin, cross-operation, replayed, or unknown evidence
receives the same redacted denial.

Only one request may claim a session at a time. A safe incomplete request may
release the claim and refresh inactivity time. Completion, cancellation, or an
unsafe outcome invalidates it. Capacity is bounded and expired entries are
removed before a new session is admitted; capacity exhaustion fails closed.

## Remaining boundary

This substrate grants no printer lifecycle or runtime authority by itself.
Issue #72 — lifecycle routes must strictly decode requests, authenticate the
owner credential before issuance, use the exact raw-header checks above, and
invalidate sessions at the required terminal transitions. Issue #73 — runtime
handoff must then reconcile committed mutations into runtime activation.
