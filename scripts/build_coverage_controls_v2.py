# ruff: noqa: E501
"""Materialize the two missing synthetic control packets (CTRL-C04 & CTRL-B08) and separate keys.

Strictly separate from the original `policy-archive/` corpus to preserve old artifacts and hashes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

ARCHIVE_ROOT = Path("packages/workflows/src/workflows/coverage_audit/policy-archive-controls")
KEY_PATH = Path("evals/datasets/coverage_audit_controls_v2_key.json")
MANIFEST_PATH = Path("evals/datasets/coverage_audit_controls_v2_manifest.json")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    files: dict[str, dict[str, str]] = {
        "CTRL-C04": {
            "policy_section_06_machinery.txt": (
                "SYNTHETIC COMMERCIAL PROPERTY FORM — SECTION 6 (OPTIONAL MACHINERY COVERAGE)\n"
                "Policy Number: SYN-2026-4404\n"
                "Named Insured: Northport Cold Milling LLC\n"
                "Policy Period: 2026-01-01 to 2027-01-01\n"
                "Section 6 (P4 §6): Optional Machinery Breakdown Coverage is NOT automatically included. "
                "Optional Machinery Breakdown Coverage applies ONLY if the insured executed and returned "
                "a signed Machinery Selection Form (Form MS-10) prior to inception or an official signed "
                "issuance record confirms election.\n"
            ),
            "intake_record_machinery.txt": (
                "SYNTHETIC UNDERWRITING PACKET INTAKE NOTE (I4 §2)\n"
                "Account: Northport Cold Milling LLC | Policy Number: SYN-2026-4404\n"
                "Intake Date: 2026-05-14\n"
                "Record Status: The supplied packet contains NO signed Machinery Selection Form (Form MS-10) "
                "and does not establish whether a signed selection form or definitive issuance record exists.\n"
                "Audit Question: Does optional machinery coverage apply?\n"
            ),
            "equipment_inventory_2026.txt": (
                "FACILITY EQUIPMENT INVENTORY (INFORMATIONAL)\n"
                "Insured: Northport Cold Milling LLC\n"
                "Units on site: 2 refrigeration chillers, 1 industrial grain mill (installed 2024).\n"
                "Note: Inventory listing does not confer optional machinery coverage.\n"
            ),
            "billing_summary_2026.txt": (
                "ANNUAL BILLING SUMMARY\n"
                "Policy Number: SYN-2026-4404 | Base Property Premium: $31,400 (Paid 2026-01-04).\n"
                "Optional endorsement line items are not itemized on this summary sheet.\n"
            ),
        },
        "CTRL-B08": {
            "policy_section_02_theft.txt": (
                "SYNTHETIC COMMERCIAL PROPERTY FORM — SECTION 2 (THEFT EXCLUSION)\n"
                "Policy Number: SYN-2026-8808\n"
                "Named Insured: Harborline Maritime LLC\n"
                "Policy Period: 2026-01-01 to 2027-01-01\n"
                "Section 2 (P18 §2): Loss or damage caused by Theft is EXCLUDED unless expressly added "
                "by executed site endorsement.\n"
            ),
            "endorsement_e18_a_theft.txt": (
                "SYNTHETIC ENDORSEMENT E18-A — SITE THEFT COVERAGE EXTENSION (E18-A §1)\n"
                "Status: EXECUTED & COUNTERSIGNED\n"
                "Policy Number: SYN-2026-8808\n"
                "Named Insured: Harborline Maritime LLC\n"
                "Effective Date: 2026-01-01\n"
                "Adds Theft coverage specifically at site HBR-4 with a $5,000 deductible per occurrence.\n"
            ),
            "site_schedule_2026.txt": (
                "SYNTHETIC AUTHORITATIVE SITE SCHEDULE (S18 §3)\n"
                "Policy Number: SYN-2026-8808 | Named Insured: Harborline Maritime LLC\n"
                "Site HBR-4: Harbor (400 Pier Rd, Terminal East)\n"
                "Site HBR-9: Bayview (810 Dock Ave, South Basin)\n"
            ),
            "issuance_register_2026.txt": (
                "SYNTHETIC POLICY ISSUANCE REGISTER (R18 §2)\n"
                "Policy Number: SYN-2026-8808\n"
                "1. E18-A issued 2026-01-01: Adds Theft coverage at HBR-4 ($5,000 deductible).\n"
                "2. E18-B issued 2026-08-20: Replaces the Theft deductible ONLY; E18-B does NOT alter "
                "or revoke Theft coverage applicability granted under E18-A.\n"
            ),
            "endorsement_e18_b_deductible.txt": (
                "SYNTHETIC ENDORSEMENT E18-B — THEFT DEDUCTIBLE AMENDMENT (E18-B §1)\n"
                "Status: EXECUTED & COUNTERSIGNED\n"
                "Policy Number: SYN-2026-8808\n"
                "Named Insured: Harborline Maritime LLC\n"
                "Effective Date: 2026-09-01\n"
                "Scope Limitation: Modifies the Theft deductible at site HBR-4 ONLY, replacing $5,000 with "
                "$12,000 strictly for losses occurring on or after 2026-09-01. Does not apply retroactively "
                "to losses occurring prior to 2026-09-01, and does not change Theft coverage applicability.\n"
            ),
            "incident_report_harbor.txt": (
                "SYNTHETIC INCIDENT & LOSS REPORT (I18 §1)\n"
                "Claim ID: INC-HBR-2026-09\n"
                "Named Insured: Harborline Maritime LLC\n"
                "Incident Location: Harbor\n"
                "Actual Date of Loss: 2026-08-27\n"
                "Date Reported: 2026-09-05\n"
                "Peril: Theft of copper cable spools from fenced yard.\n"
            ),
            "broker_note_september.txt": (
                "From: R. Delgado <rdelgado@portbroker.example>\n"
                "Date: 2026-09-06\n"
                "Subject: Harbor theft claim filed Sept 5\n"
                "Heads up — the claim was reported September 5 and the new $12,000 theft deductible took "
                "effect in September, so please verify which date controls under the executed endorsements.\n"
            ),
        },
    }

    keys: list[dict[str, Any]] = [
        {
            "packet_id": "CTRL-C04",
            "tier": "clean",
            "expected_subject": "Northport Cold Milling LLC",
            "prompt": (
                "Audit packet CTRL-C04 for Northport Cold Milling LLC. Return issue_id "
                "'optional_machinery_coverage': does optional machinery coverage apply?"
            ),
            "expected_issues": [
                {
                    "issue_id": "optional_machinery_coverage",
                    "expected_outcome": "insufficient_evidence",
                    "expected_value": "insufficient_evidence",
                    "required_controlling_docs": [
                        "policy_section_06_machinery.txt",
                        "intake_record_machinery.txt",
                    ],
                    "required_doc_patterns": [
                        {
                            "doc_id": "policy_section_06_machinery.txt",
                            "pattern": r"signed.*selection\s+form|ms-10",
                        },
                        {
                            "doc_id": "intake_record_machinery.txt",
                            "pattern": r"no\s+signed.*selection\s+form|does\s+not\s+establish",
                        },
                    ],
                    "expected_missing_keywords": ["selection form", "ms-10", "issuance record"],
                }
            ],
        },
        {
            "packet_id": "CTRL-B08",
            "tier": "branching",
            "expected_subject": "Harborline Maritime LLC",
            "prompt": (
                "Audit packet CTRL-B08 for Harborline Maritime LLC. Return TWO separate issues: "
                "(1) issue_id='theft_coverage_applicability': does theft coverage apply at Harbor? "
                "(2) issue_id='applicable_deductible': what deductible applies to this loss?"
            ),
            "expected_issues": [
                {
                    "issue_id": "theft_coverage_applicability",
                    "expected_outcome": "applies",
                    "expected_value": "applies",
                    "required_controlling_docs": [
                        "endorsement_e18_a_theft.txt",
                        "site_schedule_2026.txt",
                    ],
                    "required_doc_patterns": [
                        {
                            "doc_id": "endorsement_e18_a_theft.txt",
                            "pattern": r"theft.*hbr-4",
                        },
                        {
                            "doc_id": "site_schedule_2026.txt",
                            "pattern": r"hbr-4.*harbor",
                        },
                    ],
                    "required_scope_tokens": ["hbr-4", "harbor"],
                    "forbidden_scope_tokens": ["hbr-9", "bayview"],
                },
                {
                    "issue_id": "applicable_deductible",
                    "expected_outcome": "applies",
                    "expected_value": "5000",
                    "required_controlling_docs": [
                        "endorsement_e18_a_theft.txt",
                        "endorsement_e18_b_deductible.txt",
                        "incident_report_harbor.txt",
                    ],
                    "required_doc_patterns": [
                        {
                            "doc_id": "endorsement_e18_a_theft.txt",
                            "pattern": r"5000.*deductible|deductible.*5000",
                        },
                        {
                            "doc_id": "endorsement_e18_b_deductible.txt",
                            "pattern": r"2026-09-01",
                        },
                        {
                            "doc_id": "endorsement_e18_b_deductible.txt",
                            "pattern": r"only|does\s+not\s+apply\s+retroactively|does\s+not\s+change\s+theft",
                        },
                        {
                            "doc_id": "incident_report_harbor.txt",
                            "pattern": r"actual\s+date\s+of\s+loss.*2026-08-27",
                        },
                    ],
                    "required_dates": ["2026-08-27", "2026-09-01"],
                    "forbidden_scope_tokens": ["hbr-9", "bayview"],
                },
            ],
        },
    ]

    if ARCHIVE_ROOT.exists():
        shutil.rmtree(ARCHIVE_ROOT)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)

    file_hashes: dict[str, str] = {}
    for pid, docs in sorted(files.items()):
        pdir = ARCHIVE_ROOT / pid
        pdir.mkdir(parents=True, exist_ok=True)
        for fname, content in sorted(docs.items()):
            raw = content.encode("utf-8")
            (pdir / fname).write_bytes(raw)
            file_hashes[f"{pid}/{fname}"] = _sha256(raw)

    key_bytes = (json.dumps(keys, indent=2) + "\n").encode("utf-8")
    KEY_PATH.write_bytes(key_bytes)
    manifest: dict[str, Any] = {
        "archive_root": str(ARCHIVE_ROOT),
        "packets": [k["packet_id"] for k in keys],
        "key_sha256": _sha256(key_bytes),
        "archive_file_count": len(file_hashes),
        "archive_files_sha256": file_hashes,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Built {len(files)} control packets ({len(file_hashes)} docs); "
        f"key_sha256={manifest['key_sha256'][:12]}"
    )


if __name__ == "__main__":
    main()
