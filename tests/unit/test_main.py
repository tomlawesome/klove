from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import klove.__main__ as cli


def test_parser_has_a_safe_absolute_default() -> None:
    assert cli.parser().parse_args([]).config == Path("/etc/klove/config.toml")


def test_main_runs_selected_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selected = tmp_path / "config.toml"
    observed: list[Any] = []

    def run(coroutine: Any) -> None:
        observed.append(coroutine)
        coroutine.close()

    monkeypatch.setattr("klove.__main__.asyncio.run", run)
    assert cli.main(["--config", str(selected)]) == 0
    assert len(observed) == 1


def test_main_maps_operator_interrupt_to_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("klove.__main__.asyncio.run", interrupted)
    assert cli.main([]) == 130
