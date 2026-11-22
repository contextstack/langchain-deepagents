"""Regression tests for evidence-inspecting scorer, Deep Agents delegate config, & interleaving."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.submission_ab import (
    build_trial_schedule,
    score_submission_brief,
)
from gateway.mocks import ScriptedMockChatModel
from workflows.submission_triage.graph import (
    SharedBudgetExceededError,
    SharedBudgetMiddleware,
    SharedCallBudget,
    build_deep_agents_planner,
    build_submission_read_tools,
)
from workflows.submission_triage.models import EvidenceCitation, SubmissionBrief


def _load_case(case_id: str) -> dict[str, object]:
    for line in Path("evals/datasets/submissions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["case_id"] == case_id:
                return row
    raise KeyError(case_id)


def test_invented_citation_with_all_expected_keywords_still_fails() -> None:
    """Acceptance Criterion #2: fabricated passage containing all keywords MUST fail grading."""
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
                # Packs every expected keyword, but invents a passage that does NOT exist in doc!
                section_or_quote=(
                    "Executive Committee Appendix 99 unanimously certified Northstar "
                    "$4,200,000 revenue $2,000,000 limit effective 2026-12-01"
                ),
                fact_supported="Northstar Analytics revenue $4,200,000 and $2,000,000 limit.",
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
    assert any("84-1234567" in f for f in scores["citation_failures"])


def test_genuine_cited_brief_passes_passage_and_typed_field_checks() -> None:
    """Genuine brief with verbatim passages and typed fields passes all evidence checks."""
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
                    "Insured: Northstar Analytics Inc. Annual Revenue: $4,200,000 "
                    "Requested Limit: $2,000,000 Effective Date: 2026-12-01"
                ),
                fact_supported=(
                    "Northstar Analytics Inc. has $4,200,000 revenue and requests "
                    "$2,000,000 limit effective 2026-12-01."
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

    # Verify ONE shared budget across coordinator + specialist (both model and tool limits)
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
    from evals.submission_ab import verify_citation_against_doc  # noqa: PLC0415

    docs = {"doc-1": "Revenue is $4,200,000. 0 claims reported. Missing FEIN."}
    ok, msg = verify_citation_against_doc(
        EvidenceCitation(doc_id="unknown", section_or_quote="x", fact_supported="y"), docs
    )
    assert not ok and "Unknown doc_id" in msg

    ok, msg = verify_citation_against_doc(
        EvidenceCitation(doc_id="doc-1", section_or_quote="   ", fact_supported="y"), docs
    )
    assert not ok and "Empty" in msg

    ok, msg = verify_citation_against_doc(
        EvidenceCitation(doc_id="doc-1", section_or_quote="is a", fact_supported="y"), docs
    )
    assert not ok and "no verifiable terms" in msg

    ok, msg = verify_citation_against_doc(
        EvidenceCitation(
            doc_id="doc-1",
            section_or_quote="Missing FEIN.",
            fact_supported="FEIN is complete and confirmed.",
        ),
        docs,
    )
    assert not ok and "contradicts" in msg


def test_genuine_excerpt_with_contradicting_claim_or_wrong_typed_values_fails() -> None:
    """Reviewer regression: genuine excerpt + contradicting claim & wrong typed fields MUST fail."""
    case = _load_case("sub-01")
    contradicting_brief = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        business_classification="SaaS / B2B Software",
        annual_revenue_usd=9_999_999,  # WRONG (expected 4_200_000)
        requested_coverage_usd=5_000_000,  # WRONG (expected 2_000_000)
        effective_date="2028-01-01",  # WRONG (expected 2026-12-01)
        carrier_match="carrier-apex",
        material_facts=[
            EvidenceCitation(
                doc_id="sub-01-app",
                # Genuine excerpt from sub-01-app, paired with a CONTRADICTING claim!
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


async def test_submission_ab_mock_metrics_and_read_tools_coverage() -> None:
    """Cover UsageTrackingCallback, read-only submission tools, and mock run_submission_ab."""
    from langchain_core.messages import AIMessage  # noqa: PLC0415
    from langchain_core.outputs import ChatGeneration, LLMResult  # noqa: PLC0415
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: PLC0415
    from langgraph.types import Command  # noqa: PLC0415

    from core.config import Settings  # noqa: PLC0415
    from core.models import RunContext  # noqa: PLC0415
    from evals.metrics import UsageTrackingCallback  # noqa: PLC0415
    from evals.submission_ab import run_submission_ab  # noqa: PLC0415
    from workflows.submission_triage.graph import (  # noqa: PLC0415
        SubmissionTriageDeps,
        build_submission_triage_graph,
    )

    # 1. UsageTrackingCallback with both token_usage and usage_metadata
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

    # 2. All 4 shared read-only submission tools (happy + not-found error paths)
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

    # 3. Non-ready dispositions (ask_broker, human_review) + rejected + in-memory submit
    graph = build_submission_triage_graph(
        deps=SubmissionTriageDeps(settings=Settings(model_mode="mock")),
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

    # Rejected approval + in-memory approved submit
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
    rej = await graph.ainvoke(
        Command(resume={"decision": "reject", "approver_id": "bob"}),
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
    done = await graph.ainvoke(
        Command(resume={"decision": "approve", "approver_id": "bob", "approver_roles": ["admin"]}),
        config=cfg_ok,
        context=ctx_ok,
    )
    assert done["final_status"] == "completed"

    # 4. Offline mock run_submission_ab smoke
    rep = await run_submission_ab(
        dataset_path=Path("evals/datasets/submissions.jsonl"),
        model_alias="eval-anthropic",
        trials=1,
        variants=("baseline",),
        settings=Settings(model_mode="mock"),
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
