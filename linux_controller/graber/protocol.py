"""Pure Graber BLE protocol definitions and command encoding.

This module deliberately contains no Bluetooth I/O.  The byte sequences are
limited to behavior documented in ``docs/PROTOCOL.md`` and verified captures.
Callers are responsible for authentication, waiting for
write acknowledgements, and enforcing each step's minimum spacing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


BOTTOM_SERVICE = "de8a5aac-a99b-c315-0c80-60b63a869124"
TOP_SERVICE = "375b4079-d234-4631-9592-65c87174410c"
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"

AUTH_CHARACTERISTIC = "5b026510-4088-c297-46d8-be6cb63a8691"
BOTTOM_TARGET_CHARACTERISTIC = "3ead2b49-abbc-4602-ac80-54d4b5687bac"
BOTTOM_POSITION_CHARACTERISTIC = "ebc1ed9c-e9e1-4db3-a8f5-ff249944b442"
PRESET_CHARACTERISTIC = "e3fc1ebf-e615-44f2-b1dd-b6b95266a453"
TOP_TARGET_CHARACTERISTIC = "a506fbb1-9777-4e43-aa13-67d1bab6c0a1"
EXECUTE_CHARACTERISTIC = "bb27521d-2fd4-4187-af9f-5d0780238d7e"
TOP_POSITION_CHARACTERISTIC = "900eeaeb-0f27-4808-809c-4026a8fd7d5a"
BATTERY_CHARACTERISTIC = "00002a19-0000-1000-8000-00805f9b34fb"


class Rail(str, Enum):
    """A motor rail, expressed in the motor's raw coordinate system."""

    BOTTOM = "bottom"
    TOP = "top"


class CommandKind(str, Enum):
    SET_POSITION = "set_position"
    STOP = "stop"
    FAVORITE = "favorite"
    FULL_OPEN = "full_open"
    FULL_CLOSE = "full_close"
    PRESET = "preset"
    STACK_UP = "stack_up"
    STACK_DOWN = "stack_down"
    PAIRED_ABSOLUTE = "paired_absolute"


# Backward-friendly internal name; new controller code should use CommandKind.
Operation = CommandKind


@dataclass(frozen=True, slots=True)
class WriteStep:
    """One acknowledged GATT write and spacing before the following write."""

    characteristic: str
    value: bytes
    delay_after_s: float = 0.0
    effectful: bool = True


@dataclass(frozen=True, slots=True)
class AdvertisedPosition:
    """Validated rail values from one manufacturer-data payload."""

    bottom: int
    top: int | None = None


def auth_payload(access_code: str) -> bytes:
    """Validate and encode the observed seven-ASCII-digit access token."""

    if len(access_code) != 7 or not access_code.isascii() or not access_code.isdigit():
        raise ValueError("access code must contain exactly seven ASCII digits")
    return access_code.encode("ascii")


encode_access_code = auth_payload


