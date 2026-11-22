"""Experiment 1 paired A/B evaluation runner: Baseline vs. Deep Agents on 12 submissions."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
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

_NEGATION_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|doesn't|don't|did\s+not|didn't|"
    r"is\s+not|isn't|are\s+not|aren't|was\s+not|wasn't|were\s+not|weren't|"
    r"has\s+not|hasn't|have\s+not|haven't|cannot|can't|not\s+have|never|"
    r"neither|nor|contrary\s+to|contradicts|false\s+that|no\s+longer)\b"
)
_NUM_OR_ID_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{7}|clm-\d{4}-\d+|\d{2,})\b")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")
_MILLION_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*m(?:illion)?\b")
_THOUSAND_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*k\b")

_CLASS_CANONICAL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("saas", ("saas", "b2b software", "cloud analytics")),
    ("consulting", ("management consulting", "it & management consulting", "consulting")),
    ("accounting", ("accounting", "legal professional")),
    ("light_manufacturing", ("light manufacturing", "cnc metal")),
    ("warehouse", ("warehouse", "distribution")),
    ("wholesale", ("wholesale goods", "wholesale")),
    ("hvac", ("hvac", "mechanical")),
    ("roofing", ("roofing", "flat-roof", "torch-down")),
    ("fireworks", ("fireworks", "explosives", "pyrotechnics")),
    ("cannabis", ("cannabis",)),
)


@dataclass(frozen=True)
class TypedFact:
    """Deterministic typed fact extracted from a source span or claim."""

    field: str
    value: str
    polarity: Literal["affirmed", "negated"] = "affirmed"


def _normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip `$` and numeric commas, expand `$4.2M` -> `4200000`."""
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text).replace("$", "").lower()
    cleaned = _MILLION_RE.sub(lambda m: str(int(round(float(m.group(1)) * 1_000_000))), cleaned)
    cleaned = _THOUSAND_RE.sub(lambda m: str(int(round(float(m.group(1)) * 1_000))), cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _source_texts_for_case(case_id: str) -> dict[str, str]:
    sub = SUBMISSIONS[case_id]
    texts: dict[str, str] = {d.doc_id: f"{d.title}\n{d.content}" for d in sub.documents}
    for cid, guide in CARRIER_APPETITE_GUIDES.items():
        texts[cid] = json.dumps(serialize_carrier(guide))
    return texts


def _clause_polarity(clause: str) -> Literal["affirmed", "negated"]:
    return "negated" if _NEGATION_RE.search(clause) else "affirmed"


def resolve_source_span(doc_text: str, section_or_quote: str) -> str | None:
    """Resolve `section_or_quote` to an exact verifiable source span inside `doc_text`."""
    norm_quote = _normalize_text(section_or_quote)
    if not norm_quote:
        return None
    norm_doc = _normalize_text(doc_text)
    if norm_quote in norm_doc:
        return norm_quote

    # Fall back to matching a contiguous 1-3 line/sentence span in doc_text where every
    # concrete numeric/date/ID token in the quote exists and >=80% of content tokens match.
    raw_units = [u.strip() for u in re.split(r"[\n]+|(?<=[.!?])\s+", doc_text) if u.strip()]
    quote_nums = set(_NUM_OR_ID_RE.findall(norm_quote))
    quote_words = [w for w in _WORD_RE.findall(norm_quote) if len(w) >= 4]
    if not quote_words and not quote_nums:
        return None

    best_span: str | None = None
    best_score = 0.0
    for i in range(len(raw_units)):
        for window in (1, 2, 3):
            if i + window > len(raw_units):
                continue
            cand = _normalize_text(" ".join(raw_units[i : i + window]))
            if quote_nums and not all(tok in cand for tok in quote_nums):
                continue
            cand_words = set(_WORD_RE.findall(cand))
            if not quote_words:
                return cand
            overlap = sum(1 for w in quote_words if w in cand_words) / len(quote_words)
            if overlap >= 0.8 and overlap > best_score:
                best_score = overlap
                best_span = cand
    return best_span


def extract_typed_facts(text: str) -> set[TypedFact]:
    """Deterministically parse verifiable (field, value, polarity) facts from a span or claim."""
    norm = _normalize_text(text)
    facts: set[TypedFact] = set()
    # Split into sentence/clause units so negation stays scoped to its clause
    clauses = [c.strip() for c in re.split(r"[;\n]+|(?<=[.!?])\s+", norm) if c.strip()]
    for clause in clauses:
        pol = _clause_polarity(clause)

        # 1. Annual revenue
        for m in re.finditer(
            r"(?:annual\s+)?revenue(?::|\s+(?:is|of|at))?\s*(\d{5,9})\b"
            r"|\b(\d{5,9})\s+(?:annual\s+)?revenue\b",
            clause,
        ):
            val = m.group(1) or m.group(2)
            facts.add(TypedFact("annual_revenue", val, pol))

        # 2. Requested coverage / limit
        for m in re.finditer(
            r"(?:requested\s+)?(?:coverage|limit)(?::|\s+(?:is|of|at))?\s*(\d{5,9})\b"
            r"|\b(\d{5,9})\s+(?:coverage\s+)?limit\b",
            clause,
        ):
            val = m.group(1) or m.group(2)
            facts.add(TypedFact("requested_coverage", val, pol))

        # 3. Effective date (ISO YYYY-MM-DD)
        for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", clause):
            if any(k in clause for k in ("effective", "date", "2026-", "2027-")):
                facts.add(TypedFact("effective_date", m.group(1), pol))

        # 4. FEIN
        for m in re.finditer(r"\b(\d{2}-\d{7})\b", clause):
            facts.add(TypedFact("fein", m.group(1), pol))

        # 5. Loss history (loss-free vs open/incurred claims)
        if re.search(r"\b(?:0\s+claims|zero\s+claims|loss-free|no\s+claims)\b", clause):
            facts.add(TypedFact("loss_history", "loss_free", pol))
        if re.search(
            r"\b(?:open\s+bodily\s+injury|open\s+claim|clm-\d{4}-\d+|minor\s+property\s+claim)\b",
            clause,
        ):
            amt_m = re.search(r"\b(\d{4,7})\b", clause)
            val = amt_m.group(1) if amt_m else "has_claims"
            facts.add(TypedFact("loss_history", val, pol))

        # 6. Business classification
        for canon_cls, aliases in _CLASS_CANONICAL:
            if any(a in clause for a in aliases):
                facts.add(TypedFact("business_classification", canon_cls, pol))

        # 7. Missing / blank / pending required fields
        if re.search(r"\b(?:missing|blank|not\s+provided|pending|tbd|omitted)\b", clause):
            for field_tag, patterns in (
                ("fein", ("fein",)),
                ("effective_date", ("effective date",)),
                ("loss_history", ("loss history", "loss run")),
                ("location_schedule", ("location schedule", "warehouse addresses")),
                ("payroll_breakdown", ("payroll",)),
            ):
                if any(p in clause for p in patterns):
                    facts.add(TypedFact("missing_field", field_tag, pol))

        # 8. Superseded attachment marker
        sup_m = re.search(r"supersedes\s+(sub-\d+-[a-z0-9\-]+)", clause)
        if sup_m:
            facts.add(TypedFact("supersedes", sup_m.group(1), pol))

        # 9. Carrier appetite caps / prohibited classes
        if "prohibited_classes" in clause or "prohibited" in clause:
            for canon_cls, aliases in _CLASS_CANONICAL:
                if any(a in clause for a in aliases):
                    facts.add(TypedFact("prohibited_class", canon_cls, pol))
        if "max_coverage_usd" in clause or "max_single_loss_usd" in clause:
            for m in re.finditer(r"\b(\d{5,8})\b", clause):
                facts.add(TypedFact("carrier_cap", m.group(1), pol))

    return facts


def verify_citation_against_doc(
    citation: EvidenceCitation,
    doc_texts: dict[str, str],
) -> tuple[bool, str]:
    """Resolve `section_or_quote` to a source span and verify typed (field, value, polarity)."""
    raw_doc = doc_texts.get(citation.doc_id)
    if raw_doc is None:
        return False, f"Unknown doc_id '{citation.doc_id}'"

    resolved_span = resolve_source_span(raw_doc, citation.section_or_quote)
    if resolved_span is None:
        return (
            False,
            f"Cited excerpt '{citation.section_or_quote}' does not resolve to a source span "
            f"in '{citation.doc_id}'",
        )

    span_facts = extract_typed_facts(resolved_span)
    claim_facts = extract_typed_facts(citation.fact_supported)
    if not claim_facts:
        return (
            False,
            f"Claim '{citation.fact_supported}' on '{citation.doc_id}' contains no verifiable "
            "typed fact (ambiguous/unsupported free text)",
        )

    unsupported = [cf for cf in sorted(claim_facts, key=str) if cf not in span_facts]
    if unsupported:
        return (
            False,
            f"Claimed fact(s) {unsupported} not supported by resolved span on "
            f"'{citation.doc_id}' (span facts: {sorted(span_facts, key=str)})",
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

    # Typed-field grading on SubmissionBrief (exact expected values when registered!)
    typed_failures: list[str] = []
    if expected_disp == "ready_to_quote":
        exp_carrier = case.get("expected_carrier")
        if exp_carrier and brief.carrier_match != exp_carrier:
            typed_failures.append(f"carrier_match={brief.carrier_match} != {exp_carrier}")
        exp_rev = case.get("expected_revenue_usd")
        if exp_rev is not None:
            if brief.annual_revenue_usd != int(exp_rev):
                typed_failures.append(
                    f"annual_revenue_usd={brief.annual_revenue_usd} != {exp_rev}"
                )
        elif brief.annual_revenue_usd is None or brief.annual_revenue_usd <= 0:
            typed_failures.append("missing annual_revenue_usd")

        exp_cov = case.get("expected_coverage_usd")
        if exp_cov is not None:
            if brief.requested_coverage_usd != int(exp_cov):
                typed_failures.append(
                    f"requested_coverage_usd={brief.requested_coverage_usd} != {exp_cov}"
                )
        elif brief.requested_coverage_usd is None or brief.requested_coverage_usd <= 0:
            typed_failures.append("missing requested_coverage_usd")

        exp_eff = case.get("expected_effective_date")
        if exp_eff is not None:
            if brief.effective_date != str(exp_eff):
                typed_failures.append(f"effective_date={brief.effective_date} != {exp_eff}")
        elif not brief.effective_date:
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
