"""Packaging, knowledge-manifest, and bootstrap-layout tests (no hardware)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import xbloom_knowledge
from xbloom_knowledge import KnowledgeError

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]
CORE_DIR = REPO_ROOT / "packages" / "core"
BUILD_RELEASE = REPO_ROOT / "tools" / "build_release.py"


def _core_version() -> str:
    text = (CORE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "core pyproject.toml must declare version"
    return match.group(1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_bootstrap_module():
    path = SKILL_ROOT / "scripts" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("xbloom_bootstrap_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_build_release_module():
    path = BUILD_RELEASE
    spec = importlib.util.spec_from_file_location("xbloom_build_release_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME_LOCK_BASENAME = "requirements-runtime.lock"
# Minimal valid hashed lock body for unit tests (not used for real installs).
_MINIMAL_RUNTIME_LOCK = (
    "bleak==3.0.2 \\\n"
    "    --hash=sha256:" + ("a" * 64) + "\n"
    "pyyaml==6.0.3 \\\n"
    "    --hash=sha256:" + ("b" * 64) + "\n"
)


def _write_runtime_lock(
    rel_root: Path,
    *,
    content: str | None = None,
    basename: str = RUNTIME_LOCK_BASENAME,
) -> Path:
    path = rel_root / basename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else _MINIMAL_RUNTIME_LOCK, encoding="utf-8")
    return path


def _write_release_json(
    rel_root: Path,
    *,
    wheel_name: str,
    version: str = "1.0.1",
    sha256: str | None = None,
    layout: str = "release",
    extra: dict | None = None,
    wheel_bytes: bytes = b"fake-wheel-bytes",
    runtime_lock: str | None = RUNTIME_LOCK_BASENAME,
    runtime_lock_sha256: str | None = None,
    write_lock_file: bool = True,
    lock_content: str | None = None,
) -> Path:
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    wheel_path = wheels / wheel_name
    wheel_path.write_bytes(wheel_bytes)
    digest = sha256 if sha256 is not None else _sha256(wheel_path)
    meta = {
        "skill": "xbloom-studio-brew",
        "version": version,
        "core_version": version,
        "core_wheel": wheel_name,
        "core_wheel_sha256": digest,
        "layout": layout,
    }
    if runtime_lock is not None:
        if write_lock_file and runtime_lock == Path(runtime_lock).name:
            lock_path = _write_runtime_lock(
                rel_root, content=lock_content, basename=runtime_lock
            )
            lock_digest = (
                runtime_lock_sha256
                if runtime_lock_sha256 is not None
                else _sha256(lock_path)
            )
        else:
            lock_digest = runtime_lock_sha256 if runtime_lock_sha256 is not None else "c" * 64
        meta["runtime_lock"] = runtime_lock
        meta["runtime_lock_sha256"] = lock_digest
    if extra:
        meta.update(extra)
    path = rel_root / "vendor" / "release.json"
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return wheel_path


def test_core_pyproject_declares_console_entry_and_version():
    text = (CORE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "xbloom-studio-core"' in text
    assert f'version = "{_core_version()}"' in text
    assert "xbloom-bridge" in text
    assert "xbloom_ble.bridge:main" in text
    assert "bleak==3.0.2" in text
    assert "PyYAML==6.0.3" in text
    assert "xbloom_knowledge" in text
    # Exact setuptools pin for reproducible wheels.
    assert 'requires = ["setuptools==80.9.0"]' in text


def test_bridge_module_exposes_main_and_serve_bridge():
    from xbloom_ble import bridge

    assert callable(bridge.serve_bridge)
    assert callable(bridge.main)


def test_knowledge_manifest_is_deterministic(tmp_path):
    source = SKILL_ROOT
    first = xbloom_knowledge.build_manifest(source, version="1.0.1")
    second = xbloom_knowledge.build_manifest(source, version="1.0.1")
    assert first == second
    assert first["content_hash"] == second["content_hash"]
    assert first["files"]
    assert "SKILL.md" in first["files"]
    assert any(k.startswith("references/") for k in first["files"])
    assert any(k.startswith("assets/") for k in first["files"])

    # Writing and reloading preserves equality.
    dest = tmp_path / "knowledge"
    xbloom_knowledge.copy_knowledge_tree(source, dest)
    xbloom_knowledge.write_manifest(dest / "manifest.json", first)
    loaded = xbloom_knowledge.validate_bundle(dest, expected_version="1.0.1")
    assert loaded["content_hash"] == first["content_hash"]


def test_knowledge_rejects_missing_file(tmp_path):
    dest = tmp_path / "knowledge"
    xbloom_knowledge.copy_knowledge_tree(SKILL_ROOT, dest)
    manifest = xbloom_knowledge.build_manifest(dest, version="1.0.1")
    xbloom_knowledge.write_manifest(dest / "manifest.json", manifest)
    target = dest / "SKILL.md"
    target.unlink()
    with pytest.raises(KnowledgeError, match="missing knowledge file"):
        xbloom_knowledge.validate_bundle(dest)


def test_knowledge_rejects_tampered_file(tmp_path):
    dest = tmp_path / "knowledge"
    xbloom_knowledge.copy_knowledge_tree(SKILL_ROOT, dest)
    manifest = xbloom_knowledge.build_manifest(dest, version="1.0.1")
    xbloom_knowledge.write_manifest(dest / "manifest.json", manifest)
    target = dest / "SKILL.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="tampered knowledge file"):
        xbloom_knowledge.validate_bundle(dest)


def test_knowledge_rejects_content_hash_mismatch(tmp_path):
    dest = tmp_path / "knowledge"
    xbloom_knowledge.copy_knowledge_tree(SKILL_ROOT, dest)
    manifest = xbloom_knowledge.build_manifest(dest, version="1.0.1")
    manifest["content_hash"] = "0" * 64
    xbloom_knowledge.write_manifest(dest / "manifest.json", manifest)
    with pytest.raises(KnowledgeError, match="content_hash mismatch"):
        xbloom_knowledge.validate_bundle(dest)


def test_knowledge_rejects_path_traversal(tmp_path):
    dest = tmp_path / "knowledge"
    xbloom_knowledge.copy_knowledge_tree(SKILL_ROOT, dest)
    manifest = xbloom_knowledge.build_manifest(dest, version="1.0.1")

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("secret-payload\n", encoding="utf-8")
    # Craft a relative key that would escape the bundle root if joined naively.
    traversal = "../outside_secret.txt"
    manifest["files"][traversal] = xbloom_knowledge.sha256_file(outside)
    # Keep content_hash consistent with the (invalid) files map so we hit path checks first.
    manifest["content_hash"] = xbloom_knowledge.aggregate_content_hash(manifest["files"])
    xbloom_knowledge.write_manifest(dest / "manifest.json", manifest)

    with pytest.raises(KnowledgeError, match="traversal|relative|escapes|allowed roots"):
        xbloom_knowledge.validate_bundle(dest)

    # Absolute-style keys must also be rejected.
    with pytest.raises(KnowledgeError, match="relative|allowed roots|traversal"):
        xbloom_knowledge.safe_knowledge_relpath("/etc/passwd")
    with pytest.raises(KnowledgeError, match="traversal|empty segment"):
        xbloom_knowledge.safe_knowledge_relpath("references/../../etc/passwd")


def test_knowledge_rejects_extra_on_disk_file(tmp_path):
    dest = tmp_path / "knowledge"
    xbloom_knowledge.copy_knowledge_tree(SKILL_ROOT, dest)
    manifest = xbloom_knowledge.build_manifest(dest, version="1.0.1")
    xbloom_knowledge.write_manifest(dest / "manifest.json", manifest)
    evil = dest / "references" / "evil.md"
    evil.write_text("# injected\n", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="unexpected knowledge file"):
        xbloom_knowledge.validate_bundle(dest)


def test_core_library_version_matches_distribution():
    """Package __version__, pyproject version, and build_release must agree."""
    import xbloom_ble

    pyproject_version = _core_version()
    build_release = _load_build_release_module()
    assert xbloom_ble.__version__ == pyproject_version
    assert build_release.read_core_version() == pyproject_version
    assert xbloom_ble.__version__ == build_release.read_core_version()


def test_package_version_source_checkout_wins_over_stale_dist(monkeypatch):
    """Source pyproject is authoritative even when installed dist-info is stale.

    Reproduces checkout development via PYTHONPATH while an older wheel's
    metadata (e.g. 1.0.1) remains on the path: bridge identity must report
    the source tree version (1.2.0), not the installed distribution.
    """
    import xbloom_ble

    source_version = _core_version()
    assert source_version == "1.2.0", "fixture assumes packages/core is 1.2.0"

    # Adjacent source metadata must be discoverable in this worktree.
    pyproject = xbloom_ble._adjacent_pyproject()
    assert pyproject is not None
    assert pyproject.is_file()
    assert xbloom_ble._version_from_pyproject(pyproject) == source_version

    # Stale installed distribution must not win when source pyproject exists.
    monkeypatch.setattr(xbloom_ble, "_version_from_distribution", lambda: "1.0.1")
    assert xbloom_ble._package_version() == source_version
    assert xbloom_ble._package_version() != "1.0.1"


def test_package_version_installed_wheel_uses_distribution(monkeypatch, tmp_path):
    """When pyproject is absent (installed wheel), use importlib.metadata."""
    import xbloom_ble

    monkeypatch.setattr(xbloom_ble, "_adjacent_pyproject", lambda: None)
    monkeypatch.setattr(xbloom_ble, "_version_from_distribution", lambda: "9.9.9")
    assert xbloom_ble._package_version() == "9.9.9"

    # Helper still parses a standalone pyproject path correctly.
    fake = tmp_path / "pyproject.toml"
    fake.write_text(
        '[project]\nname = "xbloom-studio-core"\nversion = "3.4.5"\n',
        encoding="utf-8",
    )
    assert xbloom_ble._version_from_pyproject(fake) == "3.4.5"
    assert xbloom_ble._version_from_pyproject(tmp_path / "missing.toml") is None


def test_package_version_fallback_unknown_when_no_metadata(monkeypatch):
    """Neither source pyproject nor installed dist -> non-release unknown."""
    import xbloom_ble

    monkeypatch.setattr(xbloom_ble, "_adjacent_pyproject", lambda: None)
    monkeypatch.setattr(xbloom_ble, "_version_from_distribution", lambda: None)
    assert xbloom_ble._package_version() == "0+unknown"
    # Must not reintroduce a hardcoded current-release fallback.
    assert xbloom_ble._package_version() != _core_version()


def test_bootstrap_has_no_module_level_core_import():
    bootstrap = (SKILL_ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    # No top-level import of installed core packages before pip install.
    forbidden = (
        "from xbloom_paths",
        "import xbloom_paths",
        "from xbloom_ble",
        "import xbloom_ble",
        "from xbloom_catalog",
        "import xbloom_catalog",
    )
    for phrase in forbidden:
        assert phrase not in bootstrap, f"bootstrap must not import core before install: {phrase}"
    assert 'RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"' in bootstrap
    assert "is_release_layout" in bootstrap
    assert "--no-index" in bootstrap
    assert "vendor" in bootstrap
    assert "core_wheel_sha256" in bootstrap
    assert "runtime_lock_sha256" in bootstrap
    assert "--require-hashes" in bootstrap
    assert RUNTIME_LOCK_BASENAME in bootstrap
    # Must not fall back to line-to-args install of requirements.txt.
    assert "filtered" not in bootstrap
    assert "deferred" not in bootstrap.lower()


def test_xbloom_cli_has_no_module_level_core_import():
    """CLI must load with stdlib only until re-exec into the external runtime."""

    source = (SKILL_ROOT / "scripts" / "xbloom.py").read_text(encoding="utf-8")
    forbidden = (
        "from xbloom_paths",
        "import xbloom_paths",
        "from xbloom_ble",
        "import xbloom_ble",
        "from xbloom_catalog",
        "import xbloom_catalog",
        "from xbloom_history",
        "import xbloom_history",
        "from xbloom_safety",
        "import xbloom_safety",
        "from xbloom_knowledge",
        "import xbloom_knowledge",
    )
    for phrase in forbidden:
        # Allow lazy imports inside functions after re-exec/runtime is ready.
        # Only module-level (line-start) imports of core are forbidden.
        assert f"\n{phrase}" not in source and not source.startswith(phrase), (
            f"xbloom.py must not import core at module load: {phrase}"
        )
    assert 'RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"' in source
    assert "def reexec_in_local_runtime" in source
    assert "def preferred_runtime_python" in source


def test_xbloom_help_loads_without_core_isolated_interpreter(tmp_path):
    """Clean/no-site-packages Python + missing runtime must still serve --help."""

    import os

    script = SKILL_ROOT / "scripts" / "xbloom.py"
    missing_runtime = tmp_path / "no-such-runtime"
    env = {
        **os.environ,
        "XBLOOM_SKILL_RUNTIME_DIR": str(missing_runtime),
        "XBLOOM_SKILL_STATE_DIR": str(tmp_path / "state"),
    }
    env.pop("XBLOOM_SKILL_REEXEC", None)
    # -I: isolated (no site, no user site, ignore PYTHONPATH) so core cannot
    # leak in from the developer environment.
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(SKILL_ROOT),
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "doctor" in result.stdout or "usage" in result.stdout.lower()
    assert "ModuleNotFoundError" not in result.stderr
    assert "xbloom_paths" not in result.stderr


def test_xbloom_help_reexecs_into_external_runtime(tmp_path):
    """Isolated parent must re-exec into external runtime; child --help exits 0.

    Uses ``--help`` (not ``doctor``) so success does not depend on core being
    installed in the empty runtime. A child crash after writing the marker
    cannot pass: both returncode 0 and the re-exec marker are required.
    Extracted-release CI remains the real doctor smoke test.
    """

    import os
    import venv

    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False).create(runtime)
    if os.name == "nt":
        runtime_python = runtime / "Scripts" / "python.exe"
        site_packages = runtime / "Lib" / "site-packages"
    else:
        runtime_python = runtime / "bin" / "python"
        lib = runtime / "lib"
        py_dirs = sorted(lib.glob("python*"))
        assert py_dirs, f"no python lib dir under {lib}"
        site_packages = py_dirs[-1] / "site-packages"
    assert runtime_python.is_file()
    site_packages.mkdir(parents=True, exist_ok=True)

    marker = tmp_path / "reexec-marker.txt"
    # Runtime interpreter writes a sentinel when re-exec sets XBLOOM_SKILL_REEXEC.
    (site_packages / "sitecustomize.py").write_text(
        "import os\n"
        "if os.environ.get('XBLOOM_SKILL_REEXEC') == '1':\n"
        f"    open({str(marker)!r}, 'w', encoding='utf-8').write('reexec')\n",
        encoding="utf-8",
    )

    script = SKILL_ROOT / "scripts" / "xbloom.py"
    env = {
        **os.environ,
        "XBLOOM_SKILL_RUNTIME_DIR": str(runtime),
        "XBLOOM_SKILL_STATE_DIR": str(tmp_path / "state"),
    }
    env.pop("XBLOOM_SKILL_REEXEC", None)
    # Parent is isolated (no core). Child is the external runtime python.
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(SKILL_ROOT),
        check=False,
    )
    assert result.returncode == 0, (
        f"re-exec child must exit 0; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.is_file(), (
        f"re-exec did not reach runtime python; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.read_text(encoding="utf-8") == "reexec"
    assert "doctor" in result.stdout or "usage" in result.stdout.lower()


def test_bootstrap_release_vs_dev_detection(tmp_path):
    module = _load_bootstrap_module()

    dev_root = tmp_path / "dev-skill"
    dev_root.mkdir()
    (dev_root / "requirements.txt").write_text("-e ../../packages/core\n", encoding="utf-8")
    assert module.is_dev_requirements(dev_root)
    assert not module.is_release_layout(dev_root)

    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    _write_release_json(rel_root, wheel_name=wheel_name, version="1.0.1")
    (rel_root / "requirements.txt").write_text(
        "xbloom-studio-core==1.0.1\n",
        encoding="utf-8",
    )
    assert module.is_release_layout(rel_root)
    assert not module.is_dev_requirements(rel_root)
    assert module._release_core_version(rel_root) == "1.0.1"
    assert module._release_core_wheel(rel_root).name == wheel_name
    assert module._release_runtime_lock(rel_root).name == RUNTIME_LOCK_BASENAME


def test_is_release_layout_authoritative_on_release_json(tmp_path):
    """release.json presence is release even when wheels are missing/deleted."""

    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    lock_path = _write_runtime_lock(rel_root)
    lock_digest = _sha256(lock_path)

    # release.json only -- no vendor/wheels directory at all.
    (rel_root / "vendor").mkdir(parents=True, exist_ok=True)
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(
            {
                "layout": "release",
                "core_version": "1.0.1",
                "version": "1.0.1",
                "core_wheel": wheel_name,
                "core_wheel_sha256": "a" * 64,
                "runtime_lock": RUNTIME_LOCK_BASENAME,
                "runtime_lock_sha256": lock_digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert module.is_release_layout(rel_root)
    with pytest.raises(module.ReleaseMetaError, match="vendor/wheels directory missing"):
        module._load_release_meta(rel_root)

    # Named wheel deleted after metadata written.
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    with pytest.raises(module.ReleaseMetaError, match="core_wheel file not found|not found"):
        module._load_release_meta(rel_root)

    # Malformed metadata without a matching glob wheel still classifies as release
    # and fails closed (no development/PyPI fall-through via is_release_layout).
    (wheels / "unrelated-1.0.0-py3-none-any.whl").write_bytes(b"x")
    assert module.is_release_layout(rel_root)
    with pytest.raises(module.ReleaseMetaError):
        module._load_release_meta(rel_root)

    # Multiple matching core wheels for the declared version: ambiguity rule.
    good_a = wheels / "xbloom_studio_core-1.0.1-py3-none-any.whl"
    good_b = wheels / "xbloom_studio_core-1.0.1-py3-none-win_amd64.whl"
    good_a.write_bytes(b"wheel-a")
    good_b.write_bytes(b"wheel-b")
    meta = {
        "layout": "release",
        "core_version": "1.0.1",
        "version": "1.0.1",
        "core_wheel": good_a.name,
        "core_wheel_sha256": _sha256(good_a),
        "runtime_lock": RUNTIME_LOCK_BASENAME,
        "runtime_lock_sha256": lock_digest,
    }
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    assert module.is_release_layout(rel_root)
    with pytest.raises(module.ReleaseMetaError, match="ambiguous core wheels"):
        module._load_release_meta(rel_root)

    # Exactly one matching wheel for the declared version succeeds.
    good_b.unlink()
    meta["core_wheel_sha256"] = _sha256(good_a)
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    loaded = module._load_release_meta(rel_root)
    assert loaded is not None
    assert loaded["core_wheel"] == good_a.name
    assert loaded["runtime_lock"] == RUNTIME_LOCK_BASENAME


def test_bootstrap_release_meta_fail_closed_malformed_json(tmp_path):
    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "xbloom_studio_core-1.0.1-py3-none-any.whl").write_bytes(b"x")
    (rel_root / "vendor" / "release.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(module.ReleaseMetaError, match="malformed|invalid JSON"):
        module._load_release_meta(rel_root)


def test_bootstrap_release_meta_fail_closed_wrong_layout(tmp_path):
    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    _write_release_json(rel_root, wheel_name=wheel_name, layout="dev")
    with pytest.raises(module.ReleaseMetaError, match="layout must be"):
        module._load_release_meta(rel_root)


def test_bootstrap_release_meta_fail_closed_version_mismatch(tmp_path):
    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    wheel_path = wheels / wheel_name
    wheel_path.write_bytes(b"fake")
    lock_path = _write_runtime_lock(rel_root)
    meta = {
        "layout": "release",
        "core_version": "1.0.1",
        "version": "9.9.9",
        "core_wheel": wheel_name,
        "core_wheel_sha256": _sha256(wheel_path),
        "runtime_lock": RUNTIME_LOCK_BASENAME,
        "runtime_lock_sha256": _sha256(lock_path),
    }
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="does not match"):
        module._load_release_meta(rel_root)


def test_bootstrap_release_meta_fail_closed_unsafe_core_wheel_paths(tmp_path):
    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    good = wheels / "xbloom_studio_core-1.0.1-py3-none-any.whl"
    good.write_bytes(b"payload")
    digest = _sha256(good)
    lock_path = _write_runtime_lock(rel_root)
    lock_digest = _sha256(lock_path)

    unsafe_names = [
        "../xbloom_studio_core-1.0.1-py3-none-any.whl",
        "..\\xbloom_studio_core-1.0.1-py3-none-any.whl",
        "wheels/xbloom_studio_core-1.0.1-py3-none-any.whl",
        "wheels\\xbloom_studio_core-1.0.1-py3-none-any.whl",
        "/tmp/xbloom_studio_core-1.0.1-py3-none-any.whl",
        "C:\\tmp\\xbloom_studio_core-1.0.1-py3-none-any.whl",
        "xbloom_studio_core-9.9.9-py3-none-any.whl",
        "not_a_wheel.txt",
    ]
    for name in unsafe_names:
        meta = {
            "layout": "release",
            "core_version": "1.0.1",
            "version": "1.0.1",
            "core_wheel": name,
            "core_wheel_sha256": digest,
            "runtime_lock": RUNTIME_LOCK_BASENAME,
            "runtime_lock_sha256": lock_digest,
        }
        (rel_root / "vendor" / "release.json").write_text(
            json.dumps(meta) + "\n", encoding="utf-8"
        )
        with pytest.raises(module.ReleaseMetaError):
            module._load_release_meta(rel_root)


def test_bootstrap_release_meta_fail_closed_missing_fields_and_types(tmp_path):
    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    (wheels / wheel_name).write_bytes(b"x")
    lock_path = _write_runtime_lock(rel_root)
    lock_digest = _sha256(lock_path)

    # Missing core_wheel_sha256
    meta = {
        "layout": "release",
        "core_version": "1.0.1",
        "core_wheel": wheel_name,
        "runtime_lock": RUNTIME_LOCK_BASENAME,
        "runtime_lock_sha256": lock_digest,
    }
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="core_wheel_sha256"):
        module._load_release_meta(rel_root)

    # Wrong type for core_version
    meta = {
        "layout": "release",
        "core_version": 1,
        "core_wheel": wheel_name,
        "core_wheel_sha256": "a" * 64,
        "runtime_lock": RUNTIME_LOCK_BASENAME,
        "runtime_lock_sha256": lock_digest,
    }
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="core_version"):
        module._load_release_meta(rel_root)

    # Missing runtime_lock / runtime_lock_sha256
    good = wheels / wheel_name
    meta = {
        "layout": "release",
        "core_version": "1.0.1",
        "version": "1.0.1",
        "core_wheel": wheel_name,
        "core_wheel_sha256": _sha256(good),
    }
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="runtime_lock"):
        module._load_release_meta(rel_root)

    meta["runtime_lock"] = RUNTIME_LOCK_BASENAME
    # Wrong type for runtime_lock_sha256
    meta["runtime_lock_sha256"] = 123
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="runtime_lock_sha256"):
        module._load_release_meta(rel_root)


def test_bootstrap_release_meta_fail_closed_bad_hash(tmp_path):
    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    _write_release_json(
        rel_root,
        wheel_name=wheel_name,
        sha256="0" * 64,
        wheel_bytes=b"real-bytes",
    )
    with pytest.raises(module.ReleaseMetaError, match="sha256 mismatch|tampered"):
        module._load_release_meta(rel_root)


def test_bootstrap_release_meta_absent_allows_fallback(tmp_path):
    """Without release.json, wheel helper still resolves; layout stays release."""

    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    (wheels / wheel_name).write_bytes(b"fake")
    (rel_root / "requirements.txt").write_text(
        "xbloom-studio-core==1.0.1\n", encoding="utf-8"
    )
    assert module._load_release_meta(rel_root) is None
    # Wheel alone is release evidence (fail-closed classification).
    assert module.is_release_layout(rel_root)
    assert module._release_core_wheel(rel_root).name == wheel_name


def test_install_release_missing_meta_aborts_before_pip(tmp_path, monkeypatch):
    """Damaged bundle (wheel, no release.json): classify release, no pip/run."""

    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheels = rel_root / "vendor" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "xbloom_studio_core-1.0.1-py3-none-any.whl").write_bytes(b"fake")
    (rel_root / "requirements.txt").write_text(
        "xbloom-studio-core==1.0.1\n",
        encoding="utf-8",
    )
    _write_runtime_lock(rel_root)

    assert module.is_release_layout(rel_root)
    assert module._load_release_meta(rel_root) is None

    monkeypatch.setattr(module, "ROOT", rel_root)
    monkeypatch.setattr(module, "VENDOR_WHEELS", wheels)
    monkeypatch.setattr(module, "RELEASE_META", rel_root / "vendor" / "release.json")

    calls: list[list[str]] = []

    def tracking_run(args, **kwargs):
        calls.append(list(args))
        raise AssertionError(f"run/pip must not be invoked: {args}")

    monkeypatch.setattr(module, "run", tracking_run)

    with pytest.raises(SystemExit, match="release.json|refusing|unhashed"):
        module._install_release(str(sys.executable))
    assert calls == [], f"expected no run/pip calls, got {calls}"


def test_bootstrap_runtime_lock_fail_closed_unsafe_and_tampered(tmp_path):
    """Strict metadata rejects bad lock path/type/hash before any install."""

    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    _write_release_json(rel_root, wheel_name=wheel_name, version="1.0.1")
    # Baseline succeeds.
    loaded = module._load_release_meta(rel_root)
    assert loaded is not None
    assert loaded["runtime_lock"] == RUNTIME_LOCK_BASENAME

    lock_path = rel_root / RUNTIME_LOCK_BASENAME
    digest = _sha256(lock_path)
    wheel_path = rel_root / "vendor" / "wheels" / wheel_name
    wheel_digest = _sha256(wheel_path)

    unsafe_names = [
        "../requirements-runtime.lock",
        "..\\requirements-runtime.lock",
        "vendor/requirements-runtime.lock",
        "/tmp/requirements-runtime.lock",
        "C:\\tmp\\requirements-runtime.lock",
        "other-lock.txt",
    ]
    for name in unsafe_names:
        meta = {
            "layout": "release",
            "core_version": "1.0.1",
            "version": "1.0.1",
            "core_wheel": wheel_name,
            "core_wheel_sha256": wheel_digest,
            "runtime_lock": name,
            "runtime_lock_sha256": digest,
        }
        (rel_root / "vendor" / "release.json").write_text(
            json.dumps(meta) + "\n", encoding="utf-8"
        )
        with pytest.raises(module.ReleaseMetaError):
            module._load_release_meta(rel_root)

    # Missing lock file.
    meta = {
        "layout": "release",
        "core_version": "1.0.1",
        "version": "1.0.1",
        "core_wheel": wheel_name,
        "core_wheel_sha256": wheel_digest,
        "runtime_lock": RUNTIME_LOCK_BASENAME,
        "runtime_lock_sha256": digest,
    }
    lock_path.unlink()
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="runtime_lock file not found"):
        module._load_release_meta(rel_root)

    # Tampered lock content vs declared hash.
    lock_path.write_text(_MINIMAL_RUNTIME_LOCK + "#tamper\n", encoding="utf-8")
    meta["runtime_lock_sha256"] = digest  # old digest
    (rel_root / "vendor" / "release.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    with pytest.raises(module.ReleaseMetaError, match="runtime_lock_sha256 mismatch|tampered"):
        module._load_release_meta(rel_root)


def test_install_release_command_contract(tmp_path, monkeypatch):
    """Successful release install is core offline then lock --require-hashes only."""

    module = _load_bootstrap_module()
    rel_root = tmp_path / "rel-skill"
    wheel_name = "xbloom_studio_core-1.0.1-py3-none-any.whl"
    _write_release_json(rel_root, wheel_name=wheel_name, version="1.0.1")
    (rel_root / "requirements.txt").write_text(
        "# identity only\nxbloom-studio-core==1.0.1\nbleak==3.0.2\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "ROOT", rel_root)
    monkeypatch.setattr(module, "VENDOR_WHEELS", rel_root / "vendor" / "wheels")
    monkeypatch.setattr(module, "RELEASE_META", rel_root / "vendor" / "release.json")

    calls: list[list[str]] = []

    def tracking_run(args, **kwargs):
        calls.append(list(args))

    monkeypatch.setattr(module, "run", tracking_run)
    module._install_release(str(sys.executable))

    assert len(calls) == 2, calls
    core_cmd, lock_cmd = calls
    # Core: offline, no-deps, wheel path only (no PyPI name resolution).
    assert core_cmd[0] == str(sys.executable)
    assert core_cmd[1:4] == ["-m", "pip", "install"]
    assert "--no-deps" in core_cmd
    assert "--no-index" in core_cmd
    assert any(arg.endswith(wheel_name) for arg in core_cmd)
    assert "--require-hashes" not in core_cmd
    # Runtime: hashed lock file only -- never line-to-args from requirements.txt.
    assert lock_cmd[1:4] == ["-m", "pip", "install"]
    assert "--require-hashes" in lock_cmd
    assert "-r" in lock_cmd
    r_index = lock_cmd.index("-r")
    assert lock_cmd[r_index + 1].endswith(RUNTIME_LOCK_BASENAME)
    assert "--only-binary" in lock_cmd
    # No unhashed package names / version pins as positional args.
    joined = " ".join(lock_cmd)
    assert "bleak==" not in joined
    assert "PyYAML==" not in joined
    assert "pyyaml==" not in joined
    assert "xbloom-studio-core" not in joined


def test_universal_runtime_lock_committed():
    """Tracked lock excludes core; has exact pins, hashes, and platform markers."""

    lock_path = SKILL_ROOT / RUNTIME_LOCK_BASENAME
    assert lock_path.is_file(), f"missing committed lock: {lock_path}"
    text = lock_path.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "xbloom-studio-core" not in lowered
    assert "xbloom_studio_core" not in lowered
    assert "-e " not in text
    assert "file://" not in lowered
    assert "https://" not in lowered
    assert "http://" not in lowered

    # Direct roots + marker families (universal lock design).
    assert re.search(r"(?m)^bleak==3\.0\.2\b", text)
    assert re.search(r"(?mi)^pyyaml==6\.0\.3\b", text)
    assert "typing-extensions==" in lowered
    assert "sys_platform == 'linux'" in text or 'sys_platform == "linux"' in text
    assert "dbus-fast==" in lowered
    assert "sys_platform == 'darwin'" in text or 'sys_platform == "darwin"' in text
    assert "pyobjc-core==" in lowered
    assert "sys_platform == 'win32'" in text or 'sys_platform == "win32"' in text
    assert "winrt-runtime==" in lowered

    # Every logical requirement line is exact-pinned and has >=1 sha256 hash.
    # Continuations use "    --hash=sha256:..."; package lines end with " \" when hashed.
    package_lines = []
    current = None
    hashes_for_current = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("    --hash=") or line.startswith("\t--hash="):
            assert "sha256:" in line
            hashes_for_current += 1
            continue
        if current is not None:
            package_lines.append((current, hashes_for_current))
        current = line.rstrip("\\").strip()
        hashes_for_current = 0
        assert re.match(r"^[A-Za-z0-9_.-]+==[^\s;]+", current), current
        assert " @ " not in current
    if current is not None:
        package_lines.append((current, hashes_for_current))
    assert len(package_lines) >= 10, package_lines
    for req, n_hashes in package_lines:
        assert n_hashes >= 1, f"requirement missing sha256: {req}"


def test_build_release_artifacts(tmp_path):
    if not BUILD_RELEASE.is_file():
        pytest.skip("tools/build_release.py not present")
    import os

    out = tmp_path / "dist"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1704067200"}
    subprocess.run(
        [sys.executable, str(BUILD_RELEASE), "--out", str(out)],
        check=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    version = _core_version()
    wheels = list(out.glob("xbloom_studio_core-*.whl"))
    assert wheels, "expected core wheel in dist/"
    knowledge_dir = out / f"knowledge-{version}"
    assert (knowledge_dir / "manifest.json").is_file()
    assert (knowledge_dir / "SKILL.md").is_file()
    xbloom_knowledge.validate_bundle(knowledge_dir, expected_version=version)

    skill_dir = out / f"skill-xbloom-studio-brew-{version}"
    assert skill_dir.is_dir()
    assert (skill_dir / "scripts" / "bootstrap.py").is_file()
    assert (skill_dir / "vendor" / "wheels").is_dir()
    assert list((skill_dir / "vendor" / "wheels").glob("xbloom_studio_core-*.whl"))
    req = (skill_dir / "requirements.txt").read_text(encoding="utf-8")
    assert f"xbloom-studio-core=={version}" in req
    assert "-e " not in req
    assert "require-hashes" in req or RUNTIME_LOCK_BASENAME in req
    # No unhashed non-core pin contract in release requirements.txt.
    assert "bleak==" not in req
    assert "PyYAML==" not in req
    assert (out / f"skill-xbloom-studio-brew-{version}.zip").is_file()
    assert (out / f"knowledge-{version}.zip").is_file()

    # Byte-identical universal lock in the Skill bundle.
    committed_lock = SKILL_ROOT / RUNTIME_LOCK_BASENAME
    bundled_lock = skill_dir / RUNTIME_LOCK_BASENAME
    assert bundled_lock.is_file()
    assert bundled_lock.read_bytes() == committed_lock.read_bytes()

    # Release metadata includes wheel + lock hashes matching on-disk files.
    release_meta = json.loads(
        (skill_dir / "vendor" / "release.json").read_text(encoding="utf-8")
    )
    assert release_meta["layout"] == "release"
    assert release_meta["core_version"] == version
    vendored = skill_dir / "vendor" / "wheels" / release_meta["core_wheel"]
    assert vendored.is_file()
    assert release_meta["core_wheel_sha256"] == _sha256(vendored)
    assert release_meta["runtime_lock"] == RUNTIME_LOCK_BASENAME
    assert release_meta["runtime_lock_sha256"] == _sha256(bundled_lock)
    notes = (out / f"RELEASE-{version}.txt").read_text(encoding="utf-8")
    assert "require-hashes" in notes
    assert RUNTIME_LOCK_BASENAME in notes
    assert "deferred" not in notes.lower()

    # Zip contents include the wheel and runtime lock.
    with zipfile.ZipFile(out / f"skill-xbloom-studio-brew-{version}.zip") as zf:
        names = zf.namelist()
        assert any(name.startswith("vendor/wheels/xbloom_studio_core-") for name in names)
        assert RUNTIME_LOCK_BASENAME in names

    # Deterministic release-manifest covers publishable artifacts only.
    manifest_path = out / "release-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == version
    names = [entry["name"] for entry in manifest["artifacts"]]
    assert "release-manifest.json" not in names
    assert any(n.endswith(".whl") for n in names)
    assert f"knowledge-{version}.zip" in names
    assert f"skill-xbloom-studio-brew-{version}.zip" in names
    for entry in manifest["artifacts"]:
        assert set(entry) >= {"name", "version", "size", "sha256"}
        assert entry["version"] == version
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert (out / entry["name"]).stat().st_size == entry["size"]
        assert _sha256(out / entry["name"]) == entry["sha256"]


def test_build_release_byte_for_byte_reproducible(tmp_path):
    """Two consecutive clean builds must produce identical wheel and ZIP digests."""

    if not BUILD_RELEASE.is_file():
        pytest.skip("tools/build_release.py not present")

    import os

    version = _core_version()
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1704067200", "PYTHONHASHSEED": "0"}
    digests: list[dict[str, str]] = []
    for index in (1, 2):
        out = tmp_path / f"dist-{index}"
        subprocess.run(
            [sys.executable, str(BUILD_RELEASE), "--out", str(out)],
            check=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
        wheel = sorted(out.glob(f"xbloom_studio_core-{version}-*.whl"))
        assert len(wheel) == 1
        knowledge_zip = out / f"knowledge-{version}.zip"
        skill_zip = out / f"skill-xbloom-studio-brew-{version}.zip"
        assert knowledge_zip.is_file()
        assert skill_zip.is_file()
        digests.append(
            {
                "wheel": _sha256(wheel[0]),
                "knowledge_zip": _sha256(knowledge_zip),
                "skill_zip": _sha256(skill_zip),
            }
        )
    assert digests[0]["wheel"] == digests[1]["wheel"], digests
    assert digests[0]["knowledge_zip"] == digests[1]["knowledge_zip"], digests
    assert digests[0]["skill_zip"] == digests[1]["skill_zip"], digests


def test_release_manifest_verifier_accepts_valid_and_rejects_tamper(tmp_path):
    if not BUILD_RELEASE.is_file():
        pytest.skip("tools/build_release.py not present")

    import os

    build = _load_build_release_module()
    out = tmp_path / "dist"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1704067200"}
    subprocess.run(
        [sys.executable, str(BUILD_RELEASE), "--out", str(out)],
        check=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    # Valid manifest verifies cleanly.
    data = build.verify_release_manifest(out)
    assert data["artifacts"]
    assert data["schema"] == build.RELEASE_MANIFEST_SCHEMA

    # Tamper a listed artifact: size/hash must fail closed.
    version = _core_version()
    target = out / f"knowledge-{version}.zip"
    original = target.read_bytes()
    target.write_bytes(original + b"\n#tamper\n")
    with pytest.raises(RuntimeError, match="sha256 mismatch|size mismatch"):
        build.verify_release_manifest(out)

    # Restore artifact, tamper the manifest digest instead.
    target.write_bytes(original)
    manifest_path = out / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["artifacts"]:
        if entry["name"] == target.name:
            entry["sha256"] = "0" * 64
            break
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        build.verify_release_manifest(out)


def test_release_manifest_closed_set_and_schema(tmp_path):
    """Closed publishable set: one core wheel, knowledge zip, skill zip; exact schema."""

    if not BUILD_RELEASE.is_file():
        pytest.skip("tools/build_release.py not present")

    import os

    build = _load_build_release_module()
    out = tmp_path / "dist"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1704067200"}
    subprocess.run(
        [sys.executable, str(BUILD_RELEASE), "--out", str(out)],
        check=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    version = _core_version()
    manifest_path = out / "release-manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(original)

    # Baseline valid.
    build.verify_release_manifest(out)

    # Remove an entry (missing knowledge zip from closed set).
    knowledge_name = f"knowledge-{version}.zip"
    reduced = [e for e in manifest["artifacts"] if e["name"] != knowledge_name]
    manifest_path.write_text(
        json.dumps({**manifest, "artifacts": reduced}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="closed set|missing"):
        build.verify_release_manifest(out)

    # Restore, add an unexpected entry.
    unexpected_path = out / "NOTES-extra.txt"
    unexpected_path.write_text("nope\n", encoding="utf-8")
    bloated = list(manifest["artifacts"]) + [
        {
            "name": unexpected_path.name,
            "version": version,
            "size": unexpected_path.stat().st_size,
            "sha256": _sha256(unexpected_path),
        }
    ]
    manifest_path.write_text(
        json.dumps({**manifest, "artifacts": bloated}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="closed set|unexpected"):
        build.verify_release_manifest(out)

    # Alter schema.
    manifest_path.write_text(
        json.dumps({**manifest, "schema": "not-a-valid-schema/v0"}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="schema"):
        build.verify_release_manifest(out)

    # Second core wheel entry (ambiguity / closed-set violation).
    wheels = sorted(out.glob(f"xbloom_studio_core-{version}-*.whl"))
    assert len(wheels) == 1
    second = out / f"xbloom_studio_core-{version}-py3-none-win_amd64.whl"
    second.write_bytes(wheels[0].read_bytes())
    two_wheels = list(manifest["artifacts"]) + [
        {
            "name": second.name,
            "version": version,
            "size": second.stat().st_size,
            "sha256": _sha256(second),
        }
    ]
    manifest_path.write_text(
        json.dumps({**manifest, "artifacts": two_wheels}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="exactly one core wheel|closed set"):
        build.verify_release_manifest(out)

    # Restore valid for cleanliness.
    manifest_path.write_text(original, encoding="utf-8")
    build.verify_release_manifest(out)

    # JSON booleans must not pass size validation (bool subclasses int).
    for bad_size in (True, False):
        mutated = json.loads(original)
        mutated["artifacts"][0]["size"] = bad_size
        manifest_path.write_text(
            json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="invalid size"):
            build.verify_release_manifest(out)

    manifest_path.write_text(original, encoding="utf-8")
    build.verify_release_manifest(out)


def test_extracted_skill_zip_structure_and_integrity(tmp_path):
    """Extract the Skill ZIP outside the checkout and check release integrity."""

    if not BUILD_RELEASE.is_file():
        pytest.skip("tools/build_release.py not present")

    import os

    module = _load_bootstrap_module()
    version = _core_version()
    out = tmp_path / "dist"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1704067200"}
    subprocess.run(
        [sys.executable, str(BUILD_RELEASE), "--out", str(out)],
        check=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    skill_zip = out / f"skill-xbloom-studio-brew-{version}.zip"
    assert skill_zip.is_file()

    # Fresh directory outside the repo checkout tree.
    extract_root = tmp_path / "extracted-outside-checkout" / "skill"
    extract_root.mkdir(parents=True)
    with zipfile.ZipFile(skill_zip) as zf:
        # Reject absolute / traversal members defensively.
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            assert not name.startswith("/"), name
            assert ".." not in Path(name).parts, name
        zf.extractall(extract_root)

    assert (extract_root / "scripts" / "bootstrap.py").is_file()
    assert (extract_root / "scripts" / "xbloom.py").is_file()
    assert (extract_root / "SKILL.md").is_file()
    assert (extract_root / "vendor" / "release.json").is_file()
    assert (extract_root / "vendor" / "wheels").is_dir()
    assert (extract_root / RUNTIME_LOCK_BASENAME).is_file()
    req = (extract_root / "requirements.txt").read_text(encoding="utf-8")
    assert f"xbloom-studio-core=={version}" in req
    assert "-e " not in req
    assert "deferred" not in req.lower()
    # Must not depend on sibling monorepo checkout.
    assert not (extract_root / "packages" / "core").exists()
    assert not (extract_root / "packages").exists()

    meta = module._load_release_meta(extract_root)
    assert meta is not None
    assert meta["layout"] == "release"
    assert meta["core_version"] == version
    wheel = module._release_core_wheel(extract_root)
    assert wheel is not None
    assert wheel.is_file()
    assert wheel.parent == (extract_root / "vendor" / "wheels").resolve()
    assert meta["core_wheel_sha256"] == _sha256(wheel)
    lock = module._release_runtime_lock(extract_root)
    assert lock is not None
    assert lock.is_file()
    assert lock.parent == extract_root.resolve()
    assert meta["runtime_lock"] == RUNTIME_LOCK_BASENAME
    assert meta["runtime_lock_sha256"] == _sha256(lock)
    assert lock.read_bytes() == (SKILL_ROOT / RUNTIME_LOCK_BASENAME).read_bytes()

    # Deterministic ZIP entries: stable timestamps and sorted names.
    with zipfile.ZipFile(skill_zip) as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert RUNTIME_LOCK_BASENAME in names
        for info in zf.infolist():
            assert info.date_time == (2024, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_DEFLATED


# ---------------------------------------------------------------------------
# CI / release workflow source contracts (static; no full YAML grammar needed)
# ---------------------------------------------------------------------------

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEST_WORKFLOW = WORKFLOWS_DIR / "test.yml"
RELEASE_WORKFLOW = WORKFLOWS_DIR / "release.yml"


def _workflow_text(path: Path) -> str:
    assert path.is_file(), f"missing workflow: {path}"
    return path.read_text(encoding="utf-8")


def _safe_load_workflow(path: Path) -> dict:
    """Parse workflow YAML with the skill's PyYAML (safe_load only)."""
    import yaml

    data = yaml.safe_load(_workflow_text(path))
    assert isinstance(data, dict), f"workflow root must be a mapping: {path}"
    return data


