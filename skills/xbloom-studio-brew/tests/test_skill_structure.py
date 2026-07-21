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
        "scripts/xbloom_paths.py",
        "scripts/xbloom_catalog.py",
        "scripts/xbloom_history.py",
        "scripts/xbloom_ble/bridge.py",
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
    assert all("==" in line for line in lines)


def test_bootstrap_uses_external_runtime_not_installed_skill():
    bootstrap = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    paths = (ROOT / "scripts" / "xbloom_paths.py").read_text(encoding="utf-8")
    assert 'skill_runtime_dir()' in bootstrap
    assert 'Path(skill_root) / ".venv"' not in bootstrap
    assert 'RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"' in paths
