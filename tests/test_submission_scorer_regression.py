"""Regression tests for deterministic span+typed-fact scorer, Deep Agents config, & scheduling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.auth import mint_user_jwt
from core.config import Settings
from core.models import CallerIdentity, RunContext
from evals.submission_ab import (
    build_trial_schedule,
    score_submission_brief,
    verify_citation_against_doc,
)
from gateway.mocks import ScriptedMockChatModel
from workflows.submission_triage.graph import (
    SharedBudgetExceededError,
    SharedBudgetMiddleware,
    SharedCallBudget,
    SubmissionTriageDeps,
    build_deep_agents_planner,
    build_submission_read_tools,
    build_submission_triage_graph,
    build_verified_approval_command,
)
from workflows.submission_triage.models import EvidenceCitation, SubmissionBrief


def _load_case(case_id: str) -> dict[str, object]:
    for line in Path("evals/datasets/submissions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["case_id"] == case_id:
                return row
    raise KeyError(case_id)


def test_negated_revenue_claim_with_correct_typed_fields_fails_citation_grounding() -> None:
    """Standalone regression: genuine excerpt + negated claim fails even with valid typed fields."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Northstar does not have annual revenue of $4,200,000.",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["disposition_correct"] is True
    # Typed fields are 100% correct so they cannot mask the citation failure!
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert scores["citation_failures"]


