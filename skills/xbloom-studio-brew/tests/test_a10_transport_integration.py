"""Phase A10 remaining gaps: real JSON-line transport + multi TypedBridgeClient.

Existing unit coverage in test_bridge.py / test_bridge_client.py is extensive
(core.rpc direct, wrong workflow id, duplicate idempotency, BLE drop, loaded
hold, unconfirmed control, terminal release, external busy). These tests do
**not** duplicate that matrix.

They cover the genuine remaining A10 integration rows:

1. Cross-client workflow handoff / client exit over loopback JSON-line
2. Concurrent start (same request_id cache; distinct request_ids single write)
3. Daemon process loss + reconstruction over transport (status/events observe
   only; recovery.reconcile connect+query; start without re-load)

No real BLE, no external daemon subprocess. BridgeServer + BridgeCore + fake
hardware + TypedBridgeClient (auto_ensure=False; test owns the server).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from xbloom_ble.bridge import (
    READY_SENTINEL,
    REMOTE_START_ENV,
    REMOTE_START_SENTINEL,
    BridgeCore,
    BridgeError,
    BridgeServer,
    bridge_record_path,
)
from xbloom_ble.bridge_client import TypedBridgeClient, new_request_id
from xbloom_ble.telemetry import StatusEvent


# ---------------------------------------------------------------------------
# Fake hardware + helpers
# ---------------------------------------------------------------------------


def _event(
    *,
    command: int | None = None,
    state: int | None = None,
    name: str = "ack",
    machine_info: dict | None = None,
) -> StatusEvent:
    return StatusEvent(
        state=state,
        state_name=name,
        raw=b"test",
        command_code=command,
        machine_info=machine_info,
    )


class FakeBridgeClient:
    """Minimal fake BLE client for transport integration tests.

    Supports configurable fresh machine status (armed/running) for recovery
    reconcile after daemon reconstruction — only this test fake is extended.
    """

    def __init__(self, address: str):
        self.address = address
        self.is_connected = False
        self.listeners: set = set()
        self.disconnect_listeners: set = set()
        self.calls: list = []
        self._expecting_disconnect = False
        # Configurable fresh status for recovery.reconcile / post-restart start.
        self.status_state: int | None = None
        self.status_state_name: str | None = None
        self.machine_info = {
            "firmware": "V12.0D.500",
            "water_source": "tank",
            "weight_unit": "g",
            "temperature_unit": "C",
            "display": "medium",
            "pouring_radius_init": 680,
            "vibration_init": 1000,
            "serial_number": "SECRET",
        }

    def add_event_listener(self, listener):
        self.listeners.add(listener)

    def remove_event_listener(self, listener):
        self.listeners.discard(listener)

    def add_disconnect_listener(self, listener):
        self.disconnect_listeners.add(listener)

    def remove_disconnect_listener(self, listener):
        self.disconnect_listeners.discard(listener)

    def mark_disconnect_expected(self):
        self._expecting_disconnect = True

    def emit(self, event: StatusEvent):
        for listener in tuple(self.listeners):
            listener(event)

    async def connect(self):
        self.is_connected = True
        self._expecting_disconnect = False
        self.calls.append("connect")

    async def disconnect(self):
        expected = bool(self._expecting_disconnect)
        self.is_connected = False
        self.calls.append("disconnect")
        if self.disconnect_listeners:
            for listener in tuple(self.disconnect_listeners):
                listener(expected)
        self._expecting_disconnect = False

    async def open_session(self):
        self.calls.append("open_session")

    async def close_session(self):
        self.calls.append("close_session")

    async def request_status(self):
        self.calls.append("request_status")
        if self.status_state is not None:
            self.emit(
                _event(
                    command=40521,
                    state=self.status_state,
                    name=self.status_state_name or "status",
                    machine_info=dict(self.machine_info),
                )
            )
        else:
            self.emit(
                _event(
                    command=40521,
                    name="machine_info",
                    machine_info=dict(self.machine_info),
                )
            )

    async def read_machine_info(self):
        self.calls.append("read_machine_info")
        return dict(self.machine_info)

    async def load_recipe(self, recipe):
        self.calls.append(("load_recipe", recipe.name))
        event = _event(state=0x1F, name="armed")
        self.emit(event)
        return event

    async def start(self):
        self.calls.append("coffee_start")
        event = _event(state=0x22, name="starting")
        self.emit(event)
        return event

    async def cancel_brew(self):
        self.calls.append("coffee_cancel")

    async def pause_coffee(self):
        self.calls.append("coffee_pause")
        return _event(command=40518)

    async def resume_coffee(self):
        self.calls.append("coffee_resume")
        return _event(command=40524)


def _recipe(path: Path) -> Path:
    path.write_text(
        """name: A10 transport test
