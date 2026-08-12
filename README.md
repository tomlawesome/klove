# Klove

Klove is a fail-closed interface between Grove Control and Klipper printers
managed through Moonraker.

The first delivery slice is intentionally read-only. It discovers Moonraker
capabilities, maintains a canonical printer state, authenticates monitoring API
clients, and classifies Grove commands without exposing any printer-changing
transport.

See [the architecture and delivery plan](docs/architecture-plan.md) and
[the implementation plan](docs/implementation-plan.md).

