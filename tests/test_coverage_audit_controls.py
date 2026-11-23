"""Stage 4 Coverage Discrepancy Audit: 5 decoy negative controls, positive controls, & dry-run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import Settings
from core.models import RunContext
from evals.coverage_audit import (
    build_coverage_schedule,
    evaluate_routing_bar,
    run_coverage_audit_suite,
    score_coverage_audit_brief,
    score_coverage_audit_brief_v2,
    verify_coverage_citation,
    verify_coverage_citation_v2,
)
from gateway.mocks import ScriptedMockChatModel
from workflows.coverage_audit.graph import (
    CoverageAuditBudget,
    CoverageAuditDeps,
    CoverageBudgetExceededError,
    CoverageBudgetMiddleware,
    build_coverage_deep_agent,
    run_coverage_audit_attempt,
)
from workflows.coverage_audit.models import CoverageAuditBrief, CoverageCitation
from workflows.coverage_audit.tools import (
    ARCHIVE_ROOT,
    build_coverage_audit_tools,
    load_packet_documents,
)

DEV_KEY_PATH = Path("evals/datasets/coverage_audit_dev_key.json")
HELDOUT_KEY_PATH = Path("evals/datasets/coverage_audit_heldout_key.json")


def _load_key(path: Path, packet_id: str) -> dict[str, object]:
    for row in json.loads(path.read_text(encoding="utf-8")):
        if row["packet_id"] == packet_id:
            return dict(row)
    raise KeyError(packet_id)


def test_archive_contains_no_answer_keys_or_rubric_metadata() -> None:
    """Answer keys, tiers, and rubrics must stay strictly outside policy-archive/."""
    assert ARCHIVE_ROOT.is_dir()
    for p in ARCHIVE_ROOT.rglob("*"):
        if p.is_file():
            name = p.name.lower()
            assert p.suffix == ".txt"
            assert "answer" not in name
            assert "rubric" not in name
            assert "expected" not in name
            assert "key" not in name
    # Path traversal outside archive returns empty
    assert load_packet_documents("../datasets") == {}
    assert load_packet_documents("nonexistent-packet") == {}


def test_coverage_tools_read_only_and_bounded() -> None:
    """Read-only archive tools list, search, and read bounded sections cleanly."""
    log: list[dict[str, object]] = []
    tools = {
        t.name: t for t in build_coverage_audit_tools(allowed_packet_id="D02", read_log=log)
    }
    listed = json.loads(tools["list_packet_documents"].invoke({}))
    assert any(d["doc_id"] == "endorsement_ed_409.txt" for d in listed)

    hits = json.loads(tools["search_packet_documents"].invoke({"query": "ED-409 sublimit"}))
    assert hits and hits[0]["doc_id"] == "endorsement_ed_409.txt"

    sec = json.loads(
        tools["read_document_section"].invoke(
            {"doc_id": "endorsement_ed_409.txt", "start_line": 1, "end_line": 10}
        )
    )
    assert "$75,000" in sec["content"]

    missing = json.loads(
        tools["read_document_section"].invoke(
            {"doc_id": "does_not_exist.txt", "start_line": 1, "end_line": 5}
        )
    )
    assert "error" in missing
    assert len(log) == 3


def test_decoy_negative_control_1_unsigned_draft_on_d02_fails() -> None:
    """Decoy 1: citing unsigned draft ($250,000) instead of executed ED-409 ($75,000) MUST fail."""
    key = _load_key(DEV_KEY_PATH, "D02")
    decoy_brief = CoverageAuditBrief(
        packet_id="D02",
        determination="covered",
        controlling_value="$250,000",
        controlling_doc_ids=["draft_endorsement_water_2026.txt"],
        supersession_chain=["policy_declarations.txt", "draft_endorsement_water_2026.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="draft_endorsement_water_2026.txt",
                line_start=4,
                line_end=5,
                verbatim_quote="Proposed Water Damage Sublimit: $250,000",
                claim_supported="Water Damage Sublimit is $250,000.",
                subject="Cascadia Timberworks Inc.",
                value="$250,000",
                polarity="affirmed",
            )
        ],
    )
    scores = score_coverage_audit_brief(key, decoy_brief)
    assert scores["passed"] is False
    assert scores["value_correct"] is False
    assert scores["no_decoy_used"] is False


def test_decoy_negative_control_2_intermediate_superseded_endorsement_on_d02_fails() -> None:
    """Decoy 2: stopping at superseded ED-401 ($100,000) without ED-409 ($75,000) MUST fail."""
    key = _load_key(DEV_KEY_PATH, "D02")
    decoy_brief = CoverageAuditBrief(
        packet_id="D02",
        determination="covered",
        controlling_value="$100,000",
        controlling_doc_ids=["endorsement_ed_401.txt"],
        supersession_chain=["policy_declarations.txt", "endorsement_ed_401.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_ed_401.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="replaces the Base Water Damage Sublimit of $25,000 with $100,000.",
                claim_supported="Water Damage Sublimit is $100,000.",
                subject="Cascadia Timberworks Inc.",
                value="$100,000",
                polarity="affirmed",
            )
        ],
    )
    scores = score_coverage_audit_brief(key, decoy_brief)
    assert scores["passed"] is False
    assert scores["controlling_docs_ok"] is False
    assert scores["value_correct"] is False


def test_decoy_negative_control_3_false_definitive_on_insufficient_evidence_d03_fails() -> None:
    """Decoy 3: returning covered instead of insufficient_evidence on D03 MUST fail & flag."""
    key = _load_key(DEV_KEY_PATH, "D03")
    decoy_brief = CoverageAuditBrief(
        packet_id="D03",
        determination="covered",
        controlling_value="covered",
        controlling_doc_ids=["expired_2024_location_list.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="expired_2024_location_list.txt",
                line_start=4,
                line_end=4,
                verbatim_quote="2. 412 Dockside Rd (Warehouse Annex B)",
                claim_supported="Warehouse Annex B is listed.",
            )
        ],
    )
    scores = score_coverage_audit_brief(key, decoy_brief)
    assert scores["passed"] is False
    assert scores["false_definitive_on_insufficient"] is True
    assert scores["no_decoy_used"] is False


def test_decoy_negative_control_4_cross_entity_parent_endorsement_on_b04_fails() -> None:
    """Decoy 4: citing parent Summit Holdings Corp BL-402 ($1M) for Summit BioLabs LLC fails."""
    key = _load_key(HELDOUT_KEY_PATH, "B04")
    decoy_brief = CoverageAuditBrief(
        packet_id="B04",
        determination="covered",
        controlling_value="$1,000,000",
        controlling_doc_ids=["endorsement_bl_402.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_bl_402.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="Spoilage Sublimit: $1,000,000 per occurrence",
                claim_supported="Summit BioLabs LLC has $1,000,000 spoilage sublimit.",
                subject="Summit BioLabs LLC",
                value="$1,000,000",
                polarity="affirmed",
            )
        ],
    )
    scores = score_coverage_audit_brief(key, decoy_brief)
    assert scores["passed"] is False
    assert scores["no_decoy_used"] is False
    assert scores["citations_grounded"] is False


def test_decoy_negative_control_5_fabricated_quote_and_negated_clause_polarity_flip_fail() -> None:
    """Decoy 5: fabricated quote on D01 and affirmed claim on unsigned B07 clause MUST fail."""
    key_d01 = _load_key(DEV_KEY_PATH, "D01")
    fab_brief = CoverageAuditBrief(
        packet_id="D01",
        determination="covered",
        controlling_value="$2,000,000",
        controlling_doc_ids=["endorsement_gl_105.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=1,
                line_end=2,
                verbatim_quote="Special Executive Rider grants $2,000,000 limit.",
                claim_supported="Limit is $2,000,000.",
            )
        ],
    )
    scores = score_coverage_audit_brief(key_d01, fab_brief)
    assert scores["passed"] is False
    assert scores["citations_grounded"] is False

    # Also test affirmed claim on unsigned/not-executed clause in B07
    docs_b07 = load_packet_documents("B07")
    ok, reason = verify_coverage_citation(
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
    assert ok is False
    assert "Polarity mismatch" in reason


def test_positive_controls_d01_d02_d03_pass_cleanly() -> None:
    """Positive controls for clean (D01), 2-hop branching (D02), and abstention (D03) pass."""
    b_d01 = CoverageAuditBrief(
        packet_id="D01",
        determination="covered",
        controlling_value="$2,000,000",
        controlling_doc_ids=["endorsement_gl_105.txt"],
        supersession_chain=["policy_declarations.txt", "endorsement_gl_105.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote=(
                    "modifies the Base Per-Occurrence Limit from $1,000,000 to $2,000,000."
                ),
                claim_supported="Controlling Per-Occurrence limit is $2,000,000.",
                subject="Harborview Cold Storage LLC",
                value="$2,000,000",
                polarity="affirmed",
            )
        ],
    )
    assert score_coverage_audit_brief(_load_key(DEV_KEY_PATH, "D01"), b_d01)["passed"] is True

    b_d02 = CoverageAuditBrief(
        packet_id="D02",
        determination="covered",
        controlling_value="$75,000",
        controlling_doc_ids=["endorsement_ed_409.txt"],
        supersession_chain=[
            "policy_declarations.txt",
            "endorsement_ed_401.txt",
            "endorsement_ed_409.txt",
        ],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_ed_409.txt",
                line_start=6,
                line_end=7,
                verbatim_quote="Controlling Water Damage Sublimit: $75,000 per occurrence.",
                claim_supported="ED-409 supersedes ED-401 with a $75,000 Water Damage Sublimit.",
                subject="Cascadia Timberworks Inc.",
                value="$75,000",
                polarity="affirmed",
            )
        ],
    )
    assert score_coverage_audit_brief(_load_key(DEV_KEY_PATH, "D02"), b_d02)["passed"] is True

    b_d03 = CoverageAuditBrief(
        packet_id="D03",
        determination="insufficient_evidence",
        controlling_value="insufficient_evidence",
        controlling_doc_ids=["endorsement_sl_204.txt"],
        supersession_chain=["policy_declarations.txt", "endorsement_sl_204.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_sl_204.txt",
                line_start=6,
                line_end=7,
                verbatim_quote=(
                    "Coverage applies solely to locations explicitly enumerated "
                    "in Schedule A-2026"
                ),
                claim_supported=(
                    "Coverage requires Schedule A-2026, which is missing from the archive."
                ),
                subject="Redrock Distribution Co.",
                polarity="affirmed",
            )
        ],
        missing_documents=["Schedule A-2026"],
        rationale="Schedule A-2026 is absent; 2024 list is expired.",
    )
    s_d03 = score_coverage_audit_brief(_load_key(DEV_KEY_PATH, "D03"), b_d03)
    assert s_d03["passed"] is True
    assert s_d03["false_definitive_on_insufficient"] is False


def test_coverage_deep_agent_disables_general_purpose_and_enforces_shared_budget() -> None:
    """Deep Agents coverage auditor exposes ONLY policy_chain_auditor and enforces equal budget."""
    budget = CoverageAuditBudget(max_model_calls=2, max_tool_calls=2, max_total_tokens=100)
    agent = build_coverage_deep_agent(
        chat_model=ScriptedMockChatModel(responses=[]),
        model_alias="test-cov-alias",
        read_tools=build_coverage_audit_tools(allowed_packet_id="D01"),
        budget=budget,
    )
    task_tool = agent.nodes["tools"].bound.tools_by_name["task"]
    assert "- policy_chain_auditor:" in task_tool.description
    assert "- general-purpose:" not in task_tool.description

    mw = CoverageBudgetMiddleware(budget)
    mw.before_model({}, None)
    mw.before_model({}, None)
    with pytest.raises(CoverageBudgetExceededError):
        mw.before_model({}, None)
    assert mw.wrap_tool_call("t1", lambda x: x) == "t1"
    assert mw.wrap_tool_call("t2", lambda x: x) == "t2"
    with pytest.raises(CoverageBudgetExceededError):
        mw.wrap_tool_call("t3", lambda x: x)
    budget.record_tokens(80)
    with pytest.raises(CoverageBudgetExceededError):
        budget.record_tokens(30)


async def test_coverage_audit_stub_dry_run_and_routing_bar_evaluator(tmp_path: Path) -> None:
    """End-to-end offline mock dry-run + alternating schedule + routing bar math."""
    settings = Settings(model_mode="mock")
    sched = build_coverage_schedule([{"packet_id": "D01"}], trials=2)
    assert [(v, c["packet_id"], t) for v, c, t in sched] == [
        ("baseline", "D01", 1),
        ("deep_agents", "D01", 1),
        ("deep_agents", "D01", 2),
        ("baseline", "D01", 2),
    ]

    ckpt = tmp_path / "cov_dry_run.json"
    res = await run_coverage_audit_suite(
        key_path=DEV_KEY_PATH,
        model_alias="eval-anthropic",
        trials=1,
        settings=settings,
        checkpoint_path=ckpt,
    )
    assert len(res["runs"]) == 6
    assert ckpt.exists()

    # Direct single-attempt helper check
    ctx = RunContext(
        tenant_id="tenant-alpha",
        requester_id="auditor",
        requester_roles=frozenset({"underwriter"}),
        thread_id="t-dry",
        run_id="r-dry",
        request_id="req-dry",
    )
    out = await run_coverage_audit_attempt(
        packet_id="D01",
        prompt="Audit D01",
        deps=CoverageAuditDeps(settings=settings, planner_variant="deep_agents"),
        context=ctx,
    )
    assert out["packet_id"] == "D01"

    # Routing bar evaluation: Deep Agents +2 branching cases, 0 false definitive, <=2x tokens
    synth_runs = []
    for pid in ("B01", "B02", "B03", "B04"):
        synth_runs.append(
            {
                "packet_id": pid,
                "tier": "branching",
                "variant": "baseline",
                "passed": pid in {"B01"},
                "false_definitive_on_insufficient": False,
                "total_tokens": 10_000,
            }
        )
        synth_runs.append(
            {
                "packet_id": pid,
                "tier": "branching",
                "variant": "deep_agents",
                "passed": pid in {"B01", "B02", "B03"},
                "false_definitive_on_insufficient": False,
                "total_tokens": 15_000,
            }
        )
    bar = evaluate_routing_bar(synth_runs)
    assert bar["delta_branching_cases"] == 2
    assert bar["deep_agents_zero_false_definitive"] is True
    assert bar["within_2x_token_bar"] is True
    assert bar["routing_bar_met"] is True


async def test_coverage_audit_gateway_branch_and_token_budget_with_stub_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise gateway-mode execution paths for both arms and token-budget enforcement."""
    from evals.metrics import UsageTrackingCallback  # noqa: PLC0415
    from workflows.coverage_audit import graph as cov_graph  # noqa: PLC0415

    stub_brief = CoverageAuditBrief(
        packet_id="D01",
        determination="covered",
        controlling_value="$2,000,000",
        controlling_doc_ids=["endorsement_gl_105.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="Base Per-Occurrence Limit from $1,000,000 to $2,000,000.",
                claim_supported="Limit is $2,000,000.",
            )
        ],
    )

    class _StubStructured:
        async def ainvoke(self, *_a: object, **_kw: object) -> CoverageAuditBrief:
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

    ctx = RunContext(
        tenant_id="tenant-alpha",
        requester_id="auditor",
        requester_roles=frozenset({"underwriter"}),
        thread_id="t-gw",
        run_id="r-gw",
        request_id="req-gw",
    )
    gw_settings = Settings(model_mode="gateway")
    for variant in ("baseline", "deep_agents"):
        out = await run_coverage_audit_attempt(
            packet_id="D01",
            prompt="Audit D01",
            deps=CoverageAuditDeps(settings=gw_settings, planner_variant=variant),
            context=ctx,
        )
        assert out["brief"]["controlling_value"] == "$2,000,000"

    # Token budget exceeded before synthesis raises CoverageBudgetExceededError
    cb = UsageTrackingCallback()
    cb.stats.total_tokens = 200_000
    with pytest.raises(CoverageBudgetExceededError):
        await run_coverage_audit_attempt(
            packet_id="D01",
            prompt="Audit D01",
            deps=CoverageAuditDeps(
                settings=gw_settings,
                planner_variant="baseline",
                max_total_tokens=96_000,
                callbacks=[cb],
            ),
            context=ctx,
        )

    # Token budget exceeded AFTER synthesis also raises CoverageBudgetExceededError,
    # and partial doc_reads populated before failure are preserved in caller's read_log!
    cb_post = UsageTrackingCallback()
    cb_post.stats.total_tokens = 10_000
    partial_reads: list[dict[str, object]] = []

    class _PostBurstStructured:
        async def ainvoke(self, *_a: object, **_kw: object) -> CoverageAuditBrief:
            cb_post.stats.total_tokens = 120_000
            return stub_brief

    class _PostBurstModel:
        _llm_type = "mock-chat"

        def with_structured_output(self, _schema: object) -> _PostBurstStructured:
            return _PostBurstStructured()

        def with_retry(self, **_kw: object) -> _PostBurstModel:
            return self

    class _ReadingAgent:
        async def ainvoke(self, *_a: object, **_kw: object) -> dict[str, object]:
            partial_reads.append(
                {"tool": "read_document_section", "doc_id": "endorsement_gl_105.txt"}
            )
            return {"messages": ["Investigation summary"]}

    monkeypatch.setattr(cov_graph, "get_chat_model", lambda **_kw: _PostBurstModel())
    monkeypatch.setattr(cov_graph, "create_agent", lambda **_kw: _ReadingAgent())
    with pytest.raises(CoverageBudgetExceededError):
        await run_coverage_audit_attempt(
            packet_id="D01",
            prompt="Audit D01",
            deps=CoverageAuditDeps(
                settings=gw_settings,
                planner_variant="baseline",
                max_total_tokens=96_000,
                callbacks=[cb_post],
            ),
            context=ctx,
            read_log=partial_reads,
        )
    assert len(partial_reads) == 1
    assert partial_reads[0]["doc_id"] == "endorsement_gl_105.txt"