dose_g: 16
grind: 55
pours:
  - {ml: 40, temp_c: 92, pattern: spiral, pause_s: 30, rpm: 100, flow_ml_s: 3.0}
  - {ml: 100, temp_c: 92, pattern: spiral, pause_s: 5, rpm: 100, flow_ml_s: 3.0}
  - {ml: 100, temp_c: 92, pattern: spiral, pause_s: 5, rpm: 100, flow_ml_s: 3.0}
""",
        encoding="utf-8",
    )
    return path


def _environ() -> dict[str, str]:
    return {REMOTE_START_ENV: REMOTE_START_SENTINEL}


async def _await_for(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Bounded async poll so the event loop can progress (BridgeServer.run)."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _drain_release(core: BridgeCore, *, timeout: float = 2.0) -> None:
    """Wait for a scheduled prompt BLE release to finish."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = core._release_task
        if not core.release_pending and (task is None or task.done()):
            if not core.connected:
                return
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            continue
        await asyncio.sleep(0.01)


def _typed(
    state_root: Path,
    *,
    client_name: str,
    address: str = "AA:BB",
) -> TypedBridgeClient:
    return TypedBridgeClient(
        address=address,
        state_root=state_root,
        client_name=client_name,
        client_version="a10-test",
        auto_ensure=False,
        default_timeout=10.0,
    )


def _count_load(fake: FakeBridgeClient) -> int:
    return sum(
        1 for c in fake.calls if isinstance(c, tuple) and c[0] == "load_recipe"
    )


# ---------------------------------------------------------------------------
# 1. Cross-client workflow handoff and client exit
# ---------------------------------------------------------------------------


def test_a10_cross_client_handoff_and_client_exit(tmp_path):
    """Skill load → client disappears; Web observes; MCP starts; natural terminal.

    Real loopback BridgeServer + three TypedBridgeClient instances (Skill/Web/MCP)
    sharing state_root. JSON-line sockets end per request; Skill object discard
    must not cancel/disconnect/mutate the durable workflow.
    """

    recipe = _recipe(tmp_path / "recipe.yaml")
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        environ=_environ(),
        machine_info_timeout=0.1,
    )
    record = bridge_record_path(tmp_path)
    server = BridgeServer(core, record_path=record, token="a10-handoff", acquire_lock=False)

    async def go():
        server_task = asyncio.create_task(server.run())
        try:
            assert await _await_for(record.exists)
            skill = _typed(tmp_path, client_name="xbloom-skill-cli")
            web = _typed(tmp_path, client_name="xbloom-web")
            mcp = _typed(tmp_path, client_name="xbloom-mcp")

            loaded = await asyncio.to_thread(
                skill.coffee_load,
                recipe=str(recipe),
                request_id=new_request_id("load"),
            )
            wid = loaded["workflow_id"]
            assert wid
            connects_after_load = fake.calls.count("connect")
            assert connects_after_load == 1
            assert _count_load(fake) == 1

            # Skill JSON-line request/socket already ended; discard the client
            # object. Durable workflow and BLE ownership must be unchanged.
            del skill
            mid_status = await asyncio.to_thread(web.status)
            assert mid_status["active_workflow_id"] == wid
            assert mid_status["connected"] is True
            assert mid_status["running"] is True
            assert mid_status["workflow"]["workflow_id"] == wid
            assert fake.calls.count("connect") == connects_after_load
            assert fake.calls.count("disconnect") == 0
            assert "coffee_start" not in fake.calls
            assert _count_load(fake) == 1

            # Web observation with exact workflow_id — zero BLE mutation.
            events = await asyncio.to_thread(
                web.events, since=0, workflow_id=wid
            )
            assert events.get("gap_detected") is False
            assert events.get("events")
            assert fake.calls.count("connect") == connects_after_load
            assert fake.calls.count("disconnect") == 0

            # MCP start with exact workflow_id + request_id.
            start_req = new_request_id("start")
            started = await asyncio.to_thread(
                mcp.coffee_start,
                workflow_id=wid,
                confirmation=READY_SENTINEL,
                request_id=start_req,
            )
            assert started["workflow_id"] == wid
            assert started["status"] == "running"
            # load→start: one connect, one load write, one start write.
            assert fake.calls.count("connect") == 1
            assert _count_load(fake) == 1
            assert fake.calls.count("coffee_start") == 1
            assert fake.calls.count("disconnect") == 0

            # Confirmed natural terminal → durable terminal before/with release.
            fake.emit(_event(state=0x24, name="ready"))
            await asyncio.sleep(0)

            def _has_terminal() -> bool:
                return any(
                    e.get("event_type") == "terminal"
                    for e in core.store.list_workflow_events(wid)
                )

            assert await _await_for(_has_terminal, timeout=2.0)
            # Durable terminal exists at or before prompt release.
            assert _has_terminal()
            await _drain_release(core)
            assert core.connected is False
            assert fake.calls.count("disconnect") == 1

            # Daemon remains alive; status/events observation must not reconnect.
            post = await asyncio.to_thread(web.status)
            assert post["running"] is True
            assert post["connected"] is False
            assert post["active_workflow_id"] is None
            connects_before_obs = fake.calls.count("connect")
            await asyncio.to_thread(web.events, since=0, workflow_id=wid)
            await asyncio.to_thread(mcp.status)
            assert fake.calls.count("connect") == connects_before_obs
            assert fake.calls.count("disconnect") == 1
        finally:
            server.shutdown_event.set()
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
            try:
                await core.shutdown(force=True)
            except Exception:
                try:
                    core.store.close()
                except Exception:
                    pass

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 2. Concurrent starts over transport
# ---------------------------------------------------------------------------


