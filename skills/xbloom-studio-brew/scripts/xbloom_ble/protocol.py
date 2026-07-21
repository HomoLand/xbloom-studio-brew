"""Byte-exact xBloom Studio BLE wire protocol.

This module is a pure (no-BLE) port of the *verified*, round-trip-proven
builders that were reverse-engineered from an Android Bluetooth HCI capture.
It is what lets the rest of the package talk to the machine without guessing.

Frame format
------------
The official app's generic J15 command frame written to ``ffe1`` is::

    58 01 TYPE | COMMAND(u16le) | LEN(u32le) | 01 | DATA | CRC16(u16le)

* ``58 01``    — constant header; ``TYPE`` is normally ``01``.
* ``COMMAND``  — one little-endian 16-bit command identifier.
* ``LEN``      — total frame length in bytes, little-endian, including CRC.
* ``01``       — command payload marker.
* ``DATA``     — command-specific raw bytes or little-endian 32-bit values.
* ``CRC16``    — CRC-16/KERMIT over the whole frame except the last two bytes,
  stored little-endian.

The original HCI port named the two command bytes ``CMD`` and ``SEQ``. Those
legacy builder arguments remain for compatibility, but Android app analysis
confirmed that they form one command: for example ``a4 1f`` is command 8100,
``41 1f`` is 8001, and ``46 9e`` is 40518.

CRC-16/KERMIT: polynomial ``0x1021``, init ``0``, reflected input and output,
no final XOR.

GATT
----
Vendor service ``0000e0ff-3c17-d293-8e48-14fe2e4da212`` exposes:

* ``ffe1`` — command (write).
* ``ffe2`` — status (notify).
* ``ffe3`` — aux.

The coffee LOAD sequence
------------------------
Sent frame-by-frame, waiting for each ACK on ``ffe2``:

1. command 8100 (legacy ``a4 1f``) — session start.
2. command 8102 (``a6 1f``) — optional bypass water + dose.
3. command 8104 (``a8 1f``) — cup-geometry compatibility values.
4. command 8001 (``41 1f``), or 8004 for no-grind — pours + grind.

After these four frames the machine reports STATE ``0x1f`` (armed/loaded). At
that point you can either approve the brew **on the machine** or start it
remotely (below), exactly like the official app.

Starting a brew (commit / state-sensitive compatibility control / cancel)
---------------------------------------------------------------------------
Loading only *arms* the machine. To start the brew remotely, the captured
workflow uses these control frames:

* command 8002 (legacy ``42 1f``) — **commit**: the machine passes through ``0x1e``
  (awaiting-confirm); tested firmware can then auto-proceed to starting.
* command 40518 (``46 9e``) — APK 2.2.2 calls this **pause**. Target-firmware
  captures also prove a start meaning while the machine has freshly reported
  ``awaiting_confirm``. Never send it on the first transient ``0x1e``: observe the
  auto-start window and freshly recheck that the machine is still awaiting.
* command 40519 (``47 9e``) — **cancel**: abort a committed/running brew.

All three carry the constant one-byte payload ``01``. The builders reproduce
the captured bytes; callers own the state preconditions.

⚠️ SAFETY — loading and starting are separate, explicit steps
-------------------------------------------------------------
Starting a brew physically dispenses near-boiling water. The design keeps that
deliberate: :func:`build_load_frames` returns **only** the four LOAD frames and
never a commit/start opcode, so *loading a recipe can never brew by accident*.
The commit/start frames live in their own builders and are only emitted when a
caller explicitly asks to start (or cancel) a brew. Never wire commit/start as a
side effect of loading — only in response to a clear, intentional "start" action
with the machine physically ready.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping

__all__ = [
    "PATTERN_CODES",
    "VIBRATION_CODES",
    "MACHINE_PATTERN_CODES",
    "ROOM_TEMPERATURE_C",
    "LOAD_SEQ",
    "crc16_kermit",
    "xbloom_frame",
    "j15_frame",
    "frame_command",
    "build_a4",
    "build_a6",
    "build_a8",
    "build_coffee_cup_geometry_compat",
    "build_41",
    "build_load_frames",
    "build_session_start",
    "build_status_query",
    "build_save_slot",
    "build_set_mode",
    "build_commit",
    "build_start",
    "build_cancel",
    "POURS_CMD_GRIND",
    "POURS_CMD_NO_GRIND",
    "NO_GRIND",
    "NO_GRIND_WIRE",
    "CMD_SAVE_SLOT",
    "CMD_SET_MODE",
    "LOAD_SEQ",
    "BREW_SEQ",
    "COMMIT_OPCODE",
    "START_OPCODE",
    "CANCEL_OPCODE",
    "CMD_SCALE_ENTER",
    "CMD_SCALE_EXIT",
    "CMD_SCALE_TARE",
    "CMD_GRINDER_ENTER",
    "CMD_GRINDER_START",
    "CMD_GRINDER_STOP",
    "CMD_GRINDER_QUIT",
    "CMD_GRINDER_PAUSE",
    "CMD_GRINDER_RESUME",
    "CMD_BREWER_ENTER",
    "CMD_BREWER_START",
    "CMD_BREWER_STOP",
    "CMD_BREWER_QUIT",
    "CMD_BREWER_SET_PATTERN",
    "CMD_BREWER_PAUSE",
    "CMD_BREWER_RESUME",
    "CMD_BREWER_SET_TEMPERATURE",
    "CMD_COFFEE_PAUSE",
    "CMD_COFFEE_RESUME",
    "CMD_TEA_RECIPE_CODE",
    "CMD_TEA_RECIPE_MAKE",
    "CMD_RECIPE_START_QUIT",
    "CMD_SET_WEIGHT_UNIT",
    "CMD_SET_TEMPERATURE_UNIT",
    "CMD_SET_WATER_SOURCE",
    "CMD_SET_DISPLAY",
    "CMD_READ_POUR_RADIUS",
    "CMD_WRITE_POUR_RADIUS",
    "CMD_READ_VIBRATION_AMPLITUDE",
    "CMD_WRITE_VIBRATION_AMPLITUDE",
    "COFFEE_CUP_GEOMETRY_COMPAT",
    "build_scale_enter",
    "build_scale_exit",
    "build_scale_tare",
    "build_grinder_enter",
    "build_grinder_start",
    "build_grinder_stop",
    "build_grinder_quit",
    "build_grinder_pause",
    "build_grinder_resume",
    "build_brewer_enter",
    "build_brewer_start",
    "build_brewer_stop",
    "build_brewer_quit",
    "build_brewer_pause",
    "build_brewer_resume",
    "build_brewer_set_pattern",
    "build_brewer_set_temperature",
    "build_coffee_pause",
    "build_coffee_resume",
    "build_tea_recipe_code",
    "build_tea_set_cup",
    "build_tea_code_upload",
    "build_tea_load_frames",
    "build_tea_start",
    "build_recipe_start_quit",
    "build_set_weight_unit",
    "build_set_temperature_unit",
    "build_set_water_source",
    "build_set_display",
    "build_read_pour_radius",
    "build_write_pour_radius",
    "build_read_vibration_amplitude",
    "build_write_vibration_amplitude",
]

# Sequence byte used for the load sequence, and for the brew (commit/start) phase.
LOAD_SEQ = 0x1F
BREW_SEQ = 0x9E

# Command 8104 is named APP_SET_CUP by APK 2.2.2. The coffee path below keeps
# the 110/90 values from the upstream capture and successful target-firmware
# runs; those values are an opaque compatibility profile, not temperatures a
# recipe author should tune. Do not change without a controlled hardware A/B.
COFFEE_CUP_GEOMETRY_COMPAT = (110.0, 90.0)

# Brew-control opcodes. They are NOT part of the load sequence: commit and the
# state-sensitive 40518 confirm/pause command belong only to an explicit control
# workflow, while cancel belongs only to explicit recovery.
COMMIT_OPCODE = 0x42  # commit: arm → awaiting-confirm (seq 0x1f)
START_OPCODE = 0x46   # legacy name: full command 40518 is state-sensitive
CANCEL_OPCODE = 0x47  # cancel: abort a committed/running brew (seq 0x9e)

# Legacy (pattern, agitation) -> (pattern byte, second byte) compatibility
# table from captured recipes. New recipes bypass this table and encode the
# second byte as an explicit four-state vibration timing.
PATTERN_CODES: dict[tuple[str, bool], tuple[int, int]] = {
    ("spiral", True): (0x02, 0x02),   # bloom (spiral + agitation ON)
    ("spiral", False): (0x02, 0x00),  # default spiral
    ("ring", False): (0x01, 0x00),    # ring / middle
    ("center", False): (0x00, 0x01),  # center single dot
}

# The recipe byte following the pattern is not a generic agitation boolean.
# The Android recipe encoder derives it from two independent UI toggles:
# vibrate before the pour and vibrate after the pour. Keep PATTERN_CODES above
# solely for old recipe compatibility; all new recipes should use these exact
# four states independently of the pouring pattern.
VIBRATION_CODES: dict[str, int] = {
    "none": 0,
    "before": 1,
    "after": 2,
    "both": 3,
}

# Pattern values used by the app's generic J15 commands. The app UI calls
# ``ring`` "circular"; both names map to the same machine value.
MACHINE_PATTERN_CODES: dict[str, int] = {
    "center": 0,
    "ring": 1,
    "circular": 1,
    "spiral": 2,
}

# Official Android ``TemperatureConstant.RT`` value for Studio/J15. FreeSolo
# sends this through the normal brewer temperature fields as ``20 * 10``. It
# selects the room-temperature/pass-through mode; it does not promise that the
# delivered water is actively cooled to exactly 20 C.
ROOM_TEMPERATURE_C = 20

# Generic J15 command codes recovered from the official Android app. The two
# bytes at offsets 3-4 are one little-endian u16 command, not semantically a
# separate opcode and sequence. The existing load builders retain their historic
# names because they are byte-exact and already hardware-tested.
CMD_SCALE_ENTER = 8003
CMD_SCALE_EXIT = 8014
CMD_SCALE_TARE = 8500

CMD_GRINDER_ENTER = 8006
CMD_GRINDER_START = 3500
CMD_GRINDER_STOP = 3505
CMD_GRINDER_QUIT = 8012
CMD_GRINDER_PAUSE = 8018
CMD_GRINDER_RESUME = 8020

CMD_BREWER_ENTER = 8007
CMD_BREWER_START = 4506
CMD_BREWER_STOP = 4507
CMD_BREWER_QUIT = 8013
CMD_BREWER_SET_PATTERN = 8016
CMD_BREWER_PAUSE = 8019
CMD_BREWER_RESUME = 8021
CMD_BREWER_SET_TEMPERATURE = 4510

CMD_COFFEE_PAUSE = 40518
CMD_COFFEE_RESUME = 40524

CMD_TEA_RECIPE_MAKE = 4512
CMD_TEA_RECIPE_CODE = 4513
CMD_RECIPE_START_QUIT = 8017
CMD_SET_CUP = 8104

# Persistent Studio settings. Values are one little-endian u32, matching the
# Android app's MachineJ15Fragment/BleCodeFactory calls.
CMD_SET_WEIGHT_UNIT = 8005
CMD_SET_TEMPERATURE_UNIT = 8010
CMD_SET_WATER_SOURCE = 4508
CMD_SET_DISPLAY = 8103

# Mechanical tuning uses CodeModule2/buildCommandString2 and therefore frame
# type 0x02. Read commands carry no data; writes carry one u32 value.
CMD_READ_POUR_RADIUS = 11506
CMD_WRITE_POUR_RADIUS = 11507
CMD_READ_VIBRATION_AMPLITUDE = 11508
CMD_WRITE_VIBRATION_AMPLITUDE = 11509


def crc16_kermit(data: bytes) -> int:
    """CRC-16/KERMIT of ``data``.

    Polynomial ``0x1021``, init ``0``, reflected input and output, no final XOR.
    On an xBloom frame this is computed over the whole frame minus the trailing
    two CRC bytes, and stored little-endian.
    """
    crc = 0
    for byte in data:
        byte = int(f"{byte:08b}"[::-1], 2)
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return int(f"{crc:016b}"[::-1], 2)


def xbloom_frame(cmd: int, seq: int, payload: bytes) -> bytes:
    """Build a complete ``ffe1`` command frame.

    ``58 01 01 | cmd | seq | len_u16le | 00 00 | payload | crc16le``.
    """
    body = bytes([0x58, 0x01, 0x01, cmd, seq]) + b"\x00\x00" + b"\x00\x00" + payload
    total = len(body) + 2
    frame = bytearray(body)
    frame[5:7] = struct.pack("<H", total)
    crc = crc16_kermit(bytes(frame))
    return bytes(frame) + struct.pack("<H", crc)


def frame_command(frame: bytes) -> int:
    """Return the generic little-endian u16 command at frame offsets 3-4."""
    if len(frame) < 5:
        raise ValueError("frame is too short to contain a command")
    return struct.unpack_from("<H", frame, 3)[0]


def j15_frame(
    command: int,
    data: Iterable[int] = (),
    *,
    raw: bytes | None = None,
    frame_type: int = 0x01,
) -> bytes:
    """Build the official app's generic J15 command frame.

    Shape::

        58 01 TYPE | COMMAND(u16le) | LENGTH(u32le) | 01 |
        DATA(i32le...) or RAW | CRC16-KERMIT(u16le)

    ``LENGTH`` includes the complete frame, including its CRC. ``raw`` is used
    for opaque recipe-code bytes; integer ``data`` and ``raw`` are mutually
    exclusive. This is a direct port of ``VerifyCodeUtils.buildCommandString``
    from the official Android app.
    """
    command = int(command)
    if not 1 <= command <= 0xFFFF:
        raise ValueError(f"command must be 1-65535; got {command!r}")
    values = tuple(int(value) for value in data)
    if raw is not None and values:
        raise ValueError("data and raw are mutually exclusive")
    if not 0 <= int(frame_type) <= 0xFF:
        raise ValueError("frame_type must fit one byte")
    payload = bytes(raw) if raw is not None else b"".join(
        struct.pack("<I", value & 0xFFFFFFFF) for value in values
    )
    body = bytearray(
        b"\x58\x01"
        + bytes([int(frame_type)])
        + struct.pack("<H", command)
        + b"\x00\x00\x00\x00"
        + b"\x01"
        + payload
    )
    body[5:9] = struct.pack("<I", len(body) + 2)
    return bytes(body) + struct.pack("<H", crc16_kermit(bytes(body)))


def _float_bits(value: float) -> int:
    """Java ``Float.floatToIntBits`` equivalent for a finite Python float."""
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("float command values must be finite")
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _machine_pattern(pattern: str) -> int:
    try:
        return MACHINE_PATTERN_CODES[str(pattern).strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"pattern must be one of {sorted(MACHINE_PATTERN_CODES)}; got {pattern!r}"
        ) from exc


def _recipe_pattern(pattern: str) -> tuple[str, int]:
    """Normalize recipe pattern names and return ``(canonical, wire_value)``."""
    name = str(pattern).strip().lower()
    if name == "ring":
        name = "circular"
    try:
        return name, MACHINE_PATTERN_CODES[name]
    except KeyError as exc:
        raise ValueError(
            f"pattern must be one of {sorted(MACHINE_PATTERN_CODES)}; got {pattern!r}"
        ) from exc


def _pour_pattern_vibration(pour: Mapping) -> tuple[int, int]:
    """Encode one recipe's independent pattern and vibration timing.

    ``vibration`` is the exact modern field. ``agitation`` remains a deprecated
    compatibility path because published versions of this Skill emitted it and
    a captured slot recipe proves those legacy bytes. Supplying both is rejected
    so a recipe can never contain two conflicting instructions.
    """
    _canonical, pattern = _recipe_pattern(str(pour.get("pattern", "spiral")))
    has_vibration = pour.get("vibration") is not None
    has_legacy = pour.get("agitation") is not None
    if has_vibration and has_legacy:
        raise ValueError("pour cannot set both vibration and legacy agitation")
    if has_vibration:
        timing = str(pour["vibration"]).strip().lower()
        try:
            return pattern, VIBRATION_CODES[timing]
        except KeyError as exc:
            raise ValueError(
                f"vibration must be one of {sorted(VIBRATION_CODES)}; got {pour['vibration']!r}"
            ) from exc
    if has_legacy:
        legacy_name = str(pour.get("pattern", "spiral")).strip().lower()
        if legacy_name == "circular":
            legacy_name = "ring"
        try:
            return PATTERN_CODES[(legacy_name, bool(pour["agitation"]))]
        except KeyError as exc:
            raise ValueError(
                f"unsupported legacy pattern/agitation pair: "
                f"({pour.get('pattern')!r}, {pour.get('agitation')!r})"
            ) from exc
    return pattern, VIBRATION_CODES["none"]


# ---------------------------------------------------------------------------
# FreeSolo scale / grinder / brewer commands
# ---------------------------------------------------------------------------
def build_scale_enter() -> bytes:
    return j15_frame(CMD_SCALE_ENTER)


def build_scale_exit() -> bytes:
    return j15_frame(CMD_SCALE_EXIT)


def build_scale_tare() -> bytes:
    return j15_frame(CMD_SCALE_TARE)


def build_grinder_enter(grind: int, rpm: int) -> bytes:
    return j15_frame(CMD_GRINDER_ENTER, [int(grind), int(rpm)])


def build_grinder_start(grind: int, rpm: int) -> bytes:
    # The leading 1000 is the constant used by GrinderActivity in the app.
    return j15_frame(CMD_GRINDER_START, [1000, int(grind), int(rpm)])


def build_grinder_stop() -> bytes:
    return j15_frame(CMD_GRINDER_STOP)


def build_grinder_quit() -> bytes:
    return j15_frame(CMD_GRINDER_QUIT)


def build_grinder_pause() -> bytes:
    return j15_frame(CMD_GRINDER_PAUSE)


def build_grinder_resume() -> bytes:
    return j15_frame(CMD_GRINDER_RESUME)


def build_brewer_enter(temp_c: int, pattern: str = "center") -> bytes:
    # HomeActivity passes pattern first, then Float.floatToIntBits(temp*10).
    # RT uses the official 20 C sentinel, encoded through this same path.
    return j15_frame(
        CMD_BREWER_ENTER,
        [_machine_pattern(pattern), _float_bits(int(temp_c) * 10.0)],
    )


def build_brewer_start(
    volume_ml: float,
    temp_c: int,
    flow_ml_s: float = 3.5,
    pattern: str = "center",
    *,
    water_feed: int = 0,
) -> bytes:
    """Start a volume-limited FreeSolo water dispense.

    BrewerActivity encodes flow, volume, and temperature as Java float bit
    patterns after multiplying each by ten, followed by the configured water
    source and machine pattern. ``ROOM_TEMPERATURE_C`` selects the app's RT
    pass-through setting; it is a mode token, not an active cooling target.
    """
    if int(water_feed) not in {0, 1}:
        raise ValueError("water_feed must be 0 (tank) or 1 (tap)")
    return j15_frame(
        CMD_BREWER_START,
        [
            _float_bits(float(flow_ml_s) * 10.0),
            _float_bits(float(volume_ml) * 10.0),
            _float_bits(int(temp_c) * 10.0),
            int(water_feed),
            _machine_pattern(pattern),
        ],
    )


def build_brewer_stop() -> bytes:
    return j15_frame(CMD_BREWER_STOP)


def build_brewer_quit() -> bytes:
    return j15_frame(CMD_BREWER_QUIT)


def build_brewer_pause() -> bytes:
    return j15_frame(CMD_BREWER_PAUSE)


def build_brewer_resume() -> bytes:
    return j15_frame(CMD_BREWER_RESUME)


def build_brewer_set_pattern(pattern: str) -> bytes:
    """Change the active FreeSolo brewer pouring pattern.

    This belongs to standalone FreeSolo water. It does not edit a running
    automatic coffee recipe.
    """
    return j15_frame(CMD_BREWER_SET_PATTERN, [_machine_pattern(pattern)])


def build_brewer_set_temperature(temp_c: int) -> bytes:
    """Change the active FreeSolo brewer temperature target.

    The app sends a plain integer in tenths of a degree here, unlike the
    float-bit fields in the initial FreeSolo start command. ``20`` is RT and
    ``98`` is the Studio/J15 boiling-point setting.
    """
    temp_c = int(temp_c)
    if temp_c != ROOM_TEMPERATURE_C and not 40 <= temp_c <= 98:
        raise ValueError("temperature must be RT (20) or 40-98 C")
    return j15_frame(CMD_BREWER_SET_TEMPERATURE, [temp_c * 10])


# ---------------------------------------------------------------------------
# Persistent machine settings and mechanical tuning
# ---------------------------------------------------------------------------
def build_set_weight_unit(unit: str) -> bytes:
    """Persist the display/scale unit (``ml``, ``g``, or ``oz``)."""
    name = str(unit).strip().lower()
    values = {"ml": 0, "g": 1, "oz": 2}
    if name not in values:
        raise ValueError("weight unit must be ml, g, or oz")
    return j15_frame(CMD_SET_WEIGHT_UNIT, [values[name]])


def build_set_temperature_unit(unit: str) -> bytes:
    """Persist the machine display temperature unit (``C`` or ``F``)."""
    name = str(unit).strip().upper()
    values = {"F": 0, "C": 1}
    if name not in values:
        raise ValueError("temperature unit must be C or F")
    return j15_frame(CMD_SET_TEMPERATURE_UNIT, [values[name]])


def build_set_water_source(source: str) -> bytes:
    """Persist ``tank`` or direct/automatic ``tap`` water feed."""
    name = str(source).strip().lower()
    values = {"tank": 0, "tap": 1}
    if name not in values:
        raise ValueError("water source must be tank or tap")
    return j15_frame(CMD_SET_WATER_SOURCE, [values[name]])


def build_set_display(level: str) -> bytes:
    """Persist Studio display brightness (``low``, ``medium``, ``high``)."""
    name = str(level).strip().lower()
    values = {"low": 1, "medium": 8, "high": 15}
    if name not in values:
        raise ValueError("display must be low, medium, or high")
    return j15_frame(CMD_SET_DISPLAY, [values[name]])


def build_read_pour_radius() -> bytes:
    return j15_frame(CMD_READ_POUR_RADIUS, frame_type=0x02)


def build_write_pour_radius(value: int) -> bytes:
    value = int(value)
    # The app exposes five values around a per-machine baseline; known firmware
    # baselines are 560-840 and the UI applies +/-160 in 80-unit steps.
    if not 400 <= value <= 1000:
        raise ValueError("pour radius must be 400-1000")
    return j15_frame(CMD_WRITE_POUR_RADIUS, [value], frame_type=0x02)


def build_read_vibration_amplitude() -> bytes:
    return j15_frame(CMD_READ_VIBRATION_AMPLITUDE, frame_type=0x02)


def build_write_vibration_amplitude(value: int) -> bytes:
    value = int(value)
    if value not in range(1000, 1501, 100):
        raise ValueError("vibration amplitude must be 1000-1500 in 100-unit steps")
    return j15_frame(CMD_WRITE_VIBRATION_AMPLITUDE, [value], frame_type=0x02)


# ---------------------------------------------------------------------------
# Omni Tea Brewer commands and recipe-code port
# ---------------------------------------------------------------------------
def _tea_pause_bytes(seconds: int) -> tuple[int, int]:
    """Port the app's native ``TeaRecipeCreate`` pause rewrite.

    Pauses are split into a negative remainder byte and a five-bit minute count
    in the second byte: 20 s -> ``ec 00``; 60 s -> ``00 20``; 120 s ->
    ``00 40``. The official UI caps tea pauses at 120 seconds even though the
    native routine accepts up to 360.
    """
    seconds = int(seconds)
    if not 0 <= seconds <= 120:
        raise ValueError(f"tea pause must be 0-120 seconds; got {seconds}")
    minutes, remainder = divmod(seconds, 60)
    return ((-remainder) & 0xFF, (minutes * 32) & 0xFF)


def build_tea_recipe_code(
    pours: Iterable[Mapping],
    *,
    grinder_size: int = 50,
    grand_water: float = 45.0,
    rpm: int = 120,
) -> bytes:
    """Build the tea recipe blob used by command 4513.

    This ports the official app's Java ``GetRecipeCodeManager`` plus its native
    ``TeaRecipeCreate`` pause transform. The seemingly coffee-oriented suffix
    is retained because official tea records carry grinderSize plus
    ``grandWater = sum(programmed stage ml) / leaf grams``; tea mode still does
    not run the grinder. The finished ~120 ml-per-steep display and firmware
    siphon finish are not encoded as an extra pour here.
    """
    stages: list[bytes] = []
    for index, pour in enumerate(pours):
        ml = int(pour["ml"])
        temp = int(pour.get("temp", pour.get("temp_c")))
        pattern = _machine_pattern(str(pour.get("pattern", "circular")))
        vibration = int(pour.get("vibration", 0)) & 0xFF
        pause_lo, pause_hi = _tea_pause_bytes(
            int(pour.get("pause", pour.get("pause_s", 0)))
        )
        flow10 = int(round(float(pour.get("flow", pour.get("flow_ml_s", 3.5))) * 10))

        volume_parts: list[int] = []
        remaining = ml
        while remaining > 127:
            volume_parts.append(127)
            remaining -= 127
        if remaining:
            volume_parts.append(remaining)
        stage = bytearray()
        for part in volume_parts:
            stage.extend([part & 0xFF, temp & 0xFF, pattern, vibration])
        stage.extend(
            [
                pause_lo,
                pause_hi,
                int(rpm) & 0xFF if index == 0 else 0,
                flow10 & 0xFF,
            ]
        )
        stages.append(bytes(stage))

    body = b"".join(stages)
    if not body or len(body) > 0xFF:
        raise ValueError("tea recipe body must contain 1-255 bytes")
    # Java BigDecimal.byteValue truncates to the low eight bits.
    suffix = bytes(
        [int(grinder_size) & 0xFF, int(float(grand_water) * 10) & 0xFF]
    )
    return bytes([len(body)]) + body + suffix


def build_tea_set_cup(max_mm: float = 80.0, min_mm: float = 40.0) -> bytes:
    return j15_frame(CMD_SET_CUP, [_float_bits(max_mm), _float_bits(min_mm)])


def build_tea_code_upload(code: bytes) -> bytes:
    return j15_frame(CMD_TEA_RECIPE_CODE, raw=bytes(code))


def build_tea_load_frames(recipe: Mapping) -> list[bytes]:
    """Return tea setup + recipe upload frames; never the execute command."""
    code = build_tea_recipe_code(
        recipe["pours"],
        grinder_size=int(recipe.get("grinder_size", 50)),
        grand_water=float(recipe.get("grand_water", 45.0)),
        rpm=int(recipe.get("rpm", 120)),
    )
    frames = [
        build_tea_set_cup(
            float(recipe.get("cup_max_mm", 80.0)),
            float(recipe.get("cup_min_mm", 40.0)),
        ),
        build_tea_code_upload(code),
    ]
    if any(frame_command(frame) == CMD_TEA_RECIPE_MAKE for frame in frames):
        raise AssertionError("tea load frames must never execute the recipe")
    return frames


def build_tea_start() -> bytes:
    """Execute the previously uploaded tea recipe (physically dispenses hot water)."""
    return j15_frame(CMD_TEA_RECIPE_MAKE)


def build_recipe_start_quit() -> bytes:
    """Exit a loaded recipe's pre-start screen without executing it."""
    return j15_frame(CMD_RECIPE_START_QUIT)