def test_v2_negative_control_b01_heading_only_earthquake_and_empty_chain_fails() -> None:
    """Coordinator regression: B01 line-1 heading + earthquake claim + empty chain MUST fail."""
    key_b01 = _load_key(HELDOUT_KEY_PATH, "B01")
    bad_b01 = CoverageAuditBrief(
        packet_id="B01",
        determination="covered",
        controlling_value="$300,000",
        controlling_doc_ids=["endorsement_tr_115.txt"],
        supersession_chain=[],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_tr_115.txt",
                line_start=1,
                line_end=1,
                verbatim_quote="ENDORSEMENT TR-115 — FINAL TRANSIT LIMIT MODIFICATION",
                claim_supported="Policy includes earthquake-resistant certification.",
                subject="Pacific Freightways Inc.",
                polarity="affirmed",
            )
        ],
    )
    scores = score_coverage_audit_brief_v2(key_b01, bad_b01)
    assert scores["passed"] is False
    assert scores["ordered_chain_ok"] is False
    assert scores["citations_grounded"] is False
    assert scores["controlling_docs_ok"] is False


def test_v2_controls_date_omission_unrelated_claim_and_rule_out_citations() -> None:
    """v2 rejects missing dates/unrelated claims, allows rule-out citations & operative chains."""
    docs_b01 = load_packet_documents("B01")
    # Claiming effective date 2026-03-10 while citing only lines 6-7 (which omit the date) -> FAIL
    ok_dt, msg_dt = verify_coverage_citation_v2(
        CoverageCitation(
            doc_id="endorsement_tr_115.txt",
            line_start=6,
            line_end=7,
            verbatim_quote="Controlling Property-in-Transit Limit: $300,000 per conveyance.",
            claim_supported="TR-115 sets $300,000 limit effective 2026-03-10.",
            subject="Pacific Freightways Inc.",
            value="$300,000",
            polarity="affirmed",
        ),
        docs_b01,
        expected_subject="Pacific Freightways Inc.",
    )
    assert ok_dt is False and "Unsupported date" in msg_dt

    # C03 exclusion affirmed in prose -> PASSES under v2
    key_c03 = _load_key(HELDOUT_KEY_PATH, "C03")
    good_c03 = CoverageAuditBrief(
        packet_id="C03",
        determination="excluded",
        controlling_value="excluded",
        controlling_doc_ids=["cgl_coverage_form_exclusions.txt"],
        supersession_chain=["policy_declarations.txt", "cgl_coverage_form_exclusions.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="cgl_coverage_form_exclusions.txt",
                line_start=3,
                line_end=3,
                verbatim_quote=(
                    "Total Pollution Exclusion: This insurance does NOT apply to bodily injury, "
                    "property damage, or cleanup costs arising out of the actual, alleged, or "
                    "threatened discharge, dispersal, seepage, migration, release, or escape of "
                    "pollutants at any time, including sudden and accidental releases."
                ),
                claim_supported=(
                    "Total Pollution Exclusion bars coverage for sudden pollutant releases."
                ),
                subject="Heartland Chemical Blending Corp.",
                polarity="affirmed",
            )
        ],
    )
    assert score_coverage_audit_brief_v2(key_c03, good_c03)["passed"] is True

    # D02 citing unsigned draft ONLY to rule it out -> PASSES under v2!
    key_d02 = _load_key(DEV_KEY_PATH, "D02")
    good_d02_with_ruleout = CoverageAuditBrief(
        packet_id="D02",
        determination="covered",
        controlling_value="$75,000",
        controlling_doc_ids=["endorsement_ed_409.txt"],
        supersession_chain=[
            "policy_declarations.txt",
            "endorsement_ed_401.txt",
            "endorsement_ed_409.txt",
        ],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_ed_409.txt",
                line_start=6,
                line_end=7,
                verbatim_quote="Controlling Water Damage Sublimit: $75,000 per occurrence.",
                claim_supported="ED-409 supersedes ED-401 with $75,000 Water Damage Sublimit.",
                subject="Cascadia Timberworks Inc.",
                value="$75,000",
                polarity="affirmed",
            ),
            CoverageCitation(
                doc_id="draft_endorsement_water_2026.txt",
                line_start=1,
                line_end=5,
                verbatim_quote="WARNING: FOR DISCUSSION ONLY. THIS DRAFT DOES NOT BIND COVERAGE.",
                claim_supported="Draft endorsement is unsigned and does not bind coverage.",
                subject="Cascadia Timberworks Inc.",
                polarity="negated",
            ),
        ],
    )
    assert score_coverage_audit_brief_v2(key_d02, good_d02_with_ruleout)["passed"] is True

    # B07: unsigned MX-502 MUST NOT be in operative supersession_chain; B08: loss_run separate!
    key_b07 = _load_key(HELDOUT_KEY_PATH, "B07")
    good_b07 = CoverageAuditBrief(
        packet_id="B07",
        determination="insufficient_evidence",
        controlling_value="insufficient_evidence",
        controlling_doc_ids=["unsigned_endorsement_mx_502.txt"],
        supersession_chain=["policy_declarations.txt"],
        evidence_citations=[
            CoverageCitation(
                doc_id="unsigned_endorsement_mx_502.txt",
                line_start=2,
                line_end=7,
                verbatim_quote="Carrier Authorized Signature: [BLANK — NOT EXECUTED]",
                claim_supported="MX-502 is unsigned and not executed.",
                subject="Evergreen Senior Living LLC",
                polarity="negated",
            )
        ],
        missing_documents=["Executed copy of MX-502"],
    )
    assert score_coverage_audit_brief_v2(key_b07, good_b07)["passed"] is True
    bad_b07_chain = good_b07.model_copy(
        update={
            "supersession_chain": [
                "policy_declarations.txt",
                "unsigned_endorsement_mx_502.txt",
            ]
        }
    )
    assert score_coverage_audit_brief_v2(key_b07, bad_b07_chain)["ordered_chain_ok"] is False

    key_b08 = _load_key(HELDOUT_KEY_PATH, "B08")
    good_b08 = CoverageAuditBrief(
        packet_id="B08",
        determination="covered",
        controlling_value="$750,000",
        controlling_doc_ids=["endorsement_ag_209.txt", "loss_run_2026.txt"],
        supersession_chain=[
            "policy_declarations.txt",
            "endorsement_ag_201.txt",
            "endorsement_ag_209.txt",
        ],
        evidence_citations=[
            CoverageCitation(
                doc_id="endorsement_ag_209.txt",
                line_start=6,
                line_end=7,
                verbatim_quote=(
                    "Controlling Annual Aggregate Limit for the 2026 policy term: $1,200,000."
                ),
                claim_supported="AG-209 sets controlling Annual Aggregate Limit to $1,200,000.",
                subject="Atlas Industrial Services Corp.",
                value="$1,200,000",
                polarity="affirmed",
            ),
            CoverageCitation(
                doc_id="loss_run_2026.txt",
                line_start=5,
                line_end=5,
                verbatim_quote="Total Paid Erosion Against 2026 Annual Aggregate: $450,000",
                claim_supported="Loss run shows $450,000 paid erosion against 2026 aggregate.",
                subject="Atlas Industrial Services Corp.",
                value="$450,000",
                polarity="affirmed",
            ),
        ],
    )
    assert score_coverage_audit_brief_v2(key_b08, good_b08)["passed"] is True


