"""Perform a supervised setup jog and return the motor to its initial position."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from graber.bluetooth.adapter_manager import AdapterManager, ResolvedAdapter
from graber.bluetooth.ble_transport import BleakTransport
from graber.bluetooth.protocol import CommandKind, Rail
from graber.config import AdapterConfig, BlindConfig, RetryConfig
from graber.control.blind import BlindController
from graber.control.commands import BlindCommand, Outcome
from .session import SetupSession


async def jog_device(session: SetupSession, key: str) -> dict[str, Any]:
    setup = session.devices[key]
    if not setup.access_code:
        raise ValueError("save a verified access code before jogging")
    current = setup.observation.bottom_raw
    if current is None:
        raise ValueError("a current bottom position is required before jogging")
    adapter = session.adapter_for(setup.preferred_adapter)
    adapter_config = AdapterConfig(setup.preferred_adapter, adapter.address or "", adapter.usb_path,
                                   max_connections=1, max_cold_connections=1)

    async def resolver(_configs: tuple[AdapterConfig, ...]) -> list[ResolvedAdapter]:
        return [ResolvedAdapter(adapter_config.id, adapter_config.controller_address,
                                adapter.hci_name, adapter.usb_path)]

    manager = AdapterManager((adapter_config,), resolver)
    await manager.refresh()
    config = BlindConfig(setup.id, setup.friendly_name, setup.observation.address,
                         setup.dual_rail, setup.access_code, setup.preferred_adapter)
    blind = BlindController(config, manager, BleakTransport(),
                            RetryConfig(attempts=2, feedback_timeout_s=20), warm_connection_s=2)
    target = current + 5 if current <= 95 else current - 5
    try:
        outward = await blind.execute(BlindCommand(
            f"identify-{secrets.token_hex(8)}", CommandKind.SET_POSITION, Rail.BOTTOM, target
        ))
        if outward.outcome is not Outcome.COMPLETED:
            await blind.execute(BlindCommand(f"identify-stop-{secrets.token_hex(8)}", CommandKind.STOP))
            return {"status": outward.outcome.value, "restored": False, "error": outward.error}
        await asyncio.sleep(0.5)
        restored = await blind.execute(BlindCommand(
            f"identify-restore-{secrets.token_hex(8)}", CommandKind.SET_POSITION, Rail.BOTTOM, current
        ))
        if restored.outcome is not Outcome.COMPLETED:
            await blind.execute(BlindCommand(f"identify-stop-{secrets.token_hex(8)}", CommandKind.STOP))
        return {"status": outward.outcome.value, "restored": restored.outcome is Outcome.COMPLETED,
                "restore_status": restored.outcome.value}
    finally:
        await blind.close()
