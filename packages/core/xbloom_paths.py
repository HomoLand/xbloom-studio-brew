"""Cross-platform writable paths for the portable xBloom Studio Skill."""

from __future__ import annotations

import os
from collections.abc import Mapping
from os import environ as _process_environment
from pathlib import Path

# Canonical state-root env (Phase 0). Takes precedence over the legacy alias.
STATE_DIR_ENV = "XBLOOM_STATE_DIR"
# Legacy alias retained for v1; still honoured when STATE_DIR_ENV is unset.
LEGACY_STATE_DIR_ENV = "XBLOOM_SKILL_STATE_DIR"
RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"

DEFAULT_STATE_DIRNAME = ".xbloom-studio-brew"


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return _process_environment if environ is None else environ


def environment_value(
    name: str,
    default: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read one explicitly named configuration value from the process environment."""

    return _environment(environ).get(name, default)


def environment_copy(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the environment so a child-process overlay cannot mutate its source."""

    return dict(_environment(environ))


def normalize_state_root(path: Path | str) -> Path:
    """Return an absolute, expanded, normalised state-root path.

    Symlinks are resolved when the path exists; missing roots keep a stable
    absolute form so lock/record paths stay consistent across callers.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        # resolve() can fail on some Windows edge cases; fall back to absolute.
        return candidate.absolute()


def state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-writable state root, without creating it.

    Precedence: ``XBLOOM_STATE_DIR`` > ``XBLOOM_SKILL_STATE_DIR`` > default
    ``~/.xbloom-studio-brew``. The result is always normalised.
    """

    env = _environment(environ)
    configured = env.get(STATE_DIR_ENV) or env.get(LEGACY_STATE_DIR_ENV)
    if configured:
        return normalize_state_root(configured)
    return normalize_state_root(Path.home() / DEFAULT_STATE_DIRNAME)


def skill_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Compatibility alias for :func:`state_dir` (existing callers)."""

    return state_dir(environ)


def skill_runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the external virtual-environment directory.

    Managed Agent installations may be read-only or atomically replaced. Keeping
    the runtime below the user state root lets the same Skill work from a checkout,
    a package cache, or a read-only installation.
    """

    env = _environment(environ)
    configured = env.get(RUNTIME_DIR_ENV)
    if configured:
        return normalize_state_root(configured)
    return state_dir(env) / "runtime"


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
