import importlib.util
import sys
from pathlib import Path

import xbloom_paths


def _load_launcher_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses (and others) require the module to be registered first.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_default_runtime_lives_below_external_state(monkeypatch, tmp_path):
    monkeypatch.setattr(xbloom_paths.Path, "home", classmethod(lambda cls: tmp_path))

    state = xbloom_paths.skill_state_dir({})
    runtime = xbloom_paths.skill_runtime_dir({})

    assert state == xbloom_paths.normalize_state_root(tmp_path / ".xbloom-studio-brew")
    assert runtime == state / "runtime"


def test_state_and_runtime_environment_overrides_are_independent(tmp_path):
    state = tmp_path / "state"
    runtime = tmp_path / "venv"
    environ = {
        xbloom_paths.STATE_DIR_ENV: str(state),
        xbloom_paths.RUNTIME_DIR_ENV: str(runtime),
    }

    assert xbloom_paths.skill_state_dir(environ) == xbloom_paths.normalize_state_root(state)
    assert xbloom_paths.skill_runtime_dir(environ) == xbloom_paths.normalize_state_root(
        runtime
    )


def test_canonical_state_dir_precedes_legacy_alias(tmp_path):
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    environ = {
        xbloom_paths.STATE_DIR_ENV: str(canonical),
        xbloom_paths.LEGACY_STATE_DIR_ENV: str(legacy),
    }
    assert xbloom_paths.state_dir(environ) == xbloom_paths.normalize_state_root(canonical)
    # Legacy alone still works.
    assert xbloom_paths.state_dir(
        {xbloom_paths.LEGACY_STATE_DIR_ENV: str(legacy)}
    ) == xbloom_paths.normalize_state_root(legacy)


def test_environment_helpers_are_explicit_and_return_a_copy():
    source = {"XBLOOM_TEST_VALUE": "configured"}

    assert (
        xbloom_paths.environment_value("XBLOOM_TEST_VALUE", environ=source)
        == "configured"
    )
    assert xbloom_paths.environment_value("MISSING", "fallback", source) == "fallback"

    copied = xbloom_paths.environment_copy(source)
    copied["XBLOOM_TEST_VALUE"] = "changed"
    assert source["XBLOOM_TEST_VALUE"] == "configured"


def test_runtime_defaults_below_overridden_state(tmp_path):
    state = tmp_path / "agent-state"

    assert xbloom_paths.skill_runtime_dir(
        {xbloom_paths.STATE_DIR_ENV: str(state)}
    ) == xbloom_paths.normalize_state_root(state) / "runtime"


def test_preferred_runtime_uses_external_before_legacy(monkeypatch, tmp_path):
    runtime = tmp_path / "external"
    legacy_root = tmp_path / "skill"
    external_python = xbloom_paths.runtime_python_path(runtime)
    legacy_python = xbloom_paths.legacy_runtime_python(legacy_root)
    external_python.parent.mkdir(parents=True)
    external_python.touch()
    legacy_python.parent.mkdir(parents=True)
    legacy_python.touch()
    monkeypatch.setenv(xbloom_paths.RUNTIME_DIR_ENV, str(runtime))

    assert xbloom_paths.preferred_runtime_python(legacy_root) == external_python


def test_preferred_runtime_temporarily_falls_back_to_legacy(monkeypatch, tmp_path):
    runtime = tmp_path / "missing-external"
    legacy_root = tmp_path / "skill"
    legacy_python = xbloom_paths.legacy_runtime_python(legacy_root)
    legacy_python.parent.mkdir(parents=True)
    legacy_python.touch()
    monkeypatch.setenv(xbloom_paths.RUNTIME_DIR_ENV, str(runtime))

    assert xbloom_paths.preferred_runtime_python(legacy_root) == legacy_python


def test_missing_runtimes_return_external_target(monkeypatch, tmp_path):
    runtime = tmp_path / "future-runtime"
    monkeypatch.setenv(xbloom_paths.RUNTIME_DIR_ENV, str(runtime))

    assert xbloom_paths.preferred_runtime_python(
        tmp_path / "skill"
    ) == xbloom_paths.runtime_python_path(runtime)


def test_relative_state_dir_matches_core_and_launchers(monkeypatch, tmp_path):
    """Relative XBLOOM_STATE_DIR must not split one invocation across cwd."""

    skill_root = Path(__file__).resolve().parents[1]
    xbloom = _load_launcher_module(
        "xbloom_launcher_paths", skill_root / "scripts" / "xbloom.py"
    )
    bootstrap = _load_launcher_module(
        "bootstrap_launcher_paths", skill_root / "scripts" / "bootstrap.py"
    )

    monkeypatch.chdir(tmp_path)
    relative = "rel-state"
    environ = {xbloom_paths.STATE_DIR_ENV: relative}
    expected = xbloom_paths.normalize_state_root(relative)
    assert expected == (tmp_path / relative).resolve()
    assert xbloom_paths.state_dir(environ) == expected
    assert xbloom.skill_state_dir(environ) == expected
    assert xbloom.normalize_state_root(relative) == expected
    monkeypatch.setenv(xbloom_paths.STATE_DIR_ENV, relative)
    assert bootstrap._skill_state_dir() == expected
    assert bootstrap._normalize_state_root(relative) == expected
