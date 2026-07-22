"""Phase 0 daemon lock, hello handshake, and core-owned lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from xbloom_ble.bridge import (
    BRIDGE_PROTOCOL_VERSION,
    BRIDGE_RECORD_FORMAT_VERSION,
    BridgeCompatibilityError,
    BridgeCore,
    BridgeError,
    BridgeLock,
    BridgeServer,
    RPC_PROTOCOL_CURRENT,
    RPC_PROTOCOL_MAX,
    RPC_PROTOCOL_MIN,
    _atomic_json,
    _probe_record_responsive,
    bridge_call,
    bridge_is_running,
    bridge_record_path,
    config_fingerprint,
    ensure_bridge_daemon,
    require_protocol_range,
    restart_bridge_daemon_if_idle,
    start_bridge_daemon,
    stop_bridge_daemon,
)


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Bounded poll for sync tests (must not be called from a running event loop)."""

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


async def _await_for(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Bounded async poll so the event loop can progress (e.g. BridgeServer.run)."""

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def test_bridge_lock_contention(tmp_path):
    first = BridgeLock(tmp_path)
    second = BridgeLock(tmp_path)
    assert first.acquire(blocking=False) is True
    assert second.acquire(blocking=False) is False
    first.release()
    assert second.acquire(blocking=False) is True
    second.release()


def test_bridge_lock_held_across_threads(tmp_path):
    lock = BridgeLock(tmp_path)
    assert lock.acquire(blocking=False)
    results: list[bool] = []

    def contender() -> None:
        other = BridgeLock(tmp_path)
        results.append(other.acquire(blocking=False))
        if other.owned:
            other.release()

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(timeout=5)
    assert results == [False]
    lock.release()


def test_stale_record_cleaned_when_lock_available(tmp_path):
    record = tmp_path / "bridge.json"
    record.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 1,
                "token": "stale-token",
                "instance_id": "stale",
                "pid": 1,
            }
        ),
        encoding="utf-8",
    )
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="fresh-token")

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(
            lambda: record.exists()
            and "fresh-token" in record.read_text(encoding="utf-8")
        )
        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["token"] == "fresh-token"
        assert data["instance_id"] == core.instance_id
        assert "core_version" in data
        assert "rpc_protocol_min" in data
        assert "rpc_protocol_max" in data
        assert "rpc_protocol_current" in data
        assert "config_fingerprint" in data
        assert data["host"] == "127.0.0.1"
        result = await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        assert result["status"] == "shutting_down"
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())
    assert not record.exists()


def test_hello_compatible_and_incompatible(tmp_path):
    record = tmp_path / "bridge.json"
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="tok")

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(record.exists)
        hello = await asyncio.to_thread(
            bridge_call,
            "hello",
            {
                "client_name": "test-client",
                "client_version": "1.0.0",
                "protocol_min": RPC_PROTOCOL_MIN,
                "protocol_max": RPC_PROTOCOL_MAX,
            },
            record_path=record,
            require_hello=False,
            timeout=2.0,
        )
        assert hello["compatibility"]["compatible"] is True
        assert "token" not in hello
        assert hello["instance_id"] == core.instance_id

        bad = await asyncio.to_thread(
            bridge_call,
            "hello",
            {
                "client_name": "old-client",
                "client_version": "0.0.1",
                "protocol_min": 99,
                "protocol_max": 100,
            },
            record_path=record,
            require_hello=False,
            protocol_min=99,
            protocol_max=100,
            timeout=2.0,
        )
        assert bad["compatibility"]["compatible"] is False
        assert bad["compatibility"]["protocol_ok"] is False
        assert "token" not in bad

        # Diagnostics still work for incompatible clients.
        status = await asyncio.to_thread(
            bridge_call,
            "status",
            {
                "protocol_min": 99,
                "protocol_max": 100,
            },
            record_path=record,
            require_hello=False,
            protocol_min=99,
            protocol_max=100,
            timeout=2.0,
        )
        assert status["running"] is True
        assert status["compatibility"]["compatible"] is False
        assert "token" not in status

        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())


def test_pre_dispatch_rejects_incompatible_before_rpc(tmp_path, monkeypatch):
    record = tmp_path / "bridge.json"
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="tok")
    called: list[str] = []
    original_rpc = core.rpc

    async def wrapped(method, params=None):
        called.append(method)
        return await original_rpc(method, params)

    monkeypatch.setattr(core, "rpc", wrapped)

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(record.exists)
        with pytest.raises(BridgeCompatibilityError):
            await asyncio.to_thread(
                bridge_call,
                "connect",
                {},
                record_path=record,
                protocol_min=50,
                protocol_max=60,
                require_hello=False,
                timeout=2.0,
            )
        assert called == []
        # Compatible path reaches rpc.
        await asyncio.to_thread(
            bridge_call,
            "ping",
            record_path=record,
            require_hello=False,
            timeout=2.0,
        )
        assert "ping" in called or "status" in called
        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())


def test_core_owned_start_stop_uses_tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    # Ensure no accidental home state use.
    monkeypatch.delenv("XBLOOM_SKILL_STATE_DIR", raising=False)

    started = start_bridge_daemon(state_root=tmp_path, start_timeout=15.0)
    assert started.get("running") is True
    assert started.get("instance_id")
    record = bridge_record_path(tmp_path)
    assert record.is_file()
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["host"] == "127.0.0.1"
    assert "token" in data
    assert data["instance_id"] == started["instance_id"]

    # Second start reuses the same instance.
    again = ensure_bridge_daemon(state_root=tmp_path)
    assert again.get("instance_id") == started["instance_id"]
    assert again.get("started") is False

    stopped = stop_bridge_daemon(state_root=tmp_path)
    assert stopped.get("running") is False
    assert _wait_for(lambda: not bridge_is_running(record_path=record), timeout=10.0)


def test_concurrent_starters_single_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def starter() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                start_bridge_daemon(state_root=tmp_path, start_timeout=20.0)
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=starter) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    assert not errors, errors
    assert len(results) == 2
    ids = {r.get("instance_id") for r in results}
    assert len(ids) == 1
    assert bridge_is_running(record_path=bridge_record_path(tmp_path))
    stop_bridge_daemon(state_root=tmp_path)


def test_restart_if_idle_refuses_when_recovery_present(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    first = start_bridge_daemon(state_root=tmp_path, start_timeout=15.0)
    first_id = first["instance_id"]
    record = bridge_record_path(tmp_path)
    # Plant recovery while daemon is running; status must re-read from disk.
    (tmp_path / "armed-state.json").write_text("{}", encoding="utf-8")
    status = bridge_call("status", record_path=record, require_hello=False)
    assert "armed-state.json" in (status.get("recovery_records") or [])
    assert status.get("idle") is False

    outcome = restart_bridge_daemon_if_idle(state_root=tmp_path)
    # Must refuse unequivocally — never report a successful restart.
    assert outcome.get("restarted") is False
    assert outcome.get("status") == "upgrade_pending"
    assert outcome.get("instance_id") == first_id
    # Same instance must still be running (no stop/start).
    assert bridge_is_running(record_path=record)
    still = bridge_call("status", record_path=record, require_hello=False)
    assert still.get("instance_id") == first_id

    (tmp_path / "armed-state.json").unlink(missing_ok=True)
    stop_bridge_daemon(state_root=tmp_path, force=True)


def test_restart_if_idle_when_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    first = start_bridge_daemon(state_root=tmp_path, start_timeout=15.0)
    first_id = first["instance_id"]
    outcome = restart_bridge_daemon_if_idle(state_root=tmp_path)
    assert outcome.get("restarted") is True
    assert outcome.get("instance_id") != first_id or outcome.get(
        "previous_instance_id"
    ) == first_id
    assert outcome.get("running") is True
    stop_bridge_daemon(state_root=tmp_path)


def test_restart_if_idle_rejects_force(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    start_bridge_daemon(state_root=tmp_path, start_timeout=15.0)
    outcome = restart_bridge_daemon_if_idle(state_root=tmp_path, force=True)
    assert outcome.get("restarted") is False
    assert outcome.get("status") == "force_rejected"
    stop_bridge_daemon(state_root=tmp_path)


def test_start_bridge_daemon_compat_signature_ignores_script_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    # Old callers passed Path(__file__); must still work without using it.
    fake_script = tmp_path / "not-a-real-skill" / "xbloom.py"
    result = start_bridge_daemon(fake_script, state_root=tmp_path, start_timeout=15.0)
    assert result.get("running") is True
    stop_bridge_daemon(state_root=tmp_path)


def test_protocol_version_is_v3():
    # v3: mutating RPCs require request_id; workflow-bound control requires workflow_id.
    assert BRIDGE_PROTOCOL_VERSION == 3
    assert RPC_PROTOCOL_MIN == 3
    assert RPC_PROTOCOL_MAX == 3
    assert RPC_PROTOCOL_CURRENT == 3
    assert BRIDGE_RECORD_FORMAT_VERSION == 2


def test_hello_requires_declared_fields(tmp_path):
    record = tmp_path / "bridge.json"
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="tok")

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(record.exists)
        with pytest.raises(BridgeError, match="requires declared fields|client_name"):
            await asyncio.to_thread(
                bridge_call,
                "hello",
                {"protocol_min": 2, "protocol_max": 2},
                record_path=record,
                require_hello=False,
                timeout=2.0,
            )
        with pytest.raises(BridgeError, match="protocol_min|protocol_max|integers"):
            await asyncio.to_thread(
                bridge_call,
                "hello",
                {
                    "client_name": "c",
                    "client_version": "1",
                    "protocol_min": "nope",
                    "protocol_max": 2,
                },
                record_path=record,
                require_hello=False,
                timeout=2.0,
            )
        # Diagnostics remain available without a successful hello.
        ping = await asyncio.to_thread(
            bridge_call,
            "ping",
            record_path=record,
            require_hello=False,
            timeout=2.0,
        )
        assert ping.get("ok") is True or ping.get("pong") is True or "status" in ping or ping
        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())


def test_require_protocol_range_rejects_non_json_integers():
    """Floats, bools, numeric strings, missing, reversed, nonpositive — all hard fails."""

    with pytest.raises(BridgeError, match="JSON integers|integers"):
        require_protocol_range(2.9, 2)
    with pytest.raises(BridgeError, match="JSON integers|integers"):
        require_protocol_range(2, 2.0)
    with pytest.raises(BridgeError, match="JSON integers|integers"):
        require_protocol_range(True, 2)
    with pytest.raises(BridgeError, match="JSON integers|integers"):
        require_protocol_range(2, False)
    with pytest.raises(BridgeError, match="JSON integers|integers"):
        require_protocol_range("2", 2)
    with pytest.raises(BridgeError, match="required|integers"):
        require_protocol_range(None, 2)
    with pytest.raises(BridgeError, match="required|integers"):
        require_protocol_range(2, None)
    with pytest.raises(BridgeError, match="must be <=|protocol_min"):
        require_protocol_range(5, 2)
    with pytest.raises(BridgeError, match=">= 1"):
        require_protocol_range(0, 2)
    with pytest.raises(BridgeError, match=">= 1"):
        require_protocol_range(-1, 2)
    assert require_protocol_range(2, 2) == (2, 2)


def test_strict_protocol_types_rejected_before_rpc_dispatch(tmp_path, monkeypatch):
    """Envelope/hello coercion holes must not reach BridgeCore.rpc or BLE."""

    record = tmp_path / "bridge.json"
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="tok")
    called: list[str] = []
    original_rpc = core.rpc

    async def wrapped(method, params=None):
        called.append(method)
        return await original_rpc(method, params)

    monkeypatch.setattr(core, "rpc", wrapped)

    def _raw(params_override: dict, *, method: str = "connect") -> dict:
        data = json.loads(record.read_text(encoding="utf-8"))
        request = {
            "id": "t1",
            "token": data["token"],
            "method": method,
            "params": params_override if method == "hello" else {},
        }
        if method != "hello":
            request.update(params_override)
        import socket as _socket

        with _socket.create_connection(
            (data["host"], int(data["port"])), timeout=2.0
        ) as conn:
            conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.extend(chunk)
        return json.loads(chunks.decode("utf-8").split("\n", 1)[0])

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(record.exists)

        cases = [
            {"protocol_min": 2.9, "protocol_max": 2},
            {"protocol_min": True, "protocol_max": 2},
            {"protocol_min": "2", "protocol_max": 2},
            {"protocol_min": 5, "protocol_max": 2},
            {"protocol_min": 0, "protocol_max": 2},
            {"protocol_max": 2},  # missing min
            {"protocol_min": 2},  # missing max
        ]
        for envelope in cases:
            # Worker thread so BridgeServer._handle can progress on this loop.
            resp = await asyncio.to_thread(_raw, envelope, method="connect")
            assert resp.get("ok") is False, envelope
            err = str(resp.get("error") or "")
            assert any(
                token in err.lower()
                for token in ("integer", "protocol", "required", ">=")
            ), (envelope, err)
            assert "connect" not in called

        # Hello path must also reject float truncation (2.9 != protocol 2).
        for bad in (
            {
                "client_name": "c",
                "client_version": "1",
                "protocol_min": 2.9,
                "protocol_max": 2,
            },
            {
                "client_name": "c",
                "client_version": "1",
                "protocol_min": True,
                "protocol_max": 2,
            },
            {
                "client_name": "c",
                "client_version": "1",
                "protocol_min": "2",
                "protocol_max": 2,
            },
            {
                "client_name": "c",
                "client_version": "1",
                "protocol_min": 3,
                "protocol_max": 2,
            },
            {
                "client_name": "c",
                "client_version": "1",
                "protocol_min": 0,
                "protocol_max": 2,
            },
        ):
            resp = await asyncio.to_thread(_raw, bad, method="hello")
            assert resp.get("ok") is False, bad
            assert "hello" not in called

        # Public client helper also rejects before any socket write of coerced ints.
        with pytest.raises(BridgeError, match="JSON integers|integers"):
            await asyncio.to_thread(
                bridge_call,
                "ping",
                record_path=record,
                protocol_min=2.9,  # type: ignore[arg-type]
                protocol_max=2,
                require_hello=False,
                timeout=2.0,
            )

        assert called == []
        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())


def test_legacy_client_missing_envelope_rejected_on_new_server(tmp_path):
    record = tmp_path / "bridge.json"
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="tok")

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(record.exists)
        with pytest.raises(BridgeCompatibilityError, match="protocol_min|incompatible"):
            await asyncio.to_thread(
                bridge_call,
                "connect",
                {},
                record_path=record,
                require_hello=False,
                omit_protocol_envelope=True,
                timeout=2.0,
            )
        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())


def test_new_client_against_legacy_record_server(tmp_path):
    """Simulate a lockless v1-style server (status has protocol 1, no hello)."""

    record = tmp_path / "bridge.json"
    token = "legacy-tok"

    async def legacy_handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(line.decode("utf-8"))
            method = request.get("method")
            supplied = str(request.get("token") or "")
            if supplied != token:
                response = {"id": request.get("id"), "ok": False, "error": "auth"}
            elif method == "ping":
                response = {"id": request.get("id"), "ok": True, "result": {"pong": True}}
            elif method == "status":
                response = {
                    "id": request.get("id"),
                    "ok": True,
                    "result": {
                        "running": True,
                        "protocol_version": 1,
                        "rpc_protocol_min": 1,
                        "rpc_protocol_max": 1,
                        "rpc_protocol_current": 1,
                        "instance_id": "legacy_brg",
                        "idle": True,
                        "activity": None,
                        "phase": "disconnected",
                        "recovery_records": [],
                        "config_fingerprint": "legacyfp",
                    },
                }
            elif method == "hello":
                # Legacy never had proper hello; reject or ignore.
                response = {
                    "id": request.get("id"),
                    "ok": False,
                    "error": "unknown method hello",
                }
            elif method == "shutdown":
                response = {
                    "id": request.get("id"),
                    "ok": True,
                    "result": {"status": "shutting_down"},
                }
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                server.close()
                return
            elif method == "connect":
                response = {
                    "id": request.get("id"),
                    "ok": False,
                    "error": "legacy server",
                }
            else:
                response = {
                    "id": request.get("id"),
                    "ok": False,
                    "error": f"unknown {method}",
                }
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def go():
        nonlocal server
        server = await asyncio.start_server(legacy_handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        record.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "token": token,
                    "instance_id": "legacy_brg",
                    "protocol_version": 1,
                }
            ),
            encoding="utf-8",
        )
        # New client hello/compatibility fails against legacy protocol range.
        status = await asyncio.to_thread(
            bridge_call,
            "status",
            record_path=record,
            require_hello=False,
            timeout=2.0,
        )
        assert status.get("rpc_protocol_current") == 1 or status.get(
            "protocol_version"
        ) == 1
        with pytest.raises((BridgeCompatibilityError, BridgeError)):
            await asyncio.to_thread(
                bridge_call,
                "connect",
                {},
                record_path=record,
                protocol_min=RPC_PROTOCOL_MIN,
                protocol_max=RPC_PROTOCOL_MAX,
                require_hello=True,
                timeout=2.0,
            )
        # Idle upgrade path: ensure_bridge_daemon should shut down legacy and start v2.
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
        try:
            result = await asyncio.to_thread(
                ensure_bridge_daemon,
                state_root=tmp_path,
                start_timeout=15.0,
            )
            assert result.get("upgraded_from_legacy") is True or result.get(
                "rpc_protocol_current"
            ) == RPC_PROTOCOL_CURRENT
            assert result.get("running") is True
            assert int(result.get("rpc_protocol_current") or 0) == RPC_PROTOCOL_CURRENT
        finally:
            monkeypatch.undo()
            if bridge_is_running(record_path=record):
                stop_bridge_daemon(state_root=tmp_path)
            if server.is_serving():
                server.close()
                await server.wait_closed()

    server = None  # type: ignore[assignment]
    asyncio.run(go())


def test_idle_legacy_upgrade_and_active_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    record = bridge_record_path(tmp_path)
    token = "leg-tok"

    async def make_legacy(*, idle: bool, recovery: list[str]):
        shutdown_flag = asyncio.Event()

        async def handle(reader, writer):
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                request = json.loads(line.decode("utf-8"))
                method = request.get("method")
                if str(request.get("token") or "") != token:
                    resp = {"id": request.get("id"), "ok": False, "error": "auth"}
                elif method == "ping":
                    resp = {"id": request.get("id"), "ok": True, "result": {"pong": True}}
                elif method == "status":
                    resp = {
                        "id": request.get("id"),
                        "ok": True,
                        "result": {
                            "running": True,
                            "protocol_version": 1,
                            "rpc_protocol_min": 1,
                            "rpc_protocol_max": 1,
                            "rpc_protocol_current": 1,
                            "instance_id": "legacy_active" if not idle else "legacy_idle",
                            "idle": idle and not recovery,
                            "activity": None if idle else "coffee",
                            "phase": "disconnected" if idle else "running",
                            "recovery_records": recovery,
                            "config_fingerprint": "x",
                        },
                    }
                elif method == "shutdown":
                    resp = {
                        "id": request.get("id"),
                        "ok": True,
                        "result": {"status": "shutting_down"},
                    }
                    writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    shutdown_flag.set()
                    srv.close()
                    return
                else:
                    resp = {
                        "id": request.get("id"),
                        "ok": False,
                        "error": f"no {method}",
                    }
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        record.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "token": token,
                    "instance_id": "legacy_idle" if idle else "legacy_active",
                    "protocol_version": 1,
                }
            ),
            encoding="utf-8",
        )
        return srv, shutdown_flag

    async def go():
        # Active refusal
        srv, _ = await make_legacy(idle=False, recovery=[])
        try:
            outcome = await asyncio.to_thread(
                ensure_bridge_daemon, state_root=tmp_path, start_timeout=5.0
            )
            assert outcome.get("upgrade_pending") is True or outcome.get(
                "status"
            ) == "upgrade_pending"
            assert outcome.get("started") is False
            assert outcome.get("legacy_daemon") is True
        finally:
            srv.close()
            await srv.wait_closed()
            try:
                record.unlink()
            except FileNotFoundError:
                pass

        # Idle upgrade
        srv, flag = await make_legacy(idle=True, recovery=[])
        try:
            outcome = await asyncio.to_thread(
                ensure_bridge_daemon, state_root=tmp_path, start_timeout=15.0
            )
            assert await _await_for(flag.is_set, timeout=10.0)
            assert (
                outcome.get("upgraded_from_legacy") is True
                or int(outcome.get("rpc_protocol_current") or 0)
                == RPC_PROTOCOL_CURRENT
            ), outcome
            assert bridge_is_running(record_path=record)
            assert int(
                bridge_call(
                    "status", record_path=record, require_hello=False
                ).get("rpc_protocol_current")
                or 0
            ) == RPC_PROTOCOL_CURRENT
        finally:
            if bridge_is_running(record_path=record):
                stop_bridge_daemon(state_root=tmp_path)
            if srv.is_serving():
                srv.close()
                await srv.wait_closed()

    asyncio.run(go())


def test_lock_released_when_start_server_fails(tmp_path, monkeypatch):
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, token="tok")

    async def boom(*_a, **_k):
        raise OSError("injected bind failure")

    monkeypatch.setattr(asyncio, "start_server", boom)

    async def go():
        with pytest.raises(OSError, match="injected bind failure"):
            await server.run()
        # Lock must be free for a second acquisition.
        second = BridgeLock(tmp_path)
        assert second.acquire(blocking=False) is True
        second.release()

    asyncio.run(go())


def test_lock_released_when_record_publication_fails(tmp_path, monkeypatch):
    """Inject _atomic_json failure after bind; server closes and releases owned lock."""

    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, token="pub-tok")
    foreign_record = tmp_path / "foreign-bridge.json"
    foreign_payload = {
        "host": "127.0.0.1",
        "port": 9,
        "token": "other-token",
        "instance_id": "other-instance",
    }
    foreign_record.write_text(json.dumps(foreign_payload), encoding="utf-8")
    real_atomic = _atomic_json
    calls: list[Path] = []

    def failing_atomic(path, data, *, private=False):
        calls.append(Path(path))
        if Path(path) == server.record_path:
            raise OSError("injected record publication failure")
        return real_atomic(path, data, private=private)

    monkeypatch.setattr("xbloom_ble.bridge._atomic_json", failing_atomic)

    async def go():
        with pytest.raises(OSError, match="injected record publication failure"):
            await server.run()
        # Owned lock released; a second acquisition must succeed.
        second = BridgeLock(tmp_path)
        assert second.acquire(blocking=False) is True
        second.release()
        # Own record must not exist (publication failed / cleanup).
        assert not server.record_path.exists()
        # Foreign record must not be deleted by own-record cleanup.
        assert foreign_record.is_file()
        assert json.loads(foreign_record.read_text(encoding="utf-8")) == foreign_payload
        assert any(p == server.record_path for p in calls)

    asyncio.run(go())


def test_live_lockless_record_not_deleted(tmp_path):
    """A responsive lockless legacy record must not be unlinked by a new server."""

    record = tmp_path / "bridge.json"
    token = "live-lockless"

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(line.decode("utf-8"))
            if str(request.get("token") or "") != token:
                resp = {"id": request.get("id"), "ok": False, "error": "auth"}
            else:
                resp = {
                    "id": request.get("id"),
                    "ok": True,
                    "result": {"pong": True, "running": True},
                }
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def go():
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        payload = {
            "host": "127.0.0.1",
            "port": port,
            "token": token,
            "instance_id": "lockless",
            "protocol_version": 1,
        }
        record.write_text(json.dumps(payload), encoding="utf-8")
        # Probe from a worker thread so this loop can accept the connection.
        assert await asyncio.to_thread(_probe_record_responsive, payload, 0.5) is True

        core = BridgeCore(state_dir=tmp_path, environ={})
        # New server acquires OS lock but must refuse to clobber the live record.
        new_server = BridgeServer(core, record_path=record, token="new-tok")
        with pytest.raises(BridgeError, match="live bridge record"):
            await new_server.run()
        # Record still present and still the live peer.
        assert record.exists()
        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["token"] == token
        assert await asyncio.to_thread(_probe_record_responsive, data, 0.5) is True
        # Lock was released after the failure.
        probe = BridgeLock(tmp_path)
        assert probe.acquire(blocking=False) is True
        probe.release()
        srv.close()
        await srv.wait_closed()

    asyncio.run(go())


def test_live_same_token_different_instance_not_clobbered(tmp_path):
    """Token collision must not skip the live probe when instance_id differs."""

    record = tmp_path / "bridge.json"
    shared_token = "shared-token-collision"

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(line.decode("utf-8"))
            if str(request.get("token") or "") != shared_token:
                resp = {"id": request.get("id"), "ok": False, "error": "auth"}
            else:
                resp = {
                    "id": request.get("id"),
                    "ok": True,
                    "result": {"pong": True, "running": True},
                }
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def go():
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        payload = {
            "host": "127.0.0.1",
            "port": port,
            "token": shared_token,
            "instance_id": "live-instance-A",
            "protocol_version": 2,
        }
        record.write_text(json.dumps(payload), encoding="utf-8")
        assert await asyncio.to_thread(_probe_record_responsive, payload, 0.5) is True

        core = BridgeCore(state_dir=tmp_path, environ={})
        # Same token as the live peer, different instance_id — must refuse.
        new_server = BridgeServer(
            core, record_path=record, token=shared_token
        )
        assert core.instance_id != "live-instance-A"
        with pytest.raises(BridgeError, match="live bridge record"):
            await new_server.run()
        assert record.exists()
        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["token"] == shared_token
        assert data["instance_id"] == "live-instance-A"
        assert data == payload
        probe = BridgeLock(tmp_path)
        assert probe.acquire(blocking=False) is True
        probe.release()
        srv.close()
        await srv.wait_closed()

    asyncio.run(go())


def test_config_fingerprint_effective_address_only(tmp_path, monkeypatch):
    """Fingerprint uses effective address; shadowed env must not diverge hashes."""

    # Same explicit address, different shadowed env values → same fingerprint.
    fp1 = config_fingerprint(
        {"XBLOOM_ADDRESS": "AA:BB:CC:DD:EE:FF"},
        address="11:22:33:44:55:66",
    )
    fp2 = config_fingerprint(
        {"XBLOOM_ADDRESS": "FF:EE:DD:CC:BB:AA"},
        address="11:22:33:44:55:66",
    )
    assert fp1 == fp2

    # Case / strip variants of the same effective address match.
    fp_upper = config_fingerprint({}, address="AA:BB:CC:DD:EE:FF")
    fp_lower = config_fingerprint({}, address="aa:bb:cc:dd:ee:ff")
    fp_space = config_fingerprint({}, address="  AA:BB:CC:DD:EE:FF  ")
    assert fp_upper == fp_lower == fp_space

    # Genuinely different effective addresses differ.
    fp_other = config_fingerprint({}, address="11:22:33:44:55:66")
    assert fp_other != fp_upper

    # Env-only effective address still participates when no explicit address.
    fp_env_a = config_fingerprint({"XBLOOM_ADDRESS": "AA:BB:CC:DD:EE:FF"})
    fp_env_b = config_fingerprint({"XBLOOM_ADDRESS": "aa:bb:cc:dd:ee:ff"})
    assert fp_env_a == fp_env_b == fp_upper


def test_config_fingerprint_includes_address_and_mismatch_surfaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    fp_a = config_fingerprint({}, address="AA:BB")
    fp_b = config_fingerprint({}, address="CC:DD")
    assert fp_a != fp_b

    started = start_bridge_daemon(
        state_root=tmp_path, address="AA:BB", start_timeout=15.0
    )
    assert started.get("config_match") is True
    assert started.get("client_ready") is True
    assert started.get("ensured") is True
    # Client with different effective address sees mismatch; daemon stays up.
    mismatch = ensure_bridge_daemon(
        state_root=tmp_path, address="CC:DD", start_timeout=5.0
    )
    assert mismatch.get("config_match") is False
    assert mismatch.get("config_warning")
    assert mismatch.get("instance_id") == started.get("instance_id")
    # Config mismatch alone remains client-ready (protocol compatible) with warning.
    assert mismatch.get("client_ready") is True
    assert mismatch.get("ensured") is True
    assert mismatch.get("idle_restart_recommended") is True or mismatch.get(
        "status"
    ) == "config_mismatch_idle"
    stop_bridge_daemon(state_root=tmp_path)


def test_lifecycle_client_ready_contract(tmp_path, monkeypatch):
    """client_ready is true only for a confirmed protocol-compatible daemon."""

    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    record = bridge_record_path(tmp_path)

    # Normal start/reuse
    started = start_bridge_daemon(state_root=tmp_path, start_timeout=15.0)
    assert started.get("running") is True
    assert started.get("client_ready") is True
    assert started.get("ensured") is True
    reused = ensure_bridge_daemon(state_root=tmp_path, start_timeout=5.0)
    assert reused.get("client_ready") is True
    assert reused.get("ensured") is True
    assert reused.get("started") is False
    assert reused.get("instance_id") == started.get("instance_id")
    stop_bridge_daemon(state_root=tmp_path)

    token = "leg-ready-tok"

    async def make_legacy(*, idle: bool):
        shutdown_flag = asyncio.Event()

        async def handle(reader, writer):
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                request = json.loads(line.decode("utf-8"))
                method = request.get("method")
                if str(request.get("token") or "") != token:
                    resp = {"id": request.get("id"), "ok": False, "error": "auth"}
                elif method == "ping":
                    resp = {
                        "id": request.get("id"),
                        "ok": True,
                        "result": {"pong": True},
                    }
                elif method == "status":
                    resp = {
                        "id": request.get("id"),
                        "ok": True,
                        "result": {
                            "running": True,
                            "protocol_version": 1,
                            "rpc_protocol_min": 1,
                            "rpc_protocol_max": 1,
                            "rpc_protocol_current": 1,
                            "instance_id": "legacy_ready_probe",
                            "idle": idle,
                            "activity": None if idle else "coffee",
                            "phase": "disconnected" if idle else "running",
                            "recovery_records": [],
                            "config_fingerprint": "x",
                        },
                    }
                elif method == "shutdown":
                    resp = {
                        "id": request.get("id"),
                        "ok": True,
                        "result": {"status": "shutting_down"},
                    }
                    writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    shutdown_flag.set()
                    srv.close()
                    return
                else:
                    resp = {
                        "id": request.get("id"),
                        "ok": False,
                        "error": f"no {method}",
                    }
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        record.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "token": token,
                    "instance_id": "legacy_ready_probe",
                    "protocol_version": 1,
                }
            ),
            encoding="utf-8",
        )
        return srv, shutdown_flag

    async def go():
        # Active legacy: not client-ready, ensured=False
        srv, _ = await make_legacy(idle=False)
        try:
            outcome = await asyncio.to_thread(
                ensure_bridge_daemon, state_root=tmp_path, start_timeout=5.0
            )
            assert outcome.get("upgrade_pending") is True or outcome.get(
                "status"
            ) == "upgrade_pending"
            assert outcome.get("client_ready") is False
            assert outcome.get("ensured") is False
            assert outcome.get("legacy_daemon") is True
        finally:
            srv.close()
            await srv.wait_closed()
            try:
                record.unlink()
            except FileNotFoundError:
                pass

        # Idle legacy: replaced with a ready v2 daemon
        srv, flag = await make_legacy(idle=True)
        try:
            outcome = await asyncio.to_thread(
                ensure_bridge_daemon, state_root=tmp_path, start_timeout=15.0
            )
            assert await _await_for(flag.is_set, timeout=10.0)
            assert outcome.get("client_ready") is True
            assert outcome.get("ensured") is True
            assert int(outcome.get("rpc_protocol_current") or 0) == RPC_PROTOCOL_CURRENT
        finally:
            if bridge_is_running(record_path=record):
                stop_bridge_daemon(state_root=tmp_path)
            if srv.is_serving():
                srv.close()
                await srv.wait_closed()

    asyncio.run(go())

    # Concurrent starters: both report client_ready for the single instance.
    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def starter() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                start_bridge_daemon(state_root=tmp_path, start_timeout=20.0)
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=starter) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    assert not errors, errors
    assert len(results) == 2
    assert all(r.get("client_ready") is True for r in results)
    assert all(r.get("ensured") is True for r in results)
    assert len({r.get("instance_id") for r in results}) == 1
    stop_bridge_daemon(state_root=tmp_path)


def test_completed_grinder_rest_not_recovery(tmp_path):
    core = BridgeCore(state_dir=tmp_path, environ={})
    # Completed cooldown record persists intentionally.
    (tmp_path / "grinder-rest-state.json").write_text(
        json.dumps(
            {
                "in_progress": False,
                "blocked_until": 9_999_999_999,
                "owner": "bridge",
            }
        ),
        encoding="utf-8",
    )
    assert core._grinder_is_recovery() is False
    assert core.is_idle() is True
    assert "grinder-rest-state.json" not in core.recovery_record_names()
    # File preserved (not deleted by idle checks).
    assert (tmp_path / "grinder-rest-state.json").is_file()

    # In-progress is recovery / non-idle.
    (tmp_path / "grinder-rest-state.json").write_text(
        json.dumps({"in_progress": True, "owner": "bridge"}),
        encoding="utf-8",
    )
    assert core._grinder_is_recovery() is True
    assert core.is_idle() is False

    # Unreadable is recovery.
    (tmp_path / "grinder-rest-state.json").write_text("{not-json", encoding="utf-8")
    assert core._grinder_is_recovery() is True
    assert core.is_idle() is False


def test_preowned_lock_ownership_is_deterministic(tmp_path):
    lock = BridgeLock(tmp_path)
    assert lock.acquire(blocking=False)
    core = BridgeCore(state_dir=tmp_path, environ={})
    # Default: server does not own a pre-held lock.
    server = BridgeServer(core, lock=lock, token="tok")
    assert server._owns_lock is False

    async def go():
        task = asyncio.create_task(server.run())
        assert await _await_for(server.record_path.exists)
        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=server.record_path, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)
        # Caller still owns the lock.
        assert lock.owned is True
        lock.release()

    asyncio.run(go())

    # Transfer ownership: server releases on exit.
    lock2 = BridgeLock(tmp_path)
    assert lock2.acquire(blocking=False)
    core2 = BridgeCore(state_dir=tmp_path, environ={})
    server2 = BridgeServer(core2, lock=lock2, token="tok2", owns_lock=True)
    assert server2._owns_lock is True

    async def go2():
        task = asyncio.create_task(server2.run())
        assert await _await_for(server2.record_path.exists)
        await asyncio.to_thread(
            bridge_call, "shutdown", record_path=server2.record_path, timeout=2.0
        )
        await asyncio.wait_for(task, timeout=2.0)
        assert lock2.owned is False

    asyncio.run(go2())