# ---------------------------------------------------------------------------
# Payload builders (no frame header / CRC)
# ---------------------------------------------------------------------------
def build_a4() -> bytes:
    """0xa4 session-start payload (observed constant)."""
    return bytes.fromhex("01b900000001000000")


def build_a6(
    dose_g: int,
    bypass_ml: float = 0.0,
    bypass_temp_c: float = 0.0,
) -> bytes:
    """Build command-8102's bypass/dose payload.

    The official Studio/J15 app sends three 32-bit values after the payload
    marker: bypass volume as float bits, bypass temperature multiplied by ten
    as float bits, and dose as an integer. A disabled bypass is represented by
    two zero floats. This preserves the historic no-bypass bytes while making
    the app's 5-100 ml bypass feature available to recipes.
    """
    return (
        b"\x01"
        + struct.pack("<I", _float_bits(float(bypass_ml)))
        + struct.pack("<I", _float_bits(float(bypass_temp_c) * 10.0))
        + struct.pack("<I", int(dose_g))
    )


def build_coffee_cup_geometry_compat(
    first: float = COFFEE_CUP_GEOMETRY_COMPAT[0],
    second: float = COFFEE_CUP_GEOMETRY_COMPAT[1],
) -> bytes:
    """Build command-8104's opaque coffee compatibility payload.

    APK 2.2.2 calls this command ``APP_SET_CUP`` and treats both floats as cup
    geometry. The captured coffee path uses 110/90. They are deliberately not
    exposed as recipe temperatures.
    """
    f1 = struct.pack("<f", float(first))
    f2 = struct.pack("<f", float(second))
    return bytes([0x01]) + f1 + f2


