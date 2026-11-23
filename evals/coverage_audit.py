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
    r"\b(?:does\s+not|do\s+not|doesn't|not\s+apply|not\s+bound|not\s+executed|not\s+valid|"
    r"excluded|revokes|deletes|unsigned|no\s+coverage|unscheduled|expired|pending|superseded)\b",
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
                read_log=doc_reads,
            )
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


# ---------------------------------------------------------------------------
# Offline Coverage Grader v2 (strict line-span entailment, ordered operative
# chain vs. evidence-only docs, exclusion-aware polarity, rule-out allowance)
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_POLICY_NO_RE = re.compile(r"policy(?:\s+number)?\s*:\s*([a-z0-9\-]+)", re.I)
_V2_EXCLUSION_OR_GAP_RE = re.compile(
    r"\b(?:does\s+not\s+apply|does\s+not\s+bind|not\s+apply|not\s+bound|not\s+executed|"
    r"not\s+valid|excluded|exclusion|bars\s+coverage|no\s+coverage|revokes|revoked|deletes|"
    r"reinstated|unsigned|blank|pending|expired|superseded|unscheduled|draft\s+only|"
    r"parent\s+entity\s+only|subsidiaries.*excluded|declined|missing|absent|solely\s+to|"
    r"applies\s+only\s+to|does\s+not\s+apply\s+retroactively|informational\s+purposes\s+only|"
    r"confers\s+no\s+coverage|does\s+not\s+amend)\b",
    re.I,
)
_V2_OPERATIVE_SPAN_RE = re.compile(
    r"(?:\b\d{4,9}\b|\b\d{4}-\d{2}-\d{2}\b|supersedes|replaces|revokes|reinstated|"
    r"not\s+apply|excluded|solely|only\s+to|unsigned|not\s+executed|blank|pending|"
    r"expired|not\s+valid|erosion|erodes|paid\s+in\s+full|does\s+not\s+bind|does\s+not\s+amend|"
    r"confers\s+no\s+coverage|executed\s*&\s*countersigned|deductible|sublimit|limit|schedule|"
    r"symbol\s+7|unit\s*#?14|annex\s+b|plant\s+2|tier-1|parent|subsidiary|loss\s+date|"
    r"date\s+of\s+loss|date\s+of\s+incident)",
    re.I,
)
_V2_ENTITY_SUFFIX_RE = re.compile(
    r"\b(?:inc|llc|corp|corporation|co|ltd|llp|lp|group|holdings|biolabs|storage|"
    r"timberworks|distribution|systems|blending|stamping|freightways|millworks|"
    r"pumping|builders|fabrication|living|services)\b",
    re.I,
)
_V2_EVIDENCE_ONLY_DOCS: dict[str, set[str]] = {
    "B07": {"unsigned_endorsement_mx_502.txt"},
    "B08": {"loss_run_2026.txt"},
}
_V2_GLUE_WORDS = frozenset(
    {
        "about",
        "account",
        "active",
        "adds",
        "after",
        "against",
        "aggregate",
        "also",
        "amended",
        "amends",
        "amount",
        "annual",
        "applicable",
        "applies",
        "apply",
        "archive",
        "arising",
        "attached",
        "awaiting",
        "back",
        "bars",
        "base",
        "been",
        "before",
        "between",
        "binder",
        "binds",
        "blank",
        "both",
        "bound",
        "breakdown",
        "brochure",
        "broker",
        "buyback",
        "buys",
        "cannot",
        "carrier",
        "caused",
        "chain",
        "claim",
        "claimant",
        "claims",
        "clause",
        "confirm",
        "confirmation",
        "confirmed",
        "confirms",
        "conveyance",
        "copy",
        "countersignature",
        "countersigned",
        "coverage",
        "covered",
        "covers",
        "current",
        "cyber",
        "damage",
        "date",
        "dates",
        "declarations",
        "declined",
        "deductible",
        "deletes",
        "depends",
        "described",
        "does",
        "draft",
        "effective",
        "email",
        "endorsement",
        "endorsements",
        "entirety",
        "entity",
        "enumerated",
        "equipment",
        "eroded",
        "erodes",
        "erosion",
        "establish",
        "establishes",
        "evidence",
        "exclude",
        "excluded",
        "excludes",
        "exclusion",
        "exclusions",
        "executed",
        "execution",
        "expired",
        "expressly",
        "extension",
        "file",
        "final",
        "flood",
        "form",
        "from",
        "full",
        "grants",
        "have",
        "incident",
        "includes",
        "including",
        "incorporated",
        "increase",
        "increased",
        "increases",
        "indemnity",
        "informational",
        "insufficient",
        "insurance",
        "insured",
        "interim",
        "into",
        "issued",
        "limit",
        "limitation",
        "limits",
        "list",
        "listed",
        "location",
        "locations",
        "loss",
        "losses",
        "memo",
        "missing",
        "modification",
        "modifies",
        "mold",
        "named",
        "neither",
        "none",
        "note",
        "notes",
        "notice",
        "number",
        "occurred",
        "occurrence",
        "occurring",
        "only",
        "operations",
        "other",
        "paid",
        "parent",
        "part",
        "pending",
        "peril",
        "period",
        "plant",
        "policy",
        "pollutant",
        "pollutants",
        "pollution",
        "premium",
        "prior",
        "property",
        "proposal",
        "proposed",
        "prospective",
        "provide",
        "provided",
        "provides",
        "quote",
        "read",
        "receipt",
        "reference",
        "reinstated",
        "reinstates",
        "release",
        "releases",
        "remain",
        "remaining",
        "remains",
        "remediation",
        "removes",
        "removing",
        "renewal",
        "replaces",
        "replacing",
        "report",
        "requested",
        "require",
        "required",
        "requirement",
        "requires",
        "response",
        "retroactively",
        "revised",
        "revocation",
        "revoked",
        "revokes",
        "rule",
        "ruled",
        "same",
        "says",
        "schedule",
        "scheduled",
        "scope",
        "section",
        "separate",
        "separately",
        "sets",
        "shows",
        "signature",
        "signed",
        "solely",
        "specifically",
        "spoilage",
        "standard",
        "stated",
        "states",
        "status",
        "storm",
        "sublimit",
        "subsidiaries",
        "subsidiary",
        "sudden",
        "summary",
        "superseded",
        "supersedes",
        "superseding",
        "supersession",
        "symbol",
        "term",
        "terms",
        "that",
        "their",
        "there",
        "these",
        "they",
        "this",
        "through",
        "tier",
        "time",
        "total",
        "transit",
        "truck",
        "under",
        "underwriting",
        "unit",
        "unless",
        "unscheduled",
        "unsigned",
        "valid",
        "value",
        "vehicle",
        "vehicles",
        "water",
        "were",
        "which",
        "while",
        "with",
        "without",
        "workmanship",
        "worksheet",
        "written",
        "year",
    }
)