def test_v2_citation_and_controlling_clause_edge_branches() -> None:
    """Exercise all v2 validation branches: bounds, numeric/value/polarity/entity/gap checks."""
    docs_d01 = load_packet_documents("D01")
    for cit, expected_sub in (
        (
            CoverageCitation(
                doc_id="missing.txt",
                line_start=1,
                line_end=1,
                verbatim_quote="x",
                claim_supported="y",
            ),
            "unknown doc_id",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=1,
                line_end=1,
                verbatim_quote="   ",
                claim_supported="y",
            ),
            "empty verbatim_quote",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=1,
                line_end=999,
                verbatim_quote="GL-105",
                claim_supported="y",
            ),
            "invalid line range",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=1,
                line_end=2,
                verbatim_quote="$2,000,000",
                claim_supported="Limit is $2,000,000.",
            ),
            "not found in cited lines",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="$2,000,000",
                claim_supported="   ",
            ),
            "empty claim_supported",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="$2,000,000",
                claim_supported="Limit is $9,500,000.",
            ),
            "Unsupported numeric",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="$2,000,000",
                claim_supported="Limit is $2,000,000.",
                value="$9,500,000",
            ),
            "Value mismatch",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="$2,000,000",
                claim_supported="Limit is $2,000,000.",
                polarity="negated",
            ),
            "Polarity mismatch",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="$2,000,000",
                claim_supported="Limit is $2,000,000.",
                subject="Moonbeam Logistics LLC",
            ),
            "Entity/subject mismatch",
        ),
        (
            CoverageCitation(
                doc_id="endorsement_gl_105.txt",
                line_start=6,
                line_end=6,
                verbatim_quote="$2,000,000",
                claim_supported="Limit is $2,000,000 with volcanic tsunami certification.",
            ),
            "Unsupported/unrelated claim terms",
        ),
    ):
        ok, msg = verify_coverage_citation_v2(
            cit, docs_d01, expected_subject="Harborview Cold Storage LLC"
        )
        assert ok is False and expected_sub in msg

    # Date-in-range entailment passes (2026-04-12 inside 2026-01-01 to 2027-01-01)
    ok_rng, _ = verify_coverage_citation_v2(
        CoverageCitation(
            doc_id="policy_declarations.txt",
            line_start=4,
            line_end=4,
            verbatim_quote="Policy Period: 2026-01-01 to 2027-01-01",
            claim_supported="Policy is active on 2026-04-12.",
        ),
        docs_d01,
    )
    assert ok_rng is True

    # Missing expected value/exclusion/gap on controlling doc fails controlling_docs_ok
    s_miss_val = score_coverage_audit_brief_v2(
        _load_key(DEV_KEY_PATH, "D01"),
        CoverageAuditBrief(
            packet_id="D01",
            determination="covered",
            controlling_value="$2,000,000",
            controlling_doc_ids=["endorsement_gl_105.txt"],
            supersession_chain=["policy_declarations.txt", "endorsement_gl_105.txt"],
            evidence_citations=[
                CoverageCitation(
                    doc_id="endorsement_gl_105.txt",
                    line_start=5,
                    line_end=5,
                    verbatim_quote="Effective Date: 2026-01-01",
                    claim_supported="Effective Date is 2026-01-01.",
                )
            ],
        ),
    )
    assert s_miss_val["controlling_docs_ok"] is False

    s_miss_excl = score_coverage_audit_brief_v2(
        _load_key(HELDOUT_KEY_PATH, "C03"),
        CoverageAuditBrief(
            packet_id="C03",
            determination="excluded",
            controlling_value="excluded",
            controlling_doc_ids=["cgl_coverage_form_exclusions.txt"],
            supersession_chain=["policy_declarations.txt", "cgl_coverage_form_exclusions.txt"],
            evidence_citations=[
                CoverageCitation(
                    doc_id="cgl_coverage_form_exclusions.txt",
                    line_start=2,
                    line_end=2,
                    verbatim_quote="Policy Number: CGL-2026-3088",
                    claim_supported="Policy Number is CGL-2026-3088.",
                )
            ],
        ),
    )
    assert s_miss_excl["controlling_docs_ok"] is False

    s_miss_gap = score_coverage_audit_brief_v2(
        _load_key(DEV_KEY_PATH, "D03"),
        CoverageAuditBrief(
            packet_id="D03",
            determination="insufficient_evidence",
            controlling_value="insufficient_evidence",
            controlling_doc_ids=["endorsement_sl_204.txt"],
            supersession_chain=["policy_declarations.txt", "endorsement_sl_204.txt"],
            evidence_citations=[
                CoverageCitation(
                    doc_id="endorsement_sl_204.txt",
                    line_start=5,
                    line_end=5,
                    verbatim_quote="Effective Date: 2026-01-01",
                    claim_supported="Effective Date is 2026-01-01.",
                )
            ],
            missing_documents=["Schedule A-2026"],
        ),
    )
    assert s_miss_gap["controlling_docs_ok"] is False
