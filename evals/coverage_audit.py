"""Blinded deterministic grader and paired A/B runner for Coverage Discrepancy Audit."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from core.config import Settings, get_settings
from core.models import RunContext
from evals.metrics import UsageTrackingCallback
from workflows.coverage_audit.graph import CoverageAuditDeps, run_coverage_audit_attempt
from workflows.coverage_audit.models import CoverageAuditBrief, CoverageCitation
from workflows.coverage_audit.tools import ARCHIVE_ROOT, load_packet_documents

_NEG_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|doesn't|not\s+apply|not\s+bound|not\s+executed|"
    r"excluded|revokes|deletes|unsigned|no\s+coverage|unscheduled)\b",
    re.I,
)
_NUM_RE = re.compile(r"\b\d{4,9}\b")
_MILLION_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*m(?:illion)?\b", re.I)
_THOUSAND_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*k\b", re.I)


def _norm(text: str) -> str:
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text).replace("$", "").lower()
    cleaned = _MILLION_RE.sub(lambda m: str(int(round(float(m.group(1)) * 1_000_000))), cleaned)
    cleaned = _THOUSAND_RE.sub(lambda m: str(int(round(float(m.group(1)) * 1_000))), cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_controlling_value(val: str) -> str:
    n = _norm(val)
    if "insufficient" in n or "unknown" in n or "abstain" in n:
        return "insufficient_evidence"
    if "excluded" in n or "not covered" in n:
        return "excluded"
    m = _NUM_RE.search(n)
    return m.group(0) if m else n


def _resolve_full_clause(doc_text: str, quote: str, line_start: int, line_end: int) -> str | None:
    """Resolve verbatim_quote to its FULL enclosing clause inside doc_text."""
    norm_q = _norm(quote)
    if not norm_q:
        return None
    lines = doc_text.splitlines()
    # Prefer the cited line window first (expanded by 1 line for multi-line sentences)
    lo = max(0, line_start - 1)
    hi = min(len(lines), max(line_end, line_start) + 1)
    window_text = _norm(" ".join(lines[lo:hi]))
    if norm_q in window_text:
        return window_text

    # Fall back to scanning 1..3 line enclosing windows in the full document
    for w in (1, 2, 3):
        for i in range(max(0, len(lines) - w + 1)):
            cand = _norm(" ".join(lines[i : i + w]))
            if norm_q in cand:
                return cand
    return None


def verify_coverage_citation(
    citation: CoverageCitation,
    docs: dict[str, str],
) -> tuple[bool, str]:
    """Verify a CoverageCitation against the full enclosing clause of its source document."""
    doc_text = docs.get(citation.doc_id)
    if doc_text is None:
        return False, f"unreviewable: unknown doc_id '{citation.doc_id}'"

    if not citation.verbatim_quote.strip():
        return False, f"unreviewable: empty verbatim_quote on '{citation.doc_id}'"

    clause = _resolve_full_clause(
        doc_text, citation.verbatim_quote, citation.line_start, citation.line_end
    )
    if clause is None:
        return (
            False,
            f"unreviewable: verbatim_quote not found in '{citation.doc_id}'",
        )

    # Polarity check against FULL enclosing clause
    clause_neg = bool(_NEG_RE.search(clause))
    claim_norm = _norm(citation.claim_supported)
    claim_neg = bool(_NEG_RE.search(claim_norm))
    if citation.polarity == "negated" and not clause_neg:
        return (
            False,
            f"Polarity mismatch on '{citation.doc_id}': claimed negated on affirmed clause",
        )
    _negated_clause_markers = (
        "not executed",
        "unsigned",
        "does not bind",
        "not apply",
        "revokes",
        "expressly excluded",
    )
    if (
        citation.polarity == "affirmed"
        and clause_neg
        and not claim_neg
        and any(k in clause for k in _negated_clause_markers)
    ):
        return (
            False,
            f"Polarity mismatch on '{citation.doc_id}': affirmed claim on negated/unexecuted "
            "source clause",
        )

    # Numeric value grounding check
    clause_nums = set(_NUM_RE.findall(clause))
    if citation.value:
        val_nums = set(_NUM_RE.findall(_norm(citation.value)))
        if val_nums and not val_nums.issubset(clause_nums):
            return (
                False,
                f"Value mismatch on '{citation.doc_id}': {citation.value!r} not in clause",
            )

    # Subject binding check if subject provided
    if citation.subject and citation.subject.strip():
        norm_doc = _norm(doc_text)
        norm_subj = _norm(citation.subject).rstrip(".")
        tokens = [
            w
            for w in re.findall(r"[a-z0-9]+", norm_subj)
            if len(w) >= 3 and w not in {"inc", "llc", "corp", "co", "ltd", "group"}
        ]
        if tokens and not all(t in norm_doc for t in tokens):
            return (
                False,
                f"Entity/subject mismatch on '{citation.doc_id}': subject '{citation.subject}' "
                "not present in cited document",
            )

    return True, "ok"


def score_coverage_audit_brief(
    key_case: dict[str, Any],
    brief: CoverageAuditBrief,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> dict[str, Any]:
    """Deterministically grade a CoverageAuditBrief against the separate held-out/dev key."""
    packet_id = str(key_case["packet_id"])
    docs = load_packet_documents(packet_id, archive_root)

    exp_det = str(key_case["expected_determination"])
    act_det = brief.determination
    determination_correct = act_det == exp_det
    false_definitive_on_insufficient = (
        exp_det == "insufficient_evidence" and act_det != "insufficient_evidence"
    )

    exp_val = _normalize_controlling_value(str(key_case["expected_value"]))
    act_val = _normalize_controlling_value(brief.controlling_value)
    value_correct = act_val == exp_val

    decoy_docs = set(key_case.get("decoy_docs") or [])
    req_ctrl = set(key_case.get("required_controlling_docs") or [])
    cited_ctrl = set(brief.controlling_doc_ids)

    decoy_in_controlling = sorted(decoy_docs & cited_ctrl)
    decoy_in_chain = sorted(
        d for d in decoy_docs if d in set(brief.supersession_chain) and "draft" in d
    )
    no_decoy_used = len(decoy_in_controlling) == 0 and len(decoy_in_chain) == 0

    citation_failures: list[str] = []
    verified_doc_ids: set[str] = set()
    for cit in brief.evidence_citations:
        ok, reason = verify_coverage_citation(cit, docs)
        if ok:
            verified_doc_ids.add(cit.doc_id)
        else:
            citation_failures.append(reason)

    missing_req_ctrl = sorted(
        d for d in req_ctrl if d not in cited_ctrl or d not in verified_doc_ids
    )
    controlling_docs_ok = len(missing_req_ctrl) == 0
    citations_grounded = bool(brief.evidence_citations) and len(citation_failures) == 0

    # For insufficient_evidence packets, confirm the missing artifact is named
    missing_kw = [str(k).lower() for k in (key_case.get("expected_missing_keywords") or [])]
    missing_blob = _norm(" ".join(brief.missing_documents) + " " + brief.rationale)
    missing_docs_ok = (not missing_kw) or any(kw in missing_blob for kw in missing_kw)

    passed = (
        determination_correct
        and value_correct
        and controlling_docs_ok
        and no_decoy_used
        and citations_grounded
        and missing_docs_ok
    )

    return {
        "passed": passed,
        "determination_correct": determination_correct,
        "expected_determination": exp_det,
        "actual_determination": act_det,
        "false_definitive_on_insufficient": false_definitive_on_insufficient,
        "value_correct": value_correct,
        "expected_value": exp_val,
        "actual_value": act_val,
        "controlling_docs_ok": controlling_docs_ok,
        "missing_required_controlling_docs": missing_req_ctrl,
        "no_decoy_used": no_decoy_used,
        "decoy_in_controlling": decoy_in_controlling,
        "citations_grounded": citations_grounded,
        "citation_failures": citation_failures,
        "missing_docs_ok": missing_docs_ok,
    }


def evaluate_routing_bar(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the pre-registered routing bar on held-out branching packets."""
    branching_ids = sorted({r["packet_id"] for r in runs if r["tier"] == "branching"})

    def _variant_stats(variant: str) -> dict[str, Any]:
        v_all = [r for r in runs if r["variant"] == variant]
        v_branch = [r for r in v_all if r["tier"] == "branching"]
        # Case-level pass: majority of trials pass (>= 2 of 3)
        cases_passed = 0
        for pid in branching_ids:
            p_runs = [r for r in v_branch if r["packet_id"] == pid]
            if p_runs and sum(1 for r in p_runs if r["passed"]) > len(p_runs) / 2:
                cases_passed += 1
        verified_attempts = sum(1 for r in v_branch if r["passed"])
        total_branch_tokens = sum(r["total_tokens"] for r in v_branch)
        false_def = sum(1 for r in v_all if r.get("false_definitive_on_insufficient"))
        tok_per_verified = (
            round(total_branch_tokens / verified_attempts) if verified_attempts else None
        )
        return {
            "branching_cases_passed": cases_passed,
            "branching_cases_total": len(branching_ids),
            "branching_attempts_passed": verified_attempts,
            "branching_attempts_total": len(v_branch),
            "false_definitive_on_insufficient": false_def,
            "total_branching_tokens": total_branch_tokens,
            "tokens_per_verified_branching": tok_per_verified,
        }

    base = _variant_stats("baseline")
    deep = _variant_stats("deep_agents")

    delta_cases = deep["branching_cases_passed"] - base["branching_cases_passed"]
    zero_false_def = deep["false_definitive_on_insufficient"] == 0
    if (
        deep["tokens_per_verified_branching"] is not None
        and base["tokens_per_verified_branching"] is not None
        and base["tokens_per_verified_branching"] > 0
    ):
        tok_ratio = round(
            deep["tokens_per_verified_branching"] / base["tokens_per_verified_branching"], 2
        )
    else:
        tok_ratio = None
    within_2x_tokens = tok_ratio is not None and tok_ratio <= 2.0

    bar_met = (delta_cases >= 2) and zero_false_def and within_2x_tokens
    return {
        "baseline": base,
        "deep_agents": deep,
        "delta_branching_cases": delta_cases,
        "deep_agents_zero_false_definitive": zero_false_def,
        "token_cost_ratio_vs_baseline": tok_ratio,
        "within_2x_token_bar": within_2x_tokens,
        "routing_bar_met": bar_met,
    }