def _position(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("position must be an integer from 0 through 100")
    return value


def ha_to_raw_position(rail: Rail, position: int) -> int:
    """Convert HA's open-percentage convention to the motor's raw value."""

    value = _position(position)
    return value if rail is Rail.BOTTOM else 100 - value


def raw_to_ha_position(rail: Rail, position: int) -> int:
    """Convert a validated raw motor value to HA's open percentage."""

    value = _position(position)
    return value if rail is Rail.BOTTOM else 100 - value


home_assistant_to_raw = ha_to_raw_position
raw_to_home_assistant = raw_to_ha_position


def set_position(rail: Rail, raw_position: int) -> tuple[WriteStep, ...]:
    """Encode one independent absolute rail target.

    Independent absolute commands are intentionally encoded one rail at a
    time.  No API for paired absolute targets is supplied because persistence
    of two arbitrary targets has not been verified.
    """

    value = bytes((_position(raw_position),))
    if rail is Rail.BOTTOM:
        return (
            WriteStep(BOTTOM_TARGET_CHARACTERISTIC, value, 0.100, False),
            WriteStep(AUTH_CHARACTERISTIC, b"\x14"),
        )
    return (
        WriteStep(TOP_TARGET_CHARACTERISTIC, value, 0.100, False),
        WriteStep(EXECUTE_CHARACTERISTIC, b"\x14"),
    )


def stop(*, has_execute_characteristic: bool = True) -> tuple[WriteStep, ...]:
    """Encode Stop; the secondary write is used only when exposed by the motor."""

    first = WriteStep(
        AUTH_CHARACTERISTIC,
        b"\x10",
        0.100 if has_execute_characteristic else 0.0,
    )
    if not has_execute_characteristic:
        return (first,)
    return (first, WriteStep(EXECUTE_CHARACTERISTIC, b"\x10"))


def favorite() -> tuple[WriteStep, ...]:
    """Encode Favorite, verified on the documented TDBU firmware only."""

    return (WriteStep(AUTH_CHARACTERISTIC, b"\x16"),)


def full_open(*, tdbu: bool) -> tuple[WriteStep, ...]:
    if tdbu:
        return (WriteStep(AUTH_CHARACTERISTIC, b"\x12"),)
    return set_position(Rail.BOTTOM, 100)


def full_close(*, tdbu: bool) -> tuple[WriteStep, ...]:
    if tdbu:
        return (
            WriteStep(EXECUTE_CHARACTERISTIC, b"\x12", 0.050),
            WriteStep(AUTH_CHARACTERISTIC, b"\x13"),
        )
    return set_position(Rail.BOTTOM, 0)


def preset(index: int) -> tuple[WriteStep, ...]:
    """Encode the observed preset-index sequence.

    Index 5 and captured custom indices use this sequence.  Whether a given
    index is configured on a particular blind is deployment-specific.
    """

    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 255:
        raise ValueError("preset index must fit in one byte")
    return (
        WriteStep(PRESET_CHARACTERISTIC, bytes((index,)), 0.050, False),
        WriteStep(AUTH_CHARACTERISTIC, b"\x15"),
    )


def stack(direction: CommandKind) -> tuple[WriteStep, ...]:
    """Encode the fixed five-point TDBU shared relative movement."""

    if direction not in (CommandKind.STACK_UP, CommandKind.STACK_DOWN):
        raise ValueError("stack direction must be STACK_UP or STACK_DOWN")
    execute = b"\x18" if direction is CommandKind.STACK_UP else b"\x19"
    return (
        WriteStep(BOTTOM_TARGET_CHARACTERISTIC, b"\x05", 0.070, False),
        WriteStep(TOP_TARGET_CHARACTERISTIC, b"\x05", 0.070, False),
        WriteStep(EXECUTE_CHARACTERISTIC, execute),
    )


def position_from_notification(value: bytes | bytearray | memoryview) -> int:
    """Validate the first position byte from a rail notification/read."""

    raw = bytes(value)
    if not raw:
        raise ValueError("empty position notification")
    return _position(raw[0])


def battery_from_read(value: bytes | bytearray | memoryview) -> int:
    """Validate a standard Battery Level read without correcting motor data."""

    raw = bytes(value)
    if not raw:
        raise ValueError("empty battery read")
    return _position(raw[0])


def parse_manufacturer_payload(payload: bytes, *, tdbu: bool) -> AdvertisedPosition | None:
    """Parse the observed manufacturer-data position offsets.

    The manufacturer/company identifier is not interpreted because it has not
    been documented.  Payloads with invalid position bytes are ignored.  A
    TDBU payload must contain both valid rails rather than inventing a value.
    """

    if len(payload) < 4 or payload[2] > 100:
        return None
    if tdbu:
        if payload[3] > 100:
            return None
        return AdvertisedPosition(bottom=payload[2], top=payload[3])
    return AdvertisedPosition(bottom=payload[2])


def parse_manufacturer_position(data: bytes, dual_rail: bool) -> AdvertisedPosition | None:
    """Stable controller-facing wrapper around the observed advertisement parser."""

    return parse_manufacturer_payload(data, tdbu=dual_rail)


def parse_manufacturer_data(
    manufacturer_data: Mapping[int, bytes] | Iterable[bytes], *, tdbu: bool
) -> AdvertisedPosition | None:
    """Return the first valid position payload from a Bleak-style collection."""

    payloads = manufacturer_data.values() if isinstance(manufacturer_data, Mapping) else manufacturer_data
    for payload in payloads:
        parsed = parse_manufacturer_payload(bytes(payload), tdbu=tdbu)
        if parsed is not None:
            return parsed
    return None


def build_command(
    kind: CommandKind,
    *,
    rail: Rail | None = None,
    position: int | None = None,
    dual_rail: bool = True,
    has_execute_characteristic: bool | None = None,
) -> tuple[WriteStep, ...]:
    """Build one verified command without performing I/O.

    ``position`` is the raw motor position for SET_POSITION and the one-byte
    preset index for PRESET.  Paired arbitrary absolute positioning is
    explicitly unavailable because the tested sequence did not preserve both
    targets.
    """

    if kind is CommandKind.PAIRED_ABSOLUTE:
        raise NotImplementedError("paired arbitrary absolute targets are not verified")
    if kind is CommandKind.SET_POSITION:
        if rail is None or position is None:
            raise ValueError("SET_POSITION requires rail and raw position")
        if rail is Rail.TOP and not dual_rail:
            raise ValueError("top-rail commands require a dual-rail blind")
        return set_position(rail, position)
    if kind is CommandKind.STOP:
        available = dual_rail if has_execute_characteristic is None else has_execute_characteristic
        return stop(has_execute_characteristic=available)
    if kind is CommandKind.FAVORITE:
        if not dual_rail:
            raise ValueError("favorite is only verified for the documented dual-rail firmware")
        return favorite()
    if kind is CommandKind.FULL_OPEN:
        return full_open(tdbu=dual_rail)
    if kind is CommandKind.FULL_CLOSE:
        return full_close(tdbu=dual_rail)
    if kind is CommandKind.PRESET:
        if not dual_rail:
            raise ValueError("preset sequence is only verified for dual-rail blinds")
        if position is None:
            raise ValueError("PRESET requires an index in position")
        return preset(position)
    if kind in (CommandKind.STACK_UP, CommandKind.STACK_DOWN):
        if not dual_rail:
            raise ValueError("stack commands require a dual-rail blind")
        return stack(kind)
    raise ValueError(f"unsupported command kind: {kind!r}")
