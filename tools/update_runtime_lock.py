#!/usr/bin/env python3
"""Update or check the universal hashed runtime lock for xbloom-studio-brew.

The lock covers non-core runtime dependencies of ``packages/core`` (bleak,
PyYAML, and platform-specific transitive wheels). ``xbloom-studio-core`` itself
is excluded: release installs use the exact vendored wheel under
``vendor/wheels/``.

Pinned resolver
---------------
uv 0.11.28 (must match CI and this tool's ``UV_VERSION``).

Canonical compile command
-------------------------
::

    uv pip compile packages/core/pyproject.toml \\
        --universal --python-version 3.11 --generate-hashes \\
        --no-emit-package xbloom-studio-core --no-header --no-annotate -q

Tracked lock path
-----------------
``skills/xbloom-studio-brew/requirements-runtime.lock``

Usage (from repository root)
----------------------------
::

    python tools/update_runtime_lock.py --update
    python tools/update_runtime_lock.py --check

``--update`` re-resolves and overwrites the tracked lock.
``--check`` writes a temporary result (using the existing lock as constraints
for a deterministic re-resolve), byte-compares it to the tracked file, and
never mutates the tracked lock.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_PYPROJECT = REPO_ROOT / "packages" / "core" / "pyproject.toml"
LOCK_PATH = (
    REPO_ROOT / "skills" / "xbloom-studio-brew" / "requirements-runtime.lock"
)

# Keep in sync with .github/workflows (test.yml, release.yml).
UV_VERSION = "0.11.28"
PYTHON_VERSION = "3.11"

CANONICAL_COMPILE_ARGS = [
    "pip",
    "compile",
    str(CORE_PYPROJECT.relative_to(REPO_ROOT)).replace("\\", "/"),
    "--universal",
    "--python-version",
    PYTHON_VERSION,
    "--generate-hashes",
    "--no-emit-package",
    "xbloom-studio-core",
    "--no-header",
    "--no-annotate",
    # Quiet progress ("Resolved N packages") so CI/shells that treat stderr as
    # failure (e.g. PowerShell native error streams) stay deterministic.
    "-q",
]


def _find_uv() -> str:
    """Return path to ``uv``, preferring an on-PATH binary of the right version."""

    which = shutil.which("uv")
    if which is None:
        raise SystemExit(
            f"uv {UV_VERSION} is required but was not found on PATH. "
            f"Install: https://docs.astral.sh/uv/  "
            f'(example: pip install "uv=={UV_VERSION}")'
        )
    return which


def _uv_version(uv_bin: str) -> str:
    result = subprocess.run(
        [uv_bin, "--version"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # Examples: "uv 0.11.28 (...)" or "uv 0.11.28"
    text = (result.stdout or result.stderr or "").strip()
    parts = text.split()
    if len(parts) >= 2 and parts[0].lower() == "uv":
        return parts[1]
    raise SystemExit(f"unable to parse uv version from: {text!r}")


def _require_uv_version(uv_bin: str) -> None:
    actual = _uv_version(uv_bin)
    if actual != UV_VERSION:
        raise SystemExit(
            f"uv version mismatch: expected {UV_VERSION}, got {actual}. "
            f"Install the pinned resolver before updating or checking the lock."
        )


def _compile_to(out_path: Path, *, constraints: Path | None) -> None:
    uv_bin = _find_uv()
    _require_uv_version(uv_bin)
    if not CORE_PYPROJECT.is_file():
        raise SystemExit(f"missing core pyproject: {CORE_PYPROJECT}")

    cmd = [uv_bin, *CANONICAL_COMPILE_ARGS, "-o", str(out_path)]
    if constraints is not None:
        if not constraints.is_file():
            raise SystemExit(f"constraints lock missing: {constraints}")
        cmd.extend(["-c", str(constraints)])

    env = os.environ.copy()
    # Quiet progress noise; keep real errors on stderr.
    env.setdefault("UV_NO_PROGRESS", "1")
    print("Running:", " ".join(cmd), flush=True)
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(result.returncode)
    # Surface non-progress stderr only (quiet mode should leave this empty).
    if result.stderr and result.stderr.strip():
        # Filter known progress lines if a quieter uv still emits them.
        noise = ("Resolved ", "Downloading ", "Prepared ", "Installed ")
        residual = "\n".join(
            line
            for line in result.stderr.splitlines()
            if line.strip() and not line.startswith(noise)
        )
        if residual:
            print(residual, file=sys.stderr)


def cmd_update() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Intentional refresh: no constraints, free re-resolve from pyproject pins.
    _compile_to(LOCK_PATH, constraints=None)
    print(f"Updated {LOCK_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


def cmd_check() -> int:
    if not LOCK_PATH.is_file():
        raise SystemExit(
            f"tracked lock missing: {LOCK_PATH}\n"
            f"Run: python tools/update_runtime_lock.py --update"
        )

    with tempfile.TemporaryDirectory(prefix="xbloom-runtime-lock-") as tmp:
        temp_lock = Path(tmp) / "requirements-runtime.lock"
        # Deterministic re-resolve: existing lock pins transitive versions.
        _compile_to(temp_lock, constraints=LOCK_PATH)
        expected = LOCK_PATH.read_bytes()
        actual = temp_lock.read_bytes()
        if expected != actual:
            rel = LOCK_PATH.relative_to(REPO_ROOT).as_posix()
            print(
                f"runtime lock drift: {rel} does not match a fresh compile "
                f"with the tracked lock as constraints.\n"
                f"  tracked bytes: {len(expected)}\n"
                f"  fresh bytes:   {len(actual)}\n"
                f"Regenerate with: python tools/update_runtime_lock.py --update",
                file=sys.stderr,
            )
            return 1
    print(
        f"OK: {LOCK_PATH.relative_to(REPO_ROOT).as_posix()} matches "
        f"uv {UV_VERSION} universal compile"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--update",
        action="store_true",
        help="re-resolve and overwrite the tracked requirements-runtime.lock",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="compile to a temp file and byte-compare (does not mutate lock)",
    )
    args = parser.parse_args(argv)
    if args.update:
        return cmd_update()
    return cmd_check()


if __name__ == "__main__":
    raise SystemExit(main())
