"""Versioned knowledge-bundle manifest generation and validation.

The single human-maintained knowledge source is the Agent Skill tree:
``SKILL.md``, ``references/``, and ``assets/``. Release builds copy that tree
into a versioned knowledge bundle with a deterministic manifest of per-file
SHA-256 hashes and an aggregate content hash. Consumers (Web, offline installs)
must validate the manifest before trusting the files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

MANIFEST_NAME = "manifest.json"
DEFAULT_KNOWLEDGE_NAME = "xbloom-studio-knowledge"
# Paths allowed in a knowledge bundle (closed set of roots).
_ALLOWED_TOP_LEVEL = frozenset({"SKILL.md", "references", "assets"})


class KnowledgeError(ValueError):
    """Raised when a knowledge bundle is missing, incomplete, or tampered."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_knowledge_relpath(rel: str) -> str:
    """Normalize and reject unsafe relative path keys for a knowledge manifest.

    Rejects absolute paths, drive roots, empty segments, ``.`` / ``..`` traversal,
    backslashes, and any top-level name outside ``SKILL.md`` / ``references`` /
    ``assets``.
    """

    if not isinstance(rel, str) or not rel:
        raise KnowledgeError("knowledge path must be a non-empty string")
    if "\\" in rel:
        raise KnowledgeError(f"knowledge path must use POSIX separators: {rel}")
    if rel.startswith("/") or rel.startswith("~"):
        raise KnowledgeError(f"knowledge path must be relative: {rel}")
    # Windows drive / UNC style keys (e.g. C:/..., //server/share).
    pure = PurePosixPath(rel)
    if pure.is_absolute() or pure.anchor:
        raise KnowledgeError(f"knowledge path must be relative: {rel}")
    parts = pure.parts
    if not parts:
        raise KnowledgeError(f"knowledge path is empty: {rel}")
    if any(part in ("", ".", "..") for part in parts):
        raise KnowledgeError(f"knowledge path contains traversal or empty segment: {rel}")
    if parts[0] not in _ALLOWED_TOP_LEVEL:
        raise KnowledgeError(
            f"knowledge path outside allowed roots (SKILL.md/references/assets): {rel}"
        )
    if parts[0] == "SKILL.md" and len(parts) != 1:
        raise KnowledgeError(f"invalid SKILL.md path: {rel}")
    return pure.as_posix()