def _extract_run_script_bodies(workflow_text: str) -> list[str]:
    """Collect shell bodies of run: | / run: > steps (indent-based, not a full parser)."""
    lines = workflow_text.splitlines()
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^(\s*)run:\s*([|>])?\s*(.*)$", line)
        if not match:
            i += 1
            continue
        indent, block, inline = match.group(1), match.group(2), match.group(3)
        if block:
            body_lines: list[str] = []
            i += 1
            body_indent = None
            while i < len(lines):
                cur = lines[i]
                if not cur.strip():
                    body_lines.append(cur)
                    i += 1
                    continue
                cur_indent = len(cur) - len(cur.lstrip(" "))
                if body_indent is None:
                    if cur_indent <= len(indent):
                        break
                    body_indent = cur_indent
                if cur_indent < body_indent:
                    break
                body_lines.append(cur)
                i += 1
            bodies.append("\n".join(body_lines))
            continue
        if inline:
            bodies.append(inline)
        i += 1
    return bodies


def test_ci_test_workflow_runtime_versions_use_importlib_metadata():
    """Fresh-wheel step must not rely on bleak.__version__ (absent in bleak 3.0.2)."""
    text = _workflow_text(TEST_WORKFLOW)
    assert "bleak.__version__" not in text
    assert "yaml.__version__" not in text
    assert "importlib.metadata" in text
    assert "version('bleak')" in text or 'version("bleak")' in text
    assert "version('PyYAML')" in text or 'version("PyYAML")' in text
    # Still imports the modules (presence check) without package version attrs.
    assert re.search(r"\bimport\s+bleak\b", text)
    assert re.search(r"\byaml\b", text) and "import bleak, yaml" in text
    data = _safe_load_workflow(TEST_WORKFLOW)
    assert "jobs" in data


