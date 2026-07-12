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
from xbloom_ble.recipe import Recipe
from xbloom_ble.tea import TeaRecipe

# ── real-shape frames (0x57 status = 580207571f10000000c1<state>000000<crc>) ──
ARMED = "580207571f10000000c11f000000ce5e"       # 0x1f
STARTING = "580207571f10000000c122000000b399"    # 0x22
READY = "580207571f10000000c12400000029d2"       # 0x24  (coffee-ready beep, terminal)
IDLE = "580207571f10000000c1010000002d33"        # 0x01
NO_WATER = "580207571f10000000c10c000000a2b8"    # 0x0c
NO_BEANS = "580207571f10000000c10f0000000000"    # 0x0f  (dummy crc; parser ignores it)
SLOTS_SAVED = "580207571f10000000c1250000000000"  # 0x25
ACK_42 = "580207421f0c000000c1c5c2"              # commit echo
WATER35 = "5802074b9e10000000c100b8084759b4"     # 0x4b water 35.0 g
COFFEE12 = "580207155010000000c19eef4141ceba"    # 0x15 coffee 12.12 g


def _command_notification(command: int, payload: bytes = b"") -> str:
    body = (
        bytes.fromhex("580207")
        + struct.pack("<H", command)
        + struct.pack("<I", 12 + len(payload))
        + b"\xc1"
        + payload
        + b"\x00\x00"
    )
    return body.hex()


def _water_volume_notification(ml: float) -> str:
    return _command_notification(40523, struct.pack("<f", float(ml) * 1000.0))

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
        {"ml": 90, "temp_c": 85, "pattern": "ring", "pause_s": 20, "flow_ml_s": 3.5},
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


def test_start_nudges_with_0x46_when_stalled():
    fake = FakeBleak()
    fake.script[0x42] = [ACK_42]        # commit acked but machine stalls (no state)
    fake.script[0x46] = [STARTING]      # the nudge gets it going
    c = _client(fake)
    ev = run(c.start(settle=0.05))
    assert 0x46 in _cmds(fake)
    assert ev.state_name == "starting"


def test_start_returns_refusal_state():
    fake = FakeBleak()
    fake.script[0x42] = [NO_WATER]
    c = _client(fake)
    ev = run(c.start(settle=0.5))
    assert ev.state_name == "no_water"


def test_start_falls_back_to_synthetic_when_silent():
    fake = FakeBleak()
    fake.script[0x42] = []              # commit produces nothing
    fake.script[0x46] = []              # nudge produces nothing either
    c = _client(fake)
    ev = run(c.start(settle=0.02))
    assert ev.state_name == "brewing"   # best-effort synthetic; raw is empty
    assert ev.raw == b""


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
def test_scale_stream_tares_reads_and_always_exits():
    fake = FakeBleak()
    fake.script_full[8003] = [COFFEE12]
    c = _client(fake)
    events = []
    run(c.stream_scale(events.append, duration=0.1, tare=True))
    commands = _commands(fake)
    assert commands[:2] == [8003, 8500]
    assert commands[-1] == 8014
    assert any(event.scale_g == 12.12 for event in events)


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