def resolve_under_root(root: Path, rel: str) -> Path:
    """Join *rel* under *root* and require the resolved path stays inside *root*."""

    key = safe_knowledge_relpath(rel)
    root = Path(root).resolve()
    candidate = (root / key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise KnowledgeError(f"knowledge path escapes bundle root: {rel}") from exc
    return candidate


def iter_knowledge_files(root: Path) -> list[str]:
    """Return sorted relative knowledge file paths (POSIX) under *root*.

    Includes ``SKILL.md`` plus every regular file under ``references/`` and
    ``assets/``. Other Skill tree members (scripts, tests, licenses) are not
    part of the knowledge source.
    """

    root = Path(root).resolve()
    found: set[str] = set()
    skill_md = root / "SKILL.md"
    if skill_md.is_file():
        found.add("SKILL.md")
    for dirname in ("references", "assets"):
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def file_hashes(root: Path, relative_paths: Iterable[str] | None = None) -> dict[str, str]:
    root = Path(root).resolve()
    paths = list(relative_paths) if relative_paths is not None else iter_knowledge_files(root)
    hashes: dict[str, str] = {}
    for rel in paths:
        key = safe_knowledge_relpath(rel)
        path = resolve_under_root(root, key)
        if not path.is_file():
            raise KnowledgeError(f"missing knowledge file: {key}")
        hashes[key] = sha256_file(path)
    return hashes


def aggregate_content_hash(file_hashes_map: Mapping[str, str]) -> str:
    """Deterministic aggregate over sorted ``path:hash`` lines."""

    lines = [f"{path}:{file_hashes_map[path]}" for path in sorted(file_hashes_map)]
    payload = "\n".join(lines).encode("utf-8")
    return sha256_bytes(payload)


def build_manifest(
    root: Path,
    *,
    version: str,
    name: str = DEFAULT_KNOWLEDGE_NAME,
    core_version: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a knowledge manifest for *root* (Skill or extracted knowledge tree)."""

    hashes = file_hashes(root)
    if "SKILL.md" not in hashes:
        raise KnowledgeError("knowledge root is missing SKILL.md")
    if not any(key.startswith("references/") for key in hashes):
        raise KnowledgeError("knowledge root is missing references/")
    if not any(key.startswith("assets/") for key in hashes):
        raise KnowledgeError("knowledge root is missing assets/")

    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "core_version": core_version or version,
        "content_hash": aggregate_content_hash(hashes),
        "files": dict(sorted(hashes.items())),
    }
    if extra:
        for key, value in extra.items():
            if key in manifest:
                raise KnowledgeError(f"extra key collides with reserved field: {key}")
            manifest[key] = value
    return manifest


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeError(f"missing knowledge manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeError(f"invalid knowledge manifest JSON: {path}") from exc
    if not isinstance(data, dict):
        raise KnowledgeError("knowledge manifest must be a JSON object")
    return data


def validate_manifest_data(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Validate *manifest* against files under *root*.

    Raises :class:`KnowledgeError` on path traversal, missing files, unexpected
    on-disk knowledge files not listed in the manifest, hash mismatches, or
    aggregate content-hash mismatch.
    """

    root = Path(root).resolve()
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise KnowledgeError("knowledge manifest is missing a files map")
    if expected_version is not None and manifest.get("version") != expected_version:
        raise KnowledgeError(
            f"knowledge version mismatch: expected {expected_version!r}, "
            f"got {manifest.get('version')!r}"
        )

    expected_hash = manifest.get("content_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise KnowledgeError("knowledge manifest is missing content_hash")

    actual: dict[str, str] = {}
    for rel, expected in sorted(files.items()):
        if not isinstance(rel, str) or not isinstance(expected, str):
            raise KnowledgeError("knowledge manifest files entries must be strings")
        key = safe_knowledge_relpath(rel)
        path = resolve_under_root(root, key)
        if not path.is_file():
            raise KnowledgeError(f"missing knowledge file: {key}")
        digest = sha256_file(path)
        if digest != expected:
            raise KnowledgeError(f"tampered knowledge file: {key}")
        actual[key] = digest

    # Closed set: every on-disk knowledge file must be listed in the manifest.
    on_disk = set(iter_knowledge_files(root))
    listed = set(actual)
    unexpected = sorted(on_disk - listed)
    if unexpected:
        raise KnowledgeError(
            "unexpected knowledge file(s) not listed in manifest: "
            + ", ".join(unexpected)
        )
    missing_on_disk = sorted(listed - on_disk)
    if missing_on_disk:
        # Should be unreachable if per-file is_file checks passed, but keep closed.
        raise KnowledgeError(
            "manifest lists knowledge file(s) absent from disk: "
            + ", ".join(missing_on_disk)
        )

    content = aggregate_content_hash(actual)
    if content != expected_hash:
        raise KnowledgeError(
            f"knowledge content_hash mismatch: expected {expected_hash}, got {content}"
        )
    return dict(manifest)


def validate_bundle(
    root: Path,
    *,
    manifest_path: Path | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Load and validate a knowledge bundle root containing ``manifest.json``."""

    root = Path(root).resolve()
    path = Path(manifest_path) if manifest_path is not None else root / MANIFEST_NAME
    manifest = load_manifest(path)
    return validate_manifest_data(root, manifest, expected_version=expected_version)


def copy_knowledge_tree(source_root: Path, destination_root: Path) -> list[str]:
    """Copy knowledge files from *source_root* into *destination_root*.

    Returns the sorted relative POSIX paths that were copied.
    """

    import shutil

    source_root = Path(source_root).resolve()
    destination_root = Path(destination_root).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    relative = list(iter_knowledge_files(source_root))
    for rel in relative:
        src = resolve_under_root(source_root, rel)
        dest = destination_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return relative
