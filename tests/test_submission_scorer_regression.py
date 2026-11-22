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

    # Verify ONE shared budget across coordinator + specialist
    mw_coord = SharedBudgetMiddleware(budget)
    mw_spec = SharedBudgetMiddleware(budget)
    mw_coord.before_model({}, None)
    mw_spec.before_model({}, None)
    with pytest.raises(SharedBudgetExceededError):
        mw_coord.before_model({}, None)


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