def build_a8(temp1: float = 110.0, temp2: float = 90.0) -> bytes:
    """Deprecated raw-capture alias for :func:`build_coffee_cup_geometry_compat`."""

    return build_coffee_cup_geometry_compat(temp1, temp2)


def _pour_segments(p: Mapping) -> list[bytes]:
    """Turn one logical pour dict into a list of segment byte-strings.

    ``p`` keys: ``ml``, ``temp``, ``pattern``
    (``spiral``/``center``/``circular``; legacy ``ring`` is accepted),
    ``vibration`` (``none``/``before``/``after``/``both``), ``pause``
    (seconds, post-pour), ``rpm`` (int), and ``flow`` (ml/s float). The old
    ``agitation`` boolean remains supported only as a compatibility input.

    8-byte pour segment: ``[ml, temp, pattern, vibration, negpause, 00, rpm,
    flow*10]``.
    A pour whose volume exceeds 127 ml is split into 127-ml 4-byte lead
    segments followed by an 8-byte remainder carrying flow/pause/rpm.
    """
    pat, vibration = _pour_pattern_vibration(p)
    ml = int(p["ml"])
    temp = int(p["temp"]) & 0xFF
    pause = int(p.get("pause", 0))
    rpm = int(p.get("rpm", 0)) & 0xFF
    flow10 = int(round(float(p.get("flow", 3.0)) * 10)) & 0xFF
    negpause = (256 - pause) & 0xFF
    segs: list[bytes] = []
    remaining = ml
    while remaining > 127:
        segs.append(bytes([127, temp, pat, vibration]))
        remaining -= 127
    segs.append(
        bytes([remaining & 0xFF, temp, pat, vibration, negpause, 0x00, rpm, flow10])
    )
    return segs


