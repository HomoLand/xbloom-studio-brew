from pathlib import Path

import xbloom_paths


def test_default_runtime_lives_below_external_state(monkeypatch, tmp_path):
    monkeypatch.setattr(xbloom_paths.Path, "home", classmethod(lambda cls: tmp_path))

    state = xbloom_paths.skill_state_dir({})
    runtime = xbloom_paths.skill_runtime_dir({})

    assert state == tmp_path / ".xbloom-studio-brew"
    assert runtime == state / "runtime"


def test_state_and_runtime_environment_overrides_are_independent(tmp_path):
    state = tmp_path / "state"
    runtime = tmp_path / "venv"
    environ = {
        xbloom_paths.STATE_DIR_ENV: str(state),
        xbloom_paths.RUNTIME_DIR_ENV: str(runtime),
    }

    assert xbloom_paths.skill_state_dir(environ) == state
    assert xbloom_paths.skill_runtime_dir(environ) == runtime


def test_runtime_defaults_below_overridden_state(tmp_path):
    state = tmp_path / "agent-state"

    assert xbloom_paths.skill_runtime_dir(
        {xbloom_paths.STATE_DIR_ENV: str(state)}
    ) == state / "runtime"


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
