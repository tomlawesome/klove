# Security policy

Klove is pre-release software. Its only printer-changing operations are the
explicitly enabled pause, resume, and cancel controls described in ADR 0001.
Do not use development builds to control production printers. Printer owners
remain responsible for the safety of any configured `PAUSE`, `RESUME`, and
`CANCEL_PRINT` macros.

Report suspected vulnerabilities privately through GitHub's security advisory
feature. Do not open a public issue containing credentials, printer addresses,
uploaded G-code, or sensitive operational data.
