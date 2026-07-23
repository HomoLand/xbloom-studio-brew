"""Create an external per-user runtime and install pinned BLE dependencies.

Bootstrap must run *before* ``xbloom-studio-core`` is installed, so this module
uses only the standard library until pip has finished. Path/runtime helpers are
inlined (matching ``xbloom_paths`` semantics) rather than imported from core.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import venv
from pathlib import Path

# Mirrors xbloom_paths state/runtime env without importing core.
STATE_DIR_ENV = "XBLOOM_STATE_DIR"
LEGACY_STATE_DIR_ENV = "XBLOOM_SKILL_STATE_DIR"
RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"
DEFAULT_STATE_DIRNAME = ".xbloom-studio-brew"

ROOT = Path(__file__).resolve().parents[1]
VENDOR_WHEELS = ROOT / "vendor" / "wheels"
RELEASE_META = ROOT / "vendor" / "release.json"
# Committed pin for hub/skills.sh installs (no monorepo core, no vendored wheel).
# Points at a published GitHub Release core wheel + sha256; updated each release.
HUB_PIN_PATH = ROOT / "vendor" / "hub-pin.json"
# Universal hashed non-core runtime lock (committed; copied into Skill releases).
RUNTIME_LOCK_BASENAME = "requirements-runtime.lock"
RUNTIME_LOCK_PATH = ROOT / RUNTIME_LOCK_BASENAME
DEFAULT_RELEASE_REPO = "HomoLand/xbloom-studio-brew"

# Basename-only wheel name: xbloom_studio_core-<version>-<tags>.whl
_CORE_WHEEL_NAME_RE = re.compile(
    r"^xbloom_studio_core-(?P<version>[^-]+)-.+\.whl$"
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseMetaError(SystemExit):
    """Fatal error while reading or validating vendor/release.json."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_release_meta(skill_root: Path | None = None) -> dict[str, object] | None:
    """Load ``vendor/release.json`` when present.

    Returns ``None`` when the file is absent (development / unpackaged Skill).
    When the file exists, parse and validate strictly; never soft-fail or fall
    back on malformed metadata.
    """

    root = ROOT if skill_root is None else Path(skill_root)
    path = root / "vendor" / "release.json"
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseMetaError(
            f"cannot read vendor/release.json: {path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ReleaseMetaError(
            f"malformed vendor/release.json (invalid JSON): {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ReleaseMetaError(
            f"vendor/release.json must be a JSON object, got {type(data).__name__}"
        )
    return _validate_release_meta(data, skill_root=root, meta_path=path)


