"""BridgeServer JSON-line loopback server and daemon lifecycle helpers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from ._bridge_import_bridge import *  # noqa: F401,F403


def _bridge_mod() -> Any:
    return sys.modules.get("xbloom_ble.bridge")


def _resolve_atomic_json() -> Any:
    mod = _bridge_mod()
    return getattr(mod, "_atomic_json", _atomic_json)


def _resolve_bridge_core_cls() -> type[Any]:
    mod = _bridge_mod()
    cls = getattr(mod, "BridgeCore", None)
    if cls is not None:
        return cls
    from .bridge import BridgeCore

    return BridgeCore


class BridgeServer:
    """Authenticated loopback JSON-line server with lifecycle lock and hello.

    Lock ownership:
    - When ``lock`` is omitted and ``acquire_lock=True``, :meth:`run` acquires
      the lock and **owns** release on exit (success or failure).
    - When a pre-owned ``lock`` is passed, ownership is explicit via
      ``owns_lock`` (default ``False``): the caller retains release duty and
      :meth:`run` never releases it. Pass ``owns_lock=True`` to transfer
      release responsibility to the server for the remainder of the process.
    - ``acquire_lock=False`` with no lock is for lockless test/legacy harnesses
      only; production serve always holds a lock.
    """

    def __init__(
        self,
        core: Any,
        *,
        record_path: Path | None = None,
        token: str | None = None,
        lock: BridgeLock | None = None,
        acquire_lock: bool = True,
        owns_lock: bool | None = None,
    ) -> None:
        self.core = core
        self.state_root = normalize_state_root(core.state_dir)
        self.record_path = (
            Path(record_path)
            if record_path is not None
            else bridge_record_path(self.state_root)
        )
        self.token = token or secrets.token_urlsafe(32)
        self.shutdown_event = asyncio.Event()
        self.server: asyncio.Server | None = None
        self._acquire_lock = bool(acquire_lock)
        if lock is not None:
            self.lock = lock
            # Pre-owned lock: default to not releasing on cleanup.
            self._owns_lock = bool(owns_lock) if owns_lock is not None else False
        elif acquire_lock:
            self.lock = BridgeLock(self.state_root)
            # Acquired in run(); owns_lock True after successful acquire.
            self._owns_lock = False if owns_lock is None else bool(owns_lock)
        else:
            self.lock = None
            self._owns_lock = False

    def _public_identity(self) -> dict[str, Any]:
        return {
            "instance_id": self.core.instance_id,
            "pid": os.getpid(),
            "host": BRIDGE_HOST,
            "core_version": self.core.core_version,
            "rpc_protocol_min": RPC_PROTOCOL_MIN,
            "rpc_protocol_max": RPC_PROTOCOL_MAX,
            "rpc_protocol_current": RPC_PROTOCOL_CURRENT,
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "record_format_version": BRIDGE_RECORD_FORMAT_VERSION,
            "config_fingerprint": self.core.config_fingerprint,
            "started_at": self.core.started_at,
        }

    def _hello(self, params: Mapping[str, Any]) -> dict[str, Any]:
        missing = [
            field
            for field in ("client_name", "client_version", "protocol_min", "protocol_max")
            if field not in params or params.get(field) is None
        ]
        if missing:
            raise BridgeError(
                "hello requires declared fields: " + ", ".join(missing)
            )
        client_name = params.get("client_name")
        client_version = params.get("client_version")
        if not isinstance(client_name, str) or not client_name.strip():
            raise BridgeError("hello client_name must be a non-empty string")
        if not isinstance(client_version, str) or not client_version.strip():
            raise BridgeError("hello client_version must be a non-empty string")
        client_name = client_name.strip()
        client_version = client_version.strip()
        client_min, client_max = require_protocol_range(
            params.get("protocol_min"),
            params.get("protocol_max"),
            field_prefix="protocol",
        )
        client_fp = params.get("config_fingerprint")
        if client_fp is not None:
            if not isinstance(client_fp, str) or not client_fp.strip():
                raise BridgeError("hello config_fingerprint must be a non-empty string when set")
            client_fp = client_fp.strip()
        compatibility = evaluate_compatibility(
            client_protocol_min=client_min,
            client_protocol_max=client_max,
            client_config_fingerprint=client_fp,
            server_config_fingerprint=self.core.config_fingerprint,
            strict_protocol_types=True,
        )
        # Always return identity + compatibility; never expose the auth token.
        # Incompatible clients are rejected later on non-diagnostic methods.
        return {
            **self._public_identity(),
            "client_name": client_name,
            "client_version": client_version,
            "compatibility": compatibility,
            "config_match": compatibility.get("config_match"),
            "config_warning": (
                "client config fingerprint differs from daemon startup snapshot"
                if compatibility.get("config_match") is False
                else None
            ),
        }

    def _require_compatible_request(self, request: Mapping[str, Any], method: str) -> None:
        if method in DIAGNOSTIC_METHODS:
            return
        # Prefer envelope fields; fall back to params for convenience.
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        raw_min = request.get("protocol_min", params.get("protocol_min"))
        raw_max = request.get("protocol_max", params.get("protocol_max"))
        if raw_min is None or raw_max is None:
            raise BridgeCompatibilityError(
                "non-diagnostic bridge methods require protocol_min/protocol_max "
                "(call hello first via the client helper)"
            )
        try:
            client_min, client_max = require_protocol_range(raw_min, raw_max)
        except BridgeError as exc:
            # Type/range failures on the envelope are hard errors before dispatch.
            raise BridgeError(str(exc)) from exc
        compatibility = evaluate_compatibility(
            client_protocol_min=client_min,
            client_protocol_max=client_max,
            strict_protocol_types=True,
        )
        if not compatibility["compatible"]:
            raise BridgeCompatibilityError(
                "incompatible bridge client: " + "; ".join(compatibility["reasons"])
            )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id: Any = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line or len(line) > 65536:
                raise BridgeError("invalid or oversized bridge request")
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise BridgeError("bridge request must be an object")
            request_id = request.get("id")
            supplied = str(request.get("token") or "")
            if not secrets.compare_digest(supplied, self.token):
                raise BridgeError("bridge authentication failed")
            method = str(request.get("method") or "")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise BridgeError("bridge params must be an object")

            if method == "hello":
                # Authenticated handshake only; never reaches BridgeCore.rpc.
                result = self._hello(params)
            elif method == "shutdown":
                self._require_compatible_request(request, method)
                await self.core.shutdown(force=bool(params.get("force", False)))
                result = {"status": "shutting_down"}
                self.shutdown_event.set()
            elif method in {"ping", "status"}:
                # Diagnostics: never require hello; annotate compatibility when asked.
                result = await self.core.rpc(method, params)
                raw_min = request.get("protocol_min", params.get("protocol_min"))
                raw_max = request.get("protocol_max", params.get("protocol_max"))
                if raw_min is not None and raw_max is not None:
                    try:
                        client_min, client_max = require_protocol_range(
                            raw_min, raw_max, require_present=True
                        )
                        result = dict(result)
                        result["compatibility"] = evaluate_compatibility(
                            client_protocol_min=client_min,
                            client_protocol_max=client_max,
                            client_config_fingerprint=(
                                str(params["config_fingerprint"])
                                if params.get("config_fingerprint") is not None
                                else None
                            ),
                            server_config_fingerprint=self.core.config_fingerprint,
                            strict_protocol_types=True,
                        )
                    except BridgeError:
                        # Invalid optional annotation types: omit compatibility.
                        pass
            else:
                # Reject incompatible clients before any BridgeCore.rpc / BLE path.
                self._require_compatible_request(request, method)
                result = await self.core.rpc(method, params)
            # Strip token if a handler ever leaked it.
            if isinstance(result, dict):
                result = {k: v for k, v in result.items() if k != "token"}
            response = {"id": request_id, "ok": True, "result": result}
        except BridgeCompatibilityError as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": "BridgeCompatibilityError",
            }
            category = getattr(exc, "category", None)
            if category:
                response["category"] = str(category)
        except BridgeError as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": type(exc).__name__,
            }
            category = getattr(exc, "category", None)
            if category:
                # Stable wire category (e.g. device_busy_external); never tokens.
                response["category"] = str(category)
        except Exception as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": type(exc).__name__,
            }
        writer.write(
            (
                json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8")
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _release_owned_lock(self) -> None:
        if self._owns_lock and self.lock is not None and self.lock.owned:
            self.lock.release()
        self._owns_lock = False

    def _cleanup_own_record(self) -> None:
        try:
            current = _read_json(self.record_path)
        except BridgeError:
            return
        # Never delete a live record owned by another instance.
        if current.get("token") == self.token and current.get(
            "instance_id"
        ) == self.core.instance_id:
            _unlink(self.record_path)

    async def run(self) -> None:
        """Acquire ownership (if configured), bind, publish record, serve.

        Every step after lock acquisition is under a finally that releases the
        OS lock when this server owns it, so bind/record failures cannot leak
        the lock.
        """

        acquired_here = False
        if self.lock is not None and not self.lock.owned:
            if not self.lock.acquire(blocking=False):
                raise BridgeLockError(
                    f"another bridge instance holds {self.lock.path}; not starting"
                )
            acquired_here = True
            self._owns_lock = True

        try:
            # Even with the OS lock held, a lockless/legacy peer may still be
            # answering on a live record. Probe before unlinking.
            # Self-owned only when *both* token and instance_id match -- token
            # collision/reuse must not clobber a responsive different instance.
            # Run the probe in a worker thread so a same-loop test/legacy peer
            # can still accept the diagnostic connection.
            if self.record_path.exists():
                try:
                    stale = _read_json(self.record_path)
                except BridgeError:
                    stale = {}
                is_self_owned = (
                    stale.get("token") == self.token
                    and stale.get("instance_id") == self.core.instance_id
                )
                if not is_self_owned:
                    live = await asyncio.to_thread(
                        _probe_record_responsive, stale, 0.5
                    )
                    if live:
                        raise BridgeError(
                            "a live bridge record is already serving this state root; "
                            "refusing to start a second daemon or delete the live record"
                        )
                    _unlink(self.record_path)

            self.server = await asyncio.start_server(
                self._handle, BRIDGE_HOST, 0, limit=65536
            )
            try:
                socket_info = self.server.sockets[0].getsockname()
                record = {
                    **self._public_identity(),
                    "port": int(socket_info[1]),
                    "token": self.token,
                    "started_at": self.core.started_at,
                }
                _resolve_atomic_json()(self.record_path, record, private=True)
                await self.shutdown_event.wait()
            finally:
                self.server.close()
                await self.server.wait_closed()
                self._cleanup_own_record()
        finally:
            # Always release a lock we acquired (or were transferred ownership of).
            if acquired_here or self._owns_lock:
                self._release_owned_lock()


# Process-local hello cache: must include every input that affects compatibility.
_hello_ok: dict[str, str] = {}


def _hello_cache_key(
    record: Mapping[str, Any],
    record_path: Path,
    *,
    protocol_min: int,
    protocol_max: int,
    client_name: str,
    client_version: str,
    config_fingerprint_value: str | None,
) -> str:
    return (
        f"{record_path}:{record.get('instance_id')}:{record.get('token')}:"
        f"{protocol_min}:{protocol_max}:{client_name}:{client_version}:"
        f"{config_fingerprint_value or ''}"
    )


def _probe_record_responsive(
    record: Mapping[str, Any],
    timeout: float = 0.5,
) -> bool:
    """Return True if the discovery record answers authenticated diagnostics.

    Uses a blocking socket; callers on an asyncio event loop that also hosts
    the peer must invoke this via ``asyncio.to_thread`` so the peer can accept.
    """

    try:
        host = str(record.get("host") or "")
        if host != BRIDGE_HOST:
            return False
        port = int(record["port"])
        token = str(record.get("token") or "")
        if not token:
            return False
        request = {
            "id": secrets.token_hex(8),
            "token": token,
            "method": "ping",
            "params": {},
        }
        with socket.create_connection((host, port), timeout=float(timeout)) as connection:
            connection.settimeout(float(timeout))
            connection.sendall((json.dumps(request, allow_nan=False) + "\n").encode("utf-8"))
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    return False
        response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
        return bool(response.get("ok"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError, UnicodeDecodeError, IndexError):
        return False


def _daemon_protocol_info(status_or_record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize protocol fields from status/hello/record (legacy-tolerant)."""

    def _as_int(value: Any, default: int | None = None) -> int | None:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    current = _as_int(
        status_or_record.get("rpc_protocol_current")
        or status_or_record.get("protocol_version")
    )
    pmin = _as_int(status_or_record.get("rpc_protocol_min"), current)
    pmax = _as_int(status_or_record.get("rpc_protocol_max"), current)
    legacy = False
    if current is None and pmin is None and pmax is None:
        # Pre-versioned daemon: treat as protocol 1.
        current, pmin, pmax = 1, 1, 1
        legacy = True
    elif current is not None and current <= LEGACY_RPC_PROTOCOL_MAX:
        legacy = True
        if pmin is None:
            pmin = current
        if pmax is None:
            pmax = current
    elif pmax is not None and pmax < RPC_PROTOCOL_MIN:
        legacy = True
    return {
        "rpc_protocol_min": pmin if pmin is not None else 1,
        "rpc_protocol_max": pmax if pmax is not None else 1,
        "rpc_protocol_current": current if current is not None else 1,
        "legacy": legacy,
        "compatible_with_client": _protocol_overlap(
            RPC_PROTOCOL_MIN,
            RPC_PROTOCOL_MAX,
            pmin if pmin is not None else 1,
            pmax if pmax is not None else 1,
        ),
    }


