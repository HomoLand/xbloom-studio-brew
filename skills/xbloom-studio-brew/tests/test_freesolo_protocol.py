"""Golden tests for official-app FreeSolo and Tea command ports."""

import struct

from xbloom_ble.protocol import (
    CMD_BREWER_ENTER,
    CMD_BREWER_START,
    CMD_GRINDER_ENTER,
    CMD_GRINDER_START,
    CMD_SCALE_ENTER,
    CMD_SCALE_EXIT,
    CMD_SCALE_TARE,
    CMD_TEA_RECIPE_CODE,
    CMD_TEA_RECIPE_MAKE,
    ROOM_TEMPERATURE_C,
    build_brewer_enter,
    build_brewer_start,
    build_grinder_enter,
    build_grinder_start,
    build_scale_enter,
    build_scale_exit,
    build_scale_tare,
    build_tea_load_frames,
    build_tea_recipe_code,
    build_tea_start,
    crc16_kermit,
    frame_command,
    j15_frame,
)


GREEN_POURS = [
    {"ml": 90, "temp": 85, "pattern": "ring", "pause": 20, "flow": 3.5},
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
