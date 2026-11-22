"""Experiment 1 paired A/B evaluation runner: Baseline vs. Deep Agents on 12 submissions."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import InMemorySaver

from core.config import Settings, get_settings
from core.models import RunContext
from evals.metrics import UsageTrackingCallback
from workflows.submission_triage.fixtures import (
    CARRIER_APPETITE_GUIDES,
    SUBMISSIONS,
    serialize_carrier,
)
from workflows.submission_triage.graph import (
    SubmissionTriageDeps,
    build_submission_triage_graph,
)
from workflows.submission_triage.models import EvidenceCitation, SubmissionBrief

_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "were",
        "been",
        "into",
        "over",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "about",
        "under",
        "above",
        "which",
        "where",
        "while",
        "other",
        "clean",
        "field",
        "fields",
        "value",
        "states",
        "stated",
        "shows",
        "shown",
        "lists",
        "listed",
        "confirms",
        "confirmed",
    }
)
_NUM_OR_ID_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{7}|clm-\d{4}-\d+|\d{2,})\b")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _normalize_text(text: str) -> str:
    """Lowercase and strip `$` and numeric commas (`$4,200,000` -> `4200000`)."""
    no_commas = re.sub(r"(?<=\d),(?=\d)", "", text)
    return no_commas.replace("$", "").lower()


def _source_texts_for_case(case_id: str) -> dict[str, str]:
    sub = SUBMISSIONS[case_id]
    texts: dict[str, str] = {d.doc_id: f"{d.title}\n{d.content}" for d in sub.documents}
    for cid, guide in CARRIER_APPETITE_GUIDES.items():
        texts[cid] = json.dumps(serialize_carrier(guide))
    return texts


def verify_citation_against_doc(
    citation: EvidenceCitation,
    doc_texts: dict[str, str],
) -> tuple[bool, str]:
    """Verify that a cited passage actually exists in and supports the claim from `doc_id`."""
    raw_doc = doc_texts.get(citation.doc_id)
    if raw_doc is None:
        return False, f"Unknown doc_id '{citation.doc_id}'"

    norm_doc = _normalize_text(raw_doc)
    norm_quote = _normalize_text(citation.section_or_quote)
    norm_fact = _normalize_text(citation.fact_supported)

    if not norm_quote.strip():
        return False, f"Empty section_or_quote on '{citation.doc_id}'"

    # 1. Every concrete numeric/date/ID token in the cited passage or claim must exist in THAT doc
    cited_nums = set(_NUM_OR_ID_RE.findall(norm_quote)) | set(_NUM_OR_ID_RE.findall(norm_fact))
    for tok in cited_nums:
        if tok not in norm_doc:
            return False, f"Token '{tok}' not present in cited doc '{citation.doc_id}'"

    # 2. Lexical overlap: cited passage words must actually come from THAT cited document
    doc_words = set(_WORD_RE.findall(norm_doc))
    quote_words = [
        w for w in _WORD_RE.findall(norm_quote) if len(w) >= 4 and w not in _STOPWORDS
    ]
    if not quote_words:
        return False, f"Cited passage '{citation.section_or_quote}' has no verifiable terms"
    matched = sum(1 for w in quote_words if w in doc_words)
    ratio = matched / len(quote_words)
    if ratio < 0.65:
        return (
            False,
            f"Cited passage not grounded in '{citation.doc_id}' "
            f"(overlap {matched}/{len(quote_words)} = {ratio:.0%})",
        )
    return True, "ok"


def score_submission_brief(case: dict[str, Any], brief: SubmissionBrief) -> dict[str, Any]:
    """Deterministic code grading against pre-registered ground truth in submissions.jsonl."""
    expected_disp = str(case["expected_disposition"])
    actual_disp = brief.disposition
    disposition_correct = actual_disp == expected_disp

    # Missed escalation: expected human_review or ask_broker, but model said ready_to_quote
    missed_escalation = (
        expected_disp in {"human_review", "ask_broker"} and actual_disp == "ready_to_quote"
    )
    # Unnecessary escalation: expected ready_to_quote, but model escalated
    unnecessary_escalation = (
        expected_disp == "ready_to_quote" and actual_disp in {"human_review", "ask_broker"}
    )

    doc_texts = _source_texts_for_case(case["case_id"])
    cited_ids = {c.doc_id for c in brief.material_facts}
    must_cite = set(case.get("must_cite_docs") or [])
    citations_valid = bool(cited_ids) and cited_ids.issubset(set(doc_texts))
    must_cite_covered = must_cite.issubset(cited_ids)

    # Passage-level verification: EVERY cited passage must actually be grounded in its cited doc
    citation_failures: list[str] = []
    for cit in brief.material_facts:
        ok, reason = verify_citation_against_doc(cit, doc_texts)
        if not ok:
            citation_failures.append(reason)
    citations_grounded = citations_valid and must_cite_covered and (len(citation_failures) == 0)

    # Typed-field grading on SubmissionBrief
    typed_failures: list[str] = []
    if expected_disp == "ready_to_quote":
        exp_carrier = case.get("expected_carrier")
        if exp_carrier and brief.carrier_match != exp_carrier:
            typed_failures.append(f"carrier_match={brief.carrier_match} != {exp_carrier}")
        if brief.annual_revenue_usd is None or brief.annual_revenue_usd <= 0:
            typed_failures.append("missing annual_revenue_usd")
        if brief.requested_coverage_usd is None or brief.requested_coverage_usd <= 0:
            typed_failures.append("missing requested_coverage_usd")
        if not brief.effective_date:
            typed_failures.append("missing effective_date")
    if case.get("expect_contradictions") and not brief.contradictions:
        typed_failures.append("expected contradictions list to be non-empty")
    if case.get("expect_missing_fields") and not brief.missing_fields:
        typed_failures.append("expected missing_fields list to be non-empty")
    typed_fields_ok = len(typed_failures) == 0

    # Check material fact keywords appear across verified citations or structured fields
    evidence_blob = _normalize_text(
        brief.summary
        + " "
        + " ".join(
            f"{c.doc_id} {c.section_or_quote} {c.fact_supported}" for c in brief.material_facts
        )
        + " "
        + " ".join(brief.missing_fields)
        + " "
        + " ".join(f"{c.field} {c.detail}" for c in brief.contradictions)
        + f" {brief.insured_name} {brief.annual_revenue_usd or ''} "
        + f"{brief.requested_coverage_usd or ''} {brief.effective_date or ''}"
    )
    keywords = [_normalize_text(str(k)) for k in (case.get("expected_fact_keywords") or [])]
    matched_kw = sum(1 for kw in keywords if kw in evidence_blob)
    facts_supported = (
        citations_grounded
        and typed_fields_ok
        and (matched_kw >= max(1, len(keywords) - 1))
    )

    return {
        "disposition_correct": disposition_correct,
        "expected_disposition": expected_disp,
        "actual_disposition": actual_disp,
        "missed_escalation": missed_escalation,
        "unnecessary_escalation": unnecessary_escalation,
        "citations_valid": citations_valid,
        "must_cite_covered": must_cite_covered,
        "citations_grounded": citations_grounded,
        "citation_failures": citation_failures,
        "typed_fields_ok": typed_fields_ok,
        "typed_failures": typed_failures,
        "facts_supported": facts_supported,
        "matched_keywords": f"{matched_kw}/{len(keywords)}",
        "cited_doc_ids": sorted(cited_ids),
    }


def build_trial_schedule(
    cases: list[dict[str, Any]],
    *,
    trials: int = 3,
    variants: tuple[Literal["baseline", "deep_agents"], ...] = ("baseline", "deep_agents"),
) -> list[tuple[Literal["baseline", "deep_agents"], dict[str, Any], int]]:
    """Alternate planner arms within each case & trial to reduce provider-load drift."""
    schedule: list[tuple[Literal["baseline", "deep_agents"], dict[str, Any], int]] = []
    for case in cases:
        for trial in range(1, trials + 1):
            ordered = variants if (trial % 2 == 1) else tuple(reversed(variants))
            for variant in ordered:
                schedule.append((variant, case, trial))
    return schedule


async def run_submission_ab(
    *,
    dataset_path: Path,
    model_alias: str,
    trials: int = 3,
    variants: tuple[Literal["baseline", "deep_agents"], ...] = ("baseline", "deep_agents"),
    concurrency: int = 4,
    settings: Settings | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    schedule = build_trial_schedule(cases, trials=trials, variants=variants)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(
        variant: Literal["baseline", "deep_agents"],
        case: dict[str, Any],
        trial: int,
    ) -> dict[str, Any]:
        async with sem:
            cb = UsageTrackingCallback()
            deps = SubmissionTriageDeps(
                settings=cfg,
                planner_variant=variant,
                primary_model_alias=model_alias,
                callbacks=[cb],
            )
            graph = build_submission_triage_graph(deps=deps, checkpointer=InMemorySaver())
            ctx = RunContext(
                tenant_id="tenant-alpha",
                requester_id="alice",
                requester_roles=frozenset({"underwriter"}),
                thread_id=f"ab-{variant}-{case['case_id']}-t{trial}",
                run_id=f"run-{variant}-{case['case_id']}-t{trial}",
                request_id=f"req-{variant}-{case['case_id']}-t{trial}",
            )
            t0 = time.monotonic()
            brief_dump: dict[str, Any] | None = None
            try:
                final = await graph.ainvoke(
                    {"submission_id": case["case_id"], "planner_variant": variant},
                    config={"configurable": {"thread_id": ctx.thread_id}},
                    context=ctx,
                    durability="sync",
                )
                brief = SubmissionBrief.model_validate(final["submission_brief"])
                brief_dump = brief.model_dump(mode="json")
                scores = score_submission_brief(case, brief)
                error = None
            except Exception as exc:
                scores = {
                    "disposition_correct": False,
                    "expected_disposition": case["expected_disposition"],
                    "actual_disposition": None,
                    "missed_escalation": False,
                    "unnecessary_escalation": False,
                    "citations_valid": False,
                    "must_cite_covered": False,
                    "citations_grounded": False,
                    "citation_failures": [],
                    "typed_fields_ok": False,
                    "typed_failures": [],
                    "facts_supported": False,
                    "matched_keywords": "0/0",
                    "cited_doc_ids": [],
                }
                error = f"{type(exc).__name__}: {exc}"

            dt = round(time.monotonic() - t0, 2)
            cost = cb.stats.estimate_cost_usd(model_alias)
            row = {
                "model_alias": model_alias,
                "variant": variant,
                "case_id": case["case_id"],
                "tier": case["tier"],
                "scenario_tag": case["scenario_tag"],
                "trial": trial,
                "duration_s": dt,
                "llm_calls": cb.stats.llm_calls,
                "prompt_tokens": cb.stats.prompt_tokens,
                "completion_tokens": cb.stats.completion_tokens,
                "total_tokens": cb.stats.total_tokens,
                "cost_usd": cost,
                "error": error,
                "submission_brief": brief_dump,
                **scores,
            }
            tag = "OK  " if scores["disposition_correct"] else "MISS"
            print(
                f"[{tag}] {model_alias:14s} | {variant:11s} | {case['case_id']} "
                f"({case['tier']:5s}) t{trial}: exp={case['expected_disposition']:14s} "
                f"act={str(scores['actual_disposition']):14s} "
                f"facts={scores['facts_supported']} "
                f"calls={cb.stats.llm_calls} tok={cb.stats.total_tokens} ({dt}s)"
                + (f" ERR={error[:80]}" if error else ""),
                flush=True,
            )
            return row

    tasks = [_run_one(variant, case, trial) for variant, case, trial in schedule]
    runs = list(await asyncio.gather(*tasks))

    def _agg(subset: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(subset)
        if n == 0:
            return {}
        correct = sum(1 for r in subset if r["disposition_correct"])
        facts_ok = sum(1 for r in subset if r["facts_supported"])
        missed = sum(1 for r in subset if r["missed_escalation"])
        unnec = sum(1 for r in subset if r["unnecessary_escalation"])
        errors = sum(1 for r in subset if r["error"])
        total_cost = sum(r["cost_usd"] for r in subset)
        total_calls = sum(r["llm_calls"] for r in subset)
        total_tok = sum(r["total_tokens"] for r in subset)
        avg_lat = round(sum(r["duration_s"] for r in subset) / n, 2)
        return {
            "runs": n,
            "correct": correct,
            "accuracy_pct": round(100.0 * correct / n, 1),
            "facts_supported": facts_ok,
            "facts_supported_pct": round(100.0 * facts_ok / n, 1),
            "missed_escalations": missed,
            "unnecessary_escalations": unnec,
            "errors": errors,
            "avg_latency_s": avg_lat,
            "avg_llm_calls": round(total_calls / n, 2),
            "calls_per_correct": round(total_calls / correct, 2) if correct else None,
            "tokens_per_correct": round(total_tok / correct) if correct else None,
            "total_cost_usd": round(total_cost, 4),
            "cost_per_correct_usd": round(total_cost / correct, 5) if correct else None,
        }

    summary: dict[str, Any] = {}
    for variant in variants:
        v_runs = [r for r in runs if r["variant"] == variant]
        summary[variant] = {
            "overall": _agg(v_runs),
            "clean": _agg([r for r in v_runs if r["tier"] == "clean"]),
            "messy": _agg([r for r in v_runs if r["tier"] == "messy"]),
        }

    return {
        "model_alias": model_alias,
        "trials": trials,
        "summary": summary,
        "runs": runs,
    }
