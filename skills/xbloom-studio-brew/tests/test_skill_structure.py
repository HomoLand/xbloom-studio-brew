from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_skill_frontmatter_is_portable_and_minimal():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    metadata = yaml.safe_load(match.group(1))
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == ROOT.name == "xbloom-studio-brew"
    assert 1 <= len(metadata["description"]) <= 1024


def test_skill_references_and_assets_exist():
    expected = [
        "references/recipe-design.md",
        "references/recipe-baselines.md",
        "references/web-enrichment.md",
        "references/recipe-schema.md",
        "references/device-safety.md",
        "references/standalone-tools.md",
        "references/tea-brewing.md",
        "references/catalog.md",
        "references/deployment.md",
        "references/sources.md",
        "references/apk-capability-matrix.md",
        "assets/hot-template.yaml",
        "assets/flash-brew-template.yaml",
        "assets/tea-green-official.yaml",
        "assets/tea-white-official.yaml",
        "assets/tea-flower-official.yaml",
        "assets/tea-black-official.yaml",
        "assets/tea-oolong-official.yaml",
        "scripts/xbloom.py",
        "scripts/bootstrap.py",
        "agents/openai.yaml",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
    ]
    assert not [path for path in expected if not (ROOT / path).is_file()]


def test_openai_interface_invokes_the_skill_name():
    interface = yaml.safe_load((ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    assert set(interface) == {"interface"}
    metadata = interface["interface"]
    assert set(metadata) == {"display_name", "short_description", "default_prompt"}
    assert 25 <= len(metadata["short_description"]) <= 64
    assert "$xbloom-studio-brew" in metadata["default_prompt"]


def test_runtime_requirements_are_pinned():
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    # Each line is either a pinned dependency (==) or an editable local install (-e).
    assert all("==" in line or line.startswith("-e ") for line in lines)


def test_bootstrap_uses_external_runtime_not_installed_skill():
    bootstrap = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    import xbloom_paths

    # Bootstrap inlines path helpers so it can run before core is installed.
    assert 'RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"' in bootstrap
    assert "_skill_runtime_dir" in bootstrap
    assert 'Path(skill_root) / ".venv"' not in bootstrap
    assert 'from xbloom_paths' not in bootstrap
    assert getattr(xbloom_paths, "RUNTIME_DIR_ENV", None) == "XBLOOM_SKILL_RUNTIME_DIR"


def test_xbloom_cli_inlines_path_helpers_for_clean_launch():
    """CLI must not import core at module load (clean CI / no-site-packages)."""

    source = (ROOT / "scripts" / "xbloom.py").read_text(encoding="utf-8")
    import xbloom_paths

    assert 'RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"' in source
    assert "def reexec_in_local_runtime" in source
    assert "def preferred_runtime_python" in source
    assert "from xbloom_paths" not in source
    assert "import xbloom_paths" not in source
    assert getattr(xbloom_paths, "RUNTIME_DIR_ENV", None) == "XBLOOM_SKILL_RUNTIME_DIR"


def test_inlined_launcher_helpers_and_safety_constants_match_core(tmp_path):
    """Guard against drift between stdlib-only launcher helpers/constants and core."""
    import importlib.util
    import xbloom_paths
    from xbloom_ble import bridge

    def _load(name: str, rel: str):
        import sys

        path = ROOT / rel
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(name, None)
        return mod

    cli_mod = _load("xbloom_cli_contract_check", "scripts/xbloom.py")
    boot_mod = _load("xbloom_boot_contract_check", "scripts/bootstrap.py")

    for attr in (
        "STATE_DIR_ENV",
        "LEGACY_STATE_DIR_ENV",
        "RUNTIME_DIR_ENV",
        "DEFAULT_STATE_DIRNAME",
    ):
        assert getattr(cli_mod, attr) == getattr(xbloom_paths, attr)
        assert getattr(boot_mod, attr) == getattr(xbloom_paths, attr)

    sample_env = {
        "XBLOOM_STATE_DIR": str(tmp_path / "custom-state"),
        "XBLOOM_SKILL_RUNTIME_DIR": str(tmp_path / "custom-runtime"),
    }
    assert cli_mod.skill_state_dir(sample_env) == xbloom_paths.skill_state_dir(sample_env)
    assert boot_mod._skill_state_dir(sample_env) == xbloom_paths.skill_state_dir(sample_env)
    assert cli_mod.skill_runtime_dir(sample_env) == xbloom_paths.skill_runtime_dir(sample_env)
    assert boot_mod._skill_runtime_dir(sample_env) == xbloom_paths.skill_runtime_dir(sample_env)

    for sentinel_attr in (
        "REMOTE_START_ENV",
        "REMOTE_START_SENTINEL",
        "REMOTE_GRINDER_ENV",
        "REMOTE_GRINDER_SENTINEL",
        "LIVE_ADJUST_ENV",
        "LIVE_ADJUST_SENTINEL",
        "SETTINGS_WRITE_ENV",
        "SETTINGS_WRITE_SENTINEL",
        "SETTINGS_CONFIRM_SENTINEL",
        "ADVANCED_CONFIRM_SENTINEL",
        "READY_SENTINEL",
        "WATER_READY_SENTINEL",
        "GRINDER_READY_SENTINEL",
        "TEA_READY_SENTINEL",
        "SUPPORTED_FIRMWARE",
        "UNTESTED_FIRMWARE_ENV",
        "UNTESTED_FIRMWARE_SENTINEL",
    ):
        assert getattr(cli_mod, sentinel_attr) == getattr(bridge, sentinel_attr), sentinel_attr