def bridge_call(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    record_path: Path | None = None,
    client_name: str = "xbloom-studio",
    client_version: str | None = None,
    protocol_min: int = RPC_PROTOCOL_MIN,
    protocol_max: int = RPC_PROTOCOL_MAX,
    require_hello: bool | None = None,
    config_fingerprint_value: str | None = None,
    include_config_fingerprint: bool = True,
    omit_protocol_envelope: bool = False,
) -> dict[str, Any]:
    """Authenticated JSON-line RPC against the local bridge.

    Non-diagnostic methods perform hello/compatibility validation first (unless
    ``require_hello=False``). The auth token is never returned in results.
    Normal clients declare their effective config fingerprint by default.
    """

    path = Path(record_path) if record_path is not None else bridge_record_path()
    record = _read_json(path)
    host = str(record.get("host") or "")
    if host != BRIDGE_HOST:
        raise BridgeError("bridge record does not point to the required loopback host")

    # Public compatibility inputs: strict JSON integers, no float/bool/str coerce.
    pmin, pmax = require_protocol_range(protocol_min, protocol_max)

    if require_hello is None:
        # Diagnostics (hello/ping/status) skip the hello preflight.
        require_hello = method not in DIAGNOSTIC_METHODS

    version = client_version or _core_version()
    client_fp = config_fingerprint_value
    if include_config_fingerprint and client_fp is None:
        client_fp = config_fingerprint()
    cache_key = _hello_cache_key(
        record,
        path,
        protocol_min=pmin,
        protocol_max=pmax,
        client_name=client_name,
        client_version=version,
        config_fingerprint_value=client_fp,
    )

    if require_hello and _hello_ok.get(cache_key) != "ok":
        hello_params: dict[str, Any] = {
            "client_name": client_name,
            "client_version": version,
            "protocol_min": pmin,
            "protocol_max": pmax,
        }
        if client_fp is not None:
            hello_params["config_fingerprint"] = client_fp
        hello_result = _bridge_call_raw(
            "hello",
            hello_params,
            timeout=min(float(timeout), 5.0),
            record=record,
            protocol_min=pmin,
            protocol_max=pmax,
        )
        compatibility = hello_result.get("compatibility") or {}
        if not compatibility.get("compatible", True):
            raise BridgeCompatibilityError(
                "incompatible bridge client: "
                + "; ".join(compatibility.get("reasons") or ["unknown"])
            )
        _hello_ok[cache_key] = "ok"

    call_params = dict(params or {})
    if (
        include_config_fingerprint
        and client_fp is not None
        and "config_fingerprint" not in call_params
        and method in {"status", "ping", "hello"}
    ):
        call_params["config_fingerprint"] = client_fp

    return _bridge_call_raw(
        method,
        call_params,
        timeout=timeout,
        record=record,
        protocol_min=pmin,
        protocol_max=pmax,
        client_name=client_name,
        client_version=version,
        omit_protocol_envelope=omit_protocol_envelope,
        config_fingerprint_value=client_fp if include_config_fingerprint else None,
    )


