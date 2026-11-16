import pytest

from graber import protocol


def values(steps):
    return [(step.characteristic, step.value, step.delay_after_s) for step in steps]


def test_access_code_is_exactly_seven_ascii_digits():
    assert protocol.encode_access_code("0012345") == b"0012345"
    for invalid in ("123456", "12345678", "12345x7", "１２３４５６７"):
        with pytest.raises(ValueError):
            protocol.encode_access_code(invalid)


def test_bottom_and_top_absolute_sequences_are_independent():
    bottom = protocol.set_position(protocol.Rail.BOTTOM, 37)
    top = protocol.set_position(protocol.Rail.TOP, 82)
    assert values(bottom) == [
        (protocol.BOTTOM_TARGET_CHARACTERISTIC, b"\x25", 0.1),
        (protocol.AUTH_CHARACTERISTIC, b"\x14", 0.0),
    ]
    assert values(top) == [
        (protocol.TOP_TARGET_CHARACTERISTIC, b"\x52", 0.1),
        (protocol.EXECUTE_CHARACTERISTIC, b"\x14", 0.0),
    ]


def test_tdbu_full_open_close_preserve_captured_order_and_spacing():
    assert values(protocol.full_open(tdbu=True)) == [
        (protocol.AUTH_CHARACTERISTIC, b"\x12", 0.0)
    ]
    assert values(protocol.full_close(tdbu=True)) == [
        (protocol.EXECUTE_CHARACTERISTIC, b"\x12", 0.05),
        (protocol.AUTH_CHARACTERISTIC, b"\x13", 0.0),
    ]


def test_single_rail_open_close_use_absolute_targets():
    assert protocol.full_open(tdbu=False) == protocol.set_position(protocol.Rail.BOTTOM, 100)
    assert protocol.full_close(tdbu=False) == protocol.set_position(protocol.Rail.BOTTOM, 0)


def test_stop_uses_both_characteristics_when_available():
    assert values(protocol.stop()) == [
        (protocol.AUTH_CHARACTERISTIC, b"\x10", 0.1),
        (protocol.EXECUTE_CHARACTERISTIC, b"\x10", 0.0),
    ]
    assert values(protocol.stop(has_execute_characteristic=False)) == [
        (protocol.AUTH_CHARACTERISTIC, b"\x10", 0.0)
    ]
    assert len(protocol.build_command(
        protocol.CommandKind.STOP, dual_rail=False, has_execute_characteristic=True
    )) == 2


def test_favorite_and_preset_sequences():
    assert values(protocol.favorite()) == [(protocol.AUTH_CHARACTERISTIC, b"\x16", 0.0)]
    assert values(protocol.preset(5)) == [
        (protocol.PRESET_CHARACTERISTIC, b"\x05", 0.05),
        (protocol.AUTH_CHARACTERISTIC, b"\x15", 0.0),
    ]


@pytest.mark.parametrize(
    ("direction", "execute"),
    [(protocol.Operation.STACK_UP, b"\x18"), (protocol.Operation.STACK_DOWN, b"\x19")],
)
def test_stack_is_fixed_five_point_shared_relative_sequence(direction, execute):
    assert values(protocol.stack(direction)) == [
        (protocol.BOTTOM_TARGET_CHARACTERISTIC, b"\x05", 0.07),
        (protocol.TOP_TARGET_CHARACTERISTIC, b"\x05", 0.07),
        (protocol.EXECUTE_CHARACTERISTIC, execute, 0.0),
    ]


def test_top_position_is_inverted_only_at_home_assistant_boundary():
    assert protocol.raw_to_home_assistant(protocol.Rail.BOTTOM, 31) == 31
    assert protocol.raw_to_home_assistant(protocol.Rail.TOP, 31) == 69
    assert protocol.home_assistant_to_raw(protocol.Rail.TOP, 69) == 31


@pytest.mark.parametrize(
    ("rail", "raw", "ha"),
    [
        (protocol.Rail.BOTTOM, 0, 0),
        (protocol.Rail.BOTTOM, 100, 100),
        (protocol.Rail.TOP, 0, 100),
        (protocol.Rail.TOP, 100, 0),
    ],
)
def test_home_assistant_rail_boundaries_round_trip(rail, raw, ha):
    assert protocol.raw_to_ha_position(rail, raw) == ha
    assert protocol.ha_to_raw_position(rail, ha) == raw


def test_shared_stack_motion_moves_ha_rail_values_in_opposite_numeric_directions():
    before_bottom_raw, before_top_raw = 26, 71
    after_bottom_raw, after_top_raw = before_bottom_raw + 5, before_top_raw + 5
    assert (
        protocol.raw_to_ha_position(protocol.Rail.BOTTOM, before_bottom_raw),
        protocol.raw_to_ha_position(protocol.Rail.TOP, before_top_raw),
    ) == (26, 29)
    assert (
        protocol.raw_to_ha_position(protocol.Rail.BOTTOM, after_bottom_raw),
        protocol.raw_to_ha_position(protocol.Rail.TOP, after_top_raw),
    ) == (31, 24)


def test_manufacturer_payload_uses_known_offsets_and_rejects_invalid_data():
    assert protocol.parse_manufacturer_payload(b"\xaa\xbb\x1f\x47", tdbu=True) == (
        protocol.AdvertisedPosition(bottom=31, top=71)
    )
    assert protocol.parse_manufacturer_payload(b"\xaa\xbb\x1f\xfe", tdbu=True) is None
    assert protocol.parse_manufacturer_payload(b"\xaa\xbb\xfe\x47", tdbu=False) is None
    assert protocol.parse_manufacturer_payload(b"\xaa\xbb\x1f", tdbu=False) is None


def test_notification_and_battery_values_are_validated():
    assert protocol.position_from_notification(b"\x64extra") == 100
    assert protocol.battery_from_read(b"\x00") == 0
    with pytest.raises(ValueError):
        protocol.position_from_notification(b"\x65")
    with pytest.raises(ValueError):
        protocol.battery_from_read(b"")


def test_invalid_ranges_and_stack_operation_are_rejected():
    with pytest.raises(ValueError):
        protocol.set_position(protocol.Rail.BOTTOM, 101)
    with pytest.raises(ValueError):
        protocol.preset(256)
    with pytest.raises(ValueError):
        protocol.stack(protocol.Operation.STOP)


def test_only_execute_steps_cross_the_replay_boundary():
    steps = protocol.set_position(protocol.Rail.BOTTOM, 50)
    assert [step.effectful for step in steps] == [False, True]
    close = protocol.full_close(tdbu=True)
    assert [step.effectful for step in close] == [True, True]


def test_stable_build_command_api_and_explicit_paired_absolute_rejection():
    assert protocol.build_command(
        protocol.CommandKind.SET_POSITION,
        rail=protocol.Rail.BOTTOM,
        position=42,
        dual_rail=False,
    ) == protocol.set_position(protocol.Rail.BOTTOM, 42)
    with pytest.raises(ValueError, match="top-rail"):
        protocol.build_command(
            protocol.CommandKind.SET_POSITION,
            rail=protocol.Rail.TOP,
            position=42,
            dual_rail=False,
        )
    with pytest.raises(NotImplementedError, match="not verified"):
        protocol.build_command(protocol.CommandKind.PAIRED_ABSOLUTE)
    with pytest.raises(ValueError, match="only verified"):
        protocol.build_command(protocol.CommandKind.FAVORITE, dual_rail=False)