def test_ci_test_workflow_is_reusable_with_optional_ref():
    """test.yml must be callable with an optional exact-ref input for release gating."""
    text = _workflow_text(TEST_WORKFLOW)
    data = _safe_load_workflow(TEST_WORKFLOW)

    # Existing triggers preserved; workflow_call added for reusable exact-ref runs.
    # PyYAML 1.1 may coerce the key "on" to True; accept either form.
    on_block = data.get("on", data.get(True))
    assert isinstance(on_block, dict), "workflow on: must be a mapping"
    for trigger in ("push", "pull_request", "workflow_dispatch", "workflow_call"):
        assert trigger in on_block, f"test.yml must keep trigger: {trigger}"

    call = on_block["workflow_call"]
    assert isinstance(call, dict)
    inputs = call.get("inputs") or {}
    assert "ref" in inputs, "workflow_call must declare optional ref input"
    ref_input = inputs["ref"]
    assert isinstance(ref_input, dict)
    assert ref_input.get("type") == "string"
    # Optional: required false or omitted (default optional).
    assert ref_input.get("required", False) in (False, None)

    jobs = data["jobs"]
    assert "test" in jobs
    assert "release-artifacts" in jobs

    # Both checkout steps must honor inputs.ref with github.sha fallback.
    checkout_count = len(re.findall(r"(?m)^\s*-\s*uses:\s*actions/checkout@v4\s*$", text))
    assert checkout_count >= 2, f"expected both jobs to checkout, found {checkout_count}"
    ref_expr_matches = re.findall(
        r"ref:\s*\$\{\{\s*inputs\.ref\s*\|\|\s*github\.sha\s*\}\}", text
    )
    assert len(ref_expr_matches) == checkout_count, (
        "every checkout must use inputs.ref || github.sha; "
        f"checkouts={checkout_count} ref_exprs={len(ref_expr_matches)}"
    )


