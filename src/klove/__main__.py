"""Klove command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from klove.app import serve


def parser() -> argparse.ArgumentParser:
    """Build the bounded command-line contract."""
    result = argparse.ArgumentParser(prog="klove")
    result.add_argument("--config", type=Path, default=Path("/etc/klove/config.toml"))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Start Klove and convert operator interruption into a clean exit."""
    arguments = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(serve(arguments.config))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console entry point
    raise SystemExit(main())