# Grind byte sentinel — "no-grind" / brew pre-ground (grinder off).
# A recipe grind of ``0`` is a request to SKIP the grinder (brew already-ground
# coffee), not to grind at setting 0. On the wire the machine reads a valid grind
# as ``1–80``; the app encodes "grinder off" as the out-of-range byte ``0xFE`` and
# leaves the machine's stored grind SIZE untouched. (Observed in an HCI capture of
# the app's grinder-OFF save; sending an actual ``0`` grinds at the finest setting.)
NO_GRIND = 0            # recipe-level grind meaning "don't grind" (pre-ground)
NO_GRIND_WIRE = 0xFE    # the byte the machine reads as "skip the grinder"


def _grind_byte(grind: int) -> int:
    """Map a recipe grind to its wire byte: ``0`` (no-grind) → ``0xFE``, else the grind."""
    return NO_GRIND_WIRE if int(grind) == NO_GRIND else int(grind) & 0xFF


def build_41(pours: Iterable[Mapping], grind: int, tail: int = 0xA0) -> bytes:
    """0x41 pours+grind payload: ``01 | LEN(u8) | <segments> | grind | tail``.

    A ``grind`` of ``0`` is the **no-grind** sentinel (brew pre-ground): it is
    emitted as the wire byte ``0xFE``, which tells the machine to skip the grinder.
    """
    segs: list[bytes] = []
    for i, p in enumerate(pours):
        # RPM is carried ONLY on the first pour — the machine zeroes it on later
        # pours (verified byte-for-byte against the vendor app's captures).
        segs.extend(_pour_segments({**p, "rpm": 0} if i else p))
    body = b"".join(segs)
    return bytes([0x01, len(body) & 0xFF]) + body + bytes([_grind_byte(grind), tail & 0xFF])


