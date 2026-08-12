import pytest

from klove.domain.models import initial_snapshot
from klove.registry import PrinterRegistry


@pytest.mark.asyncio
async def test_registry_is_ordered_and_requires_advancing_known_snapshots() -> None:
    registry = PrinterRegistry(["zeta", "alpha"])

    assert [snapshot.printer_id for snapshot in await registry.list()] == ["alpha", "zeta"]
    assert await registry.get("missing") is None
    assert (await registry.get("alpha")) == initial_snapshot("alpha")

    advanced = initial_snapshot("alpha").model_copy(update={"revision": 1})
    await registry.replace(advanced)
    assert await registry.get("alpha") == advanced

    with pytest.raises(ValueError, match="did not advance"):
        await registry.replace(advanced)
    with pytest.raises(KeyError, match="not configured"):
        await registry.replace(initial_snapshot("missing").model_copy(update={"revision": 1}))
