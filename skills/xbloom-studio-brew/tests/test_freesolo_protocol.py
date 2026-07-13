"""Golden tests for official-app FreeSolo and Tea command ports."""

import struct

import pytest

from xbloom_ble.protocol import (
    CMD_BREWER_PAUSE,
    CMD_BREWER_RESUME,
    CMD_BREWER_SET_PATTERN,
    CMD_BREWER_SET_TEMPERATURE,
    CMD_COFFEE_PAUSE,
    CMD_COFFEE_RESUME,
    CMD_BREWER_ENTER,
    CMD_BREWER_START,
    CMD_GRINDER_ENTER,
    CMD_GRINDER_PAUSE,
    CMD_GRINDER_RESUME,
    CMD_GRINDER_START,
    CMD_READ_POUR_RADIUS,
    CMD_READ_VIBRATION_AMPLITUDE,
    CMD_SCALE_ENTER,
    CMD_SCALE_EXIT,
    CMD_SCALE_TARE,
    CMD_SET_DISPLAY,
    CMD_SET_TEMPERATURE_UNIT,
    CMD_SET_WATER_SOURCE,
    CMD_SET_WEIGHT_UNIT,
    CMD_TEA_RECIPE_CODE,
    CMD_TEA_RECIPE_MAKE,
    CMD_WRITE_POUR_RADIUS,
    CMD_WRITE_VIBRATION_AMPLITUDE,
    ROOM_TEMPERATURE_C,
    build_brewer_enter,
    build_brewer_pause,
    build_brewer_resume,
    build_brewer_set_pattern,
    build_brewer_set_temperature,
    build_brewer_start,
    build_coffee_pause,
    build_coffee_resume,
    build_grinder_enter,
    build_grinder_pause,
    build_grinder_resume,
    build_grinder_start,
    build_read_pour_radius,
    build_read_vibration_amplitude,
    build_scale_enter,
    build_scale_exit,
    build_scale_tare,
    build_set_display,
    build_set_temperature_unit,
    build_set_water_source,
    build_set_weight_unit,
    build_tea_load_frames,
    build_tea_recipe_code,
    build_tea_start,
    build_write_pour_radius,
    build_write_vibration_amplitude,
    crc16_kermit,
    frame_command,
    j15_frame,
)


GREEN_POURS = [
    {"ml": 90, "temp": 85, "pattern": "circular", "pause": 20, "flow": 3.5},
    {"ml": 90, "temp": 85, "pattern": "center", "pause": 15, "flow": 3.5},
]


def _words(frame: bytes) -> tuple[int, ...]:
    payload = frame[10:-2]
    assert len(payload) % 4 == 0
    return tuple(struct.unpack(f"<{len(payload) // 4}I", payload))


def test_generic_j15_frame_matches_official_app_layout():
    frame = j15_frame(8003)
    assert frame[:3] == bytes.fromhex("580101")
    assert frame_command(frame) == 8003
    assert struct.unpack_from("<I", frame, 5)[0] == len(frame) == 12
    assert frame[9] == 0x01
    assert struct.unpack("<H", frame[-2:])[0] == crc16_kermit(frame[:-2])


def test_scale_commands_have_exact_app_command_codes_and_no_data():
    assert frame_command(build_scale_enter()) == CMD_SCALE_ENTER == 8003
    assert frame_command(build_scale_exit()) == CMD_SCALE_EXIT == 8014
    assert frame_command(build_scale_tare()) == CMD_SCALE_TARE == 8500
    assert all(len(frame) == 12 for frame in (build_scale_enter(), build_scale_exit(), build_scale_tare()))


def test_grinder_frames_match_grinder_activity_arguments():
    enter = build_grinder_enter(62, 100)
    start = build_grinder_start(62, 100)
    assert frame_command(enter) == CMD_GRINDER_ENTER == 8006
    assert _words(enter) == (62, 100)
    assert frame_command(start) == CMD_GRINDER_START == 3500
    assert _words(start) == (1000, 62, 100)


def test_brewer_frames_match_brewer_activity_float_bit_arguments():
    enter = build_brewer_enter(85, "center")
    start = build_brewer_start(250, 85, 3.5, "spiral")
    assert frame_command(enter) == CMD_BREWER_ENTER == 8007
    pattern, temp_bits = _words(enter)
    assert pattern == 0
    assert struct.unpack("<f", struct.pack("<I", temp_bits))[0] == 850.0

    assert frame_command(start) == CMD_BREWER_START == 4506
    flow_bits, volume_bits, temp_bits, water_feed, pattern = _words(start)
    assert struct.unpack("<f", struct.pack("<I", flow_bits))[0] == 35.0
    assert struct.unpack("<f", struct.pack("<I", volume_bits))[0] == 2500.0
    assert struct.unpack("<f", struct.pack("<I", temp_bits))[0] == 850.0
    assert (water_feed, pattern) == (0, 2)


def test_brewer_rt_uses_official_room_temperature_sentinel():
    enter = build_brewer_enter(ROOM_TEMPERATURE_C, "center")
    _, enter_temp_bits = _words(enter)
    assert struct.unpack("<f", struct.pack("<I", enter_temp_bits))[0] == 200.0

    start = build_brewer_start(120, ROOM_TEMPERATURE_C, 3.5, "center")
    _, _, start_temp_bits, _, _ = _words(start)
    assert struct.unpack("<f", struct.pack("<I", start_temp_bits))[0] == 200.0


