import asyncio

import pytest

from graber.bluetooth.adapter_manager import AdapterManager, AdapterUnavailable, ResolvedAdapter
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


def test_cold_connection_reports_immediate_and_queued_wait():
    configs = (AdapterConfig("a", "AA:00:00:00:00:01", max_cold_connections=1),)

    async def resolver(_):
        return [ResolvedAdapter("a", configs[0].controller_address, "hci7")]

    manager = AdapterManager(configs, resolver)

    async def scenario():
        runtime = (await manager.candidates(
            BlindConfig("x", "X", "11", False, "1234567")
        ))[0]
        entered = asyncio.Event()
        release = asyncio.Event()
        waits = []

        async def holder():
            async with manager.cold_connection(runtime) as wait_ms:
                waits.append(wait_ms)
                entered.set()
                await release.wait()

        async def queued():
            async with manager.cold_connection(runtime) as wait_ms:
                waits.append(wait_ms)

        first = asyncio.create_task(holder())
        await entered.wait()
        second = asyncio.create_task(queued())
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(first, second)
        assert waits[0] < 10
        assert waits[1] >= 15

    run(scenario())


def test_cold_start_spacing_overlaps_attempts_after_stagger():
    config = AdapterConfig(
        "a", "AA:00:00:00:00:01", max_cold_connections=2,
        cold_start_spacing_s=0.03,
    )

    async def resolver(_):
        return [ResolvedAdapter("a", config.controller_address, "hci7")]

    manager = AdapterManager((config,), resolver)

    async def scenario():
        runtime = (await manager.candidates(
            BlindConfig("x", "X", "11", False, "1234567")
        ))[0]
        release = asyncio.Event()
        both_active = asyncio.Event()
        starts = []
        active = 0
        maximum_active = 0

        async def worker():
            nonlocal active, maximum_active
            async with manager.cold_connection(runtime) as wait_ms:
                starts.append((asyncio.get_running_loop().time(), wait_ms))
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    both_active.set()
                await release.wait()
                active -= 1

        first = asyncio.create_task(worker())
        second = asyncio.create_task(worker())
        await asyncio.wait_for(both_active.wait(), 0.3)
        assert starts[1][0] - starts[0][0] >= 0.02
        assert starts[1][1] >= 20
        assert maximum_active == 2
        release.set()
        await asyncio.gather(first, second)

    run(scenario())


def test_cold_start_spacing_is_independent_per_adapter():
    configs = (
        AdapterConfig("a", "AA:00:00:00:00:01", max_cold_connections=2,
                      cold_start_spacing_s=0.04),
        AdapterConfig("b", "AA:00:00:00:00:02", max_cold_connections=2,
                      cold_start_spacing_s=0.04),
    )

    async def resolver(_):
        return [
            ResolvedAdapter("a", configs[0].controller_address, "hci7"),
            ResolvedAdapter("b", configs[1].controller_address, "hci8"),
        ]

    manager = AdapterManager(configs, resolver)

    async def scenario():
        await manager.refresh()
        a, b = manager._runtime["a"], manager._runtime["b"]
        async def enter(runtime):
            async with manager.cold_connection(runtime):
                return asyncio.get_running_loop().time()

        # While A's second admission is sleeping behind A's spacing gate, B's
        # first admission must remain immediate.
        await enter(a)
        delayed_a = asyncio.create_task(enter(a))
        await asyncio.sleep(0)
        started = asyncio.get_running_loop().time()
        first_b = await enter(b)
        assert first_b - started < 0.025
        assert await delayed_a - started >= 0.025

    run(scenario())


def test_cancelling_during_cold_start_spacing_releases_slot_and_gate():
    config = AdapterConfig(
        "a", "AA:00:00:00:00:01", max_cold_connections=2,
        cold_start_spacing_s=0.08,
    )

    async def resolver(_):
        return [ResolvedAdapter("a", config.controller_address, "hci7")]

    manager = AdapterManager((config,), resolver)

    async def scenario():
        runtime = (await manager.candidates(
            BlindConfig("x", "X", "11", False, "1234567")
        ))[0]
        release = asyncio.Event()
        first_entered = asyncio.Event()

        async def holder():
            async with manager.cold_connection(runtime):
                first_entered.set()
                await release.wait()

        async def enter_once():
            async with manager.cold_connection(runtime):
                return True

        first = asyncio.create_task(holder())
        await first_entered.wait()
        cancelled = asyncio.create_task(enter_once())
        await asyncio.sleep(0.015)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        # The cancelled waiter released both the start gate and its cold slot.
        assert await asyncio.wait_for(enter_once(), 0.3)
        release.set()
        await first

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


def test_rebinding_hci_name_preserves_last_observed_unavailable_time(monkeypatch):
    config = AdapterConfig("a", "AA:00:00:00:00:01")
    snapshots = iter(("hci1", "hci2", None, "hci2", "hci3"))

    async def resolver(_configs):
        device = next(snapshots)
        return [] if device is None else [ResolvedAdapter("a", config.controller_address, device)]

    times = iter((1, 2, 3, 4, 5))
    monkeypatch.setattr("graber.bluetooth.adapter_manager.time.time", lambda: next(times))
    manager = AdapterManager((config,), resolver)

    async def scenario():
        await manager.refresh()
        await manager.refresh()  # New hci name, with no observed outage.
        assert manager.health()["a"]["last_unavailable"] is None
        await manager.refresh()  # Missing at time 3.
        await manager.refresh()  # Rediscovered at time 4.
        await manager.refresh()  # Rebound between refreshes again.
        health = manager.health()["a"]
        assert health["last_unavailable"] == 3
        assert health["last_recovered"] == 5
        assert health["disconnects"] == health["recoveries"] == 3

    run(scenario())