def _validate_release_meta(
    data: dict[str, object],
    *,
    skill_root: Path,
    meta_path: Path,
) -> dict[str, object]:
    """Fail closed on missing/wrong types, layout, version, wheel path, or hash."""

    layout = data.get("layout")
    if layout != "release":
        raise ReleaseMetaError(
            f"{meta_path}: layout must be the string 'release', got {layout!r}"
        )

    core_version = data.get("core_version")
    if not isinstance(core_version, str) or not core_version.strip():
        raise ReleaseMetaError(
            f"{meta_path}: core_version must be a non-empty string"
        )
    core_version = core_version.strip()

    version = data.get("version")
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            raise ReleaseMetaError(
                f"{meta_path}: version must be a non-empty string when present"
            )
        if version.strip() != core_version:
            raise ReleaseMetaError(
                f"{meta_path}: version {version!r} does not match "
                f"core_version {core_version!r}"
            )

    core_wheel = data.get("core_wheel")
    if not isinstance(core_wheel, str) or not core_wheel:
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel must be a non-empty string"
        )
    _assert_safe_core_wheel_basename(core_wheel, core_version, meta_path=meta_path)

    core_wheel_sha256 = data.get("core_wheel_sha256")
    if not isinstance(core_wheel_sha256, str) or not _SHA256_HEX_RE.fullmatch(
        core_wheel_sha256
    ):
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel_sha256 must be a 64-char lowercase hex string"
        )

    # Optional skill name, when present, must be a string.
    skill = data.get("skill")
    if skill is not None and not isinstance(skill, str):
        raise ReleaseMetaError(f"{meta_path}: skill must be a string when present")

    # Resolve wheel as a direct child of vendor/wheels (reject traversal / abs).
    wheels_dir = (skill_root / "vendor" / "wheels").resolve()
    if not wheels_dir.is_dir():
        raise ReleaseMetaError(
            f"{meta_path}: vendor/wheels directory missing: {wheels_dir}"
        )
    candidate = (wheels_dir / core_wheel).resolve()
    if candidate.parent != wheels_dir:
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel resolves outside vendor/wheels: {core_wheel!r}"
        )
    if not candidate.is_file():
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel file not found: {candidate.name}"
        )
    # Ambiguity rule: exactly one core wheel may match the declared version.
    matches = sorted(wheels_dir.glob(f"xbloom_studio_core-{core_version}-*.whl"))
    if len(matches) == 0:
        raise ReleaseMetaError(
            f"{meta_path}: no wheel matching "
            f"xbloom_studio_core-{core_version}-*.whl under vendor/wheels"
        )
    if len(matches) > 1:
        names = [path.name for path in matches]
        raise ReleaseMetaError(
            f"{meta_path}: ambiguous core wheels for version {core_version}: "
            f"{names}; expected exactly one matching wheel"
        )
    if matches[0].name != core_wheel:
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel {core_wheel!r} does not match the unique "
            f"wheel under vendor/wheels: {matches[0].name!r}"
        )

    actual = _sha256_file(candidate)
    if actual != core_wheel_sha256:
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel_sha256 mismatch for {core_wheel}: "
            f"expected {core_wheel_sha256}, got {actual} "
            f"(refusing to install a tampered wheel)"
        )

    runtime_lock = data.get("runtime_lock")
    if not isinstance(runtime_lock, str) or not runtime_lock:
        raise ReleaseMetaError(
            f"{meta_path}: runtime_lock must be a non-empty string"
        )
    _assert_safe_runtime_lock_basename(runtime_lock, meta_path=meta_path)

    runtime_lock_sha256 = data.get("runtime_lock_sha256")
    if not isinstance(runtime_lock_sha256, str) or not _SHA256_HEX_RE.fullmatch(
        runtime_lock_sha256
    ):
        raise ReleaseMetaError(
            f"{meta_path}: runtime_lock_sha256 must be a 64-char lowercase hex string"
        )

    # Resolve lock as a direct child of the Skill root (reject traversal / abs).
    skill_root_resolved = skill_root.resolve()
    lock_candidate = (skill_root / runtime_lock).resolve()
    if lock_candidate.parent != skill_root_resolved:
        raise ReleaseMetaError(
            f"{meta_path}: runtime_lock resolves outside Skill root: {runtime_lock!r}"
        )
    if not lock_candidate.is_file():
        raise ReleaseMetaError(
            f"{meta_path}: runtime_lock file not found: {lock_candidate.name}"
        )
    lock_actual = _sha256_file(lock_candidate)
    if lock_actual != runtime_lock_sha256:
        raise ReleaseMetaError(
            f"{meta_path}: runtime_lock_sha256 mismatch for {runtime_lock}: "
            f"expected {runtime_lock_sha256}, got {lock_actual} "
            f"(refusing to install from a tampered lock)"
        )

    # Return a cleaned copy with normalized strings.
    cleaned = dict(data)
    cleaned["layout"] = "release"
    cleaned["core_version"] = core_version
    cleaned["core_wheel"] = core_wheel
    cleaned["core_wheel_sha256"] = core_wheel_sha256
    cleaned["runtime_lock"] = runtime_lock
    cleaned["runtime_lock_sha256"] = runtime_lock_sha256
    if version is not None:
        cleaned["version"] = core_version
    return cleaned