def test_a10_concurrent_start_same_request_id_single_write(tmp_path):
    """Two TypedBridgeClients issue the same start request_id concurrently.

    Exactly one physical coffee_start; both results match (cache / serial lock).
    """

    recipe = _recipe(tmp_path / "recipe.yaml")
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        environ=_environ(),
        machine_info_timeout=0.1,
    )
    record = bridge_record_path(tmp_path)
    server = BridgeServer(core, record_path=record, token="a10-conc-same", acquire_lock=False)
    wid_holder: dict[str, str] = {}

    async def go():
        server_task = asyncio.create_task(server.run())
        try:
            assert await _await_for(record.exists)
            client_a = _typed(tmp_path, client_name="xbloom-web")
            client_b = _typed(tmp_path, client_name="xbloom-mcp")
            loaded = await asyncio.to_thread(
                client_a.coffee_load,
                recipe=str(recipe),
                request_id=new_request_id("load"),
            )
            wid = loaded["workflow_id"]
            wid_holder["wid"] = wid
            start_req = new_request_id("start_dup")

            def start_a():
                return client_a.coffee_start(
                    workflow_id=wid,
                    confirmation=READY_SENTINEL,
                    request_id=start_req,
                )

            def start_b():
                return client_b.coffee_start(
                    workflow_id=wid,
                    confirmation=READY_SENTINEL,
                    request_id=start_req,
                )

            # True concurrent clients against the same server (thread pool).
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = loop.run_in_executor(pool, start_a)
                fut_b = loop.run_in_executor(pool, start_b)
                results = await asyncio.gather(fut_a, fut_b, return_exceptions=True)

            successes = [r for r in results if isinstance(r, dict)]
            errors = [r for r in results if isinstance(r, BaseException)]
            assert len(successes) == 2, f"expected both cached/success, got {results!r}"
            assert not errors
            assert successes[0]["status"] == successes[1]["status"]
            assert successes[0]["workflow_id"] == successes[1]["workflow_id"] == wid
            assert fake.calls.count("coffee_start") == 1

            # Clean terminalize.
            await asyncio.to_thread(
                client_a.cancel,
                workflow_id=wid,
                request_id=new_request_id("cancel"),
            )
            await _drain_release(core)
        finally:
            server.shutdown_event.set()
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
            try:
                if core.activity is not None or core.active_workflow_id:
                    try:
                        await core.rpc(
                            "cancel",
                            {
                                "workflow_id": core.active_workflow_id
                                or wid_holder.get("wid"),
                                "request_id": f"cleanup_{uuid4().hex}",
                                "emergency": True,
                            },
                        )
                    except Exception:
                        pass
                await core.shutdown(force=True)
            except Exception:
                try:
                    core.store.close()
                except Exception:
                    pass

    asyncio.run(go())


