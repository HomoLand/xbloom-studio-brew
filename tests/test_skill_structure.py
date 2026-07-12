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
        "references/recipe-schema.md",
        "references/device-safety.md",
        "references/deployment.md",
        "references/sources.md",
        "assets/hot-template.yaml",
        "assets/flash-brew-template.yaml",
        "scripts/xbloom.py",
        "scripts/bootstrap.py",
        "agents/openai.yaml",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
    ]
    assert not [path for path in expected if not (ROOT / path).is_file()]


def test_openai_interface_invokes_the_skill_name():
    interface = yaml.safe_load((ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    assert "$xbloom-studio-brew" in interface["interface"]["default_prompt"]


def test_runtime_requirements_are_pinned():
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    assert all("==" in line for line in lines)