def test_ambiguous_unsupported_free_text_claim_fails_grounding() -> None:
    """Ambiguous free-text claims with no verifiable typed fact MUST fail as unverified."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Looks strong and acceptable for underwriting.",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("no verifiable typed fact" in f for f in scores["citation_failures"])


def test_invented_citation_with_all_expected_keywords_still_fails() -> None:
    """Fabricated passage containing all expected keywords MUST fail span resolution."""
    case = _load_case("sub-01")
    invented_brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote=(
                    "Executive Committee Appendix 99 unanimously certified Northstar "
                    "$4,200,000 revenue $2,000,000 limit effective 2026-12-01"
                ),
                fact_supported="Annual revenue is $4,200,000 and requested limit is $2,000,000.",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, invented_brief)
    assert scores["disposition_correct"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert scores["citation_failures"]


def test_wrong_doc_citation_for_fein_fails_grounding() -> None:
    """Citing sub-01-email for FEIN 84-1234567 (which only appears in sub-01-app) MUST fail."""
    case = _load_case("sub-01")
    wrong_doc_brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-email",
                section_or_quote="FEIN: 84-1234567",
                fact_supported="Insured FEIN is 84-1234567.",
            ),
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Annual revenue is $4,200,000.",
            ),
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, wrong_doc_brief)
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False


def test_genuine_cited_brief_passes_span_typed_fact_and_typed_field_checks() -> None:
    """Positive control: verbatim span + matching typed fact + exact typed fields pass."""
    case = _load_case("sub-01")
    good_brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote=(
                    "Annual Revenue: $4,200,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: 2026-12-01"
                ),
                fact_supported=(
                    "Annual revenue is $4,200,000, requested limit is $2,000,000, "
                    "effective date is 2026-12-01."
                ),
            )
        ],
        summary="Northstar Analytics Inc. is complete and within carrier-apex appetite.",
    )
    scores = score_submission_brief(case, good_brief)
    assert scores["disposition_correct"] is True
    assert scores["citations_grounded"] is True
    assert scores["typed_fields_ok"] is True
    assert scores["facts_supported"] is True


def test_genuine_excerpt_with_contradicting_claim_or_wrong_typed_values_fails() -> None:
    """Genuine loss-free excerpt + contradicting open-claim & wrong typed values MUST fail."""
    case = _load_case("sub-01")
    contradicting_brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=9_999_999,
        requested_coverage_usd=5_000_000,
        effective_date="2028-01-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="3-Year Loss History: 0 claims ($0 incurred, 2023-2026).",
                fact_supported="Account has a major open claim with $500,000 bodily injury.",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, contradicting_brief)
    assert scores["citations_grounded"] is False
    assert scores["typed_fields_ok"] is False
    assert scores["facts_supported"] is False
    assert len(scores["typed_failures"]) == 3


def test_deep_agents_disables_general_purpose_and_enforces_shared_budget() -> None:
    """Deep Agents exposes ONLY evidence_auditor (no general-purpose) and shares ONE budget."""
    from langchain_openai import ChatOpenAI  # noqa: PLC0415
    from pydantic import SecretStr  # noqa: PLC0415

    budget = SharedCallBudget(max_model_calls=2, max_tool_calls=2)
    for model in (
        ScriptedMockChatModel(responses=[]),
        ChatOpenAI(
            model="test-alias-no-gp",
            api_key=SecretStr("sk-test"),
            base_url="http://127.0.0.1:4000/v1",
        ),
    ):
        agent = build_deep_agents_planner(
            chat_model=model,
            model_alias="test-alias-no-gp",
            read_tools=build_submission_read_tools(),
            budget=budget,
        )
        task_tool = agent.nodes["tools"].bound.tools_by_name["task"]
        assert "- evidence_auditor:" in task_tool.description
        assert "- general-purpose:" not in task_tool.description

    mw_coord = SharedBudgetMiddleware(budget)
    mw_spec = SharedBudgetMiddleware(budget)
    mw_coord.before_model({}, None)
    mw_spec.before_model({}, None)
    with pytest.raises(SharedBudgetExceededError):
        mw_coord.before_model({}, None)
    assert mw_coord.wrap_tool_call("req", lambda r: f"ok:{r}") == "ok:req"
    assert mw_spec.wrap_tool_call("req2", lambda r: f"ok:{r}") == "ok:req2"
    with pytest.raises(SharedBudgetExceededError):
        mw_coord.wrap_tool_call("req3", lambda r: r)


def test_verify_citation_edge_branches() -> None:
    docs = {
        "doc-1": (
            "Annual Revenue: $4,200,000\n"
            "3-Year Loss History: 0 claims reported.\n"
            "FEIN: [MISSING]\n"
            "SUPERSEDES sub-05-app-v1.\n"
            "prohibited_classes: Roofing Contractor, max_coverage_usd: 5000000"
        )
    }
    ok, msg = verify_citation_against_doc(
        EvidenceCitation(doc_id="unknown", section_or_quote="x", fact_supported="y"), docs
    )
    assert not ok and "Unknown doc_id" in msg

    ok, msg = verify_citation_against_doc(
        EvidenceCitation(doc_id="doc-1", section_or_quote="   ", fact_supported="y"), docs
    )
    assert not ok and "does not resolve" in msg

    ok, _ = verify_citation_against_doc(
        EvidenceCitation(
            doc_id="doc-1",
            section_or_quote="FEIN: [MISSING]",
            fact_supported="FEIN is missing from the application.",
        ),
        docs,
    )
    assert ok

    ok, _ = verify_citation_against_doc(
        EvidenceCitation(
            doc_id="doc-1",
            section_or_quote="SUPERSEDES sub-05-app-v1.",
            fact_supported="Supersedes sub-05-app-v1.",
        ),
        docs,
    )
    assert ok

    ok, _ = verify_citation_against_doc(
        EvidenceCitation(
            doc_id="doc-1",
            section_or_quote="prohibited_classes: Roofing Contractor, max_coverage_usd: 5000000",
            fact_supported="Roofing is a prohibited class with max_coverage_usd 5000000.",
        ),
        docs,
    )
    assert ok

    # Multi-line window resolution + punctuation-only quote branch
    ok, _ = verify_citation_against_doc(
        EvidenceCitation(
            doc_id="doc-1",
            section_or_quote="Annual Revenue 4,200,000 reported",
            fact_supported="Annual revenue is $4,200,000.",
        ),
        docs,
    )
    assert ok

    ok, msg = verify_citation_against_doc(
        EvidenceCitation(
            doc_id="doc-1", section_or_quote="---", fact_supported="Revenue $4,200,000"
        ),
        docs,
    )
    assert not ok and "does not resolve" in msg


async def test_submission_ab_mock_metrics_and_read_tools_coverage() -> None:
    """Cover UsageTrackingCallback, read-only submission tools, and mock run_submission_ab."""
    from langchain_core.messages import AIMessage  # noqa: PLC0415
    from langchain_core.outputs import ChatGeneration, LLMResult  # noqa: PLC0415
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: PLC0415

    from evals.metrics import UsageTrackingCallback  # noqa: PLC0415
    from evals.submission_ab import run_submission_ab  # noqa: PLC0415

    cb = UsageTrackingCallback()
    await cb.on_chat_model_start({}, [])
    await cb.on_llm_end(
        LLMResult(
            generations=[],
            llm_output={"token_usage": {"prompt_tokens": 1000, "completion_tokens": 200}},
        )
    )
    ai_msg = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
    )
    await cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=ai_msg)]]))
    assert cb.stats.llm_calls == 1
    assert cb.stats.prompt_tokens == 1500
    assert cb.stats.completion_tokens == 300
    assert cb.stats.estimate_cost_usd("eval-anthropic") > 0
    assert cb.stats.estimate_cost_usd("eval-dgx-coder") == 0.0

    tools = {t.name: t for t in build_submission_read_tools()}
    assert "sub-01-app" in tools["list_submission_documents"].invoke({"submission_id": "sub-01"})
    assert "not found" in tools["list_submission_documents"].invoke({"submission_id": "missing"})
    assert "Northstar" in tools["read_submission_document"].invoke(
        {"submission_id": "sub-01", "doc_id": "sub-01-app"}
    )
    assert "not found" in tools["read_submission_document"].invoke(
        {"submission_id": "sub-01", "doc_id": "nope"}
    )
    assert "not found" in tools["read_submission_document"].invoke(
        {"submission_id": "nope", "doc_id": "nope"}
    )
    assert "carrier-apex" in tools["list_carriers"].invoke({})
    assert "Apex Commercial" in tools["lookup_carrier_appetite"].invoke(
        {"carrier_id": "carrier-apex"}
    )
    assert "not found" in tools["lookup_carrier_appetite"].invoke({"carrier_id": "nope"})

    settings = Settings(model_mode="mock")
    graph = build_submission_triage_graph(
        deps=SubmissionTriageDeps(settings=settings),
        checkpointer=InMemorySaver(),
    )
    for sub_id, expected in [
        ("sub-07", "broker_clarification_requested"),
        ("sub-06", "routed_to_human_review"),
    ]:
        ctx = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id=f"cov-{sub_id}",
            run_id=f"r-{sub_id}",
            request_id=f"rq-{sub_id}",
        )
        out = await graph.ainvoke(
            {"submission_id": sub_id},
            config={"configurable": {"thread_id": ctx.thread_id}},
            context=ctx,
        )
        assert out["final_status"] == expected

    bob_tok = mint_user_jwt(
        CallerIdentity(tenant_id="tenant-alpha", actor_id="bob", roles=frozenset({"admin"})),
        secret=settings.jwt_signing_secret,
    )

    ctx_rej = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="cov-rej",
        run_id="r-rej",
        request_id="rq-rej",
    )
    cfg_rej = {"configurable": {"thread_id": ctx_rej.thread_id}}
    await graph.ainvoke({"submission_id": "sub-01"}, config=cfg_rej, context=ctx_rej)
    intr_rej = (await graph.aget_state(cfg_rej)).interrupts[0].value
    rej = await graph.ainvoke(
        build_verified_approval_command(
            bearer_token=bob_tok,
            decision="reject",
            approval_tenant_id="tenant-alpha",
            requested_by="alice",
            operation_id=intr_rej["operation_id"],
            approved_payload_hash=intr_rej["approved_payload_hash"],
            settings=settings,
        ),
        config=cfg_rej,
        context=ctx_rej,
    )
    assert rej["final_status"] == "rejected"

    ctx_ok = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="cov-ok",
        run_id="r-ok",
        request_id="rq-ok",
    )
    cfg_ok = {"configurable": {"thread_id": ctx_ok.thread_id}}
    await graph.ainvoke({"submission_id": "sub-01"}, config=cfg_ok, context=ctx_ok)
    intr_ok = (await graph.aget_state(cfg_ok)).interrupts[0].value
    done = await graph.ainvoke(
        build_verified_approval_command(
            bearer_token=bob_tok,
            decision="approve",
            approval_tenant_id="tenant-alpha",
            requested_by="alice",
            operation_id=intr_ok["operation_id"],
            approved_payload_hash=intr_ok["approved_payload_hash"],
            settings=settings,
        ),
        config=cfg_ok,
        context=ctx_ok,
    )
    assert done["final_status"] == "completed"

    rep = await run_submission_ab(
        dataset_path=Path("evals/datasets/submissions.jsonl"),
        model_alias="eval-anthropic",
        trials=1,
        variants=("baseline",),
        settings=settings,
    )
    assert rep["summary"]["baseline"]["overall"]["runs"] == 12


def test_trial_schedule_alternates_arms_within_each_case() -> None:
    """Arms alternate within each (case, trial) and flip order across odd/even trials."""
    cases = [{"case_id": "sub-01"}, {"case_id": "sub-02"}]
    sched = build_trial_schedule(cases, trials=2)
    assert [(v, c["case_id"], t) for v, c, t in sched] == [
        ("baseline", "sub-01", 1),
        ("deep_agents", "sub-01", 1),
        ("deep_agents", "sub-01", 2),
        ("baseline", "sub-01", 2),
        ("baseline", "sub-02", 1),
        ("deep_agents", "sub-02", 1),
        ("deep_agents", "sub-02", 2),
        ("baseline", "sub-02", 2),
    ]


def test_negative_control_same_revenue_attributed_to_another_company_fails() -> None:
    """Standalone negative control: same revenue attributed to another company MUST fail."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Contoso Logistics LLC has annual revenue of $4,200,000.",
                subject="Contoso Logistics LLC",
                field="annual_revenue",
                value="4200000",
                polarity="affirmed",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("Entity/subject mismatch" in f for f in scores["citation_failures"])