def _assert_safe_basename_field(
    name: str, field: str, *, meta_path: Path
) -> None:
    """Reject path separators, absolute paths, traversal, and hidden basenames."""

    if not name or name.strip() != name:
        raise ReleaseMetaError(
            f"{meta_path}: {field} must be a basename with no surrounding whitespace"
        )
    if name != Path(name).name:
        raise ReleaseMetaError(
            f"{meta_path}: {field} must be basename-only, got {name!r}"
        )
    if "/" in name or "\\" in name:
        raise ReleaseMetaError(
            f"{meta_path}: {field} must not contain path separators, got {name!r}"
        )
    if ".." in name or name.startswith(".") or name.startswith("~"):
        raise ReleaseMetaError(
            f"{meta_path}: {field} rejects traversal/hidden names, got {name!r}"
        )
    # Reject absolute Windows/Unix forms that Path.name alone may not catch.
    if re.match(r"^[A-Za-z]:", name) or name.startswith(("/", "\\")):
        raise ReleaseMetaError(
            f"{meta_path}: {field} must not be absolute, got {name!r}"
        )


def _assert_safe_core_wheel_basename(
    name: str, version: str, *, meta_path: Path
) -> None:
    """Reject path separators, absolute paths, traversal, and pattern mismatch."""

    _assert_safe_basename_field(name, "core_wheel", meta_path=meta_path)
    match = _CORE_WHEEL_NAME_RE.fullmatch(name)
    if match is None:
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel must match "
            f"xbloom_studio_core-<version>-*.whl, got {name!r}"
        )
    if match.group("version") != version:
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel version segment {match.group('version')!r} "
            f"does not match core_version {version!r}"
        )
    expected_prefix = f"xbloom_studio_core-{version}-"
    if not (name.startswith(expected_prefix) and name.endswith(".whl")):
        raise ReleaseMetaError(
            f"{meta_path}: core_wheel must match "
            f"{expected_prefix}*.whl, got {name!r}"
        )


def _assert_safe_runtime_lock_basename(name: str, *, meta_path: Path) -> None:
    """Require the fixed lock basename under the Skill root (no path tricks)."""

    _assert_safe_basename_field(name, "runtime_lock", meta_path=meta_path)
    if name != RUNTIME_LOCK_BASENAME:
        raise ReleaseMetaError(
            f"{meta_path}: runtime_lock must be exactly {RUNTIME_LOCK_BASENAME!r}, "
            f"got {name!r}"
        )


def _release_core_version(skill_root: Path | None = None) -> str | None:
    """Return the pinned core version from release metadata or requirements.txt."""

    root = ROOT if skill_root is None else Path(skill_root)
    meta = _load_release_meta(root)
    if meta is not None:
        value = meta.get("core_version")
        if isinstance(value, str) and value.strip():
            return value.strip()
    requirements = root / "requirements.txt"
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            lower = stripped.lower().replace(" ", "")
            if lower.startswith("xbloom-studio-core=="):
                return stripped.split("==", 1)[1].strip()
    return None


def _release_core_wheel(skill_root: Path | None = None) -> Path | None:
    """Return the vendored core wheel path for a release layout.

    When ``vendor/release.json`` exists it is the sole source of truth (strict
    parse, hash check, basename-only resolution). There is no soft fallback if
    that metadata is present but invalid. When metadata is absent, fall back to
    a unique matching wheel under ``vendor/wheels/``.
    """

    root = ROOT if skill_root is None else Path(skill_root)
    wheels_dir = root / "vendor" / "wheels"
    meta = _load_release_meta(root)
    if meta is not None:
        # Validated above: basename-only, direct child, hash-checked.
        named = meta["core_wheel"]
        assert isinstance(named, str)
        return (wheels_dir / named).resolve()

    if not wheels_dir.is_dir():
        return None
    version = _release_core_version(root)
    if version:
        matches = sorted(wheels_dir.glob(f"xbloom_studio_core-{version}-*.whl"))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ReleaseMetaError(
                f"ambiguous xbloom_studio_core-{version}-*.whl under {wheels_dir}; "
                f"add vendor/release.json with core_wheel + core_wheel_sha256"
            )
    matches = sorted(wheels_dir.glob("xbloom_studio_core-*.whl"))
    if len(matches) == 1:
        return matches[0]
    return None


def _release_runtime_lock(skill_root: Path | None = None) -> Path | None:
    """Return the universal hashed runtime lock path for a release layout.

    When ``vendor/release.json`` exists, validation has already checked
    basename, parent, existence, and SHA-256. Without metadata, return the
    fixed basename under the Skill root when present (install still requires
    valid release.json).
    """

    root = ROOT if skill_root is None else Path(skill_root)
    meta = _load_release_meta(root)
    if meta is not None:
        named = meta["runtime_lock"]
        assert isinstance(named, str)
        return (root / named).resolve()
    candidate = root / RUNTIME_LOCK_BASENAME
    if candidate.is_file():
        return candidate.resolve()
    return None