def test_a10_concurrent_start_distinct_request_ids_single_write(tmp_path):
    """Two concurrent distinct request_ids: exactly one start succeeds.

    The other is deterministically rejected without a second hardware start.
    Core safety (phase gate / single _op_lock write) is not weakened.
    """

    recipe = _recipe(tmp_path / "recipe.yaml")
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        environ=_environ(),
        machine_info_timeout=0.1,
    )
    record = bridge_record_path(tmp_path)
    server = BridgeServer(
        core, record_path=record, token="a10-conc-diff", acquire_lock=False
    )
    wid_holder: dict[str, str] = {}

    async def go():
        server_task = asyncio.create_task(server.run())
        try:
            assert await _await_for(record.exists)
            client_a = _typed(tmp_path, client_name="xbloom-skill-cli")
            client_b = _typed(tmp_path, client_name="xbloom-web")
            loaded = await asyncio.to_thread(
                client_a.coffee_load,
                recipe=str(recipe),
                request_id=new_request_id("load"),
            )
            wid = loaded["workflow_id"]
            wid_holder["wid"] = wid
            req_a = new_request_id("start_a")
            req_b = new_request_id("start_b")

            def start_a():
                return client_a.coffee_start(
                    workflow_id=wid,
                    confirmation=READY_SENTINEL,
                    request_id=req_a,
                )

            def start_b():
                return client_b.coffee_start(
                    workflow_id=wid,
                    confirmation=READY_SENTINEL,
                    request_id=req_b,
                )

            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = loop.run_in_executor(pool, start_a)
                fut_b = loop.run_in_executor(pool, start_b)
                results = await asyncio.gather(fut_a, fut_b, return_exceptions=True)

            successes = [r for r in results if isinstance(r, dict)]
            failures = [r for r in results if isinstance(r, BaseException)]
            assert len(successes) == 1, f"expected one success, got {results!r}"
            assert len(failures) == 1, f"expected one failure, got {results!r}"
            assert successes[0]["status"] == "running"
            assert successes[0]["workflow_id"] == wid
            assert isinstance(failures[0], BridgeError)
            # Deterministic phase gate: no second hardware start.
            assert fake.calls.count("coffee_start") == 1
            assert "no loaded coffee recipe" in str(failures[0]).casefold() or (
                "loaded" in str(failures[0]).casefold()
            )

            await asyncio.to_thread(
                client_a.cancel,
                workflow_id=wid,
                request_id=new_request_id("cancel"),
            )
            await _drain_release(core)
        finally:
            server.shutdown_event.set()
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
            try:
                if core.activity is not None or core.active_workflow_id:
                    try:
                        await core.rpc(
                            "cancel",
                            {
                                "workflow_id": core.active_workflow_id
                                or wid_holder.get("wid"),
                                "request_id": f"cleanup_{uuid4().hex}",
                                "emergency": True,
                            },
                        )
                    except Exception:
                        pass
                await core.shutdown(force=True)
            except Exception:
                try:
                    core.store.close()
                except Exception:
                    pass

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 3. Daemon restart / reconstruction over transport
# ---------------------------------------------------------------------------


