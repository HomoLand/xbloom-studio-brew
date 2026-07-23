#!/usr/bin/env python3
"""Build GitHub Release artifacts for xbloom-studio-brew (not PyPI).

Produces under ``dist/``:

1. ``xbloom_studio_core-<version>-*.whl`` - installable core wheel
2. ``knowledge-<version>/`` + ``knowledge-<version>.zip`` - versioned knowledge
   bundle generated from the Skill's single source (``SKILL.md``,
   ``references/``, ``assets/``) with ``manifest.json`` (per-file SHA-256 and
   aggregate content hash)
3. ``skill-xbloom-studio-brew-<version>/`` + ``.zip`` - self-contained Skill
   release carrying the exact core wheel under ``vendor/wheels/``
4. ``release-manifest.json`` - deterministic name/version/size/SHA-256 for
   every publishable wheel/ZIP (excludes the manifest itself)

Builds are intended to be byte-for-byte reproducible for the wheel and both
ZIPs when ``SOURCE_DATE_EPOCH`` is fixed (default below) and the pinned
setuptools build backend is used.

Usage (from repository root):

```text
python tools/build_release.py
python tools/build_release.py --out dist
```
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "packages" / "core"
SKILL_DIR = REPO_ROOT / "skills" / "xbloom-studio-brew"
DEFAULT_OUT = REPO_ROOT / "dist"
RUNTIME_LOCK_BASENAME = "requirements-runtime.lock"
RUNTIME_LOCK_PATH = SKILL_DIR / RUNTIME_LOCK_BASENAME

# Fixed epoch for reproducible wheel/ZIP timestamps when the environment does
# not already provide SOURCE_DATE_EPOCH (Unix time, 2024-01-01T00:00:00Z).
DEFAULT_SOURCE_DATE_EPOCH = 1704067200
ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
ZIP_COMPRESS_LEVEL = 9
# Regular file mode 0o644 encoded in the high 16 bits of external_attr (Unix).
ZIP_UNIX_FILE_MODE = 0o100644 << 16
RELEASE_MANIFEST_NAME = "release-manifest.json"
RELEASE_MANIFEST_SCHEMA = "xbloom-studio-brew-release-manifest/v1"
RELEASE_MANIFEST_TOP_KEYS = frozenset({"schema", "version", "artifacts"})
RELEASE_MANIFEST_ENTRY_KEYS = frozenset({"name", "version", "size", "sha256"})

SKILL_RELEASE_EXCLUDE_DIR_NAMES = {
    ".venv",
    ".pytest_cache",
    "__pycache__",
    ".git",
    "vendor",
}
SKILL_RELEASE_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".tmp"}


def source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None or raw.strip() == "":
        return DEFAULT_SOURCE_DATE_EPOCH
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"SOURCE_DATE_EPOCH must be an integer Unix timestamp, got {raw!r}"
        ) from exc
    if value < 0:
        raise RuntimeError(f"SOURCE_DATE_EPOCH must be non-negative, got {value}")
    return value


def zip_date_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    """Convert SOURCE_DATE_EPOCH to a ZIP date_time tuple (local ZIP fields)."""

    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    # ZIP stores local civil time; use UTC components for stability across hosts.
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_knowledge_module():
    """Load ``xbloom_knowledge`` without requiring core to be installed."""

    path = CORE_DIR / "xbloom_knowledge.py"
    spec = importlib.util.spec_from_file_location("xbloom_knowledge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load knowledge module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_core_version() -> str:
    text = (CORE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("packages/core/pyproject.toml is missing version")
    return match.group(1)


def build_core_wheel(out_dir: Path, *, epoch: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Drop stale core wheels so dist/ only holds the artifact for this build.
    for stale in out_dir.glob("xbloom_studio_core-*.whl"):
        stale.unlink()
    version = read_core_version()
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(epoch)
    env.setdefault("PYTHONHASHSEED", "0")
    # Force a clean, isolated build so the pinned setuptools from pyproject.toml
    # is used rather than whatever happens to be installed in the outer env.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(out_dir),
            str(CORE_DIR),
        ],
        check=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    matching = sorted(out_dir.glob(f"xbloom_studio_core-{version}-*.whl"))
    if not matching:
        raise RuntimeError(
            f"core wheel for version {version} was not produced in {out_dir}"
        )
    if len(matching) > 1:
        # Keep the last sorted match; remove the rest to avoid ambiguity.
        for extra in matching[:-1]:
            extra.unlink()
        return matching[-1]
    return matching[0]


def write_deterministic_zip(archive: Path, root: Path, *, epoch: int) -> None:
    """Write a ZIP with stable ordering, timestamps, modes, and compression."""

    if archive.exists():
        archive.unlink()
    date_time = zip_date_time(epoch)
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=ZIP_COMPRESSION,
        compresslevel=ZIP_COMPRESS_LEVEL,
    ) as zf:
        for path in files:
            arcname = path.relative_to(root).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(filename=arcname, date_time=date_time)
            info.compress_type = ZIP_COMPRESSION
            info.create_system = 3  # Unix
            info.external_attr = ZIP_UNIX_FILE_MODE
            zf.writestr(info, data, compress_type=ZIP_COMPRESSION, compresslevel=ZIP_COMPRESS_LEVEL)


def build_knowledge_bundle(
    out_dir: Path, version: str, knowledge, *, epoch: int
) -> Path:
    bundle_dir = out_dir / f"knowledge-{version}"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    knowledge.copy_knowledge_tree(SKILL_DIR, bundle_dir)
    manifest = knowledge.build_manifest(
        bundle_dir,
        version=version,
        core_version=version,
    )
    knowledge.write_manifest(bundle_dir / knowledge.MANIFEST_NAME, manifest)
    # Round-trip validation before packaging.
    knowledge.validate_bundle(bundle_dir, expected_version=version)

    archive = out_dir / f"knowledge-{version}.zip"
    write_deterministic_zip(archive, bundle_dir, epoch=epoch)
    return bundle_dir


def _should_copy_skill_path(rel: Path) -> bool:
    parts = rel.parts
    if any(part in SKILL_RELEASE_EXCLUDE_DIR_NAMES for part in parts):
        return False
    if rel.suffix in SKILL_RELEASE_EXCLUDE_SUFFIXES:
        return False
    return True


def require_runtime_lock() -> Path:
    """Require the committed universal hashed runtime lock before packaging."""

    if not RUNTIME_LOCK_PATH.is_file():
        raise RuntimeError(
            f"missing committed runtime lock: {RUNTIME_LOCK_PATH}\n"
            "Generate with: python tools/update_runtime_lock.py --update\n"
            "Verify with:   python tools/update_runtime_lock.py --check"
        )
    text = RUNTIME_LOCK_PATH.read_text(encoding="utf-8")
    if "xbloom-studio-core" in text.lower() or "xbloom_studio_core" in text.lower():
        raise RuntimeError(
            f"{RUNTIME_LOCK_PATH.name} must exclude xbloom-studio-core "
            "(core is the vendored wheel, not a lock entry)"
        )
    if "--hash=sha256:" not in text:
        raise RuntimeError(
            f"{RUNTIME_LOCK_PATH.name} must contain sha256 hashes "
            "(regenerate with uv --generate-hashes)"
        )
    # Require at least the direct core deps and one platform-marker family.
    lowered = text.lower()
    for required in ("bleak==", "pyyaml==", "typing-extensions=="):
        if required not in lowered:
            raise RuntimeError(
                f"{RUNTIME_LOCK_PATH.name} missing expected pin: {required}"
            )
    if "sys_platform == 'linux'" not in text and 'sys_platform == "linux"' not in text:
        raise RuntimeError(
            f"{RUNTIME_LOCK_PATH.name} missing Linux platform markers (dbus-fast)"
        )
    if "sys_platform == 'darwin'" not in text and 'sys_platform == "darwin"' not in text:
        raise RuntimeError(
            f"{RUNTIME_LOCK_PATH.name} missing macOS platform markers (PyObjC)"
        )
    if "sys_platform == 'win32'" not in text and 'sys_platform == "win32"' not in text:
        raise RuntimeError(
            f"{RUNTIME_LOCK_PATH.name} missing Windows platform markers (WinRT)"
        )
    return RUNTIME_LOCK_PATH


def build_skill_bundle(
    out_dir: Path, version: str, wheel: Path, *, epoch: int, runtime_lock: Path
) -> Path:
    bundle_dir = out_dir / f"skill-xbloom-studio-brew-{version}"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(SKILL_DIR)
        if not _should_copy_skill_path(rel):
            continue
        dest = bundle_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    # Assert the universal hashed lock is present in the bundle (normal tree
    # copy should include it; fail closed if the exclude rules ever drop it).
    bundled_lock = bundle_dir / RUNTIME_LOCK_BASENAME
    if not bundled_lock.is_file():
        shutil.copy2(runtime_lock, bundled_lock)
    if not bundled_lock.is_file():
        raise RuntimeError(
            f"Skill bundle missing {RUNTIME_LOCK_BASENAME} after copy"
        )
    lock_bytes = runtime_lock.read_bytes()
    if bundled_lock.read_bytes() != lock_bytes:
        raise RuntimeError(
            f"Skill bundle {RUNTIME_LOCK_BASENAME} is not byte-identical to "
            f"the committed lock at {runtime_lock}"
        )
    lock_sha256 = sha256_file(bundled_lock)

    # Release requirements.txt is source-identity only: exact core version for
    # humans/tools. Bootstrap never pip-installs this file for non-core deps
    # (those come only from requirements-runtime.lock with --require-hashes).
    # Do not list unhashed bleak/PyYAML pins that would form a second contract.
    requirements = (
        f"# Release requirements for xbloom-studio-brew {version}\n"
        f"#\n"
        f"# Source identity: exact core version for this Skill release.\n"
        f"# Bootstrap does NOT use this file to install packages in release layout.\n"
        f"#\n"
        f"# Core: exact wheel under vendor/wheels/ via\n"
        f"#   pip install --no-deps --no-index <wheel>\n"
        f"# after verifying core_wheel_sha256 in vendor/release.json.\n"
        f"#\n"
        f"# Non-core runtime dependencies (bleak, PyYAML, platform wheels):\n"
        f"#   pip install --only-binary :all: --require-hashes -r {RUNTIME_LOCK_BASENAME}\n"
        f"# Do not install unhashed pins from this file; do not resolve core from PyPI.\n"
        f"xbloom-studio-core=={version}\n"
    )
    (bundle_dir / "requirements.txt").write_text(requirements, encoding="utf-8")

    # Dev extras only; release bootstrap --dev installs pytest after the
    # integrity-bound runtime (does not pip -r this file for core/runtime).
    requirements_dev = (
        f"# Development extras for an extracted Skill release.\n"
        f"# Runtime: bootstrap installs core wheel + {RUNTIME_LOCK_BASENAME} first.\n"
        f"# Then: pip install pytest==9.1.1  (or: python scripts/bootstrap.py --dev)\n"
        f"pytest==9.1.1\n"
    )
    (bundle_dir / "requirements-dev.txt").write_text(requirements_dev, encoding="utf-8")

    vendor_wheels = bundle_dir / "vendor" / "wheels"
    vendor_wheels.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheel, vendor_wheels / wheel.name)

    # Record release metadata for bootstrap/doctor diagnostics (strictly parsed).
    meta = {
        "skill": "xbloom-studio-brew",
        "version": version,
        "core_version": version,
        "core_wheel": wheel.name,
        "core_wheel_sha256": sha256_file(wheel),
        "runtime_lock": RUNTIME_LOCK_BASENAME,
        "runtime_lock_sha256": lock_sha256,
        "layout": "release",
    }
    (bundle_dir / "vendor" / "release.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    archive = out_dir / f"skill-xbloom-studio-brew-{version}.zip"
    write_deterministic_zip(archive, bundle_dir, epoch=epoch)
    return bundle_dir


def collect_publishable_artifacts(out_dir: Path, version: str) -> list[Path]:
    """Return sorted publishable wheel/ZIP paths (excludes release-manifest)."""

    names = [
        # Prefer the exact wheel name produced for this version.
        *[
            path.name
            for path in sorted(out_dir.glob(f"xbloom_studio_core-{version}-*.whl"))
        ],
        f"knowledge-{version}.zip",
        f"skill-xbloom-studio-brew-{version}.zip",
    ]
    # Deduplicate while preserving sorted order by name.
    seen: set[str] = set()
    artifacts: list[Path] = []
    for name in sorted(set(names)):
        if name in seen:
            continue
        seen.add(name)
        path = out_dir / name
        if not path.is_file():
            raise RuntimeError(f"missing publishable artifact: {path}")
        artifacts.append(path)
    if not any(p.name.endswith(".whl") for p in artifacts):
        raise RuntimeError(f"no core wheel found under {out_dir}")
    return sorted(artifacts, key=lambda p: p.name)


def write_release_manifest(out_dir: Path, version: str, artifacts: list[Path]) -> Path:
    """Emit deterministic release-manifest.json for publishable artifacts."""

    entries = []
    for path in sorted(artifacts, key=lambda p: p.name):
        entries.append(
            {
                "name": path.name,
                "version": version,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    payload = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "version": version,
        "artifacts": entries,
    }
    manifest_path = out_dir / RELEASE_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def verify_release_manifest(out_dir: Path, manifest_path: Path | None = None) -> dict:
    """Verify schema, closed publishable set, and per-artifact size/SHA-256.

    The closed set for a version is exactly:
    - one ``xbloom_studio_core-<version>-*.whl``
    - ``knowledge-<version>.zip``
    - ``skill-xbloom-studio-brew-<version>.zip``
    with no missing or unexpected entries.
    """

    path = (
        Path(manifest_path)
        if manifest_path is not None
        else out_dir / RELEASE_MANIFEST_NAME
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"missing release manifest: {path}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid release manifest JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("release manifest must be a JSON object")

    top_keys = set(data)
    if top_keys != RELEASE_MANIFEST_TOP_KEYS:
        missing = sorted(RELEASE_MANIFEST_TOP_KEYS - top_keys)
        unexpected = sorted(top_keys - RELEASE_MANIFEST_TOP_KEYS)
        raise RuntimeError(
            "release manifest top-level keys must be exactly "
            f"{sorted(RELEASE_MANIFEST_TOP_KEYS)}; "
            f"missing={missing}; unexpected={unexpected}"
        )

    schema = data.get("schema")
    if schema != RELEASE_MANIFEST_SCHEMA:
        raise RuntimeError(
            f"release manifest schema must be {RELEASE_MANIFEST_SCHEMA!r}, "
            f"got {schema!r}"
        )
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("release manifest missing string version")
    version = version.strip()
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("release manifest missing artifacts list")

    seen_names: set[str] = set()
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise RuntimeError("release manifest artifact entry must be an object")
        entry_keys = set(entry)
        if entry_keys != RELEASE_MANIFEST_ENTRY_KEYS:
            missing = sorted(RELEASE_MANIFEST_ENTRY_KEYS - entry_keys)
            unexpected = sorted(entry_keys - RELEASE_MANIFEST_ENTRY_KEYS)
            raise RuntimeError(
                "release manifest artifact entry keys must be exactly "
                f"{sorted(RELEASE_MANIFEST_ENTRY_KEYS)}; "
                f"missing={missing}; unexpected={unexpected}"
            )
        name = entry.get("name")
        entry_version = entry.get("version")
        size = entry.get("size")
        digest = entry.get("sha256")
        if not isinstance(name, str) or not name or name != Path(name).name:
            raise RuntimeError(f"release manifest has unsafe artifact name: {name!r}")
        if "/" in name or "\\" in name or ".." in name:
            raise RuntimeError(f"release manifest artifact name must be basename-only: {name!r}")
        if name == RELEASE_MANIFEST_NAME:
            raise RuntimeError("release manifest must not list itself as an artifact")
        if name in seen_names:
            raise RuntimeError(f"duplicate artifact name in release manifest: {name}")
        seen_names.add(name)
        if entry_version != version:
            raise RuntimeError(
                f"artifact {name} version {entry_version!r} != manifest {version!r}"
            )
        # Reject JSON booleans: bool is a subclass of int in Python.
        if type(size) is not int or size < 0:
            raise RuntimeError(f"artifact {name} has invalid size: {size!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"artifact {name} has invalid sha256: {digest!r}")

        artifact_path = out_dir / name
        if not artifact_path.is_file():
            raise RuntimeError(f"release artifact missing on disk: {artifact_path}")
        actual_size = artifact_path.stat().st_size
        if actual_size != size:
            raise RuntimeError(
                f"artifact {name} size mismatch: manifest={size} disk={actual_size}"
            )
        actual_digest = sha256_file(artifact_path)
        if actual_digest != digest:
            raise RuntimeError(
                f"artifact {name} sha256 mismatch: "
                f"manifest={digest} disk={actual_digest}"
            )

    wheel_re = re.compile(
        rf"^xbloom_studio_core-{re.escape(version)}-.+\.whl$"
    )
    wheels = sorted(name for name in seen_names if wheel_re.fullmatch(name))
    if len(wheels) != 1:
        raise RuntimeError(
            f"release manifest must list exactly one core wheel for version "
            f"{version}, found {wheels}"
        )
    required = {
        wheels[0],
        f"knowledge-{version}.zip",
        f"skill-xbloom-studio-brew-{version}.zip",
    }
    if seen_names != required:
        missing = sorted(required - seen_names)
        unexpected = sorted(seen_names - required)
        raise RuntimeError(
            "release manifest artifacts must be exactly the publishable closed "
            f"set (one core wheel, knowledge-{version}.zip, "
            f"skill-xbloom-studio-brew-{version}.zip); "
            f"missing={missing}; unexpected={unexpected}"
        )
    return data


def write_release_notes(out_dir: Path, version: str, wheel: Path, knowledge_dir: Path) -> None:
    knowledge = _load_knowledge_module()
    manifest = knowledge.load_manifest(knowledge_dir / knowledge.MANIFEST_NAME)
    notes = out_dir / f"RELEASE-{version}.txt"
    lines = [
        f"xbloom-studio-brew {version}",
        "",
        "Artifacts (GitHub Releases, not PyPI):",
        f"  - {wheel.name}",
        f"  - knowledge-{version}.zip  (content_hash={manifest.get('content_hash')})",
        f"  - skill-xbloom-studio-brew-{version}.zip",
        f"  - {RELEASE_MANIFEST_NAME}  (name/version/size/sha256 per artifact)",
        "",
        "Install core wheel:",
        f"  pip install {wheel.name}",
        "",
        "Bootstrap extracted Skill release:",
        f"  unzip skill-xbloom-studio-brew-{version}.zip -d xbloom-studio-brew",
        "  cd xbloom-studio-brew",
        "  python scripts/bootstrap.py",
        "  python scripts/xbloom.py doctor",
        "  python scripts/xbloom.py validate assets/hot-template.yaml",
        "",
        "Dependency integrity notes:",
        "  - Core is the exact vendored wheel under vendor/wheels/ (pip install",
        "    --no-deps --no-index <wheel>) verified by core_wheel_sha256 in",
        "    vendor/release.json.",
        "  - All non-core runtime dependencies install with:",
        f"      pip install --only-binary :all: --require-hashes -r {RUNTIME_LOCK_BASENAME}",
        "    The universal lock is integrity-bound by runtime_lock_sha256 in",
        "    vendor/release.json (Linux dbus-fast / macOS PyObjC / Windows WinRT",
        "    markers in one file). Non-core install needs network unless wheels",
        "    are pre-cached.",
        "",
    ]
    notes.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="output directory for release artifacts (default: dist/)",
    )
    args = parser.parse_args(argv)
    out_dir = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    epoch = source_date_epoch()
    # Ensure child processes and this process agree even if the env was empty.
    os.environ["SOURCE_DATE_EPOCH"] = str(epoch)

    knowledge = _load_knowledge_module()
    version = read_core_version()
    print(f"Building release artifacts for version {version} -> {out_dir}")
    print(f"  SOURCE_DATE_EPOCH={epoch}")

    runtime_lock = require_runtime_lock()
    print(
        f"  runtime lock: {runtime_lock.relative_to(REPO_ROOT).as_posix()} "
        f"(sha256={sha256_file(runtime_lock)[:12]}...)"
    )

    wheel = build_core_wheel(out_dir, epoch=epoch)
    print(f"  core wheel: {wheel.name}")

    knowledge_dir = build_knowledge_bundle(out_dir, version, knowledge, epoch=epoch)
    print(f"  knowledge:  {knowledge_dir.name}/ and knowledge-{version}.zip")

    skill_dir = build_skill_bundle(
        out_dir, version, wheel, epoch=epoch, runtime_lock=runtime_lock
    )
    print(f"  skill:      {skill_dir.name}/ and skill-xbloom-studio-brew-{version}.zip")

    write_release_notes(out_dir, version, wheel, knowledge_dir)

    artifacts = collect_publishable_artifacts(out_dir, version)
    manifest_path = write_release_manifest(out_dir, version, artifacts)
    verify_release_manifest(out_dir, manifest_path)
    print(f"  manifest:   {manifest_path.name} ({len(artifacts)} artifacts, verified)")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
