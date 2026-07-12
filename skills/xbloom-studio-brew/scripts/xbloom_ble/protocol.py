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
2. command 8102 (``a6 1f``) — dose.
3. command 8104 (``a8 1f``) — staging/cup float values.
4. command 8001 (``41 1f``), or 8004 for no-grind — pours + grind.

After these four frames the machine reports STATE ``0x1f`` (armed/loaded). At
that point you can either approve the brew **on the machine** or start it
remotely (below), exactly like the official app.

Starting a brew (commit / start / cancel)
-----------------------------------------
Loading only *arms* the machine. To start the brew remotely — the way the app
does when you tap "Brew" — three further command frames are used:

* command 8002 (legacy ``42 1f``) — **commit**: the machine moves to ``0x1e``
  (awaiting-confirm) and shows its ~99 s add-beans countdown.
* command 40518 (``46 9e``) — **start**: the "go" — the machine begins brewing.
* command 40519 (``47 9e``) — **cancel**: abort a committed/running brew.

All three carry the constant one-byte payload ``01`` and were captured
byte-for-byte from the vendor app (:func:`build_commit`, :func:`build_start`,
:func:`build_cancel`).

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
    "MACHINE_PATTERN_CODES",
    "LOAD_SEQ",
    "crc16_kermit",
    "xbloom_frame",
    "j15_frame",
    "frame_command",
    "build_a4",
    "build_a6",
    "build_a8",
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
    "CMD_BREWER_ENTER",
    "CMD_BREWER_START",
    "CMD_BREWER_STOP",
    "CMD_BREWER_QUIT",
    "CMD_TEA_RECIPE_CODE",
    "CMD_TEA_RECIPE_MAKE",
    "CMD_RECIPE_START_QUIT",
    "build_scale_enter",
    "build_scale_exit",
    "build_scale_tare",
    "build_grinder_enter",
    "build_grinder_start",
    "build_grinder_stop",
    "build_grinder_quit",
    "build_brewer_enter",
    "build_brewer_start",
    "build_brewer_stop",
    "build_brewer_quit",
    "build_tea_recipe_code",
    "build_tea_set_cup",
    "build_tea_code_upload",
    "build_tea_load_frames",
    "build_tea_start",
    "build_recipe_start_quit",
]

# Sequence byte used for the load sequence, and for the brew (commit/start) phase.
LOAD_SEQ = 0x1F
BREW_SEQ = 0x9E

# Brew-control opcodes. These START (or cancel) a brew — they are NOT part of the
# load sequence and are only emitted by an explicit start/cancel call.
COMMIT_OPCODE = 0x42  # commit: arm → awaiting-confirm (seq 0x1f)
START_OPCODE = 0x46   # start: the "go" — begin brewing (seq 0x9e)
CANCEL_OPCODE = 0x47  # cancel: abort a committed/running brew (seq 0x9e)

# (pattern, agitation) -> (pat_byte, agit_byte). Verified combos from the
# capture; others are best-effort extrapolation.
PATTERN_CODES: dict[tuple[str, bool], tuple[int, int]] = {
    ("spiral", True): (0x02, 0x02),   # bloom (spiral + agitation ON)
    ("spiral", False): (0x02, 0x00),  # default spiral
    ("ring", False): (0x01, 0x00),    # ring / middle
    ("center", False): (0x00, 0x01),  # center single dot
}

# Pattern values used by the app's generic J15 commands. The app UI calls
# ``ring`` "circular"; both names map to the same machine value.
MACHINE_PATTERN_CODES: dict[str, int] = {
    "center": 0,
    "ring": 1,
    "circular": 1,
    "spiral": 2,
}

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

CMD_BREWER_ENTER = 8007
CMD_BREWER_START = 4506
CMD_BREWER_STOP = 4507
CMD_BREWER_QUIT = 8013

CMD_TEA_RECIPE_MAKE = 4512
CMD_TEA_RECIPE_CODE = 4513
CMD_RECIPE_START_QUIT = 8017
CMD_SET_CUP = 8104


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