def test_negative_control_truncated_excerpt_cutting_off_source_negation_fails() -> None:
    """Standalone negative control: excerpt cutting off leading source negation MUST fail."""
    doc_texts = {
        "sub-01-app": (
            "Signed Commercial Application\n"
            "Insured: Northstar Analytics Inc.\n"
            "Northstar Analytics Inc. does not have Annual Revenue: $4,200,000.\n"
        )
    }
    cit = EvidenceCitation(
        doc_id="sub-01-app",
        section_or_quote="Annual Revenue: $4,200,000",
        fact_supported="Northstar Analytics Inc. has annual revenue of $4,200,000.",
        subject="Northstar Analytics Inc.",
        field="annual_revenue",
        value="4200000",
        polarity="affirmed",
    )
    ok, reason = verify_citation_against_doc(cit, doc_texts)
    assert ok is False
    assert "polarity" in reason.lower() or "negated" in reason.lower()


def test_negative_control_unsupported_appended_claim_fails() -> None:
    """Standalone negative control: genuine fact + unsupported appended clause MUST fail."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported=(
                    "Northstar Analytics Inc. has annual revenue of $4,200,000, "
                    "and underwriting already approved a special executive waiver."
                ),
                subject="Northstar Analytics Inc.",
                field="annual_revenue",
                value="4200000",
                polarity="affirmed",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("Unsupported appended" in f for f in scores["citation_failures"])


def test_negative_control_moonbeam_labs_different_insured_fails() -> None:
    """Audit control (1): 'A different insured named Moonbeam Labs' on Northstar revenue fails."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported=(
                    "A different insured named Moonbeam Labs has annual revenue of $4,200,000."
                ),
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("Entity/subject mismatch" in f for f in scores["citation_failures"])


