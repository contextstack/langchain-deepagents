"""Step 3 stub controls for CTRL-C04 (clean abstention) & CTRL-B08 (dual-issue theft/deductible)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import Settings
from core.models import RunContext
from evals.coverage_audit_v3 import score_issue_audit_brief
from workflows.coverage_audit.graph import CoverageAuditDeps, run_issue_audit_attempt
from workflows.coverage_audit.models import CoverageCitation, IssueAuditBrief, IssueResolution

CTRL_ARCHIVE = Path("packages/workflows/src/workflows/coverage_audit/policy-archive-controls")
CTRL_KEY_PATH = Path("evals/datasets/coverage_audit_controls_v2_key.json")


def _ctrl_key(packet_id: str) -> dict[str, object]:
    for row in json.loads(CTRL_KEY_PATH.read_text(encoding="utf-8")):
        if row["packet_id"] == packet_id:
            return dict(row)
    raise KeyError(packet_id)


def _good_c04() -> IssueAuditBrief:
    return IssueAuditBrief(
        packet_id="CTRL-C04",
        issues=[
            IssueResolution(
                issue_id="optional_machinery_coverage",
                outcome="insufficient_evidence",
                determining_value="insufficient_evidence",
                controlling_doc_ids=[
                    "policy_section_06_machinery.txt",
                    "intake_record_machinery.txt",
                ],
                citations=[
                    CoverageCitation(
                        doc_id="policy_section_06_machinery.txt",
                        line_start=5,
                        line_end=5,
                        verbatim_quote=(
                            "Optional Machinery Breakdown Coverage applies ONLY if the insured "
                            "executed and returned a signed Machinery Selection Form (Form MS-10)"
                        ),
                        claim_supported=(
                            "Optional machinery coverage requires a signed Machinery Selection "
                            "Form (Form MS-10) or signed issuance record."
                        ),
                        subject="Northport Cold Milling LLC",
                        polarity="affirmed",
                    ),
                    CoverageCitation(
                        doc_id="intake_record_machinery.txt",
                        line_start=4,
                        line_end=4,
                        verbatim_quote=(
                            "The supplied packet contains NO signed Machinery Selection Form "
                            "(Form MS-10) and does not establish whether a signed selection form "
                            "or definitive issuance record exists."
                        ),
                        claim_supported=(
                            "Packet contains no signed Form MS-10 and does not establish whether "
                            "a signed selection form or definitive issuance record exists."
                        ),
                        subject="Northport Cold Milling LLC",
                        polarity="negated",
                    ),
                ],
                missing_record_needed=(
                    "Signed Machinery Selection Form (Form MS-10) or definitive issuance record."
                ),
            )
        ],
    )


def _good_b08() -> IssueAuditBrief:
    return IssueAuditBrief(
        packet_id="CTRL-B08",
        issues=[
            IssueResolution(
                issue_id="theft_coverage_applicability",
                outcome="applies",
                determining_value="applies",
                controlling_doc_ids=["endorsement_e18_a_theft.txt", "site_schedule_2026.txt"],
                citations=[
                    CoverageCitation(
                        doc_id="endorsement_e18_a_theft.txt",
                        line_start=6,
                        line_end=6,
                        verbatim_quote=(
                            "Adds Theft coverage specifically at site HBR-4 with a $5,000 "
                            "deductible per occurrence."
                        ),
                        claim_supported="E18-A adds theft coverage at site HBR-4.",
                        subject="Harborline Maritime LLC",
                        polarity="affirmed",
                    ),
                    CoverageCitation(
                        doc_id="site_schedule_2026.txt",
                        line_start=3,
                        line_end=3,
                        verbatim_quote="Site HBR-4: Harbor (400 Pier Rd, Terminal East)",
                        claim_supported="Site HBR-4 is mapped to Harbor.",
                        subject="Harborline Maritime LLC",
                        polarity="affirmed",
                    ),
                ],
                superseded_or_out_of_scope=(
                    "E18-B changes deductible only, not theft applicability."
                ),
            ),
            IssueResolution(
                issue_id="applicable_deductible",
                outcome="applies",
                determining_value="$5,000",
                controlling_doc_ids=[
                    "endorsement_e18_a_theft.txt",
                    "endorsement_e18_b_deductible.txt",
                    "incident_report_harbor.txt",
                ],
                citations=[
                    CoverageCitation(
                        doc_id="endorsement_e18_a_theft.txt",
                        line_start=6,
                        line_end=6,
                        verbatim_quote=(
                            "Adds Theft coverage specifically at site HBR-4 with a $5,000 "
                            "deductible per occurrence."
                        ),
                        claim_supported="E18-A establishes a $5,000 theft deductible at HBR-4.",
                        subject="Harborline Maritime LLC",
                        value="$5,000",
                        polarity="affirmed",
                    ),
                    CoverageCitation(
                        doc_id="incident_report_harbor.txt",
                        line_start=5,
                        line_end=6,
                        verbatim_quote="Actual Date of Loss: 2026-08-27",
                        claim_supported="Actual date of loss was 2026-08-27 (reported 2026-09-05).",
                        subject="Harborline Maritime LLC",
                        value="2026-08-27",
                        polarity="affirmed",
                    ),
                    CoverageCitation(
                        doc_id="endorsement_e18_b_deductible.txt",
                        line_start=5,
                        line_end=6,
                        verbatim_quote=(
                            "Effective Date: 2026-09-01"
                        ),
                        claim_supported=(
                            "E18-B ($12,000 deductible only) is effective 2026-09-01 and does not "
                            "apply retroactively to the 2026-08-27 loss."
                        ),
                        subject="Harborline Maritime LLC",
                        polarity="negated",
                    ),
                ],
            ),
        ],
    )


def test_control_packets_archive_isolated_and_positive_controls_pass() -> None:
    """Both CTRL-C04 and CTRL-B08 positive controls pass cleanly; archive has no keys."""
    for p in CTRL_ARCHIVE.rglob("*"):
        if p.is_file():
            assert p.suffix == ".txt"
            assert "key" not in p.name.lower() and "rubric" not in p.name.lower()

    res_c04 = score_issue_audit_brief(_ctrl_key("CTRL-C04"), _good_c04(), archive_root=CTRL_ARCHIVE)
    assert res_c04["passed"] is True
    assert res_c04["false_definitive_on_insufficient"] is False

    res_b08 = score_issue_audit_brief(_ctrl_key("CTRL-B08"), _good_b08(), archive_root=CTRL_ARCHIVE)
    assert res_b08["passed"] is True
    assert res_b08["issues_passed"] == 2


def test_stub_negative_controls_omitted_wrong_deductible_wrong_scope_false_definitive() -> None:
    """Reject omitted issue, wrong deductible, wrong scope, and false definitive answer."""
    key_b08 = _ctrl_key("CTRL-B08")
    key_c04 = _ctrl_key("CTRL-C04")
    good_b08 = _good_b08()

    # 1. Omitted issue (only 1 of 2 issues returned on CTRL-B08)
    omitted = good_b08.model_copy(update={"issues": [good_b08.issues[0]]})
    r_omit = score_issue_audit_brief(key_b08, omitted, archive_root=CTRL_ARCHIVE)
    assert r_omit["passed"] is False
    assert r_omit["issues_passed"] == 1
    assert any(ir["omitted"] for ir in r_omit["issue_results"])

    # 2. Wrong deductible ($12,000 instead of $5,000)
    wrong_ded_issue = good_b08.issues[1].model_copy(update={"determining_value": "$12,000"})
    wrong_ded = good_b08.model_copy(update={"issues": [good_b08.issues[0], wrong_ded_issue]})
    r_ded = score_issue_audit_brief(key_b08, wrong_ded, archive_root=CTRL_ARCHIVE)
    assert r_ded["passed"] is False
    assert r_ded["issue_results"][1]["value_correct"] is False

    # 3. Wrong scope (asserting forbidden site Bayview / HBR-9)
    bad_scope_cit = good_b08.issues[0].citations[1].model_copy(
        update={
            "line_start": 4,
            "line_end": 4,
            "verbatim_quote": "Site HBR-9: Bayview (810 Dock Ave, South Basin)",
            "claim_supported": "Site HBR-9 is mapped to Bayview.",
        }
    )
    wrong_scope_issue = good_b08.issues[0].model_copy(
        update={"citations": [good_b08.issues[0].citations[0], bad_scope_cit]}
    )
    wrong_scope = good_b08.model_copy(update={"issues": [wrong_scope_issue, good_b08.issues[1]]})
    r_scope = score_issue_audit_brief(key_b08, wrong_scope, archive_root=CTRL_ARCHIVE)
    assert r_scope["passed"] is False
    assert r_scope["issue_results"][0]["scope_ok"] is False

    # 4. False definitive answer on CTRL-C04 (applies instead of insufficient_evidence)
    good_c04 = _good_c04()
    false_def = good_c04.model_copy(
        update={
            "issues": [
                good_c04.issues[0].model_copy(
                    update={"outcome": "applies", "determining_value": "applies"}
                )
            ]
        }
    )
    r_fdef = score_issue_audit_brief(key_c04, false_def, archive_root=CTRL_ARCHIVE)
    assert r_fdef["passed"] is False
    assert r_fdef["false_definitive_on_insufficient"] is True

    # 5. Prelaunch regression: replacing endorsement citations with authentic line-5 effective
    #    dates only (putting dates/sites in rationale) MUST fail propositions_ok!
    date_only_e18a = CoverageCitation(
        doc_id="endorsement_e18_a_theft.txt",
        line_start=5,
        line_end=5,
        verbatim_quote="Effective Date: 2026-01-01",
        claim_supported="E18-A effective date is 2026-01-01.",
        subject="Harborline Maritime LLC",
    )
    date_only_e18b = CoverageCitation(
        doc_id="endorsement_e18_b_deductible.txt",
        line_start=5,
        line_end=5,
        verbatim_quote="Effective Date: 2026-09-01",
        claim_supported="E18-B effective date is 2026-09-01.",
        subject="Harborline Maritime LLC",
    )
    weak_ded = good_b08.issues[1].model_copy(
        update={
            "citations": [date_only_e18a, good_b08.issues[1].citations[1], date_only_e18b],
        }
    )
    weak_b08 = good_b08.model_copy(
        update={
            "issues": [good_b08.issues[0], weak_ded],
            "rationale": "Harbor HBR-4 dates 2026-08-27 and 2026-09-01.",
        }
    )
    r_weak = score_issue_audit_brief(key_b08, weak_b08, archive_root=CTRL_ARCHIVE)
    assert r_weak["passed"] is False
    assert r_weak["issue_results"][1]["propositions_ok"] is False

    # 6. Duplicate issue_id and mismatched packet_id MUST fail structure checks
    dup_b08 = good_b08.model_copy(
        update={"issues": [good_b08.issues[0], good_b08.issues[0], good_b08.issues[1]]}
    )
    r_dup = score_issue_audit_brief(key_b08, dup_b08, archive_root=CTRL_ARCHIVE)
    assert r_dup["passed"] is False
    assert r_dup["duplicate_issue_ids"] == ["theft_coverage_applicability"]

    bad_pid = good_b08.model_copy(update={"packet_id": "WRONG-ID"})
    r_pid = score_issue_audit_brief(key_b08, bad_pid, archive_root=CTRL_ARCHIVE)
    assert r_pid["passed"] is False
    assert r_pid["packet_id_ok"] is False


async def test_run_issue_audit_attempt_mock_and_gateway_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise run_issue_audit_attempt in both mock mode and stubbed gateway mode."""
    from workflows.coverage_audit import graph as cov_graph  # noqa: PLC0415

    ctx = RunContext(
        tenant_id="tenant-alpha",
        requester_id="auditor",
        requester_roles=frozenset({"underwriter"}),
        thread_id="t-iss",
        run_id="r-iss",
        request_id="req-iss",
    )
    mock_out = await run_issue_audit_attempt(
        packet_id="CTRL-C04",
        prompt="Audit CTRL-C04",
        deps=CoverageAuditDeps(
            settings=Settings(model_mode="mock"),
            planner_variant="baseline",
            archive_root=CTRL_ARCHIVE,
        ),
        context=ctx,
    )
    assert mock_out["packet_id"] == "CTRL-C04"

    stub_brief = _good_c04()

    class _StubStructured:
        async def ainvoke(self, *_a: object, **_kw: object) -> IssueAuditBrief:
            return stub_brief

    class _StubModel:
        _llm_type = "mock-chat"

        def with_structured_output(self, _schema: object) -> _StubStructured:
            return _StubStructured()

        def with_retry(self, **_kw: object) -> _StubModel:
            return self

    class _StubAgent:
        async def ainvoke(self, *_a: object, **_kw: object) -> dict[str, object]:
            return {"messages": ["Investigation summary"]}

    monkeypatch.setattr(cov_graph, "get_chat_model", lambda **_kw: _StubModel())
    monkeypatch.setattr(cov_graph, "create_agent", lambda **_kw: _StubAgent())
    monkeypatch.setattr(cov_graph, "build_coverage_deep_agent", lambda **_kw: _StubAgent())

    gw_settings = Settings(model_mode="gateway")
    for variant in ("baseline", "deep_agents"):
        out = await run_issue_audit_attempt(
            packet_id="CTRL-C04",
            prompt="Audit CTRL-C04",
            deps=CoverageAuditDeps(
                settings=gw_settings,
                planner_variant=variant,
                archive_root=CTRL_ARCHIVE,
            ),
            context=ctx,
        )
        assert out["brief"]["issues"][0]["outcome"] == "insufficient_evidence"