def _bridge_call_raw(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    record: Mapping[str, Any],
    protocol_min: int = RPC_PROTOCOL_MIN,
    protocol_max: int = RPC_PROTOCOL_MAX,
    client_name: str | None = None,
    client_version: str | None = None,
    omit_protocol_envelope: bool = False,
    config_fingerprint_value: str | None = None,
) -> dict[str, Any]:
    host = str(record.get("host") or BRIDGE_HOST)
    request: dict[str, Any] = {
        "id": secrets.token_hex(8),
        "token": record.get("token"),
        "method": method,
        "params": dict(params or {}),
    }
    if not omit_protocol_envelope:
        pmin, pmax = require_protocol_range(protocol_min, protocol_max)
        request["protocol_min"] = pmin
        request["protocol_max"] = pmax
    if client_name is not None:
        request["client_name"] = client_name
    if client_version is not None:
        request["client_version"] = client_version
    if config_fingerprint_value is not None and method not in DIAGNOSTIC_METHODS:
        # Envelope-level declaration for non-diagnostic RPCs.
        request["config_fingerprint"] = config_fingerprint_value
    try:
        with socket.create_connection(
            (host, int(record["port"])),
            timeout=float(timeout),
        ) as connection:
            connection.settimeout(float(timeout))
            connection.sendall(
                (json.dumps(request, allow_nan=False) + "\n").encode("utf-8")
            )
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    raise BridgeError("bridge response exceeded 1 MB")
    except (OSError, KeyError, ValueError) as exc:
        raise BridgeError("bridge is not responding; inspect bridge.log or restart it") from exc
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise BridgeError("bridge returned an invalid response") from exc
    if not response.get("ok"):
        err_type = str(response.get("type") or "")
        message = str(response.get("error") or "bridge request failed")
        # Never surface raw auth material if a buggy handler leaked it.
        if "token" in message.casefold():
            message = "bridge request failed"
        category_raw = response.get("category")
        category = str(category_raw) if category_raw else None
        if err_type == "BridgeCompatibilityError" or "incompatible" in message.lower():
            raise BridgeCompatibilityError(message, category=category)
        raise BridgeError(message, category=category)
    result = response.get("result")
    if not isinstance(result, dict):
        raise BridgeError("bridge returned a non-object result")
    if "token" in result:
        result = {k: v for k, v in result.items() if k != "token"}
    return result