# Pours-frame opcode: 0x41 when the machine grinds, 0x44 when the grinder is OFF
# (no-grind / pre-ground). Both carry the same pours+grind+ratio body; only the
# opcode differs. (Verified against the vendor app's HCI captures + on-machine.)
POURS_CMD_GRIND = 0x41
POURS_CMD_NO_GRIND = 0x44


def _ratio_byte(recipe: Mapping) -> int:
    """The pours-frame's trailing byte: the brew **ratio × 10** (water:coffee).

    e.g. 1:10 → 0x64, 1:15 → 0x96, 1:16 → 0xa0. The machine validates this against
    Σ(pour ml) / dose and REJECTS a load whose ratio byte doesn't match — so it must
    be derived from the recipe, not fixed. An explicit ``tail`` overrides (edge cases)."""
    if recipe.get("tail") is not None:
        return int(recipe["tail"]) & 0xFF
    total = sum(int(p["ml"]) for p in recipe["pours"])
    dose = int(recipe.get("dose", 0))
    return (round(total / dose * 10) & 0xFF) if dose else 0xA0


def build_load_frames(recipe: Mapping) -> list[bytes]:
    """Build the ordered list of LOAD frames for a recipe.

    Returns exactly ``[a4, a6, a8, pours]`` — the four frames that *load* the
    recipe onto the machine. The pours opcode is ``0x41`` normally, or ``0x44``
    for a **no-grind** recipe (``grind == 0``, brew pre-ground). It **never**
    includes ``0x42`` (commit) or ``0x46`` (state-sensitive confirm/pause): loading
    only arms the machine, so a load can never brew by accident. The guarded client
    owns the explicit commit/state-precondition sequence.

    ``recipe`` may be a plain dict (with keys ``dose``, ``grind``, optional
    internal ``cup_geometry_compat``, optional ``tail``, optional ``seq``, and
    ``pours``) or any mapping providing the same keys. The deprecated raw-capture
    key ``stage_temps`` remains accepted only for upstream byte-test compatibility.
    :class:`xbloom_ble.recipe.Recipe` exposes a ``to_protocol_dict()`` producing
    the canonical internal shape.
    """
    seq = recipe.get("seq", LOAD_SEQ)
    if "cup_geometry_compat" in recipe and "stage_temps" in recipe:
        raise ValueError("recipe cannot set both cup_geometry_compat and stage_temps")
    cup_geometry = recipe.get(
        "cup_geometry_compat",
        recipe.get("stage_temps", COFFEE_CUP_GEOMETRY_COMPAT),
    )
    if not isinstance(cup_geometry, (tuple, list)) or len(cup_geometry) != 2:
        raise ValueError("cup_geometry_compat must contain exactly two values")
    cup_first, cup_second = cup_geometry
    tail = _ratio_byte(recipe)                                   # ratio × 10, derived
    pours_cmd = POURS_CMD_NO_GRIND if int(recipe["grind"]) == 0 else POURS_CMD_GRIND
    frames = [
        xbloom_frame(0xA4, seq, build_a4()),
        xbloom_frame(
            0xA6,
            seq,
            build_a6(
                recipe["dose"],
                recipe.get("bypass_ml", 0.0),
                recipe.get("bypass_temp_c", 0.0),
            ),
        ),
        xbloom_frame(
            0xA8,
            seq,
            build_coffee_cup_geometry_compat(cup_first, cup_second),
        ),
        xbloom_frame(pours_cmd, seq, build_41(recipe["pours"], recipe["grind"], tail)),
    ]
    # Belt-and-braces: loading is load-only. A commit/start opcode must never ride
    # in on the LOAD sequence — starting a brew is always a separate, explicit call.
    for fr in frames:
        if fr[3] in (COMMIT_OPCODE, START_OPCODE, CANCEL_OPCODE):  # pragma: no cover
            raise AssertionError("load frames must never contain a brew-start/cancel opcode")
    return frames