def test_a10_daemon_reconstruction_over_transport(tmp_path):
    """Process loss without terminal → fresh server reconstructs durable workflow.

    status/events observe only (no connect/load/start). recovery.reconcile does
    one connect+query. Explicit start runs once without re-load; terminal releases.
    """

    recipe = _recipe(tmp_path / "recipe.yaml")
    fake1 = FakeBridgeClient("AA:BB")
    core1 = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake1,
        environ=_environ(),
        machine_info_timeout=0.1,
    )
    record = bridge_record_path(tmp_path)
    server1 = BridgeServer(
        core1, record_path=record, token="a10-recon-1", acquire_lock=False
    )

    async def go():
        task1 = asyncio.create_task(server1.run())
        wid: str | None = None
        try:
            assert await _await_for(record.exists)
            skill = _typed(tmp_path, client_name="xbloom-skill-cli")
            loaded = await asyncio.to_thread(
                skill.coffee_load,
                recipe=str(recipe),
                request_id=new_request_id("load"),
            )
            wid = loaded["workflow_id"]
            assert core1.connected is True
            assert core1.active_workflow_id == wid
            assert fake1.calls.count("connect") == 1
            assert _count_load(fake1) == 1

            # Simulate daemon process loss without terminalizing/cancelling.
            # Process-local tasks/socket die; durable workflow stays active.
            # Close first store without writing a false terminal.
            server1.shutdown_event.set()
            await asyncio.wait_for(task1, timeout=5.0)
            core1.store.close()
            # Abandon process-local BLE/tasks (no core.shutdown → no cancel).
            # Server exit cleans its own bridge.json so a fresh daemon can republish.
            assert not record.exists()

            # Fresh machine + core + server on same state root.
            fake2 = FakeBridgeClient("AA:BB")
            fake2.status_state = 0x1F
            fake2.status_state_name = "armed"
            core2 = BridgeCore(
                default_address="AA:BB",
                state_dir=tmp_path,
                client_factory=lambda _a: fake2,
                environ=_environ(),
                machine_info_timeout=0.1,
            )
            assert core2.active_workflow_id == wid
            assert core2.activity == "coffee"
            assert core2.phase == "loaded"
            assert core2.connected is False
            assert fake2.calls == []

            server2 = BridgeServer(
                core2, record_path=record, token="a10-recon-2", acquire_lock=False
            )
            task2 = asyncio.create_task(server2.run())
            try:
                assert await _await_for(record.exists)
                web = _typed(tmp_path, client_name="xbloom-web")
                mcp = _typed(tmp_path, client_name="xbloom-mcp")

                # status/events after restart: same workflow, no connect/reload/start.
                status = await asyncio.to_thread(web.status)
                assert status["active_workflow_id"] == wid
                assert status["workflow"]["workflow_id"] == wid
                assert status["connected"] is False
                assert status["running"] is True
                events = await asyncio.to_thread(
                    web.events, since=0, workflow_id=wid
                )
                assert events.get("events") is not None
                assert fake2.calls == []
                assert "coffee_start" not in fake2.calls
                assert _count_load(fake2) == 0

                # Explicit recovery.reconcile: one fresh connect+query, no load/start.
                recon = await asyncio.to_thread(
                    mcp.recovery_reconcile, workflow_id=wid
                )
                assert recon.get("reconciled") is True or recon.get(
                    "reconcile_outcome"
                ) in {"loaded_armed", "armed", "loaded"}
                assert fake2.calls.count("connect") == 1
                assert "request_status" in fake2.calls
                assert _count_load(fake2) == 0
                assert "coffee_start" not in fake2.calls
                assert core2.connected is True

                # Explicit start once, without re-load.
                started = await asyncio.to_thread(
                    mcp.coffee_start,
                    workflow_id=wid,
                    confirmation=READY_SENTINEL,
                    request_id=new_request_id("start"),
                )
                assert started["status"] == "running"
                assert started["workflow_id"] == wid
                assert fake2.calls.count("coffee_start") == 1
                assert _count_load(fake2) == 0
                # No second connect after reconcile held the link.
                assert fake2.calls.count("connect") == 1

                # Finish terminal and assert release.
                fake2.emit(_event(state=0x24, name="ready"))
                await asyncio.sleep(0)
                assert await _await_for(
                    lambda: any(
                        e.get("event_type") == "terminal"
                        for e in core2.store.list_workflow_events(wid)
                    ),
                    timeout=2.0,
                )
                await _drain_release(core2)
                assert core2.connected is False
                assert fake2.calls.count("disconnect") == 1
                post = await asyncio.to_thread(web.status)
                assert post["running"] is True
                assert post["connected"] is False
                assert post["active_workflow_id"] is None
            finally:
                server2.shutdown_event.set()
                try:
                    await asyncio.wait_for(task2, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task2.cancel()
                    try:
                        await task2
                    except asyncio.CancelledError:
                        pass
                try:
                    await core2.shutdown(force=True)
                except Exception:
                    try:
                        core2.store.close()
                    except Exception:
                        pass
        finally:
            if not task1.done():
                server1.shutdown_event.set()
                try:
                    await asyncio.wait_for(task1, timeout=2.0)
                except Exception:
                    task1.cancel()
                    try:
                        await task1
                    except asyncio.CancelledError:
                        pass
            # core1 store already closed on the happy path; best-effort.
            try:
                core1.store.close()
            except Exception:
                pass

    asyncio.run(go())
