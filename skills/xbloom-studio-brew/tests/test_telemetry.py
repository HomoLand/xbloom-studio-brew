"""Telemetry decoding tests.

Notifications on ``ffe2`` use the shape ``58 02 07 | TYPE | SUB | LEN(u32le) |
c1 | payload | crc`` (distinct from the command frames we *send* to ``ffe1``).
We build faithful notification bytes with :func:`_notif` and also assert against
a handful of **golden frames captured verbatim** from the vendor app's HCI log,
so the decoder is pinned to real hardware output.
"""

import struct

import pytest

from xbloom_ble.protocol import crc16_kermit
from xbloom_ble.telemetry import (
    NotificationFrameStream,
    notification_frame_is_valid,
    parse_machine_info_payload,
    parse_notification,
)


def _finish(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16_kermit(body))


def _notif(ftype: int, state: int | None = None, sub: int = 0x1F) -> bytes:
    """Build a real-shape ``58 02 07`` notification.

    A ``0x57`` status frame carries ``c1 <state> 00 00 00``; other TYPEs (ACK
    echoes, heartbeats) carry just the ``c1`` marker.
    """
    head = bytes([0x58, 0x02, 0x07, ftype, sub])
    payload = bytes([0xC1]) + (bytes([state, 0, 0, 0]) if state is not None else b"")
    total = len(head) + 4 + len(payload) + 2
    return _finish(head + struct.pack("<I", total) + payload)


def _status(state: int) -> bytes:
    return _notif(0x57, state=state)


def _report(command: int, payload: bytes = b"") -> bytes:
    head = bytes.fromhex("580207") + struct.pack("<H", command)
    total = len(head) + 4 + 1 + len(payload) + 2
    return _finish(head + struct.pack("<I", total) + b"\xc1" + payload)


# --- state decoding (0x57 status frames) ----------------------------------

def test_idle_state():
    ev = parse_notification(_status(0x01))
    assert ev is not None
    assert ev.state == 0x01
    assert ev.state_name == "idle"
    assert ev.is_terminal


def test_armed_state():
    ev = parse_notification(_status(0x1F))
    assert ev.state == 0x1F
    assert ev.state_name == "armed"
    assert not ev.is_terminal
    assert not ev.is_heartbeat


def test_awaiting_confirm_state():
    assert parse_notification(_status(0x1E)).state_name == "awaiting_confirm"


def test_loading_state():
    assert parse_notification(_status(0x1D)).state_name == "loading"


def test_complete_state_is_terminal():
    ev = parse_notification(_status(0x41))
    assert ev.state_name == "complete"
    assert ev.is_terminal


def test_unknown_state():
    assert parse_notification(_status(0x77)).state_name == "unknown_0x77"


# --- cumulative water (0x4b), cup scale (0x15), and ACKs ------------------

def test_machine_water_report_decodes_cumulative_millilitres():
    # 0x4b = report 40523: float32 LE stores ml * 1000.
    ev = parse_notification(_report(40523, struct.pack("<f", 35_000.0)))
    assert ev.water_g == 35.0
    assert ev.dispensed_water_ml == 35.0
    assert ev.coffee_g is None
    assert not ev.is_heartbeat


def test_cup_scale_decodes_grams():
    # 0x15 = report 20501: raw cup-scale float32 LE in grams.
    ev = parse_notification(_report(20501, struct.pack("<f", 12.12)))
    assert ev.coffee_g == 12.12
    assert ev.scale_g == 12.12  # same 20501 report powers standalone scale mode
    assert ev.water_g is None


def test_dedicated_scale_report_10507_decodes_grams():
    # Full report command is 10507 = 0x290b, with float32 LE after c1.
    ev = parse_notification(_report(10507, struct.pack("<f", 12.5)))
    assert ev.command_code == 10507
    assert ev.scale_g == 12.5
    assert ev.coffee_g is None


def test_end_of_brew_liquid_values():
    # end-of-brew captures: 256.0 ml dispensed, 226.45 g raw cup reading.
    assert parse_notification(_report(40523, struct.pack("<f", 256_000.0))).water_g == 256.0
    assert parse_notification(_report(20501, struct.pack("<f", 226.45))).coffee_g == 226.45