def _normalize_state_root(path: Path | str) -> Path:
    """Match packages/core/xbloom_paths.normalize_state_root (stdlib-only)."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return candidate.absolute()


def _skill_state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV) or os.environ.get(LEGACY_STATE_DIR_ENV)
    if configured:
        return _normalize_state_root(configured)
    return _normalize_state_root(Path.home() / DEFAULT_STATE_DIRNAME)


def _skill_runtime_dir() -> Path:
    configured = os.environ.get(RUNTIME_DIR_ENV)
    if configured:
        return _normalize_state_root(configured)
    return _skill_state_dir() / "runtime"


def _runtime_python_path(runtime_dir: Path) -> Path:
    if os.name == "nt":
        return Path(runtime_dir) / "Scripts" / "python.exe"
    return Path(runtime_dir) / "bin" / "python"


def _environment_copy() -> dict[str, str]:
    return dict(os.environ)


def is_release_layout(skill_root: Path | None = None) -> bool:
    """True when release evidence marks this Skill as a release bundle.

    Either ``vendor/release.json`` or a vendored ``xbloom_studio_core-*.whl``
    counts as release evidence. Metadata alone remains authoritative when the
    named wheel is missing or deleted; a wheel alone (damaged bundle missing
    release.json) also stays classified as release so bootstrap never falls
    through to development/PyPI core installation. Install still requires
    valid release.json (see ``_install_release``).
    """

    root = ROOT if skill_root is None else Path(skill_root)
    if (root / "vendor" / "release.json").is_file():
        return True
    wheels = root / "vendor" / "wheels"
    if wheels.is_dir() and any(wheels.glob("xbloom_studio_core-*.whl")):
        return True
    return False


def is_dev_requirements(skill_root: Path | None = None) -> bool:
    """True when requirements.txt requests an editable sibling core checkout."""

    root = ROOT if skill_root is None else Path(skill_root)
    requirements = root / "requirements.txt"
    if not requirements.is_file():
        return False
    for line in requirements.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("-e ") and "packages/core" in stripped.replace("\\", "/"):
            return True
    return False


def monorepo_core_path(skill_root: Path | None = None) -> Path | None:
    """Return sibling ``packages/core`` when this Skill lives in the brew monorepo.

    Hermes/skills.sh installs only the skill folder (no ``../../packages/core``),
    so a requirements line of ``-e ../../packages/core`` must not be treated as
    a usable development layout unless that path actually exists.
    """

    root = ROOT if skill_root is None else Path(skill_root)
    candidate = (root / ".." / ".." / "packages" / "core").resolve()
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def _load_hub_pin(skill_root: Path | None = None) -> dict[str, object] | None:
    """Load ``vendor/hub-pin.json`` when present (Hermes/hub standalone install)."""

    root = ROOT if skill_root is None else Path(skill_root)
    path = root / "vendor" / "hub-pin.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseMetaError(f"malformed vendor/hub-pin.json: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseMetaError("vendor/hub-pin.json must be a JSON object")
    layout = data.get("layout")
    if layout != "hub-pin":
        raise ReleaseMetaError(
            f"vendor/hub-pin.json layout must be 'hub-pin', got {layout!r}"
        )
    for key in (
        "core_version",
        "core_wheel",
        "core_wheel_sha256",
        "core_wheel_url",
        "runtime_lock",
        "runtime_lock_sha256",
    ):
        val = data.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ReleaseMetaError(
                f"vendor/hub-pin.json missing non-empty string {key!r}"
            )
    digest = str(data["core_wheel_sha256"]).strip().lower()
    if not _SHA256_HEX_RE.match(digest):
        raise ReleaseMetaError("vendor/hub-pin.json core_wheel_sha256 must be 64 hex chars")
    lock_digest = str(data["runtime_lock_sha256"]).strip().lower()
    if not _SHA256_HEX_RE.match(lock_digest):
        raise ReleaseMetaError(
            "vendor/hub-pin.json runtime_lock_sha256 must be 64 hex chars"
        )
    data = dict(data)
    data["core_wheel_sha256"] = digest
    data["runtime_lock_sha256"] = lock_digest
    return data


def _download_file(url: str, dest: Path) -> None:
    """Download *url* to *dest* using stdlib only (no core import)."""

    import urllib.error
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 — pin URL from hub-pin
            data = resp.read()
        tmp.write_bytes(data)
        tmp.replace(dest)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise SystemExit(f"failed to download {url}: {exc}") from exc


def _install_hub_pin(python: str) -> None:
    """Install core from a pinned GitHub Release wheel + hashed runtime lock.

    Used when the Skill is installed outside the monorepo (Hermes/skills.sh)
    without a full release bundle under ``vendor/wheels/``.
    """

    pin = _load_hub_pin()
    if pin is None:
        raise SystemExit("hub-pin install requires vendor/hub-pin.json")

    core_version = str(pin["core_version"]).strip()
    wheel_name = str(pin["core_wheel"]).strip()
    wheel_url = str(pin["core_wheel_url"]).strip()
    wheel_sha = str(pin["core_wheel_sha256"]).strip().lower()
    lock_name = str(pin["runtime_lock"]).strip()
    lock_sha = str(pin["runtime_lock_sha256"]).strip().lower()

    if lock_name != RUNTIME_LOCK_BASENAME:
        raise SystemExit(
            f"hub-pin runtime_lock must be {RUNTIME_LOCK_BASENAME!r}, got {lock_name!r}"
        )
    lock = ROOT / RUNTIME_LOCK_BASENAME
    if not lock.is_file():
        raise SystemExit(f"hub-pin layout missing {RUNTIME_LOCK_BASENAME} under Skill root")
    actual_lock = _sha256_file(lock).lower()
    if actual_lock != lock_sha:
        raise SystemExit(
            f"requirements-runtime.lock hash mismatch: expected {lock_sha}, got {actual_lock}"
        )

    cache_dir = _skill_state_dir() / "cache" / "wheels"
    cache_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = cache_dir / wheel_name
    if wheel_path.is_file() and _sha256_file(wheel_path).lower() == wheel_sha:
        print(f"Using cached core wheel {wheel_path.name}")
    else:
        print(f"Downloading core wheel from {wheel_url}")
        _download_file(wheel_url, wheel_path)
        got = _sha256_file(wheel_path).lower()
        if got != wheel_sha:
            try:
                wheel_path.unlink()
            except OSError:
                pass
            raise SystemExit(
                f"downloaded wheel hash mismatch: expected {wheel_sha}, got {got}"
            )

    print(f"Installing core from {wheel_path.name} (xbloom-studio-core=={core_version})")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            str(wheel_path),
        ]
    )
    print(f"Installing non-core runtime deps from {lock.name} (--require-hashes)")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary",
            ":all:",
            "--require-hashes",
            "-r",
            str(lock),
        ]
    )


def venv_python(runtime: Path | None = None) -> Path:
    target = _skill_runtime_dir() if runtime is None else runtime
    return _runtime_python_path(target)


def run(args: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def _install_release(python: str) -> None:
    """Install the bundled core wheel offline, then hashed non-core deps.

    Valid ``vendor/release.json`` is mandatory (wheel + universal runtime lock
    fields, basenames, hashes). A damaged bundle that still has a vendored core
    wheel is classified as release, but install aborts before any pip invocation
    when metadata is missing or invalid (no unhashed fallback wheel, no
    requirements.txt line-to-args fallback, no development/PyPI core fall-through).
    """

    # Require validated release metadata (wheel + lock) before any pip/run call.
    meta = _load_release_meta()
    if meta is None:
        raise SystemExit(
            "release layout requires vendor/release.json with core_wheel + "
            "core_wheel_sha256 + runtime_lock + runtime_lock_sha256; "
            "refusing unhashed wheel install and development/PyPI fall-through"
        )

    wheels = VENDOR_WHEELS
    if not wheels.is_dir():
        raise SystemExit(f"release layout missing vendor wheels directory: {wheels}")

    # Strict release.json is required; wheel + lock hashes checked in meta load.
    wheel = _release_core_wheel()
    lock = _release_runtime_lock()
    version = _release_core_version()
    if wheel is None:
        raise SystemExit(
            f"release layout missing xbloom_studio_core wheel under {wheels}"
        )
    if lock is None:
        raise SystemExit(
            f"release layout missing {RUNTIME_LOCK_BASENAME} under Skill root"
        )
    if version is None:
        raise SystemExit(
            "release layout missing core version "
            f"(vendor/release.json or xbloom-studio-core==... in requirements.txt)"
        )

    # 1) Exact vendored core wheel (offline, no deps, no index / name resolution).
    print(f"Installing core from {wheel.name} (xbloom-studio-core=={version})")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            str(wheel),
        ]
    )
    # 2) Non-core runtime deps only via the universal hashed lock.
    # Never parse lock/requirements lines into positional pip args.
    # Never fall back to unhashed requirements.txt or PyPI core.
    # --only-binary :all: avoids unhashed PEP 517 build deps (all locked
    # packages publish wheels for supported platforms).
    print(f"Installing non-core runtime deps from {lock.name} (--require-hashes)")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary",
            ":all:",
            "--require-hashes",
            "-r",
            str(lock),
        ]
    )


def _install_dev(python: str, *, dev: bool) -> None:
    requirement = ROOT / ("requirements-dev.txt" if dev else "requirements.txt")
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirement),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also install pytest and run tests")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="override the external runtime directory for this bootstrap",
    )
    args = parser.parse_args()
    runtime = (args.runtime_dir or _skill_runtime_dir()).expanduser().resolve()

    if not venv_python(runtime).exists():
        print(f"Creating external runtime at {runtime}")
        venv.EnvBuilder(with_pip=True).create(runtime)

    python = str(venv_python(runtime))
    release = is_release_layout()
    monorepo = monorepo_core_path()
    hub_pin = None if release or monorepo is not None else _load_hub_pin()

    if release:
        print(f"Release layout detected (vendor wheels at {VENDOR_WHEELS})")
        _install_release(python)
        if args.dev:
            # Dev extras only (pytest); core already installed from the wheel.
            run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "pytest==9.1.1",
                ]
            )
        mode = "release"
    elif monorepo is not None:
        print(f"Development layout detected (editable core at {monorepo})")
        _install_dev(python, dev=args.dev)
        mode = "development"
    elif hub_pin is not None:
        print(
            "Hub/standalone layout detected "
            f"(vendor/hub-pin.json → core {hub_pin.get('core_version')})"
        )
        print(
            "Note: requirements.txt '-e ../../packages/core' is monorepo-only "
            "and is ignored here (Hermes/skills.sh installs the skill folder alone)."
        )
        _install_hub_pin(python)
        if args.dev:
            run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "pytest==9.1.1",
                ]
            )
        mode = "hub-pin"
    else:
        raise SystemExit(
            "cannot bootstrap this Skill install:\n"
            "  - not a release bundle (missing vendor/release.json + wheel),\n"
            "  - not a monorepo checkout (missing ../../packages/core),\n"
            "  - missing vendor/hub-pin.json for Hermes/hub installs.\n"
            "Fix: reinstall from a skill-*.zip release, clone the monorepo, "
            "or ensure vendor/hub-pin.json is present and network can reach "
            f"GitHub Releases for {DEFAULT_RELEASE_REPO}."
        )

    runtime_env = _environment_copy()
    runtime_env[RUNTIME_DIR_ENV] = str(runtime)
    run(
        [python, str(ROOT / "scripts" / "xbloom.py"), "doctor"],
        env=runtime_env,
    )
    if args.dev:
        subprocess.run(
            [python, "-m", "pytest", "-q"], cwd=ROOT, env=runtime_env, check=True
        )

    if args.runtime_dir is not None:
        print(
            f"Persist {RUNTIME_DIR_ENV}={runtime} for future CLI and bridge calls."
        )
    print(f"Bootstrap complete ({mode}). Run: python scripts/xbloom.py scan")


if __name__ == "__main__":
    main()
