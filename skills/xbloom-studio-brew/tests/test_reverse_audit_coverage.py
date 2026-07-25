"""Ensure docs/XBLOOM-REVERSE-AUDIT.md covers shipped STATE_NAMES / REPORT_NAMES.

Analysis-goal durable proof: drives real telemetry tables from packages/core,
not a re-implementation of the decoder.
"""

from __future__ import annotations

from pathlib import Path

from xbloom_ble.telemetry import REPORT_NAMES, STATE_NAMES

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDIT = REPO_ROOT / "docs" / "XBLOOM-REVERSE-AUDIT.md"


def test_reverse_audit_document_exists_with_required_sections():
    text = AUDIT.read_text(encoding="utf-8")
    for heading in (
        "## 1. Operation-path inventory",
        "## 2. State & status-code map",
        "## 3. Error & mismatch audit",
        "## 4. Capability gap matrix",
        "## 5. Development directions",
    ):
        assert heading in text, f"missing section {heading}"


def test_audit_lists_every_core_state_name():
    text = AUDIT.read_text(encoding="utf-8")
    for code, name in STATE_NAMES.items():
        assert name in text, f"STATE_NAMES[{code:#x}]={name!r} not cited in audit"
        # hex form used in tables
        assert f"{code:#x}" in text.lower() or f"0x{code:02x}" in text.lower()


def test_audit_lists_every_core_report_code():
    text = AUDIT.read_text(encoding="utf-8")
    for code, name in REPORT_NAMES.items():
        assert str(code) in text, f"REPORT_NAMES code {code} ({name}) not in audit"
        assert name in text, f"REPORT_NAMES name {name!r} not in audit"


def test_audit_covers_required_gap_and_error_rows():
    text = AUDIT.read_text(encoding="utf-8")
    for needle in (
        "Coffee load",
        "Tea load",
        "Easy write",
        "Easy read",
        "FreeSolo scale",
        "Catalog sync",
        "Share import",
        "40521",
        "0x45",
        "8104",
    ):
        assert needle in text, f"required audit content missing: {needle}"
