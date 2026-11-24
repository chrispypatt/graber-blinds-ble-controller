"""Discover Bluetooth controllers and resolve stable identities on Linux."""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class AdapterIdentity:
    """Stable and transient identity observed for one BlueZ controller."""

    hci_name: str
    address: str | None
    powered: bool | None
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None
    usb_manufacturer: str | None = None
    usb_product: str | None = None
    usb_serial: str | None = None
    usb_path: str | None = None
    sysfs_path: str | None = None

    @property
    def stable_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        if self.address:
            keys.append(f"address:{self.address.upper()}")
        if self.usb_serial:
            keys.append(f"usb_serial:{self.usb_serial}")
        if self.usb_path:
            keys.append(f"usb_path:{self.usb_path}")
        return tuple(keys)

    @property
    def is_usb(self) -> bool:
        return bool(self.usb_vendor_id and self.usb_product_id)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return value or None


def _usb_parent(device_path: Path) -> Path | None:
    """Return the nearest sysfs ancestor that describes a USB device."""

    current = device_path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "idVendor").is_file() and (candidate / "idProduct").is_file():
            return candidate
    return None


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )


def _powered_by_address(runner: CommandRunner) -> dict[str, bool]:
    """Read BlueZ power state without changing it."""

    try:
        listed = runner(("bluetoothctl", "list"))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {}
    addresses = re.findall(r"^Controller\s+([0-9A-Fa-f:]{17})\b", listed.stdout, re.MULTILINE)
    states: dict[str, bool] = {}
    for address in addresses:
        try:
            shown = runner(("bluetoothctl", "show", address))
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"^\s*Powered:\s*(yes|no)\s*$", shown.stdout, re.MULTILINE)
        if match:
            states[address.upper()] = match.group(1) == "yes"
    return states


def _bluez_property(runner: CommandRunner, hci_name: str, property_name: str) -> str | None:
    """Read an Adapter1 property using the stable BlueZ object path.

    Some kernels do not publish ``address`` in the Bluetooth class sysfs
    directory.  BlueZ still exposes the controller address and power state on
    ``/org/bluez/hciN``.  This read-only fallback also preserves the mapping
    between a transient HCI name and its USB parent.
    """

    try:
        result = runner((
            "busctl", "--system", "get-property", "org.bluez",
            f"/org/bluez/{hci_name}", "org.bluez.Adapter1", property_name,
        ))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    string_value = re.fullmatch(r'\s*s\s+"([^"]+)"\s*', result.stdout)
    if string_value:
        return string_value.group(1)
    boolean_value = re.fullmatch(r"\s*b\s+(true|false)\s*", result.stdout)
    if boolean_value:
        return boolean_value.group(1)
    return None


def discover_adapters(
    sysfs_root: str | Path = "/sys/class/bluetooth",
    *,
    runner: CommandRunner = _default_runner,
) -> list[AdapterIdentity]:
    """Discover BlueZ HCI controllers and their stable USB identity.

    ``sysfs_root`` is injectable so the mapping can be tested without Bluetooth
    hardware. Missing sysfs or missing ``bluetoothctl`` returns an empty or
    partially populated result rather than changing system state.
    """

    root = Path(sysfs_root)
    powered = _powered_by_address(runner)
    adapters: list[AdapterIdentity] = []
    try:
        hci_entries = sorted(root.glob("hci*"), key=lambda item: item.name)
    except OSError:
        hci_entries = []
    for entry in hci_entries:
        address = _read_text(entry / "address")
        if not address:
            address = _bluez_property(runner, entry.name, "Address")
        address = address.upper() if address else None
        device_link = entry / "device"
        try:
            device_path = device_link.resolve(strict=True)
        except (FileNotFoundError, OSError):
            device_path = entry.resolve()
        usb = _usb_parent(device_path)
        adapters.append(
            AdapterIdentity(
                hci_name=entry.name,
                address=address,
                powered=(
                    powered.get(address)
                    if address in powered
                    else {"true": True, "false": False}.get(
                        _bluez_property(runner, entry.name, "Powered") or ""
                    )
                ),
                usb_vendor_id=_read_text(usb / "idVendor") if usb else None,
                usb_product_id=_read_text(usb / "idProduct") if usb else None,
                usb_manufacturer=_read_text(usb / "manufacturer") if usb else None,
                usb_product=_read_text(usb / "product") if usb else None,
                usb_serial=_read_text(usb / "serial") if usb else None,
                usb_path=usb.name if usb else None,
                sysfs_path=str(device_path),
            )
        )
    return adapters


