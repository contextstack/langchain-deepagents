"""Experiment 1 paired A/B evaluation runner: Baseline vs. Deep Agents on 12 submissions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import InMemorySaver

from core.config import Settings, get_settings
from core.models import RunContext
from evals.metrics import UsageTrackingCallback
from workflows.submission_triage.fixtures import CARRIER_APPETITE_GUIDES, SUBMISSIONS
from workflows.submission_triage.graph import (
    SubmissionTriageDeps,
    build_submission_triage_graph,
)
from workflows.submission_triage.models import SubmissionBrief


def score_submission_brief(case: dict[str, Any], brief: SubmissionBrief) -> dict[str, Any]:
    """Deterministic scoring against pre-registered ground truth in submissions.jsonl."""
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

    valid_doc_ids = {d.doc_id for d in SUBMISSIONS[case["case_id"]].documents} | set(
        CARRIER_APPETITE_GUIDES.keys()
    )
    cited_ids = {c.doc_id for c in brief.material_facts}
    # Citations grounded: non-empty, all cited doc_ids actually exist in this submission,
    # and every required doc_id from ground truth is cited.
    must_cite = set(case.get("must_cite_docs") or [])
    citations_valid = bool(cited_ids) and cited_ids.issubset(valid_doc_ids)
    must_cite_covered = must_cite.issubset(cited_ids)

    # Check material fact keywords appear across cited facts or summary
    evidence_blob = (
        brief.summary
        + " "
        + " ".join(
            f"{c.doc_id} {c.section_or_quote} {c.fact_supported}" for c in brief.material_facts
        )
        + " "
        + " ".join(brief.missing_fields)
        + " "
        + " ".join(f"{c.field} {c.detail}" for c in brief.contradictions)
    ).lower()
    keywords = [str(k).lower() for k in (case.get("expected_fact_keywords") or [])]
    matched_kw = sum(1 for kw in keywords if kw in evidence_blob)
    facts_supported = citations_valid and must_cite_covered and (
        matched_kw >= max(1, len(keywords) - 1)
    )

    return {
        "disposition_correct": disposition_correct,
        "expected_disposition": expected_disp,
        "actual_disposition": actual_disp,
        "missed_escalation": missed_escalation,
        "unnecessary_escalation": unnecessary_escalation,
        "citations_valid": citations_valid,
        "must_cite_covered": must_cite_covered,
        "facts_supported": facts_supported,
        "matched_keywords": f"{matched_kw}/{len(keywords)}",
        "cited_doc_ids": sorted(cited_ids),
    }


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
            try:
                final = await graph.ainvoke(
                    {"submission_id": case["case_id"], "planner_variant": variant},
                    config={"configurable": {"thread_id": ctx.thread_id}},
                    context=ctx,
                    durability="sync",
                )
                brief = SubmissionBrief.model_validate(final["submission_brief"])
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

    tasks = [
        _run_one(variant, case, trial)
        for variant in variants
        for case in cases
        for trial in range(1, trials + 1)
    ]
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