def build_brewer_enter(temp_c: int, pattern: str = "center") -> bytes:
    # HomeActivity passes pattern first, then Float.floatToIntBits(temp*10).
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
    source and machine pattern.
    """
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
    values are retained because the five official tea recipes all carry them;
    tea mode does not run the grinder.
    """
    stages: list[bytes] = []
    for index, pour in enumerate(pours):
        ml = int(pour["ml"])
        temp = int(pour.get("temp", pour.get("temp_c")))
        pattern = _machine_pattern(str(pour.get("pattern", "ring")))
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


def build_a6(dose_g: int) -> bytes:
    """0xa6 dose payload: dose grams as ``u8`` at offset 9."""
    pl = bytearray(13)
    pl[0] = 0x01
    pl[9] = int(dose_g) & 0xFF
    return bytes(pl)


def build_a8(temp1: float = 110.0, temp2: float = 90.0) -> bytes:
    """0xa8 stage-temps payload: ``01`` + f32le(temp1) + f32le(temp2).

    The captured standard case is ``01 0000dc42 0000b442`` = 110.0, 90.0.
    """
    f1 = struct.pack("<f", float(temp1))
    f2 = struct.pack("<f", float(temp2))
    return bytes([0x01]) + f1 + f2


def _pour_segments(p: Mapping) -> list[bytes]:
    """Turn one logical pour dict into a list of segment byte-strings.

    ``p`` keys: ``ml``, ``temp``, ``pattern`` ('spiral'|'center'|'ring'),
    ``agitation`` (bool), ``pause`` (seconds, post-pour), ``rpm`` (int),
    ``flow`` (ml/s float).

    8-byte pour segment: ``[ml, temp, pat, agit, negpause, 00, rpm, flow*10]``.
    A pour whose volume exceeds 127 ml is split into 127-ml 4-byte lead
    segments followed by an 8-byte remainder carrying flow/pause/rpm.
    """
    pat, agit = PATTERN_CODES[(p.get("pattern", "spiral"), bool(p.get("agitation", False)))]
    ml = int(p["ml"])
    temp = int(p["temp"]) & 0xFF
    pause = int(p.get("pause", 0))
    rpm = int(p.get("rpm", 0)) & 0xFF
    flow10 = int(round(float(p.get("flow", 3.0)) * 10)) & 0xFF
    negpause = (256 - pause) & 0xFF
    segs: list[bytes] = []
    remaining = ml
    while remaining > 127:
        segs.append(bytes([127, temp, pat, agit]))
        remaining -= 127
    segs.append(bytes([remaining & 0xFF, temp, pat, agit, negpause, 0x00, rpm, flow10]))
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
    includes ``0x42`` (commit) or ``0x46`` (start): loading only arms the machine,
    so a load can never brew by accident. To start a brew, call the dedicated
    :func:`build_commit`/:func:`build_start` builders explicitly.

    ``recipe`` may be a plain dict (with keys ``dose``, ``grind``, optional
    ``stage_temps``, optional ``tail``, optional ``seq``, and ``pours``) or any
    mapping providing the same keys. :class:`xbloom_ble.recipe.Recipe` exposes
    a ``to_protocol_dict()`` producing exactly this shape.
    """
    seq = recipe.get("seq", LOAD_SEQ)
    t1, t2 = recipe.get("stage_temps", (110.0, 90.0))
    tail = _ratio_byte(recipe)                                   # ratio × 10, derived
    pours_cmd = POURS_CMD_NO_GRIND if int(recipe["grind"]) == 0 else POURS_CMD_GRIND
    frames = [
        xbloom_frame(0xA4, seq, build_a4()),
        xbloom_frame(0xA6, seq, build_a6(recipe["dose"])),
        xbloom_frame(0xA8, seq, build_a8(t1, t2)),
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
    """The ``0x46`` **start** frame — the "go" that begins brewing.

    Sent after :func:`build_commit` (machine at ``0x1e``); the machine begins
    brewing (STATE ``0x3b``). Constant payload ``01``, seq ``0x9e`` (the brew
    phase id). Byte-exact vs the app's capture (``580101469e0c0000000180a1``).

    ⚠️ This physically dispenses near-boiling water. Emit it only when the machine
    is ready and someone intends to brew.
    """
    return xbloom_frame(START_OPCODE, BREW_SEQ, b"\x01")


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