def build_commit() -> bytes:
    """The ``0x42`` **commit** frame — arms → awaiting-confirm.

    After a recipe is loaded (machine at STATE ``0x1f`` armed), sending this moves
    the machine to STATE ``0x1e`` (awaiting-confirm) with its ~99 s add-beans
    countdown — the same frame the vendor app sends when you tap "Brew". Constant
    payload ``01``, seq ``0x1f``. Byte-exact vs the app's capture
    (``580101421f0c000000017fcf``).

    ⚠️ This is a brew-control frame: it is a step toward physically starting a
    brew. Emit it only in response to an explicit start action.
    """
    return xbloom_frame(COMMIT_OPCODE, LOAD_SEQ, b"\x01")


def build_start() -> bytes:
    """Build state-sensitive command 40518 for an awaiting-confirm start.

    APK 2.2.2 names this command ``全流程冲泡暂停``. On the captured target
    firmware, the same bytes start only while a fresh status report says
    ``awaiting_confirm``; while running they pause/abort. Constant payload ``01``,
    seq ``0x9e``; captured frame ``580101469e0c0000000180a1``.

    ⚠️ Emit only after the auto-start observation window and a fresh current-state
    recheck still reports awaiting-confirm, with an intentional, physically ready
    brew. A timeout or the first transient 0x1e is not evidence of a stall.
    """
    return xbloom_frame(START_OPCODE, BREW_SEQ, b"\x01")


