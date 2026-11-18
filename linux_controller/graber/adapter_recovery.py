"""Opt-in recovery for a uniquely matched USB Bluetooth controller."""

import logging
import subprocess
import time
from pathlib import Path

from .diagnostics import AdapterIdentity, _bluez_property, _default_runner, discover_adapters

LOG = logging.getLogger("graber")


def recover_adapter_power(adapter: AdapterIdentity) -> bool:
    """Unblock and power one USB adapter. Return whether it is ready."""

    def run(*args: str) -> None:
        subprocess.run(args, check=True, capture_output=True, timeout=5)

    try:
        matches = [item for item in discover_adapters() if item.address == adapter.address]
        if matches != [adapter] or not adapter.is_usb:
            return False
        rfkill = [
            entry for entry in Path("/sys/class/rfkill").glob("rfkill*")
            if (entry / "name").read_text().strip() == adapter.hci_name
        ]
        if len(rfkill) != 1 or (rfkill[0] / "hard").read_text().strip() != "0":
            return False
        if (rfkill[0] / "soft").read_text().strip() == "1":
            run("/usr/sbin/rfkill", "unblock", rfkill[0].name.removeprefix("rfkill"))
        for delay in (0.0, 0.5, 1.0):
            time.sleep(delay)
            if _bluez_property(_default_runner, adapter.hci_name, "Address") != adapter.address:
                return False
            if _bluez_property(_default_runner, adapter.hci_name, "Powered") == "true":
                break
            try:
                run(
                    "busctl", "--system", "set-property", "org.bluez",
                    f"/org/bluez/{adapter.hci_name}", "org.bluez.Adapter1", "Powered", "b", "true",
                )
                break
            except subprocess.CalledProcessError:
                if delay == 1.0:
                    raise
        ready = any(
            item.address == adapter.address and item.hci_name == adapter.hci_name
            and item.usb_path == adapter.usb_path and item.powered is True
            for item in discover_adapters()
        )
        if ready:
            LOG.info("recovered USB Bluetooth adapter on %s", adapter.usb_path)
        return ready
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("USB Bluetooth recovery failed on %s: %s", adapter.usb_path, type(exc).__name__)
        return False