def test_brewer_start_supports_both_studio_water_sources():
    tank = _words(build_brewer_start(120, 85, water_feed=0))
    tap = _words(build_brewer_start(120, 85, water_feed=1))
    assert tank[-2] == 0
    assert tap[-2] == 1


def test_interactive_control_frames_match_apk_command_codes():
    assert frame_command(build_coffee_pause()) == CMD_COFFEE_PAUSE == 40518
    assert frame_command(build_coffee_resume()) == CMD_COFFEE_RESUME == 40524
    assert frame_command(build_brewer_pause()) == CMD_BREWER_PAUSE == 8019
    assert frame_command(build_brewer_resume()) == CMD_BREWER_RESUME == 8021
    assert frame_command(build_grinder_pause()) == CMD_GRINDER_PAUSE == 8018
    assert frame_command(build_grinder_resume()) == CMD_GRINDER_RESUME == 8020

    pattern = build_brewer_set_pattern("spiral")
    assert frame_command(pattern) == CMD_BREWER_SET_PATTERN == 8016
    assert _words(pattern) == (2,)

    temperature = build_brewer_set_temperature(85)
    assert frame_command(temperature) == CMD_BREWER_SET_TEMPERATURE == 4510
    assert _words(temperature) == (850,)

    rt = build_brewer_set_temperature(ROOM_TEMPERATURE_C)
    assert _words(rt) == (200,)


def test_persistent_setting_frames_are_byte_exact_to_apk_encoding():
    frames = {
        "weight_g": build_set_weight_unit("g"),
        "temperature_c": build_set_temperature_unit("C"),
        "water_tap": build_set_water_source("tap"),
        "display_high": build_set_display("high"),
    }
    assert {name: frame_command(frame) for name, frame in frames.items()} == {
        "weight_g": CMD_SET_WEIGHT_UNIT,
        "temperature_c": CMD_SET_TEMPERATURE_UNIT,
        "water_tap": CMD_SET_WATER_SOURCE,
        "display_high": CMD_SET_DISPLAY,
    }
    assert (CMD_SET_WEIGHT_UNIT, CMD_SET_TEMPERATURE_UNIT) == (8005, 8010)
    assert (CMD_SET_WATER_SOURCE, CMD_SET_DISPLAY) == (4508, 8103)
    assert frames["weight_g"].hex() == "580101451f10000000010100000007ab"
    assert frames["temperature_c"].hex() == "5801014a1f1000000001010000004bb7"
    assert frames["water_tap"].hex() == "5801019c111000000001010000009ced"
    assert frames["display_high"].hex() == "580101a71f10000000010f000000f313"


def test_advanced_tuning_uses_code_module2_frame_type_and_exact_values():
    frames = [
        build_read_pour_radius(),
        build_write_pour_radius(720),
        build_read_vibration_amplitude(),
        build_write_vibration_amplitude(1300),
    ]
    assert [frame_command(frame) for frame in frames] == [
        CMD_READ_POUR_RADIUS,
        CMD_WRITE_POUR_RADIUS,
        CMD_READ_VIBRATION_AMPLITUDE,
        CMD_WRITE_VIBRATION_AMPLITUDE,
    ]
    assert (
        CMD_READ_POUR_RADIUS,
        CMD_WRITE_POUR_RADIUS,
        CMD_READ_VIBRATION_AMPLITUDE,
        CMD_WRITE_VIBRATION_AMPLITUDE,
    ) == (11506, 11507, 11508, 11509)
    assert all(frame[:3] == bytes.fromhex("580102") for frame in frames)
    assert frames[0].hex() == "580102f22c0c0000000155de"
    assert frames[1].hex() == "580102f32c1000000001d0020000bf83"
    assert frames[2].hex() == "580102f42c0c000000019886"
    assert frames[3].hex() == "580102f52c100000000114050000f8b3"


@pytest.mark.parametrize(
    ("builder", "value"),
    [
        (build_write_pour_radius, 399),
        (build_write_pour_radius, 1001),
        (build_write_vibration_amplitude, 999),
        (build_write_vibration_amplitude, 1250),
        (build_write_vibration_amplitude, 1600),
    ],
)
def test_advanced_tuning_rejects_values_outside_apk_ui_envelope(builder, value):
    with pytest.raises(ValueError):
        builder(value)


def test_official_green_tea_blob_is_byte_exact():
    # Port of GetRecipeCodeManager + native TeaRecipeCreate for the public
    # official green-tea recipe. Suffix 32 c2 = grinderSize 50 and
    # byteValue(grandWater 45 * 10).
    code = build_tea_recipe_code(GREEN_POURS)
    assert code.hex() == "105a550100ec0078235a550000f100002332c2"


def test_tea_pause_minute_encoding_matches_native_transform():
    code = build_tea_recipe_code(
        [{"ml": 90, "temp": 99, "pattern": "ring", "pause": 120, "flow": 3.5}]
    )
    # stage = 5a 63 01 00 | pause 00 40 | rpm 78 | flow 23
    assert code[5:7] == bytes.fromhex("0040")


def test_tea_load_and_execute_are_separate_commands():
    frames = build_tea_load_frames({"pours": GREEN_POURS})
    assert [frame_command(frame) for frame in frames] == [8104, CMD_TEA_RECIPE_CODE]
    assert CMD_TEA_RECIPE_MAKE not in {frame_command(frame) for frame in frames}
    assert frame_command(build_tea_start()) == CMD_TEA_RECIPE_MAKE == 4512