def test_negative_control_structured_field_mismatch_on_correct_revenue_prose_fails() -> None:
    """Audit control (2): structured field=requested_coverage with revenue prose/excerpt fails."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Northstar Analytics Inc. has annual revenue of $4,200,000.",
                subject="Northstar Analytics Inc.",
                field="requested_coverage",
                value="4200000",
                polarity="affirmed",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("Structured tuple" in f for f in scores["citation_failures"])


def test_negative_control_unverified_inline_qualifier_earthquake_mitigation_fails() -> None:
    """Audit control (3): 'exclusively from certified earthquake mitigation' on revenue fails."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported=(
                    "Northstar has annual revenue of $4,200,000 exclusively from "
                    "certified earthquake mitigation."
                ),
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("Unsupported appended/extra assertion" in f for f in scores["citation_failures"])


def test_negative_control_unknown_structured_field_vocabulary_fails() -> None:
    """Structured field outside the allowed vocabulary is explicitly rejected as unreviewable."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Northstar Analytics Inc. has annual revenue of $4,200,000.",
                subject="Northstar Analytics Inc.",
                field="custom_unverified_field",
                value="4200000",
                polarity="affirmed",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["citations_grounded"] is False
    assert scores["unreviewable_citations"]


def test_negative_control_absent_controlling_document_fails() -> None:
    """Standalone negative control: omitting must_cite_docs (sub-01-app) MUST fail."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-email",
                section_or_quote="Revenue is $4,200,000, requesting $2,000,000 limit",
                fact_supported="Annual revenue is $4,200,000 and requested limit is $2,000,000.",
                subject="Northstar Analytics Inc.",
                field="annual_revenue",
                value="4200000",
                polarity="affirmed",
            )
        ],
        summary="Northstar Analytics Inc. $4,200,000 revenue, $2,000,000 limit, 2026-12-01.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["typed_fields_ok"] is True
    assert scores["must_cite_covered"] is False
    assert scores["citations_grounded"] is False
    assert scores["facts_supported"] is False
    assert any("Absent controlling document" in f for f in scores["citation_failures"])


