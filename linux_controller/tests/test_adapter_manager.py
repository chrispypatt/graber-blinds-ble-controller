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


def test_loss_and_recovery_rebinds_transient_hci_name_and_updates_health():
    config = AdapterConfig("a", "AA:00:00:00:00:01")
    snapshots = [
        [ResolvedAdapter("a", config.controller_address, "hci1", "1-2.3")],
        [],
        [ResolvedAdapter("a", config.controller_address, "hci7", "1-2.3")],
    ]

    async def resolver(_configs):
        return snapshots.pop(0)

    manager = AdapterManager((config,), resolver)

    async def scenario():
        await manager.refresh()
        assert manager.health()["a"]["status"] == "online"
        await manager.refresh()
        lost = manager.health()["a"]
        assert lost["status"] == "unavailable"
        assert lost["disconnects"] == 1
        assert lost["last_unavailable"] is not None
        await manager.refresh()
        recovered = manager.health()["a"]
        assert recovered["status"] == "online"
        assert recovered["recoveries"] == 1
        assert recovered["disconnects"] == 1
        assert recovered["last_recovered"] is not None
        blind = BlindConfig("x", "X", "11", False, "1234567", "a")
        assert (await manager.candidates(blind))[0].resolved.device == "hci7"

    run(scenario())


def test_queued_cold_work_rechecks_adapter_and_fails_boundedly():
    config = AdapterConfig("a", "AA:00:00:00:00:01", max_cold_connections=1)
    online = True

    async def resolver(_configs):
        if online:
            return [ResolvedAdapter("a", config.controller_address, "hci1")]
        return []

    manager = AdapterManager((config,), resolver)

    async def scenario():
        nonlocal online
        await manager.refresh()
        runtime = manager._runtime["a"]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first():
            async with manager.cold_connection(runtime):
                entered.set()
                await release.wait()

        holder = asyncio.create_task(first())
        await entered.wait()
        waiting = asyncio.create_task(manager.cold_connection(runtime).__aenter__())
        await asyncio.sleep(0)
        online = False
        await manager.refresh()
        release.set()
        await holder
        with pytest.raises(AdapterUnavailable):
            await asyncio.wait_for(waiting, 0.2)

    run(scenario())


def test_normal_gatt_failure_does_not_offline_adapter():
    config = AdapterConfig("a", "AA:00:00:00:00:01")

    async def resolver(_configs):
        return [ResolvedAdapter("a", config.controller_address, "hci1")]

    manager = AdapterManager((config,), resolver)

    async def scenario():
        await manager.refresh()
        runtime = manager._runtime["a"]
        manager.record_failure(runtime, RuntimeError("GATT operation timed out"))
        assert runtime.resolved is not None
        manager.record_failure(
            runtime,
            RuntimeError("org.freedesktop.DBus.Error.UnknownObject: adapter vanished"),
        )
        assert runtime.resolved is None
        assert manager.health()["a"]["disconnects"] == 1

    run(scenario())


def test_resolver_failure_is_contained_and_health_recovers():
    config = AdapterConfig("a", "AA:00:00:00:00:01")
    calls = 0

    async def resolver(_configs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("bluetoothctl temporarily failed")
        return [ResolvedAdapter("a", config.controller_address, "hci1")]

    manager = AdapterManager((config,), resolver)

    async def scenario():
        await manager.refresh()
        runtime = manager._runtime["a"]
        original = runtime.resolved
        await manager.refresh()
        unknown = manager.health()["a"]
        assert unknown["status"] == "unknown"
        assert unknown["resolution_failures"] == 1
        assert "temporarily failed" in unknown["last_resolution_error"]
        assert runtime.resolved is original
        await manager.refresh()
        healthy = manager.health()["a"]
        assert healthy["status"] == "online"
        assert healthy["last_resolution_error"] is None

    run(scenario())
