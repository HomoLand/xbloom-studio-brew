"""Async Bluetooth LE client for the xBloom Studio (via ``bleak``).

This is the only module that touches hardware. It discovers the machine,
connects, writes guarded coffee/tea and FreeSolo frames, and streams telemetry.

Safety model: loading and starting are **separate, explicit** operations.
:meth:`XBloomClient.load_recipe` only *loads* (writes ``a4, a6, a8, 41`` and
returns once the machine is armed at STATE ``0x1f``) — it never starts a brew, so
a load can never brew by accident. :meth:`XBloomClient.start` is the deliberate
"go": it sends commit (``0x42``) + start (``0x46``) to launch the brew remotely,
exactly like the app's Brew button. :meth:`XBloomClient.brew` is the convenience
that loads then starts. :meth:`XBloomClient.cancel_brew` aborts (``0x47``).

⚠️ Starting coffee/tea or standalone water physically dispenses hot water, and
standalone grinding runs a motor. Public Agent workflows must use the gated CLI,
not call these client methods directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

from .protocol import (
    CMD_BREWER_PAUSE,
    CMD_BREWER_RESUME,
    CMD_BREWER_STOP,
    CMD_COFFEE_PAUSE,
    CMD_COFFEE_RESUME,
    CMD_GRINDER_PAUSE,
    CMD_GRINDER_RESUME,
    CMD_TEA_RECIPE_CODE,
    CMD_TEA_RECIPE_MAKE,
    CMD_READ_POUR_RADIUS,
    CMD_READ_VIBRATION_AMPLITUDE,
    CMD_SET_DISPLAY,
    CMD_SET_TEMPERATURE_UNIT,
    CMD_SET_WATER_SOURCE,
    CMD_SET_WEIGHT_UNIT,
    CMD_WRITE_POUR_RADIUS,
    CMD_WRITE_VIBRATION_AMPLITUDE,
    ROOM_TEMPERATURE_C,
    build_brewer_enter,
    build_brewer_pause,
    build_brewer_quit,
    build_brewer_resume,
    build_brewer_set_pattern,
    build_brewer_set_temperature,
    build_brewer_start,
    build_brewer_stop,
    build_cancel,
    build_coffee_pause,
    build_coffee_resume,
    build_commit,
    build_grinder_enter,
    build_grinder_pause,
    build_grinder_quit,
    build_grinder_resume,
    build_grinder_start,
    build_grinder_stop,
    build_load_frames,
    build_recipe_start_quit,
    build_read_pour_radius,
    build_read_vibration_amplitude,
    build_save_slot,
    build_scale_enter,
    build_scale_exit,
    build_scale_tare,
    build_session_start,
    build_set_mode,
    build_set_display,
    build_set_temperature_unit,
    build_set_water_source,
    build_set_weight_unit,
    build_start,
    build_status_query,
    build_tea_load_frames,
    build_tea_start,
    build_write_pour_radius,
    build_write_vibration_amplitude,
    frame_command,
)
from .recipe import Recipe
from .tea import TeaRecipe
from .telemetry import (
    BREWER_MODE_COMMAND,
    BREWER_TEMPERATURE_COMMAND,
    MACHINE_INFO_COMMAND,
    NotificationFrameStream,
    StatusEvent,
    parse_notification,
)

log = logging.getLogger("xbloom_ble")

# Vendor GATT identifiers.
SERVICE_UUID = "0000e0ff-3c17-d293-8e48-14fe2e4da212"
CHAR_COMMAND = "0000ffe1-0000-1000-8000-00805f9b34fb"  # ffe1 — write
CHAR_STATUS = "0000ffe2-0000-1000-8000-00805f9b34fb"   # ffe2 — notify
CHAR_AUX = "0000ffe3-0000-1000-8000-00805f9b34fb"      # ffe3 — aux
NAME_PREFIX = "XBLOOM"

# State byte that means "recipe loaded / armed".
STATE_ARMED = 0x1F
STATE_AWAITING_CONFIRM = 0x1E
# Brew lifecycle states: 0x22 starting/grinding, 0x3b brewing. On some machines commit
# auto-proceeds through these; on others the machine waits in awaiting-confirm (0x1e)
# and needs the 0x46 start frame.
STATE_STARTING = 0x22
STATE_BREWING = 0x3B
# Machine-refused states (it checks water/beans right after commit, before pouring).
STATE_NO_WATER = 0x0C
STATE_NO_BEANS = 0x0F
# Slot-save status states (see telemetry): 0x43 saving, 0x25 saved, 0x01 idle.
STATE_IDLE = 0x01
STATE_SLOTS_SAVED = 0x25

# Generic notification/report command codes from the official app.
REPORT_BREWER_STOP = 40511


class XBloomError(RuntimeError):
    """Raised on BLE / protocol errors in the client."""


def _require_event(event: StatusEvent | None, operation: str) -> StatusEvent:
    """Require an ACK event without relying on removable Python assertions."""

    if event is None:
        raise XBloomError(f"{operation} returned no acknowledgement event")
    return event


async def scan(timeout: float = 8.0):
    """Discover xBloom machines.

    Returns a list of ``bleak.backends.device.BLEDevice`` whose advertisement
    exposes the vendor service UUID *or* whose name starts with ``XBLOOM``.
    """
    from bleak import BleakScanner

    log.info("scanning for xBloom machines (%.0fs)…", timeout)
    found: dict[str, object] = {}
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in devices.items():
        name = (adv.local_name or getattr(device, "name", None) or "") or ""
        service_uuids = {u.lower() for u in (adv.service_uuids or [])}
        if SERVICE_UUID.lower() in service_uuids or name.upper().startswith(NAME_PREFIX):
            found[address] = device
            log.info("found %s (%s)", name or "?", address)
    return list(found.values())


class XBloomClient:
    """A connected session with one xBloom Studio.

    Use as an async context manager::

        async with XBloomClient(address) as client:
            await client.load_recipe(recipe)
            await client.stream_telemetry(on_event, duration=300)
    """

    def __init__(self, address: str, *, ack_timeout: float = 10.0):
        self.address = address
        self.ack_timeout = ack_timeout
        self._client = None
        self._notification_stream = NotificationFrameStream()
        self._notif_queue: asyncio.Queue[StatusEvent] = asyncio.Queue()
        # Held-session state (see open_session): once a session is open we keep the
        # ffe2 subscription up so the machine shows "connected", but we only *queue*
        # notifications while an operation is actively consuming them (``_consuming``)
        # — otherwise the machine's continuous idle stream would grow the queue forever.
        self._subscribed = False       # ffe2 notify subscription is active
        self._session_active = False   # hold the subscription across operations
        self._consuming = False        # an operation wants frames queued right now
        self._write_lock = asyncio.Lock()
        self._event_listeners: set[
            Callable[[StatusEvent], Awaitable[None] | None]
        ] = set()
        self._command_waiters: dict[int, set[asyncio.Future[StatusEvent]]] = {}

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        """True while the underlying BLE link is up (for held-connection callers)."""
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        from bleak import BleakClient

        # Idempotent: a held-connection caller (e.g. the TUI) may call connect() to
        # "ensure connected" — if the link is already up, this is a fast no-op rather
        # than leaking a second BleakClient.
        if self.is_connected:
            return
        log.info("connecting to %s…", self.address)
        self._notification_stream.reset()
        self._client = BleakClient(self.address)
        await self._client.connect()
        if not self._client.is_connected:
            raise XBloomError(f"failed to connect to {self.address}")
        log.info("connected")

    async def disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
            log.info("disconnected")
        for waiters in self._command_waiters.values():
            for future in waiters:
                if not future.done():
                    future.cancel()
        self._command_waiters.clear()
        self._client = None
        self._subscribed = False
        self._session_active = False
        self._consuming = False
        self._notification_stream.reset()

    async def open_session(self, *, settle: float = 0.3) -> None:
        """Register as an app-style session so the machine shows it's **connected**.

        Mirrors exactly what the phone app does the moment it connects (verified from
        the HCI capture): subscribe to ffe2 status notifications, then write the
        ``a4`` session-start frame. The machine responds by streaming status and
        lighting its paired/connected icon, and the session is **held** — the ffe2
        subscription stays up across brews (idle frames are dropped, see
        :meth:`_on_notify`) so the link stays warm and no per-brew re-handshake is
        needed. The app sends no periodic keepalive, so neither do we.

        This is a session handshake, **not** a brew: ``a4`` only opens a session and
        never dispenses water (the brew opcodes ``0x42``/``0x46`` live only in
        :meth:`start`). Safe to call on every connect; idempotent-ish (re-sending
        ``a4`` is harmless).
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        self._session_active = True
        await self._ensure_subscribed()
        log.info("→ a4 (open session — machine shows connected)")
        await self._client.write_gatt_char(CHAR_COMMAND, build_session_start(), response=False)
        await asyncio.sleep(settle)

    async def close_session(self) -> None:
        """Drop the held session (stop holding the ffe2 subscription). The BLE link
        itself stays up until :meth:`disconnect`."""
        self._session_active = False
        await self._stop_notify()

    async def __aenter__(self) -> XBloomClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def _on_notify(self, _sender, data: bytearray) -> None:
        chunk = bytes(data)
        crc_before = self._notification_stream.stats.invalid_crc_frames
        length_before = self._notification_stream.stats.invalid_length_frames
        frames = self._notification_stream.feed(chunk)
        crc_rejected = self._notification_stream.stats.invalid_crc_frames - crc_before
        length_rejected = (
            self._notification_stream.stats.invalid_length_frames - length_before
        )
        if crc_rejected or length_rejected:
            log.warning(
                "rejected malformed xBloom notification data (crc=%d length=%d)",
                crc_rejected,
                length_rejected,
            )
        if not frames:
            log.debug(
                "← chunk %s (pending=%d)",
                chunk.hex(),
                self._notification_stream.pending_bytes,
            )
        for raw in frames:
            event = parse_notification(raw)
            # Full raw chatter at DEBUG (enable with `--debug`) — this is how we
            # retain still-unknown reports for later evidence-based decoding.
            log.debug(
                "← %s%s",
                raw.hex(),
                f"  [{event.state_name}]" if event is not None else "",
            )
            if event is not None:
                self._dispatch_event(event)

    def _dispatch_event(self, event: StatusEvent) -> None:
        """Deliver one validated event to ACK waiters, listeners, and consumers."""

        waiters = self._command_waiters.get(event.command_code or -1, set()).copy()
        for future in waiters:
            if not future.done():
                future.set_result(event)
        for listener in tuple(self._event_listeners):
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # pragma: no cover - observers must never break BLE I/O
                log.exception("xBloom event listener failed")
        # Only queue while an operation is consuming. During an idle held session the
        # machine streams status continuously (heartbeats, scale, idle-state frames) —
        # dropping those here keeps the queue bounded instead of growing unbounded.
        if self._consuming:
            self._notif_queue.put_nowait(event)

    def add_event_listener(
        self, listener: Callable[[StatusEvent], Awaitable[None] | None]
    ) -> None:
        """Observe every decoded notification without consuming operation queues."""
        self._event_listeners.add(listener)

    def remove_event_listener(
        self, listener: Callable[[StatusEvent], Awaitable[None] | None]
    ) -> None:
        self._event_listeners.discard(listener)

    async def send_command(
        self,
        frame: bytes,
        *,
        expect_command: int | None = None,
        timeout: float | None = None,
        response_optional: bool = False,
    ) -> StatusEvent | None:
        """Serialize one write and optionally await its matching report/ACK.

        Waiters are registered before the write so immediate notifications cannot
        race past a persistent bridge. The normal operation queue still receives
        the same event; observing an ACK here never steals it from another workflow.
        When ``response_optional`` is true, a completed BLE write with no matching
        report returns ``None`` instead of pretending that the physical write failed.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        await self._ensure_subscribed()
        future: asyncio.Future[StatusEvent] | None = None
        if expect_command is not None:
            future = asyncio.get_running_loop().create_future()
            self._command_waiters.setdefault(int(expect_command), set()).add(future)
        try:
            async with self._write_lock:
                await self._client.write_gatt_char(CHAR_COMMAND, bytes(frame), response=False)
            if future is None:
                return None
            try:
                return await asyncio.wait_for(
                    future, timeout=self.ack_timeout if timeout is None else float(timeout)
                )
            except asyncio.TimeoutError:
                if response_optional:
                    return None
                raise XBloomError(
                    f"timed out waiting for command 0x{int(expect_command):04x}"
                ) from None
        finally:
            if future is not None:
                waiters_for_command = self._command_waiters.get(int(expect_command))
                if waiters_for_command is not None:
                    waiters_for_command.discard(future)
                    if not waiters_for_command:
                        self._command_waiters.pop(int(expect_command), None)

    async def _ensure_subscribed(self) -> None:
        """Subscribe to ffe2 status notifications (idempotent)."""
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        if self._subscribed:
            return
        await self._client.start_notify(CHAR_STATUS, self._on_notify)
        self._subscribed = True

    async def _start_notify(self) -> None:
        # Ensure we're listening, start with a clean queue (drop any idle-session
        # backlog), and mark that this operation wants frames.
        await self._ensure_subscribed()
        while not self._notif_queue.empty():
            self._notif_queue.get_nowait()
        self._consuming = True

    async def _stop_notify(self) -> None:
        # Operation finished consuming. Keep the subscription up if a session is held
        # (so the machine stays "connected"); otherwise tear it down.
        self._consuming = False
        if self._session_active:
            return
        if self._subscribed and self._client is not None and self._client.is_connected:
            try:
                await self._client.stop_notify(CHAR_STATUS)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        self._subscribed = False
        self._notification_stream.reset()

    async def _drain_until_state(self, state: int, timeout: float) -> StatusEvent:
        """Wait for a status event whose state byte equals ``state``."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise XBloomError(
                    f"timed out waiting for state 0x{state:02x} after {timeout:.0f}s"
                )
            try:
                event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise XBloomError(
                    f"timed out waiting for state 0x{state:02x} after {timeout:.0f}s"
                ) from None
            if event.is_heartbeat:
                continue
            log.debug("status: %s (raw=%s)", event.state_name, event.raw.hex())
            if event.state == state:
                return event

    async def _drain_for_command(self, command: int, timeout: float) -> StatusEvent:
        """Wait for a notification carrying the full generic u16 command code."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise XBloomError(
                    f"timed out waiting for command 0x{command:04x} after {timeout:.0f}s"
                )
            try:
                event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise XBloomError(
                    f"timed out waiting for command 0x{command:04x} after {timeout:.0f}s"
                ) from None
            if event.command_code == command:
                return event

    async def _drain_water_completion(
        self, target_ml: float, timeout: float
    ) -> StatusEvent | None:
        """Wait for brewer stop while retaining its latest metered water volume."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        latest_ml: float | None = None
        cup_baseline_g: float | None = None
        cup_delta_peak_g: float | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            dispensed = event.dispensed_water_ml
            if dispensed is not None:
                # Report 40523 is named WaterVolume by the app; it transports
                # cumulative millilitres as a float scaled by 1000. ``water_g``
                # is populated only as a deprecated compatibility alias.
                latest_ml = max(latest_ml or 0.0, float(dispensed))
            cup_weight = event.cup_weight_g
            if cup_weight is not None:
                value = float(cup_weight)
                cup_baseline_g = (
                    value if cup_baseline_g is None else min(cup_baseline_g, value)
                )
                delta = max(0.0, value - cup_baseline_g)
                cup_delta_peak_g = max(cup_delta_peak_g or 0.0, delta)
            if event.command_code != REPORT_BREWER_STOP:
                continue
            if latest_ml is None:
                raise XBloomError(
                    "brewer stopped but no metered water volume was observed"
                )
            tolerance = max(5.0, float(target_ml) * 0.05)
            if latest_ml < float(target_ml) - tolerance:
                raise XBloomError(
                    f"brewer stopped early at {latest_ml:.1f} ml; target was {target_ml:.1f} ml"
                )
            if latest_ml > float(target_ml) + (tolerance * 2):
                raise XBloomError(
                    f"brewer reported {latest_ml:.1f} ml; target was {target_ml:.1f} ml"
                )
            event.water_g = latest_ml
            event.dispensed_water_ml = latest_ml
            if cup_delta_peak_g is not None:
                event.report_values = {
                    **(event.report_values or {}),
                    "cup_delta_g": round(cup_delta_peak_g, 2),
                }
            return event

    # ------------------------------------------------------------------
    # Loading a coffee recipe (load-only — never starts a brew)
    # ------------------------------------------------------------------
    async def load_recipe(self, recipe: Recipe, *, settle: float = 2.0) -> StatusEvent:
        """Load ``recipe`` onto the machine and return once it is armed.

        Writes the LOAD frames to ``ffe1`` — ``a4`` (session start), a ``0x56``
        status handshake, then ``a6`` (dose/bypass), ``a8`` (fixed captured cup-
        geometry compatibility data), and the pours frame
        (``0x41``, or ``0x44`` for a no-grind recipe) — waiting for each ACK on
        ``ffe2``, and returns the ``StatusEvent`` once the machine reaches STATE
        ``0x1f`` (armed / loaded). **This never starts a brew** — the human
        approves on the machine.

        ``settle`` (seconds) is the pause after ``a4``+``0x56`` to let the machine
        leave its post-connect transitional state before staging. On a fresh
        connection the machine will not arm if the dose/temps/pours frames are sent
        immediately — it needs the handshake + this settle first (verified on
        hardware; this is the fix for the previous "loads never arm" issue).
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        recipe.validate()
        # frames == [a4, a6, a8, pours]; the pours opcode is chosen by build_load_frames.
        frames = build_load_frames(recipe.to_protocol_dict())
        a4, load_frames = frames[0], frames[1:]

        await self._start_notify()
        try:
            # The command characteristic (ffe1) accepts ONLY a Write Command (ATT
            # 0x52, write-without-response); ACKs and status arrive as ffe2
            # notifications, which accumulate in self._notif_queue and are read by
            # _drain_until_state below. We pace the writes with small fixed delays
            # rather than round-tripping each ACK: the machine needs the frames
            # spaced out, and consuming ACKs off the queue here would race the state
            # wait. (Verified on hardware — this is the fix for "loads never arm".)
            # 1. Session start + status handshake, then let the machine settle out of
            #    its transitional post-connect state before staging.
            log.info("→ a4 (session start) + 0x56 (handshake), then settle %.1fs", settle)
            await self._client.write_gatt_char(CHAR_COMMAND, a4, response=False)
            await asyncio.sleep(0.5)
            await self._client.write_gatt_char(CHAR_COMMAND, build_status_query(), response=False)
            await asyncio.sleep(settle)
            # 2. Dose/bypass, fixed cup geometry, pours — the pours frame arms it.
            for i, frame in enumerate(load_frames):
                log.info("→ load frame %d/%d (cmd=0x%02x)", i + 2, len(load_frames) + 1, frame[3])
                await self._client.write_gatt_char(CHAR_COMMAND, frame, response=False)
                await asyncio.sleep(0.4)
            armed = await self._drain_until_state(STATE_ARMED, self.ack_timeout)
            log.info("recipe loaded — machine armed (awaiting human approval)")
            return armed
        finally:
            await self._stop_notify()

    # ------------------------------------------------------------------
    # Starting / cancelling a brew  (explicit — dispenses hot water)
    # ------------------------------------------------------------------
    async def _drain_for_any(self, states: set[int], timeout: float) -> StatusEvent | None:
        """Return the first status event whose state is in ``states``, or ``None`` on
        timeout. Skips heartbeats; consumes intervening frames."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if event.is_heartbeat:
                continue
            log.debug("status: %s", event.state_name)
            if event.state in states:
                return event

    async def start(self, *, settle: float = 8.0) -> StatusEvent:
        """Start the currently-armed brew (call :meth:`load_recipe` first).

        Sends commit (``0x42``) and then **adapts to the machine**: after commit some
        machines auto-proceed straight through awaiting-confirm → grinding → brewing,
        while others sit in awaiting-confirm waiting for a start press. So we *watch*
        for up to ``settle`` seconds:

        * If the machine reaches **grinding (0x22)** or **brewing (0x3b)** on its own,
          the brew is underway — we do **not** send ``0x46`` (sending it into a running
          brew aborts it back to armed — verified on hardware).
        * Only if it **stalls in awaiting-confirm** do we send the ``0x46`` start frame
          to nudge it (this is what the vendor app's capture needed).

        The state-sensitive 40518 compatibility frame is sent only after a fresh
        ``awaiting_confirm`` report. A silent/unknown commit outcome fails closed.

        ⚠️ This physically dispenses near-boiling water. Only call it when the machine
        is ready (water/beans/cup in) and someone intends to brew.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        await self._start_notify()
        try:
            log.info("→ 0x42 commit (start the brew)")
            await self._client.write_gatt_char(CHAR_COMMAND, build_commit(), response=False)
            # After commit the machine either acts (auto-proceeds to grinding/brewing, or
            # refuses with no-water/no-beans), or reports awaiting-confirm. In ANY
            # "acted" case we must NOT send 40518 — the APK names it pause, and hardware
            # proves that sending it into a running brew aborts back to armed.
            acted = {STATE_STARTING, STATE_BREWING, STATE_NO_WATER, STATE_NO_BEANS}
            ev = await self._drain_for_any(acted | {STATE_AWAITING_CONFIRM}, settle)
            if ev is None:
                raise XBloomError(
                    "commit outcome is unconfirmed; refusing state-sensitive 40518 control"
                )
            if ev.state in acted:
                log.info("machine acted on commit (%s) — not sending 0x46", ev.state_name)
                return ev
            # A fresh awaiting-confirm state makes the hardware-derived start meaning
            # unambiguous for this instant; only then emit the APK's pause command.
            log.info("machine explicitly awaiting confirm — → state-sensitive 40518")
            await self._client.write_gatt_char(CHAR_COMMAND, build_start(), response=False)
            ev = await self._drain_for_any(acted, 5.0)
            if ev is not None:
                log.info("brew started (%s)", ev.state_name)
                return ev
            raise XBloomError("40518 was sent from awaiting-confirm, but start is unconfirmed")
        finally:
            await self._stop_notify()

    async def brew(self, recipe: Recipe, *, settle: float = 2.0) -> StatusEvent:
        """Load ``recipe`` and immediately start brewing (load + :meth:`start`).

        Convenience for the app-style "tap and brew" flow: it stages the recipe
        (arming the machine) and then sends commit + start. ⚠️ Same hot-water
        caveat as :meth:`start` — it brews for real.
        """
        await self.load_recipe(recipe, settle=settle)
        return await self.start()

    async def cancel_brew(self) -> None:
        """Abort a committed/running brew (``0x47`` cancel), returning toward idle."""
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        log.info("→ 0x47 cancel (aborting brew)")
        await self._client.write_gatt_char(CHAR_COMMAND, build_cancel(), response=False)

    async def request_status(self) -> None:
        """Ask the connected Studio to emit its current state and machine info."""
        await self.send_command(build_status_query())

    async def read_machine_info(self, *, timeout: float | None = None) -> dict[str, object]:
        """Request and return the decoded Studio settings/identity report 40521."""
        event = await self.send_command(
            build_status_query(),
            expect_command=MACHINE_INFO_COMMAND,
            timeout=self.ack_timeout if timeout is None else timeout,
        )
        if event is None or not event.machine_info:
            raise XBloomError("machine-info report 40521 had no decodable payload")
        return dict(event.machine_info)

    async def set_machine_settings(
        self,
        *,
        weight_unit: str | None = None,
        temperature_unit: str | None = None,
        water_source: str | None = None,
        display: str | None = None,
    ) -> dict[str, object]:
        """Persist selected Studio settings and return a 40521 readback.

        This is deliberately a thin, exact transport operation. CLI/Agent
        callers own authorization, idle-state checks, rollback, and comparison
        with the returned readback.
        """
        commands: list[tuple[int, bytes]] = []
        if weight_unit is not None:
            commands.append((CMD_SET_WEIGHT_UNIT, build_set_weight_unit(weight_unit)))
        if temperature_unit is not None:
            commands.append(
                (CMD_SET_TEMPERATURE_UNIT, build_set_temperature_unit(temperature_unit))
            )
        if water_source is not None:
            commands.append((CMD_SET_WATER_SOURCE, build_set_water_source(water_source)))
        if display is not None:
            commands.append((CMD_SET_DISPLAY, build_set_display(display)))
        if not commands:
            raise ValueError("at least one machine setting is required")
        for command, frame in commands:
            await self.send_command(frame, expect_command=command)
        return await self.read_machine_info()

    async def read_advanced_settings(self) -> dict[str, int]:
        """Read current pour-radius and vibration-amplitude values."""
        radius = await self.send_command(
            build_read_pour_radius(), expect_command=CMD_READ_POUR_RADIUS
        )
        vibration = await self.send_command(
            build_read_vibration_amplitude(),
            expect_command=CMD_READ_VIBRATION_AMPLITUDE,
        )
        if radius is None or radius.report_value is None:
            raise XBloomError("pour-radius report 11506 had no value")
        if vibration is None or vibration.report_value is None:
            raise XBloomError("vibration-amplitude report 11508 had no value")
        return {
            "pour_radius": int(radius.report_value),
            "vibration_amplitude": int(vibration.report_value),
        }

    async def write_advanced_settings(
        self,
        *,
        pour_radius: int | None = None,
        vibration_amplitude: int | None = None,
    ) -> dict[str, int]:
        """Persist selected mechanical tuning values, then read them back."""
        if pour_radius is None and vibration_amplitude is None:
            raise ValueError("at least one advanced setting is required")
        if pour_radius is not None:
            await self.send_command(
                build_write_pour_radius(pour_radius),
                expect_command=CMD_WRITE_POUR_RADIUS,
            )
        if vibration_amplitude is not None:
            await self.send_command(
                build_write_vibration_amplitude(vibration_amplitude),
                expect_command=CMD_WRITE_VIBRATION_AMPLITUDE,
            )
        return await self.read_advanced_settings()

    async def pause_coffee(self) -> StatusEvent:
        """Pause a running automatic coffee recipe (not a FreeSolo dispense)."""
        event = await self.send_command(
            build_coffee_pause(), expect_command=CMD_COFFEE_PAUSE
        )
        return _require_event(event, "coffee pause")

    async def resume_coffee(self) -> StatusEvent:
        """Resume a paused automatic coffee recipe."""
        event = await self.send_command(
            build_coffee_resume(), expect_command=CMD_COFFEE_RESUME
        )
        return _require_event(event, "coffee resume")

    # ------------------------------------------------------------------
    # FreeSolo tools: scale, grinder, and volume-limited water dispense
    # ------------------------------------------------------------------
    async def stream_scale(
        self,
        on_event: Callable[[StatusEvent], Awaitable[None] | None],
        *,
        duration: float = 30.0,
        tare: bool = False,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        """Enter standalone scale mode, stream gram readings, then always exit.

        On tested Studio firmware, the official ``8003`` scale-enter command
        automatically zeros whatever load is present at entry. ``tare=True``
        sends an *additional* explicit ``8500`` tare after entry; it cannot
        disable the firmware's entry auto-zero. To measure an object's absolute
        weight, enter with the platform empty, wait for ``on_ready``, then place
        the object. To measure net contents, enter with the empty vessel already
        present. The app's timer is local UI state, so callers that need a timer
        should use event timestamps.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        if not 0.1 <= float(duration) <= 3600:
            raise XBloomError("scale duration must be 0.1-3600 seconds")

        await self._start_notify()
        entered = False
        try:
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_scale_enter(), response=False
            )
            entered = True
            await asyncio.sleep(0.3)
            if tare:
                await self._client.write_gatt_char(
                    CHAR_COMMAND, build_scale_tare(), response=False
                )
                await asyncio.sleep(0.2)
            if on_ready is not None:
                result = on_ready()
                if asyncio.iscoroutine(result):
                    await result

            loop = asyncio.get_event_loop()
            deadline = loop.time() + float(duration)
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                try:
                    event = await asyncio.wait_for(
                        self._notif_queue.get(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    return
                if event.scale_g is None:
                    continue
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            if entered and self._client is not None and self._client.is_connected:
                try:
                    await self._client.write_gatt_char(
                        CHAR_COMMAND, build_scale_exit(), response=False
                    )
                except Exception as exc:  # pragma: no cover - best-effort safety cleanup
                    log.warning("failed to exit scale mode cleanly: %s", exc)
            await self._stop_notify()

    async def tare_scale(self) -> None:
        """Send the official explicit re-tare command in an active scale session.

        Firmware does not provide a dependable dedicated acknowledgement for
        this UI action. A successful serialized BLE write is therefore reported
        as command acceptance; the following scale readings are the observable
        result.
        """

        await self.send_command(build_scale_tare())

    async def grind(self, grind: int, rpm: int, *, seconds: float) -> StatusEvent:
        """Run the standalone grinder for at most 30 seconds, then stop and quit.

        The caller must enforce the official 60-second rest interval between
        sessions. Stop/quit frames are attempted from ``finally`` on cancellation
        or errors so a normal interrupted process does not leave the motor running.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        if not 1 <= int(grind) <= 80:
            raise XBloomError("grind must be 1-80")
        if not 60 <= int(rpm) <= 120:
            raise XBloomError("rpm must be 60-120")
        if not 0.1 <= float(seconds) <= 30.0:
            raise XBloomError("grinder runtime must be 0.1-30 seconds")

        entered = False
        started = False
        finished_runtime = False
        stop_ack: StatusEvent | None = None
        cleanup_error: Exception | None = None
        await self._start_notify()
        try:
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_grinder_enter(grind, rpm), response=False
            )
            entered = True
            await self._drain_for_command(8006, self.ack_timeout)
            await asyncio.sleep(0.3)
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_grinder_start(grind, rpm), response=False
            )
            started = True
            await self._drain_for_command(3500, self.ack_timeout)
            await asyncio.sleep(float(seconds))
            finished_runtime = True
        finally:
            if self._client is not None and self._client.is_connected:
                if started:
                    try:
                        await self._client.write_gatt_char(
                            CHAR_COMMAND, build_grinder_stop(), response=False
                        )
                        stop_ack = await self._drain_for_command(3505, self.ack_timeout)
                    except Exception as exc:  # pragma: no cover - safety cleanup
                        cleanup_error = exc
                        log.warning("failed to stop grinder cleanly: %s", exc)
                if entered:
                    try:
                        await asyncio.sleep(0.2)
                        await self._client.write_gatt_char(
                            CHAR_COMMAND, build_grinder_quit(), response=False
                        )
                    except Exception as exc:  # pragma: no cover - best-effort cleanup
                        log.warning("failed to quit grinder mode cleanly: %s", exc)
            await self._stop_notify()
            if finished_runtime and cleanup_error is not None:
                raise XBloomError(
                    f"grinder runtime elapsed but stop was not acknowledged: {cleanup_error}"
                ) from cleanup_error
        if stop_ack is None:  # pragma: no cover - normal path invariant
            raise XBloomError("grinder stop acknowledgement missing")
        return stop_ack

    async def start_grinder_session(self, grind: int, rpm: int) -> StatusEvent:
        """Enter and start FreeSolo grinding without owning its runtime timer.

        A long-lived bridge must call :meth:`stop_grinder_session` eventually.
        Public Agent workflows must retain the normal runtime and cooldown gates.
        """
        if not 1 <= int(grind) <= 80:
            raise XBloomError("grind must be 1-80")
        if not 60 <= int(rpm) <= 120:
            raise XBloomError("rpm must be 60-120")
        await self.send_command(
            build_grinder_enter(grind, rpm), expect_command=8006
        )
        await asyncio.sleep(0.3)
        event = await self.send_command(
            build_grinder_start(grind, rpm), expect_command=3500
        )
        return _require_event(event, "grinder start")

    async def pause_grinder(self) -> StatusEvent:
        event = await self.send_command(
            build_grinder_pause(), expect_command=CMD_GRINDER_PAUSE
        )
        return _require_event(event, "grinder pause")

    async def resume_grinder(self) -> StatusEvent:
        event = await self.send_command(
            build_grinder_resume(), expect_command=CMD_GRINDER_RESUME
        )
        return _require_event(event, "grinder resume")

    async def stop_grinder_session(self) -> StatusEvent:
        event: StatusEvent | None = None
        try:
            event = await self.send_command(build_grinder_stop(), expect_command=3505)
        finally:
            try:
                await asyncio.sleep(0.2)
                await self.send_command(build_grinder_quit())
            finally:
                await self._stop_notify()
        return _require_event(event, "grinder stop")

    async def dispense_water(
        self,
        volume_ml: float,
        temp_c: int,
        *,
        flow_ml_s: float = 3.5,
        pattern: str = "center",
        water_feed: int = 0,
        timeout: float | None = None,
    ) -> StatusEvent:
        """Dispense a firmware-limited volume of water in FreeSolo brewer mode.

        Completion is confirmed by the app's brewer-out/stop report. On timeout,
        interruption, or error, stop and quit frames are attempted before raising.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        if not 20 <= float(volume_ml) <= 360:
            raise XBloomError("water volume must be 20-360 ml")
        if int(temp_c) != ROOM_TEMPERATURE_C and not 40 <= int(temp_c) <= 98:
            raise XBloomError("water temperature must be RT or 40-98 C")
        flow10 = round(float(flow_ml_s) * 10)
        if flow10 not in range(30, 36) or abs(flow10 / 10 - float(flow_ml_s)) > 1e-6:
            raise XBloomError("water flow must be 3.0-3.5 ml/s in 0.1 steps")
        pattern = str(pattern).strip().lower()
        if pattern == "ring":
            pattern = "circular"
        if pattern not in {"center", "spiral", "circular"}:
            raise XBloomError("water pattern must be center, spiral, or circular")
        if int(water_feed) not in {0, 1}:
            raise XBloomError("water source must be 0 (tank) or 1 (tap)")
        wait_timeout = (
            float(timeout)
            if timeout is not None
            else min(360.0, float(volume_ml) / float(flow_ml_s) + 180.0)
        )
        if not 5 <= wait_timeout <= 600:
            raise XBloomError("water completion timeout must be 5-600 seconds")

        await self._start_notify()
        entered = False
        started = False
        completed = False
        try:
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_brewer_enter(temp_c, pattern), response=False
            )
            entered = True
            await asyncio.sleep(0.4)
            await self._client.write_gatt_char(
                CHAR_COMMAND,
                build_brewer_start(
                    volume_ml,
                    temp_c,
                    flow_ml_s,
                    pattern,
                    water_feed=int(water_feed),
                ),
                response=False,
            )
            started = True
            event = await self._drain_water_completion(float(volume_ml), wait_timeout)
            if event is None:
                raise XBloomError(
                    f"water dispense was not confirmed complete within {wait_timeout:.0f}s"
                )
            completed = True
            return event
        finally:
            if self._client is not None and self._client.is_connected:
                if started and not completed:
                    try:
                        await self._client.write_gatt_char(
                            CHAR_COMMAND, build_brewer_stop(), response=False
                        )
                    except Exception as exc:  # pragma: no cover - safety cleanup
                        log.warning("failed to stop water cleanly: %s", exc)
                if entered:
                    try:
                        await asyncio.sleep(0.2)
                        await self._client.write_gatt_char(
                            CHAR_COMMAND, build_brewer_quit(), response=False
                        )
                    except Exception as exc:  # pragma: no cover - best-effort cleanup
                        log.warning("failed to quit brewer mode cleanly: %s", exc)
            await self._stop_notify()

    async def start_water_session(
        self,
        volume_ml: float,
        temp_c: int,
        *,
        flow_ml_s: float = 3.5,
        pattern: str = "center",
        water_feed: int = 0,
    ) -> None:
        """Start bounded FreeSolo water while keeping the session interactive."""
        if not 20 <= float(volume_ml) <= 360:
            raise XBloomError("water volume must be 20-360 ml")
        if int(temp_c) != ROOM_TEMPERATURE_C and not 40 <= int(temp_c) <= 98:
            raise XBloomError("water temperature must be RT or 40-98 C")
        flow10 = round(float(flow_ml_s) * 10)
        if flow10 not in range(30, 36) or abs(flow10 / 10 - float(flow_ml_s)) > 1e-6:
            raise XBloomError("water flow must be 3.0-3.5 ml/s in 0.1 steps")
        pattern = str(pattern).strip().lower()
        if pattern == "ring":
            pattern = "circular"
        if pattern not in {"center", "spiral", "circular"}:
            raise XBloomError("water pattern must be center, spiral, or circular")
        if int(water_feed) not in {0, 1}:
            raise XBloomError("water source must be 0 (tank) or 1 (tap)")
        await self.send_command(build_brewer_enter(temp_c, pattern))
        await asyncio.sleep(0.4)
        await self.send_command(
            build_brewer_start(
                volume_ml,
                temp_c,
                flow_ml_s,
                pattern,
                water_feed=int(water_feed),
            )
        )

    async def pause_water(self) -> StatusEvent:
        event = await self.send_command(
            build_brewer_pause(), expect_command=CMD_BREWER_PAUSE
        )
        return _require_event(event, "water pause")

    async def resume_water(self) -> StatusEvent:
        event = await self.send_command(
            build_brewer_resume(), expect_command=CMD_BREWER_RESUME
        )
        return _require_event(event, "water resume")

    async def set_water_pattern(self, pattern: str) -> StatusEvent | None:
        """Set FreeSolo pattern and optionally observe its separate mode report.

        Firmware ``V12.0D.500`` applies command 8016 but does not echo 8016. The
        app defines 8107 as the corresponding machine report, so absence of that
        optional report is not a failed BLE write.
        """
        return await self.send_command(
            build_brewer_set_pattern(pattern),
            expect_command=BREWER_MODE_COMMAND,
            timeout=min(1.5, self.ack_timeout),
            response_optional=True,
        )

    async def set_water_temperature(self, temp_c: int) -> StatusEvent | None:
        """Set FreeSolo temperature and optionally observe report 8108."""
        return await self.send_command(
            build_brewer_set_temperature(temp_c),
            expect_command=BREWER_TEMPERATURE_COMMAND,
            timeout=min(1.5, self.ack_timeout),
            response_optional=True,
        )

    async def stop_water_session(self) -> StatusEvent:
        """Stop an interactive dispense and require the explicit 4507 echo.

        Report 40511 belongs to natural brewer completion. A controlled hardware
        stop on ``V12.0D.500`` instead echoes APP_BREWER_STOP (4507), then accepts
        APP_BREWER_QUIT (8013) and returns idle.
        """
        try:
            event = await self.send_command(
                build_brewer_stop(), expect_command=CMD_BREWER_STOP
            )
        finally:
            await self.quit_water_session()
        return _require_event(event, "water stop")

    async def quit_water_session(self) -> None:
        """Leave FreeSolo brewer mode after a natural or requested stop."""
        await asyncio.sleep(0.2)
        await self.send_command(build_brewer_quit())
        await self._stop_notify()

    # ------------------------------------------------------------------
    # Omni Tea Brewer: load and execute remain separate
    # ------------------------------------------------------------------
    async def load_tea_recipe(self, recipe: TeaRecipe, *, settle: float = 2.0) -> StatusEvent:
        """Upload a tea recipe and stop at the pre-start screen; never execute it."""
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        recipe.validate()
        frames = build_tea_load_frames(recipe.to_protocol_dict())
        if [frame_command(frame) for frame in frames] != [8104, CMD_TEA_RECIPE_CODE]:
            raise XBloomError("tea load frame invariant failed")

        await self._start_notify()
        try:
            # The phone app already has a live app session when TeaRecipeViewModel
            # uploads these frames. A one-shot CLI connection must reproduce that
            # handshake before cup/recipe setup, just as coffee loading does.
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_session_start(), response=False
            )
            await asyncio.sleep(0.5)
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_status_query(), response=False
            )
            await asyncio.sleep(settle)
            last: StatusEvent | None = None
            for frame in frames:
                command = frame_command(frame)
                await self._client.write_gatt_char(CHAR_COMMAND, frame, response=False)
                last = await self._drain_for_command(command, 30.0)
                await asyncio.sleep(0.3)
            return _require_event(last, "tea recipe load")
        finally:
            await self._stop_notify()

    async def start_tea(self) -> StatusEvent:
        """Execute a previously uploaded tea recipe (physically dispenses hot water)."""
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        await self._start_notify()
        try:
            frame = build_tea_start()
            await self._client.write_gatt_char(CHAR_COMMAND, frame, response=False)
            return await self._drain_for_command(CMD_TEA_RECIPE_MAKE, self.ack_timeout)
        finally:
            await self._stop_notify()

    async def unload_tea_recipe(self) -> None:
        """Exit the tea pre-start screen without executing the loaded recipe."""
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        await self._client.write_gatt_char(
            CHAR_COMMAND, build_recipe_start_quit(), response=False
        )

    async def save_slots(
        self,
        recipes: Sequence[Recipe] | Mapping[object, Recipe],
        *,
        scale: bool | Sequence[bool] = True,
        ensure_pro: bool = True,
        end_in_auto: bool = True,
    ) -> None:
        """Program the machine's three Easy-Mode preset slots (A, B, C) in one batch.

        ``recipes`` is either a sequence of **exactly three** :class:`Recipe`
        (slots A, B, C in order) or a mapping keyed by ``0/1/2`` or ``"A"/"B"/"C"``.
        The slots let you brew hands-free from the machine's dial later. **This
        never brews** — every frame is a ``0x2CF6`` slot write, never a brew-start
        opcode.

        ``scale`` toggles the on-brew scale in each stored preset: a single bool
        applies to all three, or pass a 3-element sequence for per-slot control.

        Why all three at once: the machine only *stores* the presets after it has
        received the whole A/B/C set (it then saves atomically — status
        ``0x43`` → ``0x25`` → idle). Writing a single slot leaves it hung and it
        shows **RETRY**, so this always writes the full trio; there is no commit
        frame.

        ⚠️ These presets live **on the machine**. Opening the xBloom app and
        reassigning a slot will push the app's own choices over BLE and overwrite
        what you set here — so program the slots when you intend to drive the
        machine from its dial, not the app.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        ordered = self._normalize_slots(recipes)
        scales = self._normalize_scale(scale)
        frames = []
        for i, recipe in enumerate(ordered):
            recipe.validate()
            frames.append(build_save_slot(recipe.to_protocol_dict(), i, scale=scales[i]))

        await self._start_notify()
        try:
            # 1. Open a session (a4), then force PRO mode. Slot writes are ONLY accepted in
            #    PRO mode: AUTO mode (the on-machine A/B/C selector) parks the machine in
            #    status 0x41 and rejects writes (RETRY); PRO mode drops it to 0x01 (idle),
            #    where saves land. Sending PRO is what makes the idle wait below reliable.
            await self._client.write_gatt_char(
                CHAR_COMMAND, build_session_start(), response=False
            )
            if ensure_pro:
                log.info("→ set PRO mode (slot writes require it)")
                await self._client.write_gatt_char(
                    CHAR_COMMAND, build_set_mode(pro=True), response=False
                )
            try:
                await self._drain_until_state(STATE_IDLE, self.ack_timeout)
            except XBloomError:
                log.warning("machine idle not confirmed; proceeding (is it in AUTO mode?)")
            await asyncio.sleep(1.0)

            # 2. Write all three slot frames back-to-back (NO commit). The machine
            #    acks each with a c2d204 notify; it stores the set once complete.
            for i, frame in enumerate(frames):
                log.info("→ save slot %s (scale=%s)", "ABC"[i], scales[i])
                await self._client.write_gatt_char(CHAR_COMMAND, frame, response=False)
                await asyncio.sleep(0.5)

            # 3. Confirm the save: the machine reports 0x25 (slots_saved). If it
            #    hangs at 0x43 (saving) and never reaches 0x25, the save failed.
            await self._drain_until_state(STATE_SLOTS_SAVED, self.ack_timeout)
            log.info("presets stored to slots A/B/C")

            # 4. Return the machine to AUTO mode so the freshly-written A/B/C presets are
            #    ready to pick on the dial (that's how they're brewed).
            if end_in_auto:
                log.info("→ back to AUTO mode (presets ready on the dial)")
                await self._client.write_gatt_char(
                    CHAR_COMMAND, build_set_mode(pro=False), response=False
                )
                await asyncio.sleep(0.3)
        finally:
            await self._stop_notify()

    @staticmethod
    def _normalize_slots(
        recipes: Sequence[Recipe] | Mapping[object, Recipe],
    ) -> list[Recipe]:
        """Return recipes as an ordered [A, B, C] list, requiring all three."""
        keymap = {0: 0, 1: 1, 2: 2, "a": 0, "b": 1, "c": 2, "A": 0, "B": 1, "C": 2}
        if isinstance(recipes, Mapping):
            out: list[Recipe | None] = [None, None, None]
            for key, recipe in recipes.items():
                idx = keymap.get(key if not isinstance(key, str) else key.lower())
                if idx is None:
                    raise XBloomError(f"unknown slot key {key!r} (use 0/1/2 or A/B/C)")
                out[idx] = recipe
            if any(r is None for r in out):
                raise XBloomError("save_slots needs all three slots (A, B and C)")
            return [r for r in out if r is not None]
        seq = list(recipes)
        if len(seq) != 3:
            raise XBloomError(f"save_slots needs exactly 3 recipes (A, B, C); got {len(seq)}")
        return seq

    @staticmethod
    def _normalize_scale(scale: bool | Sequence[bool]) -> list[bool]:
        """Expand ``scale`` to a per-slot [A, B, C] list of bools."""
        if isinstance(scale, bool):
            return [scale, scale, scale]
        vals = [bool(s) for s in scale]
        if len(vals) != 3:
            raise XBloomError(f"scale sequence must have 3 entries; got {len(vals)}")
        return vals

    # ------------------------------------------------------------------
    # Telemetry streaming
    # ------------------------------------------------------------------
    def _on_aux_notify(self, _sender, data: bytearray) -> None:
        """Log-only handler for the ``ffe3`` aux characteristic (capture/diagnostic).

        The live scale weights the app shows are NOT on ``ffe2`` (that carries only
        state + a pour counter). They may stream on ``ffe3`` — this taps it purely to
        capture the raw bytes at DEBUG so the format can be decoded. It never feeds the
        telemetry event stream and never affects the brew.
        """
        log.debug("←aux %s", bytes(data).hex())

    async def stream_telemetry(
        self,
        on_event: Callable[[StatusEvent], Awaitable[None] | None],
        duration: float = 300.0,
        *,
        stop_on_terminal: bool = True,
        capture_aux: bool = False,
    ) -> None:
        """Subscribe to ``ffe2`` and invoke ``on_event`` for each status event.

        Runs for up to ``duration`` seconds. If ``stop_on_terminal`` is set,
        returns early once a terminal state (complete / idle) is seen.
        ``on_event`` may be a plain or async callable.

        If ``capture_aux`` is set, ALSO subscribe to the ``ffe3`` aux characteristic
        and log its raw frames at DEBUG (diagnostic only — used with ``--debug`` to
        hunt for the live-scale weight stream). This is best-effort: if ``ffe3`` can't
        be subscribed it's logged and ignored, never breaking the brew.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        await self._start_notify()
        aux_on = False
        if capture_aux:
            try:
                await self._client.start_notify(CHAR_AUX, self._on_aux_notify)
                aux_on = True
                log.debug("aux capture on (ffe3) — hunting for the live-weight stream")
            except Exception as exc:  # noqa: BLE001 - diagnostic tap, never fatal
                log.debug("aux capture unavailable: %s", exc)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + duration
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.info("telemetry duration elapsed")
                    return
                try:
                    event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    log.info("telemetry duration elapsed")
                    return
                if event.is_heartbeat:
                    continue
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result
                if stop_on_terminal and event.is_terminal:
                    log.info("terminal state '%s' reached", event.state_name)
                    return
        finally:
            if aux_on:
                try:
                    await self._client.stop_notify(CHAR_AUX)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
            await self._stop_notify()