def _ordered_subsequence(expected: list[str], observed: list[str]) -> bool:
    rem = iter(observed)
    return all(item in rem for item in expected)


def _policy_numbers_in_doc(doc_text: str) -> set[str]:
    return {m.group(1).lower() for m in _POLICY_NO_RE.finditer(doc_text)}


def verify_coverage_citation_v2(
    citation: CoverageCitation,
    docs: dict[str, str],
    *,
    expected_subject: str | None = None,
) -> tuple[bool, str]:
    """Strict v2 citation verifier: exact line span, entailment, polarity, and scope."""
    doc_text = docs.get(citation.doc_id)
    if doc_text is None:
        return False, f"unreviewable: unknown doc_id '{citation.doc_id}'"

    if not citation.verbatim_quote.strip():
        return False, f"unreviewable: empty verbatim_quote on '{citation.doc_id}'"

    lines = doc_text.splitlines()
    if not (1 <= citation.line_start <= citation.line_end <= len(lines)):
        return (
            False,
            f"unreviewable: invalid line range {citation.line_start}-{citation.line_end} "
            f"for '{citation.doc_id}' (total {len(lines)} lines)",
        )

    span_raw = " ".join(lines[citation.line_start - 1 : citation.line_end])
    span_norm = _norm(span_raw)
    quote_norm = _norm(citation.verbatim_quote)
    if quote_norm not in span_norm:
        return (
            False,
            f"unreviewable: verbatim_quote not found in cited lines "
            f"{citation.line_start}-{citation.line_end} of '{citation.doc_id}'",
        )

    # Reject heading-only citations (e.g. line 1 title only) for substantive claims
    if not _V2_OPERATIVE_SPAN_RE.search(span_norm):
        return (
            False,
            f"Heading-only/non-operative span (lines {citation.line_start}-{citation.line_end}) "
            f"on '{citation.doc_id}' cannot support substantive coverage claim",
        )

    claim_norm = _norm(citation.claim_supported)
    if not claim_norm:
        return False, f"unreviewable: empty claim_supported on '{citation.doc_id}'"

    # Verify ALL dates and numeric amounts asserted in claim_supported or value exist in cited span
    span_nums = set(_NUM_RE.findall(span_norm))
    span_dates = set(_DATE_RE.findall(span_norm))
    claim_nums = set(_NUM_RE.findall(claim_norm))
    claim_dates = set(_DATE_RE.findall(claim_norm))
    # Ignore 4-digit year numbers that are part of an already-present date or policy number
    span_all_digits = span_nums | {d[:4] for d in span_dates} | set(
        re.findall(r"\b\d{4}\b", span_norm)
    )
    sorted_span_dates = sorted(span_dates)
    missing_dates: list[str] = []
    for d in sorted(claim_dates):
        if d in span_dates:
            continue
        # Date-in-range entailment: e.g. '2026-01-01 to 2027-01-01' entails '2026-04-12'
        if len(sorted_span_dates) >= 2 and sorted_span_dates[0] <= d <= sorted_span_dates[-1]:
            continue
        # Prior effective-date entailment: e.g. 'Effective Date: 2026-01-01' + 'prior to 2026-04-12'
        if (
            len(sorted_span_dates) == 1
            and sorted_span_dates[0] <= d
            and re.search(
                r"\b(?:prior\s+to|before|on\s+or\s+after|active\s+on|in\s+effect)\b",
                claim_norm,
            )
        ):
            continue
        missing_dates.append(d)
    if missing_dates:
        return (
            False,
            f"Unsupported date(s) {missing_dates} in claim_supported not present in "
            f"lines {citation.line_start}-{citation.line_end} of '{citation.doc_id}'",
        )
    # Exclude dates already entailed above from the raw 4-digit numeric check
    entailed_years = {d[:4] for d in claim_dates if d not in missing_dates}
    span_all_digits = span_all_digits | entailed_years
    missing_nums = [n for n in sorted(claim_nums) if n not in span_all_digits]
    if missing_nums:
        return (
            False,
            f"Unsupported numeric value(s) {missing_nums} in claim_supported not present in "
            f"lines {citation.line_start}-{citation.line_end} of '{citation.doc_id}'",
        )
    if citation.value:
        val_norm = _norm(citation.value)
        val_nums = [n for n in _NUM_RE.findall(val_norm) if n not in span_all_digits]
        val_dates = [d for d in _DATE_RE.findall(val_norm) if d not in span_dates]
        if val_nums or val_dates:
            return (
                False,
                f"Value mismatch on '{citation.doc_id}': {citation.value!r} not in cited span",
            )

    # Polarity & exclusion/gap entailment check against the exact cited span
    span_is_excl_or_gap = bool(_V2_EXCLUSION_OR_GAP_RE.search(span_norm))
    claim_is_excl_or_gap = bool(_V2_EXCLUSION_OR_GAP_RE.search(claim_norm))
    _v2_nonbinding_markers = (
        "not executed",
        "unsigned",
        "does not bind",
        "not apply",
        "revokes",
        "expressly excluded",
        "not valid",
        "confers no coverage",
        "does not amend",
    )
    if (
        span_is_excl_or_gap
        and not claim_is_excl_or_gap
        and any(k in span_norm for k in _v2_nonbinding_markers)
    ):
        return (
            False,
            f"Polarity contradiction on '{citation.doc_id}': affirmative claim on "
            "excluded/unsigned/revoked/non-binding source clause",
        )
    if citation.polarity == "negated" and not span_is_excl_or_gap and not _NEG_RE.search(span_norm):
        return (
            False,
            f"Polarity mismatch on '{citation.doc_id}': negated polarity on affirmed source span",
        )

    # Subject / entity binding (allow policy-number linkage to declarations; reject cross-entity!)
    doc_norm = _norm(doc_text)
    decl_text = docs.get("policy_declarations.txt", "")
    same_policy = bool(
        _policy_numbers_in_doc(doc_text) & _policy_numbers_in_doc(decl_text)
    )
    allowed_subject_text = (
        f"{doc_norm} {_norm(decl_text)}"
        if same_policy and "subsidiaries and affiliates expressly excluded" not in doc_norm
        else doc_norm
    )
    if citation.subject and citation.subject.strip():
        subj_norm = _norm(citation.subject).rstrip(".")
        if _V2_ENTITY_SUFFIX_RE.search(subj_norm):
            subj_tokens = [
                w
                for w in re.findall(r"[a-z0-9]+", subj_norm)
                if len(w) >= 3 and w not in {"inc", "llc", "corp", "co", "ltd", "group"}
            ]
            if subj_tokens and not all(t in allowed_subject_text for t in subj_tokens):
                return (
                    False,
                    f"Entity/subject mismatch on '{citation.doc_id}': subject "
                    f"'{citation.subject}' not supported by document scope",
                )

    # Extra-claim / unrelated-assertion check (catches e.g. 'earthquake-resistant certification')
    span_words = set(re.findall(r"[a-z0-9]+", span_norm))
    doc_entity_words = set(re.findall(r"[a-z0-9]+", _norm(expected_subject or "")))
    extra_words = [
        w
        for w in re.findall(r"[a-z0-9]+", claim_norm)
        if len(w) >= 5
        and not w.isdigit()
        and w not in span_words
        and w not in doc_entity_words
        and w not in _V2_GLUE_WORDS
    ]
    if len(extra_words) >= 2:
        return (
            False,
            f"Unsupported/unrelated claim terms {sorted(set(extra_words))} on '{citation.doc_id}' "
            f"(not present in lines {citation.line_start}-{citation.line_end})",
        )

    return True, "ok"