def build_coffee_pause() -> bytes:
    """Pause a running automatic coffee recipe.

    Command 40518 is state-sensitive: hardware proves an awaiting-confirm start
    meaning, while APK 2.2.2 defines the running-state meaning as pause. The frame
    is byte-identical; only a current machine state makes the intent unambiguous.
    """
    return build_start()


def build_coffee_resume() -> bytes:
    return j15_frame(CMD_COFFEE_RESUME)


def build_cancel() -> bytes:
    """The ``0x47`` **cancel** frame — abort a committed/running brew.

    Returns the machine toward idle without completing the brew. Constant payload
    ``01``, seq ``0x9e``. Byte-exact vs the app's capture
    (``580101479e0c00000001553e``).
    """
    return xbloom_frame(CANCEL_OPCODE, BREW_SEQ, b"\x01")


def build_session_start() -> bytes:
    """The ``0xa4`` session-start frame the app sends once, right after connecting.

    :meth:`XBloomClient.save_slots` sends this before the slot writes so the
    machine is in a live session and reaches its idle/ready state; the same frame
    is the first of the LOAD sequence. Carries no brew-start opcode.
    """
    return xbloom_frame(0xA4, LOAD_SEQ, build_a4())


def build_status_query() -> bytes:
    """The ``0x56`` status/handshake frame the app sends right after ``a4`` on connect.

    The machine replies with a status/info notification. Empirically the machine will
    not arm a freshly-connected session until it has settled past its post-connect
    transitional state; the app sends this (then waits) before staging a recipe, and
    :meth:`XBloomClient.load_recipe` does the same so the load reliably reaches the
    armed state. Carries no brew-start opcode.
    """
    return xbloom_frame(0x56, LOAD_SEQ, b"\x01")