def bridge_is_running(*, record_path: Path | None = None) -> bool:
    try:
        bridge_call(
            "ping",
            timeout=0.5,
            record_path=record_path,
            require_hello=False,
            include_config_fingerprint=False,
        )
        return True
    except BridgeError:
        return False


def _legacy_shutdown(
    record: Mapping[str, Any],
    *,
    force: bool = False,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Token-authenticated shutdown without hello/envelope (legacy wire)."""

    return _bridge_call_raw(
        "shutdown",
        {"force": bool(force)},
        timeout=timeout,
        record=record,
        omit_protocol_envelope=True,
    )


def _spawn_bridge_process(
    *,
    address: str | None,
    state_root: Path,
    log_path: Path,
    environ: Mapping[str, str] | None = None,
) -> subprocess.Popen[Any]:
    """Spawn the core-owned daemon (no Skill script path required)."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Parent-parser flags must precede the subcommand (argparse).
    command = [sys.executable, "-m", "xbloom_ble.bridge"]
    if address:
        command.extend(["--address", str(address)])
    command.append("serve")
    child_env = environment_copy(environ)
    # Ensure the child uses the same state root.
    child_env["XBLOOM_STATE_DIR"] = str(state_root)
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            env=child_env,
            **popen_kwargs,
        )


def _poll_bridge_status(
    record_path: Path,
    *,
    timeout: float = 8.0,
    log_path: Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        # Bounded polling without long sleeps; short yield only.
        time.sleep(0.05)
        try:
            return bridge_call(
                "status",
                timeout=0.5,
                record_path=record_path,
                require_hello=False,
            )
        except BridgeError as exc:
            last_error = exc
    detail = f": {last_error}" if last_error else ""
    hint = f"; inspect {log_path}" if log_path is not None else ""
    raise BridgeError(f"bridge did not start{hint}{detail}")


def _annotate_config(
    status: dict[str, Any],
    *,
    address: str | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Attach client-effective config fingerprint match fields to a status dict."""

    client_fp = config_fingerprint(environ, address=address)
    server_fp = status.get("config_fingerprint")
    config_match: bool | None
    if server_fp is None:
        config_match = None
    else:
        config_match = str(server_fp) == client_fp
    status["client_config_fingerprint"] = client_fp
    status["config_match"] = config_match
    if config_match is False:
        status["config_warning"] = (
            "client effective config fingerprint differs from the running daemon; "
            "restart only when idle (never forces active work)"
        )
    else:
        status.pop("config_warning", None)
    return status


def _status_is_safely_idle(status: Mapping[str, Any]) -> bool:
    if status.get("activity") is not None:
        return False
    if list(status.get("recovery_records") or []):
        return False
    if status.get("idle") is False:
        return False
    phase = status.get("phase")
    if phase is not None and str(phase) not in SAFE_IDLE_PHASES:
        return False
    return bool(status.get("idle", True))


def _mark_lifecycle_result(
    status: dict[str, Any],
    *,
    client_ready: bool,
    ensured: bool | None = None,
) -> dict[str, Any]:
    """Attach lifecycle contract fields. ``client_ready`` means a confirmed
    protocol-compatible running daemon is available for normal client RPCs.
    """

    status["client_ready"] = bool(client_ready)
    if ensured is not None:
        status["ensured"] = bool(ensured)
    elif "ensured" not in status:
        # Prefer ensured=False when we could not provide a compatible daemon.
        status["ensured"] = bool(client_ready)
    return status


def _upgrade_or_reuse_running_daemon(
    *,
    path: Path,
    root: Path,
    address: str | None,
    environ: Mapping[str, str] | None,
    start_timeout: float,
    started_flag: bool,
) -> dict[str, Any]:
    """Handle a responsive daemon: upgrade legacy/idle-mismatch or annotate reuse."""

    status = bridge_call(
        "status",
        record_path=path,
        require_hello=False,
        include_config_fingerprint=True,
    )
    status = dict(status)
    proto = _daemon_protocol_info(status)
    status = _annotate_config(status, address=address, environ=environ)
    status["protocol_compatible"] = proto["compatible_with_client"]
    status["legacy_daemon"] = proto["legacy"]

    needs_upgrade = proto["legacy"] or not proto["compatible_with_client"]
    config_mismatch = status.get("config_match") is False
    safely_idle = _status_is_safely_idle(status)

    if needs_upgrade:
        if not safely_idle:
            status["started"] = False
            status["already_running"] = True
            status["upgrade_pending"] = True
            status["status"] = "upgrade_pending"
            status["reason"] = "legacy_or_incompatible_daemon_not_idle"
            status["message"] = (
                "running daemon is protocol-incompatible but busy or has recovery; "
                "preserving active work until a clean idle stop"
            )
            # Not client-ready: incompatible, and ensure could not replace it.
            return _mark_lifecycle_result(
                status, client_ready=False, ensured=False
            )
        # Idle legacy/incompatible: token-auth shutdown without hello, then relaunch.
        try:
            record = _read_json(path)
            _legacy_shutdown(record, force=False, timeout=start_timeout)
        except BridgeError as exc:
            status["started"] = False
            status["upgrade_pending"] = True
            status["status"] = "upgrade_pending"
            status["reason"] = "legacy_shutdown_failed"
            status["message"] = str(exc)
            return _mark_lifecycle_result(
                status, client_ready=False, ensured=False
            )
        deadline = time.monotonic() + float(start_timeout)
        while time.monotonic() < deadline and bridge_is_running(record_path=path):
            time.sleep(0.05)
        if bridge_is_running(record_path=path):
            status["started"] = False
            status["upgrade_pending"] = True
            status["status"] = "upgrade_pending"
            status["reason"] = "legacy_stop_pending"
            status["message"] = "legacy daemon did not exit after authenticated shutdown"
            return _mark_lifecycle_result(
                status, client_ready=False, ensured=False
            )
        launched = start_bridge_daemon(
            address=address,
            state_root=root,
            environ=environ,
            start_timeout=start_timeout,
        )
        launched = dict(launched)
        launched["upgraded_from_legacy"] = True
        launched["previous_instance_id"] = status.get("instance_id")
        # start_bridge_daemon already attaches client_ready/ensured.
        return launched

    if config_mismatch and safely_idle:
        # Config mismatch alone may remain client-ready (protocol ok) with warning.
        status["started"] = False
        status["already_running"] = True
        status["idle_restart_recommended"] = True
        status["status"] = "config_mismatch_idle"
        status["message"] = (
            "running daemon config fingerprint differs; daemon is idle -- "
            "call restart-if-idle to apply the client config"
        )
        return _mark_lifecycle_result(status, client_ready=True, ensured=True)

    if config_mismatch and not safely_idle:
        status["started"] = False
        status["already_running"] = True
        status["upgrade_pending"] = False
        status["idle_restart_recommended"] = False
        status["status"] = "config_mismatch_active"
        status["message"] = (
            "running daemon config fingerprint differs but work is active/recovery; "
            "not restarting"
        )
        return _mark_lifecycle_result(status, client_ready=True, ensured=True)

    status["started"] = started_flag
    status["already_running"] = not started_flag
    status["upgrade_pending"] = False
    return _mark_lifecycle_result(status, client_ready=True, ensured=True)


def ensure_bridge_daemon(
    *,
    address: str | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    start_timeout: float = 8.0,
) -> dict[str, Any]:
    """Return status of a running bridge, starting or upgrading one if needed.

    Never force-stops active work. Legacy/incompatible idle daemons are shut down
    with a token-authenticated legacy path, then replaced. Config mismatches are
    surfaced; only idle daemons advertise an idle-restart recommendation.
    """

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else skill_state_dir()
    )
    path = bridge_record_path(root)
    if bridge_is_running(record_path=path):
        return _upgrade_or_reuse_running_daemon(
            path=path,
            root=root,
            address=address,
            environ=environ,
            start_timeout=start_timeout,
            started_flag=False,
        )
    return start_bridge_daemon(
        address=address,
        state_root=root,
        environ=environ,
        start_timeout=start_timeout,
    )


def start_bridge_daemon(
    script_path: Path | str | None = None,
    *,
    address: str | None = None,
    record_path: Path | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    start_timeout: float = 8.0,
) -> dict[str, Any]:
    """Start the core-owned bridge daemon.

    ``script_path`` is accepted for temporary backwards compatibility with
    callers that previously launched ``scripts/xbloom.py bridge serve``. New
    call sites must not depend on a Skill script path; the daemon is always
    spawned as ``python -m xbloom_ble.bridge``.
    """

    if record_path is not None:
        path = Path(record_path)
        root = normalize_state_root(path.parent)
    elif state_root is not None:
        root = normalize_state_root(state_root)
        path = bridge_record_path(root)
    else:
        root = skill_state_dir()
        path = bridge_record_path(root)

    if bridge_is_running(record_path=path):
        return _upgrade_or_reuse_running_daemon(
            path=path,
            root=root,
            address=address,
            environ=environ,
            start_timeout=start_timeout,
            started_flag=False,
        )

    # Probe lock: if another instance holds it, wait briefly for its record.
    # Also refuse to start if a lockless live record answers diagnostics.
    if path.exists():
        try:
            existing = _read_json(path)
        except BridgeError:
            existing = {}
        if existing and _probe_record_responsive(existing, timeout=0.5):
            # Live peer without us holding its lock -- do not race it.
            # Route through the same compatibility/upgrade decision path.
            return _upgrade_or_reuse_running_daemon(
                path=path,
                root=root,
                address=address,
                environ=environ,
                start_timeout=start_timeout,
                started_flag=False,
            )

    probe = BridgeLock(root)
    if not probe.acquire(blocking=False):
        try:
            # Concurrent starter holds the lock; wait for its record then apply
            # the same compatibility normalization (do not bypass upgrade path).
            _poll_bridge_status(path, timeout=start_timeout)
            return _upgrade_or_reuse_running_daemon(
                path=path,
                root=root,
                address=address,
                environ=environ,
                start_timeout=start_timeout,
                started_flag=False,
            )
        except BridgeError as exc:
            raise BridgeError(
                "bridge lock is held but the daemon did not become ready"
            ) from exc
    # We hold the lock in this probe process - release so the child can acquire.
    probe.release()

    log_path = root / BRIDGE_LOG_NAME
    child_env = environment_copy(environ)
    if address:
        # Align child fingerprint with requested address.
        child_env.setdefault("XBLOOM_ADDRESS", str(address))
    _spawn_bridge_process(
        address=address,
        state_root=root,
        log_path=log_path,
        environ=child_env,
    )
    status = _poll_bridge_status(path, timeout=start_timeout, log_path=log_path)
    status = dict(status)
    status = _annotate_config(status, address=address, environ=child_env)
    proto = _daemon_protocol_info(status)
    status["protocol_compatible"] = proto["compatible_with_client"]
    status["legacy_daemon"] = proto["legacy"]
    status["started"] = True
    status["already_running"] = False
    status["upgrade_pending"] = False
    ready = bool(
        status.get("running")
        and proto["compatible_with_client"]
        and not proto["legacy"]
    )
    _mark_lifecycle_result(status, client_ready=ready, ensured=ready)
    # script_path is intentionally unused for spawning (compat signature only).
    _ = script_path
    return status


def stop_bridge_daemon(
    *,
    force: bool = False,
    state_root: Path | str | None = None,
    record_path: Path | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Request a clean shutdown of the running bridge daemon.

    Compatible daemons use hello + envelope. Legacy/incompatible idle daemons
    use a token-authenticated shutdown without hello so an upgrade can proceed.
    Active legacy work is never force-stopped by the upgrade path (``force`` only
    applies to the daemon's own shutdown semantics for an active activity).
    """

    if record_path is not None:
        path = Path(record_path)
    else:
        root = (
            normalize_state_root(state_root)
            if state_root is not None
            else skill_state_dir()
        )
        path = bridge_record_path(root)
    if not bridge_is_running(record_path=path):
        return {"running": False, "status": "already_stopped"}

    record = _read_json(path)
    status: dict[str, Any] = {}
    try:
        status = bridge_call(
            "status",
            record_path=path,
            require_hello=False,
            include_config_fingerprint=False,
            timeout=min(float(timeout), 2.0),
        )
    except BridgeError:
        status = {}
    proto = _daemon_protocol_info(status or record)
    use_legacy = proto["legacy"] or not proto["compatible_with_client"]

    if use_legacy:
        result = _legacy_shutdown(record, force=force, timeout=timeout)
    else:
        result = bridge_call(
            "shutdown",
            {"force": bool(force)},
            record_path=path,
            timeout=timeout,
            require_hello=True,
        )
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if not bridge_is_running(record_path=path):
            out = dict(result)
            out["running"] = False
            out["status"] = "stopped"
            return out
        time.sleep(0.05)
    return {**dict(result), "running": True, "status": "stop_pending"}


def restart_bridge_daemon_if_idle(
    *,
    address: str | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Restart only when the daemon is idle and has no recovery records.

    ``force`` is rejected: it must never bypass active work or recovery.
    ``restarted`` is True only after the old instance is confirmed gone and a
    new instance is confirmed healthy; otherwise status is pending/failure.
    """

    if force:
        return {
            "restarted": False,
            "status": "force_rejected",
            "reason": "force_not_supported",
            "message": (
                "restart-if-idle never force-stops active work or recovery; "
                "omit force and wait until the daemon is idle"
            ),
        }

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else skill_state_dir()
    )
    path = bridge_record_path(root)
    if not bridge_is_running(record_path=path):
        started = start_bridge_daemon(
            address=address, state_root=root, environ=environ
        )
        if started.get("running") and started.get("instance_id"):
            return {
                **started,
                "restarted": True,
                "reason": "was_not_running",
            }
        return {
            **dict(started),
            "restarted": False,
            "status": "start_failed",
            "reason": "was_not_running_start_unconfirmed",
        }

    status = bridge_call(
        "status",
        record_path=path,
        require_hello=False,
        include_config_fingerprint=True,
    )
    idle = bool(status.get("idle"))
    recovery = list(status.get("recovery_records") or [])
    activity = status.get("activity")
    if activity is not None or not idle or recovery or not _status_is_safely_idle(status):
        return {
            "restarted": False,
            "status": "upgrade_pending",
            "reason": "bridge_not_idle",
            "activity": activity,
            "idle": idle,
            "recovery_records": recovery,
            "instance_id": status.get("instance_id"),
            "config_fingerprint": status.get("config_fingerprint"),
            "message": (
                "bridge is busy or has recovery records; "
                "config/upgrade will apply after a clean idle stop"
            ),
        }

    previous_id = status.get("instance_id")
    stop_result = stop_bridge_daemon(state_root=root, record_path=path)
    if stop_result.get("status") != "stopped" or stop_result.get("running"):
        return {
            "restarted": False,
            "status": stop_result.get("status") or "stop_pending",
            "reason": "old_instance_not_confirmed_gone",
            "previous_instance_id": previous_id,
            "stop_result": stop_result,
            "message": "old bridge instance did not confirm exit; not starting a replacement",
        }

    started = start_bridge_daemon(
        address=address, state_root=root, environ=environ
    )
    if not (
        started.get("running")
        and started.get("instance_id")
        and bridge_is_running(record_path=path)
    ):
        return {
            **dict(started),
            "restarted": False,
            "status": "start_failed",
            "reason": "new_instance_not_confirmed_healthy",
            "previous_instance_id": previous_id,
            "message": "old instance stopped but replacement was not confirmed healthy",
        }
    return {
        **started,
        "restarted": True,
        "previous_instance_id": previous_id,
    }


async def serve_bridge(
    *,
    address: str | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    acquire_lock: bool = True,
) -> None:
    """Run the bridge server, holding the lifecycle lock for the process."""

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else skill_state_dir()
    )
    core = _resolve_bridge_core_cls()(
        default_address=address,
        state_dir=root,
        environ=environ,
    )
    server = BridgeServer(core, acquire_lock=acquire_lock)
    await server.run()


def main(argv: list[str] | None = None) -> None:
    """Console entry for the core-owned ``xbloom-bridge`` daemon."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="xbloom-bridge",
        description=(
            "Run or manage the loopback-only xBloom Studio BLE bridge daemon. "
            "Binds to 127.0.0.1, writes bridge.json under XBLOOM_STATE_DIR "
            "(or legacy XBLOOM_SKILL_STATE_DIR), and holds bridge.lock for "
            "single-instance ownership."
        ),
    )
    parser.add_argument(
        "--address",
        default=None,
        help="preferred BLE address/identifier; defaults to XBLOOM_ADDRESS or scan",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the daemon in the foreground (default)")
    sub.add_parser("start", help="ensure a background daemon is running")
    stop_p = sub.add_parser("stop", help="stop a running daemon")
    stop_p.add_argument(
        "--force",
        action="store_true",
        help="force stop even if an activity is active",
    )
    sub.add_parser("status", help="print daemon status JSON")
    sub.add_parser(
        "restart-if-idle",
        help="restart only when idle with no recovery records",
    )

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        asyncio.run(serve_bridge(address=args.address))
        return
    if command == "start":
        result = ensure_bridge_daemon(address=args.address)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if command == "stop":
        result = stop_bridge_daemon(force=bool(getattr(args, "force", False)))
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if command == "status":
        path = bridge_record_path()
        if not bridge_is_running(record_path=path):
            result = {"running": False, "record": str(path)}
        else:
            result = bridge_call("status", record_path=path, require_hello=False)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if command == "restart-if-idle":
        result = restart_bridge_daemon_if_idle(address=args.address)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
