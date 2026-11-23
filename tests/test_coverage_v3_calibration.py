"""Step 1 offline calibration suite for v3 coverage evidence scoring.

Reproduces both v2 failures (B01 TR-101 direction swap false positive; C01 date paraphrase false
negative) and tests all 8 separately authored calibration categories under v3.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.coverage_audit import score_coverage_audit_brief_v2, verify_coverage_citation_v2
from evals.coverage_audit_v3 import score_coverage_audit_brief_v3, verify_coverage_citation_v3
from workflows.coverage_audit.models import CoverageAuditBrief, CoverageCitation
from workflows.coverage_audit.tools import load_packet_documents

HELDOUT_KEY_PATH = Path("evals/datasets/coverage_audit_heldout_key.json")
HELDOUT_V1_V2_PATH = Path("artifacts/experiments/coverage_audit_heldout_v1_v2.json")


def _heldout_key(packet_id: str) -> dict[str, object]:
    for row in json.loads(HELDOUT_KEY_PATH.read_text(encoding="utf-8")):
        if row["packet_id"] == packet_id:
            return dict(row)
    raise KeyError(packet_id)


def test_repro_a_b01_trial3_tr101_direction_swap_v2_false_positive_fixed_in_v3() -> None:
    """Reproduce v2 false positive on mutated B01 trial 3 citation[2] and verify v3 catches it."""
    bundle = json.loads(HELDOUT_V1_V2_PATH.read_text(encoding="utf-8"))
    passing_rows = [r for r in bundle["runs"] if r["v2_passed"]]
    assert len(passing_rows) == 1
    orig = passing_rows[0]
    assert orig["packet_id"] == "B01" and orig["trial"] == 3

    mutated_dict = json.loads(json.dumps(orig["brief"]))
    mutated_dict["evidence_citations"][2]["claim_supported"] = (
        "Endorsement TR-101 reduced the limit from $250,000 to $100,000 per conveyance."
    )
    mutated_dict["evidence_citations"][2]["value"] = "$100,000"
    mutated_brief = CoverageAuditBrief.model_validate(mutated_dict)
    key_b01 = _heldout_key("B01")

    # Confirm v2 falsely passes the mutated brief
    v2_res = score_coverage_audit_brief_v2(key_b01, mutated_brief)
    assert v2_res["passed"] is True

    # Confirm v3 rejects citation[2] as contradicted and fails the brief
    v3_res = score_coverage_audit_brief_v3(key_b01, mutated_brief)
    assert v3_res["passed"] is False
    assert v3_res["contradicted_citations"] >= 1
    assert v3_res["citation_verdicts"][2]["verdict"] == "contradicted"
    assert "swap" in v3_res["citation_verdicts"][2]["reason"].lower()


def test_repro_b_c01_policy_period_paraphrases_v2_false_negative_fixed_in_v3() -> None:
    """Reproduce v2 false negative on C01 date paraphrase and verify v3 supports both."""
    docs_c01 = load_packet_documents("C01")
    quote = "Policy Period: 2026-01-01 to 2027-01-01"
    c1 = CoverageCitation(
        doc_id="policy_declarations.txt",
        line_start=4,
        line_end=4,
        verbatim_quote=quote,
        claim_supported="The policy period includes 2026-04-08.",
        subject="Beacon Health Systems LLC",
    )
    c2 = CoverageCitation(
        doc_id="policy_declarations.txt",
        line_start=4,
        line_end=4,
        verbatim_quote=quote,
        claim_supported="The incident date of 2026-04-08 falls within the policy period.",
        subject="Beacon Health Systems LLC",
    )

    # Confirm v2 rejects the equivalent second paraphrase
    ok_v2_1, _ = verify_coverage_citation_v2(c1, docs_c01, expected_subject="Beacon Health Systems")
    ok_v2_2, _ = verify_coverage_citation_v2(c2, docs_c01, expected_subject="Beacon Health Systems")
    assert ok_v2_1 is True
    assert ok_v2_2 is False

    # Confirm v3 supports BOTH equivalent true claims
    r1 = verify_coverage_citation_v3(c1, docs_c01, expected_subject="Beacon Health Systems LLC")
    r2 = verify_coverage_citation_v3(c2, docs_c01, expected_subject="Beacon Health Systems LLC")
    assert r1["verdict"] == "supported"
    assert r2["verdict"] == "supported"


def test_v3_calibration_role_value_direction_and_supersession_swaps() -> None:
    """Category 1: Role/value/direction swaps and supersession-target swaps -> contradicted."""
    docs_b01 = load_packet_documents("B01")
    # Supersession direction swap: TR-115 supersedes TR-108, NOT TR-101
    r_sup = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_tr_115.txt",
            line_start=6,
            line_end=6,
            verbatim_quote="Expressly supersedes and replaces Endorsement TR-108.",
            claim_supported="Endorsement TR-115 supersedes TR-101.",
        ),
        docs_b01,
    )
    assert r_sup["verdict"] == "contradicted"

    # Replaced value asserted as new value
    r_role = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_tr_101.txt",
            line_start=6,
            line_end=6,
            verbatim_quote=(
                "Replaces Base Property-in-Transit Limit of $100,000 with $250,000 per conveyance."
            ),
            claim_supported="Endorsement TR-101 establishes the transit limit.",
            value="$100,000",
        ),
        docs_b01,
    )
    assert r_role["verdict"] == "contradicted"


def test_v3_calibration_wrong_actor_site_date_negation_stale_missing_cross_source() -> None:
    """Categories 2–8: actor, site, date, negation, stale authority, missing, cross-source."""
    # Wrong actor / entity (B04 parent endorsement applied to subsidiary)
    docs_b04 = load_packet_documents("B04")
    r_actor = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_bl_402.txt",
            line_start=6,
            line_end=6,
            verbatim_quote=(
                "Spoilage Sublimit: $1,000,000 per occurrence for Summit Holdings Corp. operations."
            ),
            claim_supported="Summit BioLabs LLC has $1,000,000 spoilage sublimit.",
            subject="Summit BioLabs LLC",
            value="$1,000,000",
        ),
        docs_b04,
    )
    assert r_actor["verdict"] == "contradicted"

    # Wrong site (B06 Plant 2 endorsement applied to Plant 1)
    docs_b06 = load_packet_documents("B06")
    r_site = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_dd_304.txt",
            line_start=6,
            line_end=6,
            verbatim_quote=(
                "Amends Endorsement DD-301 solely for Plant 2 (Toledo): "
                "controlling deductible for Plant 2 is $35,000 per occurrence."
            ),
            claim_supported="Controlling deductible for Plant 1 is $35,000.",
            value="$35,000",
        ),
        docs_b06,
    )
    assert r_site["verdict"] == "contradicted"

    # Wrong date (B02 prospective FL-201 effective 2026-04-01 claimed to apply to 2026-03-15)
    docs_b02 = load_packet_documents("B02")
    r_date_bad = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_fl_201.txt",
            line_start=5,
            line_end=5,
            verbatim_quote="Effective Date: 2026-04-01",
            claim_supported="FL-201 applies to the 2026-03-15 flood loss.",
        ),
        docs_b02,
    )
    assert r_date_bad["verdict"] == "contradicted"

    # Valid paraphrase: FL-201 does not apply to 2026-03-15 loss -> supported
    r_date_ok = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_fl_201.txt",
            line_start=5,
            line_end=6,
            verbatim_quote="Effective Date: 2026-04-01",
            claim_supported="FL-201 is prospective and does not apply to the 2026-03-15 loss.",
            polarity="negated",
        ),
        docs_b02,
    )
    assert r_date_ok["verdict"] == "supported"

    # Negation / stale authority (unsigned MX-502 claimed bound)
    docs_b07 = load_packet_documents("B07")
    r_neg = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="unsigned_endorsement_mx_502.txt",
            line_start=7,
            line_end=7,
            verbatim_quote="Carrier Authorized Signature: [BLANK — NOT EXECUTED]",
            claim_supported="MX-502 was executed and bound.",
            polarity="affirmed",
        ),
        docs_b07,
    )
    assert r_neg["verdict"] == "contradicted"

    # Cross-source overclaim: C01 Jan 3 billing receipt cannot alone prove no lapse before April
    docs_c01 = load_packet_documents("C01")
    r_cross = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="billing_receipt_q1.txt",
            line_start=2,
            line_end=2,
            verbatim_quote="Policy: PKG-2026-1011 | Paid in Full: $42,800 | Date: 2026-01-03",
            claim_supported="Policy was paid in full on 2026-01-03 with no lapse before April.",
        ),
        docs_c01,
    )
    assert r_cross["verdict"] == "unverified"

    # Missing evidence / unsupported fabricated concept
    r_fab = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_cy_102.txt",
            line_start=6,
            line_end=6,
            verbatim_quote=(
                "The Cyber Incident Response Sublimit is increased from $100,000 to $500,000."
            ),
            claim_supported="Sublimit is $500,000 with earthquake-resistant certification.",
        ),
        docs_c01,
    )
    assert r_fab["verdict"] == "unverified"


def test_v3_independent_relationship_role_swap_controls() -> None:
    """All 4 independent role-swap controls -> contradicted."""
    # 1. B01 TR-115 line 7: $300,000 transit limit called a deductible
    r1 = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_tr_115.txt",
            line_start=7,
            line_end=7,
            verbatim_quote="Controlling Property-in-Transit Limit: $300,000 per conveyance.",
            claim_supported=(
                "The controlling property-in-transit deductible is $300,000 per conveyance."
            ),
            subject="Pacific Freightways Inc.",
            value="$300,000",
        ),
        load_packet_documents("B01"),
    )
    assert r1["verdict"] == "contradicted"

    # 2. B06 DD-304 line 6: swaps Plant 1 ($50k) and Plant 2 ($35k) deductible assignments
    r2 = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_dd_304.txt",
            line_start=6,
            line_end=6,
            verbatim_quote=(
                "Amends Endorsement DD-301 solely for Plant 2 (Toledo): controlling deductible "
                "for Plant 2 is $35,000 per occurrence. Plant 1 remains at $50,000."
            ),
            claim_supported="Plant 1 has a $35,000 deductible and Plant 2 remains at $50,000.",
            subject="Ironworks Fabrication Inc.",
        ),
        load_packet_documents("B06"),
    )
    assert r2["verdict"] == "contradicted"

    # 3. B04 BL-408 line 6: $250,000 spoilage sublimit called a deductible
    r3 = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="endorsement_bl_408.txt",
            line_start=6,
            line_end=6,
            verbatim_quote=(
                "Grants Spoilage Coverage specifically to Summit BioLabs LLC with a controlling "
                "sublimit of $250,000 per occurrence."
            ),
            claim_supported=(
                "Summit BioLabs LLC gets a $250,000 spoilage deductible per occurrence."
            ),
            subject="Summit BioLabs LLC",
            value="$250,000",
        ),
        load_packet_documents("B04"),
    )
    assert r3["verdict"] == "contradicted"

    # 4. B08 loss_run_2026 line 5: $450,000 paid erosion called current annual aggregate limit
    r4 = verify_coverage_citation_v3(
        CoverageCitation(
            doc_id="loss_run_2026.txt",
            line_start=5,
            line_end=5,
            verbatim_quote="Total Paid Erosion Against 2026 Annual Aggregate: $450,000",
            claim_supported="The current annual aggregate limit is $450,000.",
            subject="Atlas Industrial Services Corp.",
            value="$450,000",
        ),
        load_packet_documents("B08"),
    )
    assert r4["verdict"] == "contradicted"
