## Outcome

Describe the observable outcome and link the delivery issue.

Ordinary branches start from and target `develop`. Release trains merge
`develop` into `preview`, then the accepted `preview` into `main`.

## Safety and compatibility

- Fail-closed behaviour:
- Grove contract impact:
- Moonraker/Klipper compatibility:
- Secrets, logs, and operator impact:

## Validation

- [ ] Formatting, linting, strict typing, and tests pass.
- [ ] Authentication/authorization/translation/safety coverage remains 100%.
- [ ] Negative, malformed, stale, and disconnected cases are covered.
- [ ] No arbitrary G-code or unreviewed actuator path was introduced.
- [ ] No secrets, generated artifacts, or debug output are included.
