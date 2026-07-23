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