async def resolve_configured_adapters(
    configs: tuple[Any, ...], *, recover_power: Callable[[AdapterIdentity], bool] | None = None,
) -> list[Any]:
    """Resolve configured stable selectors to transient BlueZ adapter names.

    Imports are local to keep this read-only diagnostics module usable on its
    own and to avoid a package import cycle.
    """
    import asyncio
    from graber.bluetooth.adapter_manager import ResolvedAdapter

    discovered = await asyncio.to_thread(discover_adapters)
    usb_adapters = [adapter for adapter in discovered if adapter.is_usb]
    addresses = [config.controller_address.upper() for config in configs]
    resolved = []
    for config in configs:
        try:
            match = select_adapter(
                usb_adapters, address=config.controller_address, usb_path=config.usb_path
            )
        except LookupError:
            if not config.usb_path or addresses.count(config.controller_address.upper()) != 1:
                continue
            try:
                match = select_adapter(usb_adapters, address=config.controller_address)
            except LookupError:
                continue
        if match.powered is not True:
            # Only recover a uniquely identifiable, explicitly opted-in USB radio.
            if (match.powered is not False or recover_power is None or not config.auto_recover_power
                    or addresses.count(config.controller_address.upper()) != 1
                    or sum(adapter.address == match.address for adapter in discovered) != 1):
                continue
            if not await asyncio.to_thread(recover_power, match):
                continue
        resolved.append(ResolvedAdapter(config.id, match.address or "", match.hci_name, match.usb_path))
    return resolved


def select_adapter(
    adapters: Iterable[AdapterIdentity],
    *,
    address: str | None = None,
    usb_serial: str | None = None,
    usb_path: str | None = None,
) -> AdapterIdentity:
    """Resolve exactly one adapter from one or more stable selectors."""

    if not any((address, usb_serial, usb_path)):
        raise ValueError("an address, usb_serial, or usb_path selector is required")
    normalized_address = address.upper() if address else None
    matches = [
        adapter
        for adapter in adapters
        if (normalized_address is None or adapter.address == normalized_address)
        and (usb_serial is None or adapter.usb_serial == usb_serial)
        and (usb_path is None or adapter.usb_path == usb_path)
    ]
    if not matches:
        raise LookupError("no Bluetooth controller matches the stable selector")
    if len(matches) > 1:
        raise LookupError("stable selector is ambiguous; add controller address or USB path")
    return matches[0]


def adapter_preflight(
    adapters: Sequence[AdapterIdentity],
    *,
    expected: int = 2,
    require_usb: bool = True,
) -> list[CheckResult]:
    usb = [adapter for adapter in adapters if adapter.is_usb]
    selected = usb if require_usb else list(adapters)
    results = [
        CheckResult(
            "controller_count",
            len(selected) >= expected,
            f"found {len(selected)} eligible controller(s); expected at least {expected}",
        ),
        CheckResult(
            "unique_addresses",
            len({item.address for item in selected if item.address}) == len(selected),
            "eligible controller addresses must be present and unique",
        ),
        CheckResult(
            "stable_identity",
            all(item.address and (item.usb_serial or item.usb_path) for item in selected),
            "each controller needs an address plus USB serial or physical path",
        ),
        CheckResult(
            "powered",
            bool(selected) and all(item.powered is True for item in selected),
            "each eligible controller must report Powered: yes",
        ),
    ]
    return results


def run_host_checks(runner: CommandRunner = _default_runner) -> list[CheckResult]:
    """Run the documented, non-mutating host inspection commands."""

    checks = (
        ("usb", ("lsusb",)),
        ("bluez_controllers", ("bluetoothctl", "list")),
        ("rfkill", ("rfkill", "list", "bluetooth")),
        ("bluetooth_service", ("systemctl", "is-active", "bluetooth.service")),
    )
    results: list[CheckResult] = []
    for name, command in checks:
        try:
            completed = runner(command)
            detail = (completed.stdout or completed.stderr).strip()
            results.append(CheckResult(name, completed.returncode == 0, detail[:2000]))
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            results.append(CheckResult(name, False, f"{type(exc).__name__}: {exc}"))
    return results


def redact_address(address: str | None) -> str | None:
    if not address:
        return address
    parts = address.split(":")
    return ":".join((*parts[:3], "XX", "XX", parts[-1])) if len(parts) == 6 else "<redacted>"


def adapter_as_dict(adapter: AdapterIdentity, *, show_private: bool = False) -> dict[str, Any]:
    value = asdict(adapter)
    if not show_private:
        value["address"] = redact_address(adapter.address)
        if value["usb_serial"]:
            value["usb_serial"] = "<redacted>"
        value["sysfs_path"] = None
    value["stable_keys"] = list(adapter.stable_keys) if show_private else []
    return value
