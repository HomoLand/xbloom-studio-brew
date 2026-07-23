"""Vendored xbloom-ble core for unofficial xBloom Studio BLE control.

This package speaks the reverse-engineered BLE protocol of the xBloom Studio
pour-over machine. There is no official API. The vendored client contains both
load-only and brew-control primitives; public Agent workflows must call them
through ``scripts/xbloom.py``, which applies the skill's stricter safety gates.
"""

from __future__ import annotations

from pathlib import Path

_DIST_NAME = "xbloom-studio-core"
_UNKNOWN_VERSION = "0+unknown"


def _adjacent_pyproject() -> Path | None:
    """Return ``packages/core/pyproject.toml`` when imported from a source checkout.

    Layout: ``packages/core/xbloom_ble/__init__.py`` → sibling ``pyproject.toml``.
    Installed wheels have no adjacent project metadata, so this returns ``None``.
    """
    candidate = Path(__file__).resolve().parent.parent / "pyproject.toml"
    return candidate if candidate.is_file() else None


def _version_from_pyproject(path: Path) -> str | None:
    """Parse ``[project].version`` from *path* using stdlib tomllib (Python >=3.11)."""
    import tomllib

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _version_from_distribution() -> str | None:
    """Return the installed distribution version, or ``None`` if not installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return None


def _package_version() -> str:
    """Resolve core version: source pyproject first, then installed dist, else unknown.

    When developing against a checkout (``PYTHONPATH=packages/core`` or editable
    install), an older installed dist-info must not override the source tree's
    ``pyproject.toml`` version — that would make bridge identity / ``core_version``
    lie. Installed wheels have no adjacent pyproject and use importlib.metadata.
    """
    source = _adjacent_pyproject()
    if source is not None:
        parsed = _version_from_pyproject(source)
        if parsed is not None:
            return parsed
    installed = _version_from_distribution()
    if installed is not None:
        return installed
    return _UNKNOWN_VERSION


__version__ = _package_version()

from .protocol import PATTERN_CODES, build_load_frames, crc16_kermit, xbloom_frame
from .recipe import Pour, Recipe, RecipeError
from .tea import TeaPour, TeaRecipe, TeaRecipeError
from .telemetry import STATE_NAMES, StatusEvent, parse_notification

__all__ = [
    "__version__",
    "build_load_frames",
    "PATTERN_CODES",
    "crc16_kermit",
    "xbloom_frame",
    "Recipe",
    "Pour",
    "RecipeError",
    "TeaRecipe",
    "TeaPour",
    "TeaRecipeError",
    "StatusEvent",
    "parse_notification",
    "STATE_NAMES",
]