def score_coverage_audit_brief_v2(
    key_case: dict[str, Any],
    brief: CoverageAuditBrief,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> dict[str, Any]:
    """Offline v2 coverage grader: line-span entailment, ordered operative chain, & anchors."""
    packet_id = str(key_case["packet_id"])
    docs = load_packet_documents(packet_id, archive_root)
    expected_subject = str(key_case.get("expected_subject") or "")

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
    evidence_only = _V2_EVIDENCE_ONLY_DOCS.get(packet_id, set())
    expected_chain = list(key_case.get("expected_chain") or [])
    operative_chain = [d for d in expected_chain if d not in evidence_only]

    cited_ctrl = set(brief.controlling_doc_ids)
    decoy_in_controlling = sorted(decoy_docs & cited_ctrl)
    decoy_in_chain = sorted(
        d for d in decoy_docs if d in set(brief.supersession_chain) and "draft" in d
    )
    evidence_only_in_chain = sorted(evidence_only & set(brief.supersession_chain))
    no_decoy_used = len(decoy_in_controlling) == 0 and len(decoy_in_chain) == 0

    ordered_chain_ok = _ordered_subsequence(operative_chain, list(brief.supersession_chain)) and (
        len(evidence_only_in_chain) == 0
    )

    # Verify every citation against its exact cited line span
    citation_failures: list[str] = []
    verified_doc_spans: dict[str, list[str]] = {}
    for cit in brief.evidence_citations:
        ok, reason = verify_coverage_citation_v2(cit, docs, expected_subject=expected_subject)
        if ok:
            lines = docs[cit.doc_id].splitlines()
            span_txt = _norm(" ".join(lines[cit.line_start - 1 : cit.line_end]))
            verified_doc_spans.setdefault(cit.doc_id, []).append(span_txt)
        else:
            citation_failures.append(reason)

    # Every required controlling doc must be in controlling_doc_ids AND have a verified operative
    # span that grounds the controlling value/exclusion/gap (not just a title or unrelated line).
    missing_req_ctrl: list[str] = []
    for req_doc in sorted(req_ctrl):
        spans = verified_doc_spans.get(req_doc) or []
        if req_doc not in cited_ctrl or not spans:
            missing_req_ctrl.append(req_doc)
            continue
        joined = " ".join(spans)
        if exp_det in {"covered", "covered_with_deductible"} and exp_val.isdigit():
            # For B08 arithmetic ($1.2M - $450k = $750k), each doc grounds its own operand
            if packet_id == "B08":
                needed = "1200000" if req_doc == "endorsement_ag_209.txt" else "450000"
                if needed not in joined:
                    missing_req_ctrl.append(f"{req_doc}:missing_{needed}")
            elif exp_val not in joined:
                missing_req_ctrl.append(f"{req_doc}:missing_{exp_val}")
        elif exp_det == "excluded":
            if not _V2_EXCLUSION_OR_GAP_RE.search(joined):
                missing_req_ctrl.append(f"{req_doc}:missing_exclusion_clause")
        elif exp_det == "insufficient_evidence" and not re.search(
            r"(?:solely|only\s+to|unsigned|blank|not\s+executed|pending|schedule)",
            joined,
        ):
            missing_req_ctrl.append(f"{req_doc}:missing_gap_clause")

    controlling_docs_ok = len(missing_req_ctrl) == 0
    citations_grounded = bool(brief.evidence_citations) and len(citation_failures) == 0

    missing_kw = [str(k).lower() for k in (key_case.get("expected_missing_keywords") or [])]
    missing_blob = _norm(" ".join(brief.missing_documents) + " " + brief.rationale)
    missing_docs_ok = (not missing_kw) or any(kw in missing_blob for kw in missing_kw)

    passed = (
        determination_correct
        and value_correct
        and controlling_docs_ok
        and ordered_chain_ok
        and no_decoy_used
        and citations_grounded
        and missing_docs_ok
    )

    return {
        "scorer_version": "v2",
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
        "ordered_chain_ok": ordered_chain_ok,
        "operative_chain_expected": operative_chain,
        "evidence_only_in_chain": evidence_only_in_chain,
        "no_decoy_used": no_decoy_used,
        "decoy_in_controlling": decoy_in_controlling,
        "citations_grounded": citations_grounded,
        "citation_failures": citation_failures,
        "missing_docs_ok": missing_docs_ok,
    }