def build_coverage_schedule(
    cases: list[dict[str, Any]],
    *,
    trials: int = 3,
    variants: tuple[Literal["baseline", "deep_agents"], ...] = ("baseline", "deep_agents"),
) -> list[tuple[Literal["baseline", "deep_agents"], dict[str, Any], int]]:
    """Alternate arm order within each packet & trial."""
    sched: list[tuple[Literal["baseline", "deep_agents"], dict[str, Any], int]] = []
    for case in cases:
        for trial in range(1, trials + 1):
            order = variants if (trial % 2 == 1) else tuple(reversed(variants))
            for variant in order:
                sched.append((variant, case, trial))
    return sched


async def run_coverage_audit_suite(
    *,
    key_path: Path,
    model_alias: str,
    trials: int = 3,
    variants: tuple[Literal["baseline", "deep_agents"], ...] = ("baseline", "deep_agents"),
    settings: Settings | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    """Run paired coverage audit attempts sequentially (concurrency=1) with alternating arms."""
    cfg = settings or get_settings()
    cases: list[dict[str, Any]] = json.loads(key_path.read_text(encoding="utf-8"))
    schedule = build_coverage_schedule(cases, trials=trials, variants=variants)

    runs: list[dict[str, Any]] = []
    for idx, (variant, case, trial) in enumerate(schedule, 1):
        cb = UsageTrackingCallback()
        deps = CoverageAuditDeps(
            settings=cfg,
            planner_variant=variant,
            primary_model_alias=model_alias,
            archive_root=archive_root,
            callbacks=[cb],
        )
        ctx = RunContext(
            tenant_id="tenant-alpha",
            requester_id="auditor",
            requester_roles=frozenset({"underwriter"}),
            thread_id=f"cov-{variant}-{case['packet_id']}-t{trial}",
            run_id=f"run-cov-{variant}-{case['packet_id']}-t{trial}",
            request_id=f"req-cov-{variant}-{case['packet_id']}-t{trial}",
        )
        t0 = time.monotonic()
        brief_dump: dict[str, Any] | None = None
        doc_reads: list[dict[str, Any]] = []
        error: str | None = None
        try:
            out = await run_coverage_audit_attempt(
                packet_id=case["packet_id"],
                prompt=case["prompt"],
                deps=deps,
                context=ctx,
            )
            doc_reads = list(out.get("doc_reads") or [])
            brief_obj = CoverageAuditBrief.model_validate(out["brief"])
            brief_dump = brief_obj.model_dump(mode="json")
            scores = score_coverage_audit_brief(case, brief_obj, archive_root=archive_root)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            scores = {
                "passed": False,
                "determination_correct": False,
                "expected_determination": case["expected_determination"],
                "actual_determination": None,
                "false_definitive_on_insufficient": False,
                "value_correct": False,
                "expected_value": _normalize_controlling_value(str(case["expected_value"])),
                "actual_value": None,
                "controlling_docs_ok": False,
                "missing_required_controlling_docs": list(
                    case.get("required_controlling_docs") or []
                ),
                "no_decoy_used": True,
                "decoy_in_controlling": [],
                "citations_grounded": False,
                "citation_failures": [],
                "missing_docs_ok": False,
            }

        dt = round(time.monotonic() - t0, 2)
        cost = cb.stats.estimate_cost_usd(model_alias)
        row = {
            "packet_id": case["packet_id"],
            "tier": case["tier"],
            "variant": variant,
            "trial": trial,
            "model_alias": model_alias,
            "duration_s": dt,
            "llm_calls": cb.stats.llm_calls,
            "tool_calls": cb.stats.tool_calls,
            "tools_called": list(cb.stats.tools_called),
            "model_ids_requested": cb.stats.unique_requested_models,
            "model_ids_responded": cb.stats.unique_response_models,
            "prompt_tokens": cb.stats.prompt_tokens,
            "completion_tokens": cb.stats.completion_tokens,
            "total_tokens": cb.stats.total_tokens,
            "cost_usd": cost,
            "error": error,
            "doc_reads": doc_reads,
            "brief": brief_dump,
            **scores,
        }
        runs.append(row)
        tag = "PASS" if scores["passed"] else "FAIL"
        print(
            f"[{tag} {idx:02d}/{len(schedule)}] {variant:11s} | {case['packet_id']} "
            f"({case['tier']:9s}) t{trial}: det={scores['actual_determination']} "
            f"val={scores['actual_value']} calls={cb.stats.llm_calls} "
            f"tools={cb.stats.tool_calls} tok={cb.stats.total_tokens} ({dt}s)"
            + (f" ERR={error[:70]}" if error else ""),
            flush=True,
        )
        if checkpoint_path is not None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "model_alias": model_alias,
                        "trials": trials,
                        "routing_bar": evaluate_routing_bar(runs),
                        "runs": runs,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    return {
        "model_alias": model_alias,
        "trials": trials,
        "routing_bar": evaluate_routing_bar(runs),
        "runs": runs,
    }