def test_scale_zero_and_negative_readings_are_kept():
    # A genuine 0.0 ml cumulative report at brew start is kept…
    assert parse_notification(_report(40523, struct.pack("<f", 0.0))).water_g == 0.0
    # …and cup removal after the mandatory entry auto-zero remains visible.
    negative = parse_notification(_report(20501, struct.pack("<f", -38.41)))
    assert negative.coffee_g == negative.scale_g == -38.41

    dedicated = _report(10507, struct.pack("<f", -12.5))
    assert parse_notification(dedicated).scale_g == -12.5


def test_machine_info_report_matches_app_fixed_width_layout():
    payload = bytearray(63)
    payload[0:13] = b"SN12345678901"
    payload[13:19] = b"J15   "
    payload[19:29] = b"V12.0D.500"
    struct.pack_into("<f", payload, 29, 42.5)
    payload[33:42] = bytes([1, 2, 3, 1, 92, 8, 24, 1, 1])
    payload[51:55] = bytes.fromhex("91327856")
    struct.pack_into("<I", payload, 55, 7)
    struct.pack_into("<I", payload, 59, 9)

    info = parse_machine_info_payload(payload)
    assert info == {
        "serial_number": "SN12345678901",
        "model": "J15",
        "firmware": "V12.0D.500",
        "area_ap": 42.5,
        "water_enough": True,
        "system_status": 2,
        "user_count": 3,
        "water_source": "tap",
        "grind_setting": 62,
        "display": "medium",
        "voltage_raw": 24,
        "temperature_unit": "C",
        "weight_unit": "g",
        "mode": "auto",
        "pouring_radius_init": 7,
        "vibration_init": 9,
    }

    report = _report(40521, bytes(payload))
    event = parse_notification(report)
    assert event.command_code == 40521
    assert event.state_name == "machine_info"
    assert event.machine_info == info


def test_command_echo_is_ack():
    # A notification whose TYPE byte equals the command sent = that command's ACK.
    for cmd in (0xA4, 0xA6, 0xA8, 0x41):
        ev = parse_notification(_notif(cmd))
        assert ev is not None
        assert ev.state is None
        assert ev.state_name == f"ack_0x{cmd:02x}"
        assert ev.raw[3] == cmd  # this is how the client matches an ACK
        assert ev.command_code == 0x1F00 | cmd


def test_control_grade_freesolo_reports_are_named_and_decoded():
    paused = parse_notification(_report(9010))
    assert paused.report_name == paused.state_name == "brewer_paused"

    pattern = parse_notification(_report(8107, struct.pack("<I", 2)))
    assert pattern.report_name == "brewer_pattern"
    assert pattern.brewer_pattern == "spiral"

    temperature = parse_notification(_report(8108, struct.pack("<I", 85)))
    assert temperature.report_name == "brewer_temperature"
    assert temperature.brewer_temperature_value == 85


def test_water_volume_is_machine_dispensed_volume_not_tank_inventory():
    event = parse_notification(_report(40523, struct.pack("<f", 123_400.0)))
    assert event.command_code == 40523
    assert event.report_name == "water_volume"
    assert event.dispensed_water_ml == 123.4
    assert event.water_g == 123.4  # retained compatibility alias
    assert event.cup_weight_g is None


def test_persistent_settings_report_decodes_three_independent_values():
    event = parse_notification(_report(8015, struct.pack("<III", 2, 0, 1)))
    assert event.report_name == event.state_name == "settings_changed"
    assert event.report_values == {
        "weight_unit": "oz",
        "temperature_unit": "F",
        "water_source": "tap",
    }


@pytest.mark.parametrize(
    ("command", "name", "value"),
    [
        (11506, "pour_radius", 720),
        (11507, "pour_radius_written", 720),
        (11508, "vibration_amplitude", 1300),
        (11509, "vibration_amplitude_written", 1300),
    ],
)
def test_advanced_setting_reports_decode_u32(command, name, value):
    event = parse_notification(_report(command, struct.pack("<I", value)))
    assert event.report_name == event.state_name == name
    assert event.report_value == value


@pytest.mark.parametrize(
    ("command", "name"),
    [(9012, "tea_soaking"), (40515, "tea_paused"), (9011, "tea_restarted")],
)
def test_tea_phase_reports_are_named(command, name):
    event = parse_notification(_report(command))
    assert event.report_name == event.state_name == name