# Easy-Mode preset slots (A/B/C = 0/1/2). Programming the slots writes a preset
# onto the machine; it does NOT brew.
#
# ⚠️ Slot save is a BATCH-OF-THREE, no-commit operation (reverse-engineered from
# two vendor-app captures + confirmed on hardware). The app writes all three
# slots (A, B, C) as ``0x2CF6`` frames back-to-back — each acked by the machine
# with a ``58 02 07 f6 2c … c2 d204`` notification — and then the machine saves
# the whole set atomically, signalled by a ``0xf8`` notify and the status
# progression ``0x43`` (saving) → ``0x25`` (saved) → ``0x01`` (idle). There is NO
# separate "commit" frame: writing a single slot (or adding a trailing commit)
# leaves the machine hung at ``0x43`` and it shows RETRY. So the client always
# writes all three at once. See :meth:`XBloomClient.save_slots`.
CMD_SAVE_SLOT = 0x2CF6  # 11510
SLOT_FLAG_SCALE_ON = 0x12
SLOT_FLAG_SCALE_OFF = 0x02


def build_save_slot(recipe: Mapping, slot: int, scale: bool = True) -> bytes:
    """Build the frame that writes ``recipe`` to Easy-Mode preset ``slot`` (0=A, 1=B, 2=C).

    Frame::

        58 01 02 | f6 2c(=0x2CF6) | LEN(u32 LE) | 01 | slot | flags | <0x41 blob> | CRC16

    ``flags`` is ``0x12`` with the on-brew **scale enabled** (the default) or
    ``0x02`` with it disabled. The ``<0x41 blob>`` is the same pours+grind+ratio
    body as the LOAD ``0x41`` frame (minus its leading ``0x01``).

    This programs a preset only — it never starts a brew (the command is
    ``0x2CF6``, never ``0x42``/``0x46``). Verified byte-for-byte against the
    vendor app's captured slot writes. Note the machine only *stores* the slots
    once all three (A/B/C) have been written in one batch — see
    :meth:`XBloomClient.save_slots`.
    """
    if slot not in (0, 1, 2):
        raise ValueError(f"slot must be 0 (A), 1 (B) or 2 (C); got {slot!r}")
    if float(recipe.get("bypass_ml", 0.0) or 0.0):
        raise ValueError(
            "Easy-Mode slot command 11510 has no bypass field; refusing to drop bypass_ml"
        )
    tail = _ratio_byte(recipe)                               # ratio × 10, derived (matches the app)
    blob = build_41(recipe["pours"], recipe["grind"], tail)  # 01 | len | pours | grind | tail
    flags = SLOT_FLAG_SCALE_ON if scale else SLOT_FLAG_SCALE_OFF
    payload = bytes([0x01, slot, flags]) + blob[1:]          # drop the 0x41 leading 0x01
    body = bytearray(bytes([0x58, 0x01, 0x02]) + struct.pack("<H", CMD_SAVE_SLOT)
                     + b"\x00\x00\x00\x00" + payload)
    body[5:9] = struct.pack("<I", len(body) + 2)             # 4-byte LEN incl. CRC
    return bytes(body) + struct.pack("<H", crc16_kermit(bytes(body)))


# Machine operating mode (verified from an HCI capture of the app's mode toggle).
# Slot writes are ONLY accepted in PRO mode — in AUTO mode (the on-machine A/B/C recipe
# selector) the machine sits in status 0x41 and rejects them (RETRY). PRO mode drops it to
# status 0x01 (idle), where saves land. So :meth:`XBloomClient.save_slots` forces PRO first.
CMD_SET_MODE = 0x2CF7  # 11511
MODE_PRO_PAYLOAD = bytes.fromhex("00000000")   # → status 0x01 (idle); slot writes accepted
MODE_AUTO_PAYLOAD = bytes.fromhex("91327856")  # → status 0x41; the A/B/C preset selector


def build_set_mode(pro: bool = True) -> bytes:
    """Build the frame that switches the machine between PRO and AUTO mode.

    Frame: ``58 01 02 | f7 2c(=0x2CF7) | LEN(u32 LE) | 01 | <4-byte mode> | CRC16``. ``pro=True``
    selects PRO mode (``00000000`` → status ``0x01`` idle, where slot writes are accepted);
    ``pro=False`` selects AUTO mode (``91327856`` → the on-machine A/B/C recipe selector). This
    only changes the display mode — it never brews. Byte-exact vs the vendor app.
    """
    payload = bytes([0x01]) + (MODE_PRO_PAYLOAD if pro else MODE_AUTO_PAYLOAD)
    body = bytearray(bytes([0x58, 0x01, 0x02]) + struct.pack("<H", CMD_SET_MODE)
                     + b"\x00\x00\x00\x00" + payload)
    body[5:9] = struct.pack("<I", len(body) + 2)
    return bytes(body) + struct.pack("<H", crc16_kermit(bytes(body)))
