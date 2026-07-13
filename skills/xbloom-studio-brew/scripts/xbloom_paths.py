"""Cross-platform writable paths for the portable xBloom Studio Skill."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

STATE_DIR_ENV = "XBLOOM_SKILL_STATE_DIR"
RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def skill_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-writable state root, without creating it."""

    env = _environment(environ)
    configured = env.get(STATE_DIR_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".xbloom-studio-brew"


def skill_runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the external virtual-environment directory.

    Managed Agent installations may be read-only or atomically replaced. Keeping
    the runtime below the user state root lets the same Skill work from a checkout,
    a package cache, or a read-only installation.
    """

    env = _environment(environ)
    configured = env.get(RUNTIME_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return skill_state_dir(env) / "runtime"


def runtime_python_path(runtime_dir: Path) -> Path:
    if os.name == "nt":
        return Path(runtime_dir) / "Scripts" / "python.exe"
    return Path(runtime_dir) / "bin" / "python"


def legacy_runtime_python(skill_root: Path) -> Path:
    """Path used by releases before the runtime moved outside the Skill."""

    return runtime_python_path(Path(skill_root) / ".venv")


def preferred_runtime_python(
    skill_root: Path, environ: Mapping[str, str] | None = None
) -> Path:
    """Prefer the external runtime, with a temporary legacy-install fallback."""

    external = runtime_python_path(skill_runtime_dir(environ))
    if external.exists():
        return external
    legacy = legacy_runtime_python(skill_root)
    return legacy if legacy.exists() else external