def test_positive_control_structured_evidence_citation_passes() -> None:
    """Genuine positive control with subject, field, value, and polarity passes cleanly."""
    case = _load_case("sub-01")
    brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=4_200_000,
        requested_coverage_usd=2_000_000,
        effective_date="2026-12-01",
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Annual Revenue: $4,200,000",
                fact_supported="Northstar Analytics Inc. has annual revenue of $4,200,000.",
                subject="Northstar Analytics Inc.",
                field="annual_revenue",
                value="4200000",
                polarity="affirmed",
            ),
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Requested Limit: $2,000,000",
                fact_supported="Requested limit is $2,000,000.",
                subject="Northstar Analytics Inc.",
                field="requested_coverage",
                value="2000000",
                polarity="affirmed",
            ),
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote="Effective Date: 2026-12-01",
                fact_supported="Effective date is 2026-12-01.",
                subject="Northstar Analytics Inc.",
                field="effective_date",
                value="2026-12-01",
                polarity="affirmed",
            ),
        ],
        summary="Northstar Analytics Inc. complete and eligible for carrier-apex.",
    )
    scores = score_submission_brief(case, brief)
    assert scores["disposition_correct"] is True
    assert scores["typed_fields_ok"] is True
    assert scores["citations_grounded"] is True
    assert scores["unreviewable_citations"] == []
    assert scores["facts_supported"] is True


async def test_usage_tracking_callback_records_model_provenance_and_tool_calls() -> None:
    """UsageTrackingCallback captures exact requested/responded model IDs and tool calls."""
    from langchain_core.messages import AIMessage  # noqa: PLC0415
    from langchain_core.outputs import ChatGeneration, LLMResult  # noqa: PLC0415

    from evals.metrics import UsageTrackingCallback  # noqa: PLC0415

    cb = UsageTrackingCallback()
    await cb.on_chat_model_start(
        {"kwargs": {"model": "eval-anthropic"}},
        [[]],
        invocation_params={"model_name": "eval-anthropic"},
    )
    await cb.on_tool_start({"name": "read_submission_document"}, "{}")
    msg = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        response_metadata={"model_name": "anthropic/claude-sonnet-5-5"},
    )
    await cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]))
    assert cb.stats.llm_calls == 1
    assert cb.stats.tool_calls == 1
    assert cb.stats.tools_called == ["read_submission_document"]
    assert cb.stats.unique_requested_models == ["eval-anthropic"]
    assert cb.stats.unique_response_models == ["anthropic/claude-sonnet-5-5"]
    assert cb.stats.total_tokens == 125


def test_structured_fact_canonicalizer_all_field_types() -> None:
    """Exercise structured tuple verification across all allowed field types."""
    docs = {
        "sub-01-app": (
            "Signed Application\n"
            "Insured: Northstar Analytics Inc.\n"
            "FEIN: 84-1234567\n"
            "Class: SaaS / B2B Software\n"
            "Annual Revenue: $4,200,000\n"
            "Effective Date: 2026-12-01\n"
            "3-Year Loss History: 0 claims reported.\n"
        )
    }
    for quote, claim, fld, val in (
        (
            "Effective Date: 2026-12-01",
            "Effective date is 2026-12-01.",
            "effective_date",
            "2026-12-01",
        ),
        ("FEIN: 84-1234567", "FEIN is 84-1234567.", "fein", "84-1234567"),
        (
            "Class: SaaS / B2B Software",
            "Business classification is SaaS / B2B Software.",
            "business_classification",
            "SaaS / B2B Software",
        ),
        (
            "3-Year Loss History: 0 claims reported.",
            "Loss history is 0 claims.",
            "loss_history",
            "0 claims",
        ),
        (
            "Insured: Northstar Analytics Inc.",
            "Named insured is Northstar Analytics Inc.",
            "insured_name",
            "Northstar Analytics Inc.",
        ),
    ):
        ok, reason = verify_citation_against_doc(
            EvidenceCitation(
                doc_id="sub-01-app",
                section_or_quote=quote,
                fact_supported=claim,
                subject="Northstar Analytics Inc.",
                field=fld,
                value=val,
                polarity="affirmed",
            ),
            docs,
        )
        assert ok is True, reason
