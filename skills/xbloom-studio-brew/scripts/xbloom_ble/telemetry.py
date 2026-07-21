"""Decode xBloom Studio status notifications (the ``ffe2`` characteristic).

The machine pushes status frames to the ``ffe2`` notify characteristic. Unlike
the command frames sent to ``ffe1``, notifications use a distinct envelope
(verified from the vendor app's HCI capture)::

    58 02 07 | COMMAND/REPORT(u16le) | LEN(u32le) | 0xc1 | payload | CRC16(u16le)

The low command byte at offset 3 remains useful for the legacy coffee parser:
  - a **command echo / ACK** carries the full command written by the app.
  - ``0x57`` — a **status** frame; the byte right after ``0xc1`` is the machine
    *state* (see table).
  - full report 10507 carries standalone-scale grams; report 20501 (low byte
    ``0x15``) carries the cup-scale reading in grams.
  - report 40523 (low byte ``0x4b``) carries cumulative machine-dispensed water
    in millilitres, encoded as a float multiplied by 1000. It is not tank level.
  - ``0x49`` — machine-info dump (serial + firmware string), ``0x39`` etc. carry
    live brew progress (best-effort, not needed for load-only).

State byte (inside a ``0x57`` frame, right after ``0xc1``)
---------------------------------------------------------
====  ============================  =========================================
Byte  Name                          Meaning
====  ============================  =========================================
0x01  idle                          Idle / ready (also seen at brew end).
0x0c  no_water                      Refused: no water (checked right after commit).
0x0f  no_beans                      Refused: wants beans (machine WAITS here).
0x10  brewing                       Live pour / brew in progress (see note below).
0x1d  loading                       Recipe being received.
0x1f  armed                         Recipe loaded, armed, awaiting approval.
0x1e  awaiting_confirm              Waiting for the human to confirm on device.
0x22  starting                      Post-commit: grinding / spinning up.
0x3b  brewing                       Brew in progress (seen on the app-capture firmware).
0x41  complete                      Brew complete.
0x43  saving_slots                  Easy-Mode slot batch being stored.
0x25  slots_saved                   Easy-Mode slots stored OK (then → idle).
====  ============================  =========================================

Note on the brew sequence (observed on firmware V12.0D.500): after commit the
machine goes ``awaiting_confirm (0x1e) → starting (0x22)``, then **grinds SILENTLY**
— it emits no ``0x57`` *status* frame for ~20 s (only the scale stream, reading ~0)
— before it reports the pour as ``0x10``. Consumers must not treat that gap as a
stalled brew or send state-sensitive command 40518 merely because the first 0x1e
was observed.

The state ``0x1f`` (armed) is what :meth:`XBloomClient.load_recipe` waits for
after sending the four LOAD frames — the machine is armed and prompting the human.

Live liquid telemetry: the machine streams report 40523 (~10x/s) as cumulative
dispensed water and report 20501 as the raw cup-scale weight. The canonical
fields are :attr:`StatusEvent.dispensed_water_ml` and
:attr:`StatusEvent.cup_weight_g`; the older ``water_g``/``coffee_g`` names remain
compatibility aliases.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .protocol import crc16_kermit

__all__ = [
    "STATE_NAMES",
    "IGNORED_STATES",
    "TERMINAL_STATES",
    "NotificationFrameStats",
    "NotificationFrameStream",
    "StatusEvent",
    "notification_frame_is_valid",
    "parse_notification",
    "parse_machine_info_payload",
    "is_idle_or_complete",
]

NOTIFICATION_PREFIX = b"\x58\x02\x07"
NOTIFICATION_HEADER_LENGTH = 9
MIN_NOTIFICATION_FRAME_LENGTH = 12
MAX_NOTIFICATION_FRAME_LENGTH = 65_535


@dataclass
class NotificationFrameStats:
    """Counters for rejected or resynchronised ``ffe2`` bytes."""

    frames_emitted: int = 0
    invalid_crc_frames: int = 0
    invalid_length_frames: int = 0
    bytes_discarded: int = 0


def notification_frame_is_valid(frame: bytes) -> bool:
    """Return whether one complete Studio notification has valid shape and CRC."""

    frame = bytes(frame)
    if len(frame) < MIN_NOTIFICATION_FRAME_LENGTH:
        return False
    if not frame.startswith(NOTIFICATION_PREFIX):
        return False
    declared = struct.unpack_from("<I", frame, 5)[0]
    if declared != len(frame) or declared > MAX_NOTIFICATION_FRAME_LENGTH:
        return False
    expected = struct.unpack_from("<H", frame, len(frame) - 2)[0]
    return crc16_kermit(frame[:-2]) == expected


class NotificationFrameStream:
    """Reassemble and validate the byte stream delivered by ``ffe2`` callbacks.

    The Android app keeps a persistent buffer because one logical Studio frame can
    be split across notifications and one callback can contain multiple frames. It
    also rejects invalid lengths and CRCs before dispatch. This class mirrors that
    behaviour while resynchronising on the next ``58 02 07`` prefix after noise.
    """

    def __init__(self, *, max_frame_length: int = MAX_NOTIFICATION_FRAME_LENGTH):
        if max_frame_length < MIN_NOTIFICATION_FRAME_LENGTH:
            raise ValueError("max_frame_length is smaller than a notification header")
        self.max_frame_length = int(max_frame_length)
        self._buffer = bytearray()
        self.stats = NotificationFrameStats()

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()

    def _discard_without_prefix(self) -> None:
        """Discard noise while retaining a possible split prefix suffix."""

        keep = 0
        for size in range(1, min(len(NOTIFICATION_PREFIX), len(self._buffer) + 1)):
            if self._buffer[-size:] == NOTIFICATION_PREFIX[:size]:
                keep = size
        discard = len(self._buffer) - keep
        if discard:
            del self._buffer[:discard]
            self.stats.bytes_discarded += discard

    def feed(self, chunk: bytes | bytearray) -> list[bytes]:
        """Add callback bytes and return every complete CRC-valid frame."""

        if chunk:
            self._buffer.extend(chunk)
        frames: list[bytes] = []
        while self._buffer:
            start = self._buffer.find(NOTIFICATION_PREFIX)
            if start < 0:
                self._discard_without_prefix()
                break
            if start:
                del self._buffer[:start]
                self.stats.bytes_discarded += start
            if len(self._buffer) < NOTIFICATION_HEADER_LENGTH:
                break

            declared = struct.unpack_from("<I", self._buffer, 5)[0]
            if not MIN_NOTIFICATION_FRAME_LENGTH <= declared <= self.max_frame_length:
                del self._buffer[0]
                self.stats.bytes_discarded += 1
                self.stats.invalid_length_frames += 1
                continue
            if len(self._buffer) < declared:
                break

            frame = bytes(self._buffer[:declared])
            if not notification_frame_is_valid(frame):
                # Shift by one byte, like the app, so a valid frame accidentally
                # covered by a corrupt length can still be recovered.
                del self._buffer[0]
                self.stats.bytes_discarded += 1
                self.stats.invalid_crc_frames += 1
                continue

            del self._buffer[:declared]
            frames.append(frame)
            self.stats.frames_emitted += 1
        return frames

STATE_NAMES: dict[int, str] = {
    0x01: "idle",
    0x0C: "no_water",          # machine has no water (checked before grinding)
    0x0F: "no_beans",          # machine wants beans (add beans, or cancel) — it WAITS here
    0x10: "brewing",           # live pour / brew in progress (observed on HW: the machine
                               #   grinds SILENTLY after 0x22, then reports 0x10 as it pours)
    0x1D: "loading",
    0x1F: "armed",
    0x1E: "awaiting_confirm",
    0x22: "starting",          # post-confirm: grinding / spinning up
    0x23: "brewing",           # mid-pour sub-state (keeps the status on "brewing…")
    0x24: "ready",             # brew DONE — the "coffee ready" beep. The cup is still on
                               #   the scale; the machine only returns to idle (0x01) once
                               #   it's lifted, so 'ready' is the real end-of-brew signal.
    0x3B: "brewing",
    0x41: "complete",
    0x43: "saving_slots",
    0x25: "slots_saved",
}

# Full report identifiers for live liquid telemetry. Dispatch must use these
# complete values because unrelated reports can share the same low command byte.
WATER_VOLUME_COMMAND = 40523   # cumulative dispensed ml * 1000
CUP_WEIGHT_COMMAND = 20501     # raw cup-scale grams
SCALE_WEIGHT_COMMAND = 10507   # dedicated FreeSolo scale grams
MACHINE_INFO_COMMAND = 40521
BREWER_MODE_COMMAND = 8107
BREWER_TEMPERATURE_COMMAND = 8108
SETTINGS_CHANGED_COMMAND = 8015
ADVANCED_VALUE_COMMANDS = frozenset({11506, 11507, 11508, 11509})
REPORT_NAMES: dict[int, str] = {
    8009: "machine_sleeping",
    8011: "machine_awake",
    8015: "settings_changed",
    8023: "machine_activity",
    8105: "grinder_size",
    8106: "grinder_speed",
    8111: "easy_mode_begin",
    8113: "tea_soak_time_changed",
    8203: "abnormal_gear_position",
    8204: "abnormal_dose_or_water",
    9000: "grinder_state",
    9001: "brewer_state",
    9002: "machine_state",
    9003: "grinder_started",
    9004: "grinder_exited",
    9005: "brewer_started",
    9006: "brewer_exited",
    9008: "machine_state",
    9009: "grinder_paused",
    9010: "brewer_paused",
    9011: "tea_restarted",
    9012: "tea_soaking",
    40501: "xpod_detected",
    40502: "coffee_brewer_started",
    40505: "gear_position",
    40507: "grinder_stopped",
    40510: "pour_stage",
    40511: "brewer_stopped",
    40512: "brew_ready",
    40513: "brew_ready_alt",
    40515: "tea_paused",
    40517: "idle_grinding_error",
    40520: "bypass_started",
    40522: "no_water_report",
    40523: "water_volume",
    40526: "current_grinder",
    40527: "vibration_before_pour",
    11506: "pour_radius",
    11507: "pour_radius_written",
    11508: "vibration_amplitude",
    11509: "vibration_amplitude_written",
    11518: "easy_mode_state",
}
ERROR_REPORTS = frozenset({8203, 8204, 40517, 40522})
# Heartbeat state sentinels (0x15/0x4b as *states*) — kept for the is_heartbeat property
# and back-compat; the live streams above are keyed by TYPE, not state.
IGNORED_STATES = frozenset({0x15, 0x4B})

# States that mean the brew is over. 0x24 = "coffee ready" (the beep — the true end of
# a brew, cup still on the scale); 0x01 = idle (only reached once the cup is lifted).
# Making 0x24 terminal is what lets a plain stream_telemetry consumer (the CLI) stop at
# the beep instead of hanging until cup-off. (0x41 kept for firmwares that report it.)
TERMINAL_STATES = frozenset({0x24, 0x41, 0x01})

STATUS_COMMAND = 8023  # Full u16 machine-activity/status report.
STATE_MARKER = 0xC1


@dataclass
class StatusEvent:
    """A decoded status notification."""

    state: int | None
    state_name: str
    raw: bytes
    #: Full little-endian u16 command/report code from notification offsets 3-4.
    command_code: int | None = None
    #: Deprecated compatibility name for cumulative dispensed millilitres.
    water_g: float | None = None
    #: Canonical cumulative machine-dispensed water for this operation.
    dispensed_water_ml: float | None = None
    #: Deprecated compatibility name for the raw cup-scale reading in grams.
    coffee_g: float | None = None
    #: Canonical raw cup-scale reading. Kept beside ``coffee_g`` for compatibility.
    cup_weight_g: float | None = None
    #: Standalone electronic-scale reading in grams (FreeSolo scale mode).
    scale_g: float | None = None
    #: Read-only Studio/J15 machine details from report 40521.
    machine_info: dict[str, object] | None = None
    #: Named control-grade report where the APK provides stable semantics.
    report_name: str | None = None
    #: Applied FreeSolo pattern reported by command 8107.
    brewer_pattern: str | None = None
    #: Raw app/display temperature value reported by command 8108.
    brewer_temperature_value: int | None = None
    #: First little-endian u32 carried by a structured report, when applicable.
    report_value: int | None = None
    #: Named structured values such as persistent settings readback.
    report_values: dict[str, object] | None = None
    #: True for APK-defined alarm/error reports.
    is_error: bool = False

    def __post_init__(self) -> None:
        """Normalize legacy v1 names at the decoder boundary.

        Internal consumers can use the unit-correct canonical fields while old
        integrations that construct or read ``water_g``/``coffee_g`` continue
        to work through the v1 compatibility window.
        """

        if self.dispensed_water_ml is None and self.water_g is not None:
            self.dispensed_water_ml = self.water_g
        elif self.water_g is None and self.dispensed_water_ml is not None:
            self.water_g = self.dispensed_water_ml
        if self.cup_weight_g is None and self.coffee_g is not None:
            self.cup_weight_g = self.coffee_g
        elif self.coffee_g is None and self.cup_weight_g is not None:
            self.coffee_g = self.cup_weight_g

    @property
    def is_heartbeat(self) -> bool:
        return self.state in IGNORED_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def __str__(self) -> str:
        bits = [self.state_name]
        if self.water_g is not None and self.dispensed_water_ml is None:
            bits.append(f"water={self.water_g:g}")
        if self.dispensed_water_ml is not None:
            bits.append(f"dispensed={self.dispensed_water_ml:g}ml")
        if self.coffee_g is not None and self.cup_weight_g is None:
            bits.append(f"coffee={self.coffee_g:g}g")
        if self.cup_weight_g is not None:
            bits.append(f"cup={self.cup_weight_g:g}g")
        if self.scale_g is not None:
            bits.append(f"scale={self.scale_g:g}g")
        if self.report_name is not None:
            bits.append(self.report_name)
        if self.brewer_pattern is not None:
            bits.append(f"pattern={self.brewer_pattern}")
        if self.brewer_temperature_value is not None:
            bits.append(f"temperature={self.brewer_temperature_value}")
        if self.report_value is not None:
            bits.append(f"value={self.report_value}")
        if self.is_error:
            bits.append("ERROR")
        return " ".join(bits)


def _u32(payload: bytes, offset: int = 0) -> int | None:
    if len(payload) < offset + 4:
        return None
    return struct.unpack_from("<I", payload, offset)[0]


def _marker_idx(data: bytes) -> int:
    """Offset of the ``0xc1`` payload marker in a ``58 02 07`` notification.

    The header is fixed width (``58 02 07`` + TYPE + SUB + 4-byte LEN = 9 bytes),
    so the marker sits at offset 9; fall back to a search for robustness.
    """
    if len(data) > 9 and data[9] == STATE_MARKER:
        return 9
    return data.find(STATE_MARKER, 5)


def _decode_float_measurement(
    data: bytes, *, scale: float, allow_negative: bool = False
) -> float | None:
    """Decode one finite float32 measurement after the notification marker.

    ``scale`` converts the wire value into millilitres or grams. Returns ``None``
    for NaN or values beyond the Studio's useful measurement envelope. Negative
    values are accepted only for signed scale reports.
    """
    marker = _marker_idx(data)
    if marker < 0 or marker + 5 > len(data):
        return None
    try:
        raw = struct.unpack_from("<f", data, marker + 1)[0]
    except struct.error:
        return None
    value = raw * scale
    minimum = -2000.0 if allow_negative else 0.0
    if value != value or value < minimum or value > 2000.0:  # NaN or out of range
        return None
    return round(value, 2)


def parse_machine_info_payload(payload: bytes) -> dict[str, object] | None:
    """Decode the app's fixed-width Studio/J15 report-40521 payload.

    This mirrors ``MachineInfoBleModel`` from the audited Android app. Optional
    tail fields were added by later firmware, so short legacy reports still
    return the stable identity/settings prefix.
    """
    payload = bytes(payload)
    if len(payload) < 42:
        return None

    def ascii_field(start: int, end: int) -> str:
        return payload[start:end].decode("ascii", errors="replace").rstrip("\x00 ")

    try:
        area_ap = struct.unpack_from("<f", payload, 29)[0]
    except struct.error:
        return None
    if area_ap != area_ap:  # NaN
        area_ap = 0.0

    water_source_raw = payload[36]
    led_raw = payload[38]
    temp_unit_raw = payload[40]
    weight_unit_raw = payload[41]
    info: dict[str, object] = {
        "serial_number": ascii_field(0, 13),
        "model": ascii_field(13, 19),
        "firmware": ascii_field(19, 29),
        "area_ap": round(float(area_ap), 3),
        "water_enough": bool(payload[33]),
        "system_status": payload[34],
        "user_count": payload[35],
        "water_source": {0: "tank", 1: "tap"}.get(
            water_source_raw, f"unknown_{water_source_raw}"
        ),
        "grind_setting": max(payload[37] - 30, 1),
        "display": {1: "low", 8: "medium", 15: "high"}.get(
            led_raw, f"unknown_{led_raw}"
        ),
        "voltage_raw": payload[39],
        "temperature_unit": {0: "F", 1: "C"}.get(
            temp_unit_raw, f"unknown_{temp_unit_raw}"
        ),
        "weight_unit": {0: "ml", 1: "g", 2: "oz"}.get(
            weight_unit_raw, f"unknown_{weight_unit_raw}"
        ),
    }
    if len(payload) >= 55:
        info["mode"] = "auto" if payload[51:55].hex() == "91327856" else "pro"
    if len(payload) >= 59:
        info["pouring_radius_init"] = struct.unpack_from("<I", payload, 55)[0]
    if len(payload) >= 63:
        info["vibration_init"] = struct.unpack_from("<I", payload, 59)[0]
    return info


def parse_notification(
    data: bytes, *, validate_crc: bool = True
) -> StatusEvent | None:
    """Decode a raw ``ffe2`` notification into a :class:`StatusEvent`.

    ``data`` may be ``bytes``, ``bytearray``, or a hex string. Returns ``None``
    for frames that are not recognisable notifications (so callers can simply
    skip them). Frame shape: ``58 02 07 | COMMAND(u16le) | LEN(u32le) | c1 |
    payload | crc``. CRC validation is on by default; forensic callers examining
    deliberately incomplete captures may opt out explicitly.
    """
    if isinstance(data, str):
        data = bytes.fromhex(data.replace(" ", ""))
    else:
        data = bytes(data)

    if len(data) < MIN_NOTIFICATION_FRAME_LENGTH:
        return None
    if not data.startswith(NOTIFICATION_PREFIX):
        return None
    declared = struct.unpack_from("<I", data, 5)[0]
    if declared != len(data) or declared > MAX_NOTIFICATION_FRAME_LENGTH:
        return None
    if validate_crc and not notification_frame_is_valid(data):
        return None

    command_code = struct.unpack_from("<H", data, 3)[0]

    # Match the full 16-bit report before consulting the historic low-byte TYPE.
    # 8011 (machine awake) also ends in 0x4b, so low-byte dispatch would silently
    # misclassify it as water. Compatibility aliases remain populated at this layer
    # until the version-1 JSON boundary is retired.
    if command_code == WATER_VOLUME_COMMAND:
        ml = _decode_float_measurement(data, scale=0.001)
        return StatusEvent(
            state=None,
            state_name="scale",
            raw=data,
            command_code=command_code,
            water_g=ml,
            dispensed_water_ml=ml,
            report_name=REPORT_NAMES.get(command_code),
        )
    if command_code == CUP_WEIGHT_COMMAND:
        g = _decode_float_measurement(
            data,
            scale=1.0,
            allow_negative=True,
        )
        return StatusEvent(
            state=None,
            state_name="scale",
            raw=data,
            command_code=command_code,
            coffee_g=g,
            cup_weight_g=g,
            scale_g=g,
        )
    if command_code == SCALE_WEIGHT_COMMAND:
        g = _decode_float_measurement(data, scale=1.0, allow_negative=True)
        return StatusEvent(
            state=None,
            state_name="scale",
            raw=data,
            command_code=command_code,
            scale_g=g,
        )

    marker = _marker_idx(data)
    payload = data[marker + 1 : -2] if marker >= 0 else b""

    # Official report 8023 is both the status envelope and machine activity. The
    # APK decodes its first
    # u32 as an activity index. Preserve the richer hardware-derived state name
    # while also exposing the official report identity and raw index.
    if command_code == STATUS_COMMAND and payload:
        state = payload[0]
        name = STATE_NAMES.get(state, f"unknown_0x{state:02x}")
        return StatusEvent(
            state=state,
            state_name=name,
            raw=data,
            command_code=command_code,
            report_name=REPORT_NAMES.get(command_code),
            report_value=_u32(payload),
        )

    if command_code == MACHINE_INFO_COMMAND:
        return StatusEvent(
            state=None,
            state_name="machine_info",
            raw=data,
            command_code=command_code,
            machine_info=parse_machine_info_payload(payload),
        )

    if command_code == BREWER_MODE_COMMAND and len(payload) >= 4:
        value = struct.unpack_from("<I", payload, 0)[0]
        return StatusEvent(
            state=None,
            state_name="brewer_pattern",
            raw=data,
            command_code=command_code,
            report_name="brewer_pattern",
            brewer_pattern={0: "center", 1: "circular", 2: "spiral"}.get(
                value, f"unknown_{value}"
            ),
        )

    if command_code == BREWER_TEMPERATURE_COMMAND and len(payload) >= 4:
        value = struct.unpack_from("<I", payload, 0)[0]
        return StatusEvent(
            state=None,
            state_name="brewer_temperature",
            raw=data,
            command_code=command_code,
            report_name="brewer_temperature",
            brewer_temperature_value=int(value),
        )

    if command_code == SETTINGS_CHANGED_COMMAND and len(payload) >= 12:
        weight_raw, temperature_raw, source_raw = struct.unpack_from("<III", payload, 0)
        values: dict[str, object] = {
            "weight_unit": {0: "ml", 1: "g", 2: "oz"}.get(
                weight_raw, f"unknown_{weight_raw}"
            ),
            "temperature_unit": {0: "F", 1: "C"}.get(
                temperature_raw, f"unknown_{temperature_raw}"
            ),
            "water_source": {0: "tank", 1: "tap"}.get(
                source_raw, f"unknown_{source_raw}"
            ),
        }
        return StatusEvent(
            state=None,
            state_name="settings_changed",
            raw=data,
            command_code=command_code,
            report_name=REPORT_NAMES[command_code],
            report_values=values,
        )

    if command_code in ADVANCED_VALUE_COMMANDS:
        value = _u32(payload)
        return StatusEvent(
            state=None,
            state_name=REPORT_NAMES[command_code],
            raw=data,
            command_code=command_code,
            report_name=REPORT_NAMES[command_code],
            report_value=value,
        )

    # APK model classes decode these reports as a single little-endian integer.
    if command_code in {
        8105, 8106, 8111, 8113, 40505, 40510, 40522, 40526
    }:
        value = _u32(payload)
        if command_code == 8105 and value is not None:
            value -= 30  # DeviceGrinderSizeBleModel applies this offset.
        return StatusEvent(
            state=None,
            state_name=REPORT_NAMES[command_code],
            raw=data,
            command_code=command_code,
            report_name=REPORT_NAMES[command_code],
            report_value=value,
            is_error=command_code in ERROR_REPORTS,
        )

    if command_code == 40501 and len(payload) >= 6:
        xid = payload[:6].decode("ascii", errors="replace").rstrip("\x00 ")
        return StatusEvent(
            state=None,
            state_name="xpod_detected",
            raw=data,
            command_code=command_code,
            report_name=REPORT_NAMES[command_code],
            report_values={"xid": xid},
        )

    if command_code == 11518:
        return StatusEvent(
            state=None,
            state_name="easy_mode_state",
            raw=data,
            command_code=command_code,
            report_name=REPORT_NAMES[command_code],
            report_values={"raw_mode": payload[:4].hex()},
        )

    # Otherwise it is a command echo/ACK or a still-unmodeled progress report.
    # ``command_code`` preserves the full u16 identity; the legacy short ACK label
    # stays stable for existing logs and consumers.
    return StatusEvent(
        state=None,
        state_name=REPORT_NAMES.get(
            command_code, f"ack_0x{command_code & 0xFF:02x}"
        ),
        raw=data,
        command_code=command_code,
        report_name=REPORT_NAMES.get(command_code),
        report_value=_u32(payload),
        is_error=command_code in ERROR_REPORTS,
    )


def is_idle_or_complete(event: StatusEvent) -> bool:
    """True if the event indicates the brew is over (complete or back to idle)."""
    return event.state in TERMINAL_STATES
