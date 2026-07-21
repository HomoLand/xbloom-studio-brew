"""Client (BLE I/O) tests against a scripted fake ``bleak`` layer.

``xbloom_ble.client`` is the only module that touches hardware. We never talk to a
real machine here: a :class:`FakeBleak` stands in for ``bleak.BleakClient`` and
delivers ``ffe2`` notifications through the registered callback in response to the
command frames the client writes — so the full load / start / cancel / save-slots /
telemetry flows run headless and deterministically.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from xbloom_ble.client import CHAR_STATUS, XBloomClient, XBloomError, scan
from xbloom_ble.protocol import crc16_kermit, frame_command
from xbloom_ble.recipe import Recipe
from xbloom_ble.tea import TeaRecipe

# ── real-shape frames (0x57 status = 580207571f10000000c1<state>000000<crc>) ──
ARMED = "580207571f10000000c11f000000ce5e"       # 0x1f
AWAITING_CONFIRM = "580207571f10000000c11e0000007542"  # 0x1e
STARTING = "580207571f10000000c122000000b399"    # 0x22
READY = "580207571f10000000c12400000029d2"       # 0x24  (coffee-ready beep, terminal)
IDLE = "580207571f10000000c1010000002d33"        # 0x01
NO_WATER = "580207571f10000000c10c000000a2b8"    # 0x0c
NO_BEANS = "580207571f10000000c10f0000006f9d"    # 0x0f
SLOTS_SAVED = "580207571f10000000c12500000092ce"  # 0x25
ACK_42 = "580207421f0c000000c1c5c2"              # commit echo
WATER35 = "5802074b9e10000000c100b808470686"     # 40523 water 35.0 ml
COFFEE12 = "580207155010000000c19eef4141ceba"    # 0x15 coffee 12.12 g


def _command_notification(command: int, payload: bytes = b"") -> str:
    body_without_crc = (
        bytes.fromhex("580207")
        + struct.pack("<H", command)
        + struct.pack("<I", 12 + len(payload))
        + b"\xc1"
        + payload
    )
    body = body_without_crc + struct.pack("<H", crc16_kermit(body_without_crc))
    return body.hex()


def _water_volume_notification(ml: float) -> str:
    return _command_notification(40523, struct.pack("<f", float(ml) * 1000.0))


def _machine_info_notification(
    *, water_source: int = 1, display: int = 15, temperature_unit: int = 1,
    weight_unit: int = 1, radius: int = 720, vibration: int = 1300
) -> str:
    payload = bytearray(63)
    payload[0:13] = b"SN12345678901"
    payload[13:19] = b"J15   "
    payload[19:29] = b"V12.0D.500"
    struct.pack_into("<f", payload, 29, 42.5)
    payload[33:42] = bytes(
        [1, 2, 1, water_source, 92, display, 24, temperature_unit, weight_unit]
    )
    payload[51:55] = bytes.fromhex("91327856")
    struct.pack_into("<I", payload, 55, radius)
    struct.pack_into("<I", payload, 59, vibration)
    return _command_notification(40521, bytes(payload))

RECIPE = Recipe.from_dict({
    "name": "T", "dose_g": 16, "grind": 55, "ratio": 15,
    "pours": [{"ml": 40, "temp_c": 92, "pattern": "spiral", "pause_s": 30,
               "rpm": 100, "flow_ml_s": 3.0},
              {"ml": 200, "temp_c": 92, "pattern": "spiral", "pause_s": 5,
               "rpm": 100, "flow_ml_s": 3.0}],
})

TEA_RECIPE = TeaRecipe.from_dict({
    "name": "Green", "kind": "tea", "leaf_g": 4, "output_ml_per_steep": 120,
    "pours": [
        {"ml": 90, "temp_c": 85, "pattern": "circular", "pause_s": 20, "flow_ml_s": 3.5},
        {"ml": 90, "temp_c": 85, "pattern": "center", "pause_s": 15, "flow_ml_s": 3.5},
    ],
})


class FakeBleak:
    """Scripted stand-in for ``bleak.BleakClient``.

    Delivers per-command notifications: when the client writes a frame, we push the
    scripted ``ffe2`` frames for that command byte back through the notify callback.
    """

    def __init__(self, address="AA:BB:CC:DD:EE:FF", **_):
        self.address = address
        self.is_connected = False
        self.writes: list[bytes] = []
        self._cb = None
        self._aux_cb = None
        self._slot_writes = 0
        # command byte (offset 3) -> frames to push after that write
        self.script: dict[int, list[str]] = {
            0x41: [ARMED],            # pours frame -> machine arms
            0x44: [ARMED],            # no-grind pours -> arms
            0x42: [ACK_42, STARTING],  # commit -> acts (grinding)
            0xF7: [IDLE],             # set-mode -> idle (PRO ready / back to AUTO)
        }
        self.script_full: dict[int, list[str]] = {}

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char, cb):
        if char == CHAR_STATUS:
            self._cb = cb
        else:
            self._aux_cb = cb

    async def stop_notify(self, char):
        pass

    def _push(self, hx: str):
        if self._cb is not None:
            self._cb(None, bytearray(bytes.fromhex(hx)))

    async def write_gatt_char(self, char, data, response=False):
        data = bytes(data)
        self.writes.append(data)
        cmd = data[3]
        command = int.from_bytes(data[3:5], "little")
        for hx in self.script_full.get(command, []):
            self._push(hx)
        for hx in self.script.get(cmd, []):
            self._push(hx)
        if cmd == 0xF6:  # a slot write; the machine stores after the full trio
            self._slot_writes += 1
            if self._slot_writes >= 3:
                self._push(SLOTS_SAVED)


def _cmds(fake: FakeBleak) -> list[int]:
    return [w[3] for w in fake.writes]


def _commands(fake: FakeBleak) -> list[int]:
    return [int.from_bytes(w[3:5], "little") for w in fake.writes]


def _client(fake: FakeBleak) -> XBloomClient:
    c = XBloomClient("AA:BB:CC:DD:EE:FF")
    c._client = fake
    fake.is_connected = True
    return c


def run(coro):
    return asyncio.run(coro)


# ── scan / connect ─────────────────────────────────────────────────────────
def test_scan_matches_by_name(monkeypatch):
    import bleak

    class Dev:
        address = "AA:BB:CC:DD:EE:FF"
        name = "XBLOOM-TEST"

    class Adv:
        local_name = "XBLOOM-TEST"
        service_uuids = []

    async def fake_discover(timeout=8.0, return_adv=True):
        return {"AA:BB:CC:DD:EE:FF": (Dev(), Adv())}

    monkeypatch.setattr(bleak.BleakScanner, "discover", staticmethod(fake_discover))
    found = run(scan(timeout=0.01))
    assert found and found[0].address == "AA:BB:CC:DD:EE:FF"


def test_connect_and_context_manager(monkeypatch):
    import bleak

    fake = FakeBleak()
    monkeypatch.setattr(bleak, "BleakClient", lambda addr: fake)

    async def go():
        async with XBloomClient("AA:BB:CC:DD:EE:FF") as c:
            assert c._client.is_connected
        assert not fake.is_connected  # __aexit__ disconnected

    run(go())


# ── loading (arms only, never brews) ───────────────────────────────────────
def test_load_recipe_arms_and_sends_four_frames():
    fake = FakeBleak()
    c = _client(fake)
    ev = run(c.load_recipe(RECIPE, settle=0.01))
    assert ev.state_name == "armed"
    # a4 (session) + status query (0x56) + a6 + a8 + pours(0x41); no brew opcodes.
    cmds = _cmds(fake)
    assert cmds[:2] == [0xA4, 0x56]
    assert cmds[-1] == 0x41
    assert not ({0x42, 0x46, 0x47} & set(cmds)), "loading must never brew"


def test_load_recipe_requires_connection():
    c = XBloomClient("AA:BB:CC:DD:EE:FF")  # never connected
    with pytest.raises(XBloomError):
        run(c.load_recipe(RECIPE, settle=0.01))


# ── starting (adaptive) ────────────────────────────────────────────────────
def test_start_acts_on_commit_without_0x46():
    fake = FakeBleak()
    c = _client(fake)
    ev = run(c.start(settle=0.5))
    assert ev.state_name == "starting"
    assert 0x42 in _cmds(fake)
    assert 0x46 not in _cmds(fake)  # machine acted -> don't nudge


def test_start_does_not_pause_transient_awaiting_confirm():
    fake = FakeBleak()
    # Real V12.0D.500 sequence: 0x1e is transitional, then grinding starts.
    fake.script[0x42] = [ACK_42, AWAITING_CONFIRM, STARTING]
    c = _client(fake)
    ev = run(c.start(settle=0.05))
    assert ev.state_name == "starting"
    assert 0x46 not in _cmds(fake)


def test_start_nudges_with_0x46_when_stalled():
    fake = FakeBleak()
    fake.script[0x42] = [ACK_42, AWAITING_CONFIRM]
    fake.script[0x56] = [AWAITING_CONFIRM]  # fresh current-state recheck
    fake.script[0x46] = [STARTING]      # the nudge gets it going
    c = _client(fake)
    ev = run(c.start(settle=0.05))
    assert _cmds(fake)[-3:] == [0x42, 0x56, 0x46]
    assert ev.state_name == "starting"


def test_start_returns_refusal_state():
    fake = FakeBleak()
    fake.script[0x42] = [NO_WATER]
    c = _client(fake)
    ev = run(c.start(settle=0.5))
    assert ev.state_name == "no_water"


def test_start_refuses_state_sensitive_40518_when_commit_state_is_silent():
    fake = FakeBleak()
    fake.script[0x42] = []
    c = _client(fake)
    with pytest.raises(XBloomError, match="commit outcome is unconfirmed"):
        run(c.start(settle=0.02))
    assert 0x46 not in _cmds(fake)


def test_start_fails_if_40518_outcome_is_unconfirmed():
    fake = FakeBleak()
    fake.script[0x42] = [AWAITING_CONFIRM]
    fake.script[0x56] = [AWAITING_CONFIRM]
    fake.script[0x46] = []
    c = _client(fake)
    with pytest.raises(XBloomError, match="start is unconfirmed"):
        run(c.start(settle=0.02))
    assert 0x46 in _cmds(fake)


def test_start_reports_if_40518_returns_machine_to_armed():
    fake = FakeBleak()
    fake.script[0x42] = [AWAITING_CONFIRM]
    fake.script[0x56] = [AWAITING_CONFIRM]
    fake.script[0x46] = [ARMED]
    c = _client(fake)
    with pytest.raises(XBloomError, match="possible start/pause race"):
        run(c.start(settle=0.02))


def test_start_refuses_40518_when_awaiting_state_cannot_be_revalidated():
    fake = FakeBleak()
    fake.script[0x42] = [AWAITING_CONFIRM]
    fake.script[0x56] = []
    c = _client(fake)
    with pytest.raises(XBloomError, match="current state could not be revalidated"):
        run(c.start(settle=0.02))
    assert 0x56 in _cmds(fake)
    assert 0x46 not in _cmds(fake)


def test_start_accepts_progress_observed_by_current_state_recheck():
    fake = FakeBleak()
    fake.script[0x42] = [AWAITING_CONFIRM]
    fake.script[0x56] = [STARTING]
    c = _client(fake)
    ev = run(c.start(settle=0.02))
    assert ev.state_name == "starting"
    assert 0x46 not in _cmds(fake)


# ── brew (load + start) & cancel ───────────────────────────────────────────
def test_brew_loads_then_starts():
    fake = FakeBleak()
    c = _client(fake)
    ev = run(c.brew(RECIPE, settle=0.01))
    cmds = _cmds(fake)
    assert 0x41 in cmds and 0x42 in cmds     # loaded then committed
    assert ev.state_name == "starting"


def test_cancel_sends_0x47():
    fake = FakeBleak()
    c = _client(fake)
    run(c.cancel_brew())
    assert _cmds(fake) == [0x47]


# ── FreeSolo scale / grinder / brewer & dedicated tea path ────────────────
def test_scale_stream_default_enters_without_extra_tare_and_signals_ready_first():
    fake = FakeBleak()
    fake.script_full[8003] = [COFFEE12]
    c = _client(fake)
    timeline = []
    run(
        c.stream_scale(
            lambda event: timeline.append(("reading", event.scale_g)),
            duration=0.1,
            on_ready=lambda: timeline.append(("ready", None)),
        )
    )
    assert _commands(fake) == [8003, 8014]
    assert timeline[0] == ("ready", None)
    assert ("reading", 12.12) in timeline


def test_scale_stream_explicit_tare_is_additional_and_always_exits():
    fake = FakeBleak()
    fake.script_full[8003] = [COFFEE12]
    c = _client(fake)
    events = []
    run(c.stream_scale(events.append, duration=0.1, tare=True))
    commands = _commands(fake)
    assert commands[:2] == [8003, 8500]
    assert commands[-1] == 8014
    assert any(event.scale_g == 12.12 for event in events)


def test_active_scale_retare_is_a_serialized_write_without_fake_ack():
    fake = FakeBleak()
    c = _client(fake)
    run(c.tare_scale())
    assert _commands(fake) == [8500]


def test_grinder_stops_and_quits_after_timed_run():
    fake = FakeBleak()
    fake.script_full[8006] = [_command_notification(8006)]
    fake.script_full[3500] = [_command_notification(3500)]
    fake.script_full[3505] = [_command_notification(3505)]
    c = _client(fake)
    run(c.grind(62, 100, seconds=0.1))
    assert _commands(fake) == [8006, 3500, 3505, 8012]


def test_grinder_cleanup_runs_when_cancelled():
    fake = FakeBleak()
    fake.script_full[8006] = [_command_notification(8006)]
    fake.script_full[3500] = [_command_notification(3500)]
    fake.script_full[3505] = [_command_notification(3505)]
    c = _client(fake)

    async def go():
        task = asyncio.create_task(c.grind(62, 100, seconds=5))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert _commands(fake) == [8006, 3500, 3505, 8012]


def test_water_waits_for_completion_then_quits_without_forced_stop():
    fake = FakeBleak()
    fake.script_full[4506] = [
        _water_volume_notification(120),
        _command_notification(40511),
    ]
    c = _client(fake)
    event = run(c.dispense_water(120, 85, timeout=5))
    assert event.command_code == 40511
    assert event.water_g == 120
    assert _commands(fake) == [8007, 4506, 8013]


def test_water_accepts_official_rt_sentinel():
    fake = FakeBleak()
    fake.script_full[4506] = [
        _water_volume_notification(120),
        _command_notification(40511),
    ]
    c = _client(fake)
    event = run(c.dispense_water(120, 20, timeout=5))
    assert event.command_code == 40511
    assert _commands(fake) == [8007, 4506, 8013]


def test_water_passes_selected_tap_source_to_start_frame():
    fake = FakeBleak()
    fake.script_full[4506] = [
        _water_volume_notification(120),
        _command_notification(40511),
    ]
    c = _client(fake)
    run(c.dispense_water(120, 85, water_feed=1, timeout=5))
    start = next(frame for frame in fake.writes if frame_command(frame) == 4506)
    words = struct.unpack(f"<{(len(start[10:-2]) // 4)}I", start[10:-2])
    assert words[-2] == 1


def test_water_accepts_canonical_circular_pattern_and_encodes_value_one():
    fake = FakeBleak()
    fake.script_full[4506] = [
        _water_volume_notification(120),
        _command_notification(40511),
    ]
    c = _client(fake)
    run(c.dispense_water(120, 85, pattern="circular", timeout=5))
    start = next(frame for frame in fake.writes if frame_command(frame) == 4506)
    words = struct.unpack(f"<{(len(start[10:-2]) // 4)}I", start[10:-2])
    assert words[-1] == 1


@pytest.mark.parametrize("temp_c", [19, 21, 39, 99])
def test_water_rejects_values_outside_rt_or_numeric_range(temp_c):
    fake = FakeBleak()
    c = _client(fake)
    with pytest.raises(XBloomError, match="RT or 40-98 C"):
        run(c.dispense_water(120, temp_c, timeout=5))
    assert _commands(fake) == []


def test_water_early_stop_is_failure_and_forces_cleanup():
    fake = FakeBleak()
    fake.script_full[4506] = [
        _water_volume_notification(50),
        _command_notification(40511),
    ]
    c = _client(fake)
    with pytest.raises(XBloomError, match="stopped early"):
        run(c.dispense_water(120, 85, timeout=5))
    assert _commands(fake) == [8007, 4506, 4507, 8013]


def test_tea_load_never_executes_then_start_is_separate():
    fake = FakeBleak()
    fake.script_full[8104] = [_command_notification(8104)]
    fake.script_full[4513] = [_command_notification(4513)]
    fake.script_full[4512] = [_command_notification(4512)]
    c = _client(fake)
    run(c.load_tea_recipe(TEA_RECIPE, settle=0))
    assert _commands(fake) == [8100, 8022, 8104, 4513]
    run(c.start_tea())
    assert _commands(fake) == [8100, 8022, 8104, 4513, 4512]


# ── save-slots (never brews) ───────────────────────────────────────────────
def test_save_slots_programs_three_and_never_brews():
    fake = FakeBleak()
    c = _client(fake)
    run(c.save_slots([RECIPE, RECIPE, RECIPE]))
    cmds = _cmds(fake)
    assert cmds.count(0xF6) == 3                     # three slot writes
    assert not ({0x42, 0x46, 0x47} & set(cmds))      # never a brew opcode


def test_save_slots_rejects_wrong_count():
    fake = FakeBleak()
    c = _client(fake)
    with pytest.raises(XBloomError):
        run(c.save_slots([RECIPE, RECIPE]))


# ── telemetry streaming ────────────────────────────────────────────────────
def test_stream_telemetry_decodes_weights_and_stops_on_ready():
    fake = FakeBleak()
    c = _client(fake)
    events = []

    async def feed():
        await asyncio.sleep(0.02)
        for hx in (WATER35, COFFEE12, READY, IDLE):
            fake._push(hx)
            await asyncio.sleep(0.01)

    async def go():
        await asyncio.gather(
            c.stream_telemetry(events.append, duration=5.0),
            feed(),
        )

    run(go())
    assert any(e.water_g == 35.0 for e in events)
    assert any(e.coffee_g == 12.12 for e in events)
    assert events[-1].state_name == "ready"     # stopped at the beep (0x24 terminal)


def test_stream_telemetry_capture_aux_taps_ffe3():
    fake = FakeBleak()
    c = _client(fake)

    async def feed():
        await asyncio.sleep(0.02)
        fake._push(READY)

    async def go():
        await asyncio.gather(
            c.stream_telemetry(lambda e: None, duration=5.0, capture_aux=True),
            feed(),
        )

    run(go())
    assert fake._aux_cb is not None    # the ffe3 aux tap was subscribed


def test_stream_telemetry_honours_duration():
    fake = FakeBleak()
    c = _client(fake)
    # nothing is ever pushed -> returns when the (tiny) duration elapses, no hang
    run(c.stream_telemetry(lambda e: None, duration=0.05))


# ── held session (open_session): the on-connect handshake that shows "connected" ──
def test_open_session_subscribes_and_sends_a4():
    """open_session mirrors the phone app: subscribe to ffe2 + send the a4 frame."""
    fake = FakeBleak()
    c = _client(fake)
    run(c.open_session())
    assert fake._cb is not None                 # subscribed to ffe2
    assert _cmds(fake) == [0xA4]                 # exactly the session-start frame
    assert c._session_active and c._subscribed


def test_persistent_control_waiters_and_listeners_do_not_steal_acks():
    fake = FakeBleak()
    c = _client(fake)
    observed = []
    c.add_event_listener(observed.append)
    for command in (40518, 40524, 8018, 8020, 8019, 8021):
        fake.script_full[command] = [_command_notification(command)]
    fake.script_full[8016] = [_command_notification(8107, struct.pack("<I", 2))]
    fake.script_full[4510] = [_command_notification(8108, struct.pack("<I", 85))]

    async def go():
        assert (await c.pause_coffee()).command_code == 40518
        assert (await c.resume_coffee()).command_code == 40524
        assert (await c.pause_grinder()).command_code == 8018
        assert (await c.resume_grinder()).command_code == 8020
        assert (await c.pause_water()).command_code == 8019
        assert (await c.resume_water()).command_code == 8021
        assert (await c.set_water_pattern("spiral")).command_code == 8107
        assert (await c.set_water_temperature(85)).command_code == 8108

    run(go())
    assert [event.command_code for event in observed] == [
        40518,
        40524,
        8018,
        8020,
        8019,
        8021,
        8107,
        8108,
    ]
    assert not c._command_waiters


def test_persistent_settings_write_each_exact_command_then_read_back_40521():
    fake = FakeBleak()
    c = _client(fake)
    for command in (8005, 8010, 4508, 8103):
        fake.script_full[command] = [_command_notification(command)]
    fake.script_full[8022] = [_machine_info_notification()]

    result = run(
        c.set_machine_settings(
            weight_unit="g",
            temperature_unit="C",
            water_source="tap",
            display="high",
        )
    )

    assert _commands(fake) == [8005, 8010, 4508, 8103, 8022]
    assert result["weight_unit"] == "g"
    assert result["temperature_unit"] == "C"
    assert result["water_source"] == "tap"
    assert result["display"] == "high"


def test_advanced_settings_write_and_readback_use_all_four_code_module2_commands():
    fake = FakeBleak()
    c = _client(fake)
    fake.script_full[11507] = [
        _command_notification(11507, struct.pack("<I", 720))
    ]
    fake.script_full[11509] = [
        _command_notification(11509, struct.pack("<I", 1300))
    ]
    fake.script_full[11506] = [
        _command_notification(11506, struct.pack("<I", 720))
    ]
    fake.script_full[11508] = [
        _command_notification(11508, struct.pack("<I", 1300))
    ]

    result = run(
        c.write_advanced_settings(pour_radius=720, vibration_amplitude=1300)
    )

    assert _commands(fake) == [11507, 11509, 11506, 11508]
    assert result == {"pour_radius": 720, "vibration_amplitude": 1300}


def test_setting_and_advanced_writes_require_at_least_one_requested_value():
    fake = FakeBleak()
    c = _client(fake)
    with pytest.raises(ValueError, match="at least one machine setting"):
        run(c.set_machine_settings())
    with pytest.raises(ValueError, match="at least one advanced setting"):
        run(c.write_advanced_settings())
    assert fake.writes == []


def test_live_pattern_write_succeeds_when_optional_8107_report_is_absent():
    fake = FakeBleak()
    c = _client(fake)
    c.ack_timeout = 0.01
    assert run(c.set_water_pattern("spiral")) is None
    assert _commands(fake) == [8016]


def test_interactive_water_stop_waits_for_4507_echo_then_quits():
    fake = FakeBleak()
    fake.script_full[4507] = [_command_notification(4507)]
    c = _client(fake)
    event = run(c.stop_water_session())
    assert event.command_code == 4507
    assert _commands(fake) == [4507, 8013]


def test_grinder_stop_ack_timeout_still_sends_quit_and_cleans_notification_mode():
    fake = FakeBleak()
    c = _client(fake)
    c.ack_timeout = 0.01
    with pytest.raises(XBloomError, match="timed out"):
        run(c.stop_grinder_session())
    assert 3505 in _commands(fake)
    assert 8012 in _commands(fake)
    assert c._consuming is False


def test_idle_session_drops_notifications():
    """While a session is held but no op is consuming, the machine's idle stream is
    dropped so the queue can't grow unbounded."""
    fake = FakeBleak()
    c = _client(fake)
    run(c.open_session())
    for _ in range(50):                          # simulate the machine's idle chatter
        fake._push(IDLE)
        fake._push(WATER35)
    assert c._notif_queue.empty()                # nothing queued while idle


def test_session_held_across_a_load():
    """A load reuses the held subscription and leaves it up afterwards (no teardown),
    and post-load idle frames are still dropped."""
    fake = FakeBleak()
    c = _client(fake)
    run(c.open_session())
    armed = run(c.load_recipe(RECIPE, settle=0))
    assert armed.state == 0x1F                   # armed via the queued ARMED frame
    assert c._subscribed and c._session_active   # subscription held past the op
    fake._push(IDLE)
    assert c._notif_queue.empty()                # back to idle → dropped again


def test_start_notify_drains_stale_backlog():
    """Starting consumption clears any stale queued events first."""
    from xbloom_ble.telemetry import StatusEvent
    fake = FakeBleak()
    c = _client(fake)
    c._notif_queue.put_nowait(StatusEvent(state=0x99, state_name="stale", raw=b""))
    run(c._start_notify())
    assert c._notif_queue.empty() and c._consuming is True


def test_disconnect_resets_session():
    fake = FakeBleak()
    c = _client(fake)
    run(c.open_session())
    run(c.disconnect())
    assert not c._session_active and not c._subscribed and not c._consuming