def test_release_workflow_source_contracts():
    """Static contracts for gated release.yml: tag resolve, validation, publish."""
    text = _workflow_text(RELEASE_WORKFLOW)
    data = _safe_load_workflow(RELEASE_WORKFLOW)

    # Dispatch input / event name mapped through env, not shell interpolation.
    assert re.search(r"(?m)^\s*INPUT_TAG:\s*\$\{\{\s*inputs\.tag\s*\}\}", text)
    assert re.search(r"(?m)^\s*EVENT_NAME:\s*\$\{\{\s*github\.event_name\s*\}\}", text)
    assert 'TAG="${INPUT_TAG}"' in text
    assert "${EVENT_NAME}" in text

    run_bodies = _extract_run_script_bodies(text)
    assert run_bodies, "expected at least one run: script body"
    joined_runs = "\n".join(run_bodies)
    assert "${{ inputs.tag }}" not in joined_runs
    assert "${{ inputs." not in joined_runs
    assert "${{ github.event_name }}" not in joined_runs
    # Job/step outputs must enter shell only via env, not raw expressions in run.
    assert "${{ steps.tag.outputs.tag }}" not in joined_runs
    assert "${{ steps.artifacts.outputs." not in joined_runs
    assert "${{ needs.resolve.outputs.tag }}" not in joined_runs
    assert "${{ needs." not in joined_runs

    # No Python-style !r Bash parameter expansion on TAG.
    assert "${TAG!r}" not in text
    assert not re.search(r"\$\{[^}]*!r\}", text)

    jobs = data["jobs"]
    assert isinstance(jobs, dict)
    # Explicit three-job gate: resolve -> validate (reusable test) -> publish.
    assert set(jobs) >= {"resolve", "validate", "publish"}

    resolve_job = jobs["resolve"]
    validate_job = jobs["validate"]
    publish_job = jobs["publish"]

    # Default workflow permissions: contents read only. Write only on publish
    # (gh release create). resolve/validate must not receive contents: write.
    top_perms = data.get("permissions")
    assert isinstance(top_perms, dict), "release.yml must declare top-level permissions"
    assert top_perms.get("contents") == "read", (
        "default permissions.contents must be read (not write)"
    )
    assert top_perms.get("contents") != "write"
    # resolve: inherit default read; must not elevate to write.
    resolve_perms = resolve_job.get("permissions")
    if resolve_perms is not None:
        assert isinstance(resolve_perms, dict)
        assert resolve_perms.get("contents") != "write"
        assert resolve_perms.get("contents") in (None, "read")
    # validate: reusable call; contents read only (explicit or inherited).
    validate_perms = validate_job.get("permissions")
    if validate_perms is not None:
        assert isinstance(validate_perms, dict)
        assert validate_perms.get("contents") != "write"
        assert validate_perms.get("contents") in (None, "read")
    # publish: must grant contents write for gh release create only.
    publish_perms = publish_job.get("permissions")
    assert isinstance(publish_perms, dict), (
        "publish job must declare permissions (contents: write)"
    )
    assert publish_perms.get("contents") == "write"

    assert "outputs" in resolve_job
    assert "tag" in resolve_job["outputs"]

    # Cross-platform validation calls reusable test.yml with the exact tag ref.
    assert validate_job.get("uses") in (
        "./.github/workflows/test.yml",
        ".github/workflows/test.yml",
    )
    validate_needs = validate_job.get("needs")
    if isinstance(validate_needs, str):
        assert validate_needs == "resolve"
    else:
        assert "resolve" in list(validate_needs or [])
    with_block = validate_job.get("with") or {}
    assert "ref" in with_block
    ref_value = with_block["ref"]
    assert "needs.resolve.outputs.tag" in str(ref_value)

    # Publish requires both resolve and successful validation (all matrix jobs).
    publish_needs = publish_job.get("needs")
    if isinstance(publish_needs, str):
        publish_needs_list = [publish_needs]
    else:
        publish_needs_list = list(publish_needs or [])
    assert "resolve" in publish_needs_list
    assert "validate" in publish_needs_list

    # Tag-validation run body must not invoke python (format-only resolve).
    tag_body = next(
        (b for b in run_bodies if "INPUT_TAG" in b or "GITHUB_REF_NAME" in b),
        "",
    )
    assert tag_body, "tag validation run body not found"
    assert "python" not in tag_body.lower()

    # In publish: checkout exact tag, then setup-python, then core version parse.
    tag_step_idx = text.find("Resolve and verify release tag")
    # Prefer publish job checkout (second actions/checkout if any; first may be absent
    # from resolve). Locate checkout that pins needs.resolve.outputs.tag.
    checkout_tag_idx = text.find("needs.resolve.outputs.tag")
    setup_idx = text.find("actions/setup-python")
    version_extract_idx = text.find("VERSION=$(python")
    if version_extract_idx == -1:
        version_extract_idx = text.find("Path('packages/core/pyproject.toml')")
    if version_extract_idx == -1:
        version_extract_idx = text.find('Path("packages/core/pyproject.toml")')
    assert setup_idx != -1, "release workflow must use actions/setup-python"
    assert version_extract_idx != -1, "expected repository Python version extraction"
    assert checkout_tag_idx != -1, "publish/validate must pin needs.resolve.outputs.tag"
    assert 0 <= tag_step_idx < setup_idx < version_extract_idx, (
        "order must be: tag resolve -> setup-python -> core version parse"
    )
    # setup-python must precede the Python parse of repository files.
    assert setup_idx < version_extract_idx

    setup_slice = text[setup_idx : setup_idx + 400]
    assert "cache: pip" in setup_slice
    assert "cache-dependency-path" in setup_slice

    # Publish assets: wheel, knowledge zip, Skill zip, release-manifest.
    # Notes file is --notes-file only (not uploaded as a release asset).
    # --verify-tag: do not auto-create a missing tag from the default branch.
    create_idx = text.find("gh release create")
    assert create_idx != -1
    create_tail = text[create_idx : create_idx + 500]
    assert "--verify-tag" in create_tail
    assert "--notes-file" in create_tail
    assert '"${WHEEL}"' in create_tail
    assert '"${KNOWLEDGE}"' in create_tail
    assert '"${SKILL}"' in create_tail
    assert '"${MANIFEST}"' in create_tail
    without_notes_flag = re.sub(
        r'--notes-file\s+"\$\{NOTES\}"', "", create_tail
    )
    without_notes_flag = re.sub(
        r"--notes-file\s+\$\{NOTES\}", "", without_notes_flag
    )
    assert not re.search(
        r'(?m)^\s*"\$\{NOTES\}"\s*\\?\s*$', without_notes_flag
    ), "NOTES must not be uploaded as a release asset"
    assert not re.search(
        r"(?m)^\s*\$\{NOTES\}\s*\\?\s*$", without_notes_flag
    ), "NOTES must not be uploaded as a release asset"

    # Safer env pattern for tag on the publish step (resolved job output).
    assert re.search(
        r"(?m)^\s*TAG:\s*\$\{\{\s*needs\.resolve\.outputs\.tag\s*\}\}", text
    )
    assert re.search(
        r"(?m)^\s*WHEEL:\s*\$\{\{\s*steps\.artifacts\.outputs\.wheel\s*\}\}", text
    )
    assert re.search(
        r"(?m)^\s*NOTES:\s*\$\{\{\s*steps\.artifacts\.outputs\.notes\s*\}\}", text
    )

    # Publish job must create the GitHub Release (only place for gh release create).
    assert "gh release create" in text
    # Guard against a second independent un-gated publish path.
    assert text.count("gh release create") == 1
    # The one release-create invocation must pass --verify-tag (not optional).
    assert create_tail.count("--verify-tag") >= 1
    assert re.search(
        r"(?m)^\s*gh release create\b[\s\S]*?--verify-tag\b",
        create_tail,
    ), "gh release create must include --verify-tag"

    # GITHUB_ENV / set -u version case consistency (v1.2.0 publish regression).
    # Linux runners treat env names as case-sensitive: writing version= leaves VERSION
    # unset under set -u and aborts collect/upload before gh release create.
    assert re.search(
        r'(?m)^\s*echo\s+"VERSION=\$\{VERSION\}"\s*>>\s*"\$\{GITHUB_ENV\}"\s*$',
        text,
    ), "publish must export uppercase VERSION to GITHUB_ENV for set -u consumers"
    assert not re.search(
        r'(?m)^\s*echo\s+"version=\$\{VERSION\}"\s*>>\s*"\$\{GITHUB_ENV\}"\s*$',
        text,
    ), "must not write lowercase version= to GITHUB_ENV (case mismatch with ${VERSION})"
    # Downstream set -u publish bodies must read uppercase VERSION after the export.
    version_export_idx = text.find('echo "VERSION=${VERSION}" >> "${GITHUB_ENV}"')
    assert version_export_idx != -1
    after_export = text[version_export_idx:]
    for needle in (
        'VERSION="${VERSION}"',
        "skill-xbloom-studio-brew-${VERSION}.zip",
        "knowledge-${VERSION}.zip",
        "xbloom_studio_core-${VERSION}-*.whl",
        "RELEASE-${VERSION}.txt",
    ):
        assert needle in after_export, (
            f"post-export publish path must use uppercase ${{VERSION}}: missing {needle!r}"
        )