def test_tea_soak_time_error_and_xpod_reports_are_structured():
    soak = parse_notification(_report(8113, struct.pack("<I", 45)))
    assert soak.report_name == "tea_soak_time_changed"
    assert soak.report_value == 45

    error = parse_notification(_report(40522, struct.pack("<I", 1)))
    assert error.report_name == "no_water_report"
    assert error.report_value == 1
    assert error.is_error is True

    xpod = parse_notification(_report(40501, b"AB12CD"))
    assert xpod.report_name == "xpod_detected"
    assert xpod.report_values == {"xid": "AB12CD"}


def test_full_command_dispatch_prevents_low_byte_report_collisions():
    awake = parse_notification(_report(8011))  # 0x1f4b shares 0x4b with water
    assert awake.command_code == 8011
    assert awake.state_name == awake.report_name == "machine_awake"
    assert awake.dispensed_water_ml is None

    activity = parse_notification(_report(8023, struct.pack("<I", 7)))
    assert activity.command_code == 8023
    assert activity.report_name == "machine_activity"
    assert activity.report_value == 7
    assert activity.state == 7
    assert activity.state_name == "unknown_0x07"

    unrelated_status_low_byte = parse_notification(
        _report(10583, struct.pack("<I", 7))
    )
    assert unrelated_status_low_byte.command_code == 10583
    assert unrelated_status_low_byte.state is None


def test_notification_stream_reassembles_split_and_coalesced_frames():
    stream = NotificationFrameStream()
    armed = _status(0x1F)
    awake = _report(8011)

    assert stream.feed(armed[:6]) == []
    assert stream.pending_bytes == 6
    assert stream.feed(armed[6:] + awake) == [armed, awake]
    assert stream.pending_bytes == 0
    assert stream.stats.frames_emitted == 2


def test_notification_stream_resynchronises_and_rejects_bad_frames():
    stream = NotificationFrameStream()
    valid = _status(0x01)
    corrupt_crc = bytearray(_status(0x1F))
    corrupt_crc[-1] ^= 0xFF
    invalid_length = bytearray(_report(8011))
    struct.pack_into("<I", invalid_length, 5, 1)

    assert stream.feed(b"noise\x58" + b"\x02") == []
    frames = stream.feed(b"\x07" + bytes(corrupt_crc[3:]) + bytes(invalid_length) + valid)
    assert frames == [valid]
    assert stream.stats.invalid_crc_frames >= 1
    assert stream.stats.invalid_length_frames >= 1
    assert stream.stats.bytes_discarded > 0


def test_direct_parser_is_crc_strict_with_explicit_forensic_escape_hatch():
    frame = bytearray(_report(8011))
    assert notification_frame_is_valid(frame)
    frame[-1] ^= 0xFF
    assert parse_notification(frame) is None
    assert parse_notification(frame, validate_crc=False).report_name == "machine_awake"


# --- misc ------------------------------------------------------------------

def test_non_notification_bytes_return_none():
    assert parse_notification(b"\x00\x01\x02") is None
    assert parse_notification(b"") is None
    assert parse_notification(b"\x58\x02\x07") is None  # too short


def test_accepts_hex_string():
    assert parse_notification(_status(0x1F).hex()).state_name == "armed"


# --- golden frames captured verbatim from the vendor app ------------------

def test_golden_captured_frames():
    # (hex, expected state_name, expected state) — real ffe2 notifications.
    cases = [
        ("580207571f10000000c11f000000ce5e", "armed", 0x1F),
        ("580207571f10000000c1010000002d33", "idle", 0x01),
        ("580207571f10000000c11e0000007542", "awaiting_confirm", 0x1E),
        ("580207a61f0c000000c12b8f", "ack_0xa6", None),   # a6 (dose) ACK
        ("580207411f0c000000c1ab6a", "ack_0x41", None),   # 41 (pours) ACK
        ("5802074b9e10000000c100000000fd32", "scale", None),   # 40523: 0.0 ml
        ("580207155010000000c10000000016b5", "scale", None),   # 20501 cup: 0.0 g
    ]
    for hx, name, state in cases:
        ev = parse_notification(hx)
        assert ev is not None, hx
        assert ev.state_name == name, (hx, ev.state_name)
        assert ev.state == state, (hx, ev.state)
