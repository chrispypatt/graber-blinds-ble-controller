import asyncio

import pytest

from graber.adapter_manager import AdapterManager, AdapterUnavailable, ResolvedAdapter
from graber.config import AdapterConfig, BlindConfig


def run(coro):
    return asyncio.run(coro)


def test_prefers_configured_external_adapter_and_fails_over():
    configs = (
        AdapterConfig("a", "AA:00:00:00:00:01"),
        AdapterConfig("b", "AA:00:00:00:00:02"),
    )
    async def resolver(_):
        return [ResolvedAdapter("b", configs[1].controller_address, "hci2", "1-2.3")]
    manager = AdapterManager(configs, resolver)
    blind = BlindConfig("x", "X", "11:22:33:44:55:66", False, "1234567", "a", ("b",))
    candidates = run(manager.candidates(blind))
    assert [x.config.id for x in candidates] == ["b"]
    assert candidates[0].resolved.device == "hci2"


def test_no_silent_use_of_unconfigured_controller():
    configs = (AdapterConfig("a", "AA:00:00:00:00:01"),)
    async def resolver(_):
        return []
    manager = AdapterManager(configs, resolver)
    blind = BlindConfig("x", "X", "11", False, "1234567")
    with pytest.raises(AdapterUnavailable):
        run(manager.candidates(blind))


def test_cold_connection_limit_is_independent_per_adapter():
    configs = (AdapterConfig("a", "AA:00:00:00:00:01", max_cold_connections=1),)
    async def resolver(_):
        return [ResolvedAdapter("a", configs[0].controller_address, "hci7")]
    manager = AdapterManager(configs, resolver)

    async def scenario():
        runtime = (await manager.candidates(BlindConfig("x", "X", "11", False, "1234567")))[0]
        entered = []
        release = asyncio.Event()
        async def worker(number):
            async with manager.cold_connection(runtime):
                entered.append(number)
                if number == 1:
                    await release.wait()
        first = asyncio.create_task(worker(1))
        await asyncio.sleep(0)
        second = asyncio.create_task(worker(2))
        await asyncio.sleep(0)
        assert entered == [1]
        release.set()
        await asyncio.gather(first, second)
        assert entered == [1, 2]
    run(scenario())
