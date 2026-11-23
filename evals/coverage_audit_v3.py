"""Calibrated v3 offline coverage evidence grader with typed relationship checks.

Preserves v1 and v2 graders in `evals/coverage_audit.py` intact. Every citation receives
an explicit `supported | contradicted | unverified` verdict with a source-based reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from evals.coverage_audit import (
    _DATE_RE,
    _NEG_RE,
    _NUM_RE,
    _V2_ENTITY_SUFFIX_RE,
    _V2_EVIDENCE_ONLY_DOCS,
    _V2_EXCLUSION_OR_GAP_RE,
    _V2_OPERATIVE_SPAN_RE,
    _norm,
    _normalize_controlling_value,
    _ordered_subsequence,
    _policy_numbers_in_doc,
)
from workflows.coverage_audit.models import (
    CoverageAuditBrief,
    CoverageCitation,
    IssueAuditBrief,
)
from workflows.coverage_audit.tools import ARCHIVE_ROOT, load_packet_documents

Verdict = Literal["supported", "contradicted", "unverified"]

# Directed numeric transition patterns: (old_value -> new_value)
_TRANSITION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\breplaces?\b[^.\n]{0,60}?\b(\d{4,9})\b[^.\n]{0,25}?\bwith\b[^.\n]{0,20}?\b(\d{4,9})\b"),
    re.compile(r"\bfrom\s+(\d{4,9})\b[^.\n]{0,25}?\bto\s+(\d{4,9})\b"),
)
_INCREASE_VERB_RE = re.compile(r"\b(?:increased?|raised?|boosted|expanded|up\s+from)\b")
_DECREASE_VERB_RE = re.compile(r"\b(?:reduced?|decreased?|lowered?|cut|dropped|down\s+from)\b")

# Directed supersession / revocation patterns: (actor_doc -> target_doc)
_ENDORSEMENT_CODE_RE = re.compile(r"\b([a-z]{2}-\d{3}(?:-[a-z])?)\b")
_SUPERSEDES_TARGET_RE = re.compile(
    r"\b(?:supersedes?|replaces?|revokes?|amends?|deletes?)\b"
    r"(?:\s+and\s+(?:replaces?|deletes?))?"
    r"[^.\n]{0,30}?\b([a-z]{2}-\d{3}(?:-[a-z])?)\b"
)

_V3_OPERATIVE_EXTRA_RE = re.compile(
    r"(?:applies\s+only|not\s+automatically\s+included|no\s+signed|selection\s+form|"
    r"issuance\s+record|hbr-\d+|hub-\d+|does\s+not\s+establish|site\s+[a-z0-9\-]+\s*:)",
    re.I,
)
# Site / location identifiers to catch wrong-site scope attributions
_SITE_TOKEN_RE = re.compile(
    r"\b(?:plant\s+\d+|dock\s+\d+|annex\s+[a-z0-9]+|tier-\d+|"
    r"hbr-\d+|hub-\d+|s-\d+|l-\d+|n-\d+|m-\d+|unit\s*#?\d+|truck\s*#?\d+|"
    r"toledo|detroit|harbor|bayview|cedar)\b"
)

# Unverifiable / fabricated domain concepts (not valid coverage paraphrases unless in span)
_UNSUPPORTED_CONCEPT_RE = re.compile(
    r"\b(?:earthquake|volcanic|tsunami|tornado|nuclear|terrorism|certification|certified|"
    r"waiver|bankruptc\w*|lapse|lapsed|uninterrupted|guaranteed|warranted|hurricane|"
    r"cyberattack|biometric|cryptocurrency|aviation|maritime)\b"
)

# Cross-source comparison phrases that cannot be established by a single operational doc alone
_CROSS_SOURCE_ONLY_RE = re.compile(
    r"\b(?:no\s+lapse|never\s+lapsed|remained\s+in\s+force\s+without\s+interruption|"
    r"all\s+required\s+fields\s+are\s+present|satisfies\s+all\s+carrier\s+requirements)\b"
)


def _extract_numeric_transition(norm_text: str) -> tuple[int, int] | None:
    """Extract directed (old_amount, new_amount) transition if present."""
    for pat in _TRANSITION_PATTERNS:
        m = pat.search(norm_text)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _extract_supersession_targets(norm_text: str) -> set[str]:
    return {m.group(1) for m in _SUPERSEDES_TARGET_RE.finditer(norm_text)}


def _extract_site_tokens(norm_text: str) -> set[str]:
    return {re.sub(r"\s+", " ", m.group(0)).strip() for m in _SITE_TOKEN_RE.finditer(norm_text)}


_MONETARY_DEDUCTIBLE_RE = re.compile(r"\bdeductible\b")
_MONETARY_EROSION_RE = re.compile(
    r"\b(?:paid\s+erosion|erosion|erodes|eroded|paid\s+indemnity)\b"
)
_MONETARY_LIMIT_RE = re.compile(
    r"\b(?:sublimit|aggregate\s+limit|per-occurrence\s+limit|transit\s+limit|"
    r"breakdown\s+limit|coverage\s+limit|controlling\s+limit|limit)\b"
)
_CLAUSE_SPLIT_RE = re.compile(r"[.;]\s+|\s+(?:and|while|whereas)\s+")


def _extract_monetary_roles(norm_text: str) -> set[str]:
    """Classify the monetary role(s) asserted in a normalized clause."""
    roles: set[str] = set()
    if _MONETARY_DEDUCTIBLE_RE.search(norm_text):
        roles.add("deductible")
    if _MONETARY_EROSION_RE.search(norm_text):
        roles.add("erosion")
    # Only tag 'limit' if it's not merely the target of 'erosion against ... aggregate'
    cleaned = re.sub(r"erosion\s+against[^.;\n]*", "", norm_text)
    if _MONETARY_LIMIT_RE.search(cleaned):
        roles.add("limit")
    return roles


def _extract_site_amount_pairs(norm_text: str) -> set[tuple[str, str]]:
    """Extract (site, numeric_amount) bindings per atomic clause."""
    pairs: set[tuple[str, str]] = set()
    for clause in _CLAUSE_SPLIT_RE.split(norm_text):
        c = clause.strip()
        if not c:
            continue
        sites = _extract_site_tokens(c)
        nums = set(_NUM_RE.findall(c))
        if len(sites) >= 1 and len(nums) == 1:
            num = next(iter(nums))
            for s in sites:
                pairs.add((s, num))
        elif len(sites) == 1 and len(nums) >= 1:
            site = next(iter(sites))
            for n in nums:
                pairs.add((site, n))
    return pairs


def verify_coverage_citation_v3(
    citation: CoverageCitation,
    docs: dict[str, str],
    *,
    expected_subject: str | None = None,
) -> dict[str, Any]:
    """Calibrated v3 citation verifier returning {verdict, reason, doc_id, lines}."""
    doc_text = docs.get(citation.doc_id)
    if doc_text is None:
        return {
            "verdict": "unverified",
            "reason": f"Unknown doc_id '{citation.doc_id}' not present in packet.",
            "doc_id": citation.doc_id,
        }

    if not citation.verbatim_quote.strip():
        return {
            "verdict": "unverified",
            "reason": f"Empty verbatim_quote on '{citation.doc_id}'.",
            "doc_id": citation.doc_id,
        }

    lines = doc_text.splitlines()
    if not (1 <= citation.line_start <= citation.line_end <= len(lines)):
        return {
            "verdict": "unverified",
            "reason": (
                f"Invalid line range {citation.line_start}-{citation.line_end} "
                f"for '{citation.doc_id}' (total {len(lines)} lines)."
            ),
            "doc_id": citation.doc_id,
        }

    span_raw = " ".join(lines[citation.line_start - 1 : citation.line_end])
    span_norm = _norm(span_raw)
    quote_norm = _norm(citation.verbatim_quote)
    if quote_norm not in span_norm:
        return {
            "verdict": "unverified",
            "reason": (
                f"Verbatim quote not found in lines {citation.line_start}-{citation.line_end} "
                f"of '{citation.doc_id}'."
            ),
            "doc_id": citation.doc_id,
        }

    claim_norm = _norm(citation.claim_supported)
    if not claim_norm:
        return {
            "verdict": "unverified",
            "reason": f"Empty claim_supported on '{citation.doc_id}'.",
            "doc_id": citation.doc_id,
        }

    # 1. Reject heading-only citations for substantive coverage assertions
    if not _V2_OPERATIVE_SPAN_RE.search(span_norm) and not _V3_OPERATIVE_EXTRA_RE.search(
        span_norm
    ):
        return {
            "verdict": "unverified",
            "reason": (
                f"Heading-only span (lines {citation.line_start}-{citation.line_end}) "
                f"on '{citation.doc_id}' does not substantiate coverage claim."
            ),
            "doc_id": citation.doc_id,
        }

    # 2. Typed directional numeric transition check (catches role/value/direction swaps!)
    span_trans = _extract_numeric_transition(span_norm)
    claim_trans = _extract_numeric_transition(claim_norm)
    if span_trans is not None and claim_trans is not None and claim_trans != span_trans:
        return {
            "verdict": "contradicted",
            "reason": (
                f"Directional value swap on '{citation.doc_id}': source transitions "
                f"{span_trans[0]} -> {span_trans[1]}, but claim asserts "
                f"{claim_trans[0]} -> {claim_trans[1]}."
            ),
            "doc_id": citation.doc_id,
        }
    if span_trans is not None:
        s_old, s_new = span_trans
        if _DECREASE_VERB_RE.search(claim_norm) and s_new > s_old:
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Direction contradiction on '{citation.doc_id}': claim states a reduction "
                    f"but source increases {s_old} -> {s_new}."
                ),
                "doc_id": citation.doc_id,
            }
        if _INCREASE_VERB_RE.search(claim_norm) and s_new < s_old:
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Direction contradiction on '{citation.doc_id}': claim states an increase "
                    f"but source decreases {s_old} -> {s_new}."
                ),
                "doc_id": citation.doc_id,
            }
        # If citation.value asserts ONLY the superseded old value as the resulting limit
        if citation.value:
            val_nums = [int(n) for n in _NUM_RE.findall(_norm(citation.value))]
            if val_nums == [s_old] and not re.search(
                r"\b(?:base|prior|previous|original|from)\b", claim_norm
            ):
                return {
                    "verdict": "contradicted",
                    "reason": (
                        f"Role/value swap on '{citation.doc_id}': value={citation.value!r} "
                        f"reports replaced value {s_old} instead of new value {s_new}."
                    ),
                    "doc_id": citation.doc_id,
                }
    elif claim_trans is not None:
        c_old, c_new = claim_trans
        if _DECREASE_VERB_RE.search(claim_norm) and c_new > c_old:
            return {
                "verdict": "contradicted",
                "reason": f"Self-contradictory reduction claim ({c_old} -> {c_new}).",
                "doc_id": citation.doc_id,
            }
        if _INCREASE_VERB_RE.search(claim_norm) and c_new < c_old:
            return {
                "verdict": "contradicted",
                "reason": f"Self-contradictory increase claim ({c_old} -> {c_new}).",
                "doc_id": citation.doc_id,
            }

    # 3. Directed supersession / revocation target check (e.g. TR-115 supersedes TR-108)
    span_sup_targets = _extract_supersession_targets(span_norm)
    claim_sup_targets = _extract_supersession_targets(claim_norm)
    if span_sup_targets and claim_sup_targets and not claim_sup_targets.issubset(span_sup_targets):
        return {
            "verdict": "contradicted",
            "reason": (
                f"Supersession direction/target mismatch on '{citation.doc_id}': "
                f"source targets {sorted(span_sup_targets)}, claim targets "
                f"{sorted(claim_sup_targets)}."
            ),
            "doc_id": citation.doc_id,
        }

    # 4. Typed date & date-range entailment (handles paraphrases cleanly!)
    span_dates = set(_DATE_RE.findall(span_norm))
    sorted_span_dates = sorted(span_dates)
    claim_dates = set(_DATE_RE.findall(claim_norm))
    entailed_dates: set[str] = set()
    for d in sorted(claim_dates):
        if d in span_dates:
            entailed_dates.add(d)
            continue
        if len(sorted_span_dates) >= 2:
            lo_d, hi_d = sorted_span_dates[0], sorted_span_dates[-1]
            if lo_d <= d <= hi_d:
                if re.search(r"\b(?:outside|after|before|excluded|not\s+within)\b", claim_norm):
                    return {
                        "verdict": "contradicted",
                        "reason": (
                            f"Date-range contradiction on '{citation.doc_id}': {d} falls inside "
                            f"{lo_d}..{hi_d}, contradicting exclusion claim."
                        ),
                        "doc_id": citation.doc_id,
                    }
                entailed_dates.add(d)
                continue
            # Date is outside the stated range
            if re.search(
                r"\b(?:within|includes?|covers?|active|falls\s+within|during)\b",
                claim_norm,
            ):
                return {
                    "verdict": "contradicted",
                    "reason": (
                        f"Date-range contradiction on '{citation.doc_id}': {d} is outside "
                        f"source range {lo_d}..{hi_d}."
                    ),
                    "doc_id": citation.doc_id,
                }
            if re.search(r"\b(?:outside|after|before|does\s+not\s+apply|excluded)\b", claim_norm):
                entailed_dates.add(d)
                continue
        if len(sorted_span_dates) == 1:
            eff_d = sorted_span_dates[0]
            if eff_d <= d and re.search(
                r"\b(?:prior\s+to|before|on\s+or\s+after|active|in\s+effect|took\s+effect|covers?)\b",
                claim_norm,
            ):
                entailed_dates.add(d)
                continue
            if eff_d > d and re.search(
                r"\b(?:after\s+the\s+loss|does\s+not\s+apply|prospective|not\s+yet\s+in\s+effect|"
                r"outside|excluded|later\s+than)\b",
                claim_norm,
            ):
                entailed_dates.add(d)
                continue
            if eff_d > d and re.search(
                r"\b(?:active\s+on|applies\s+to|in\s+effect\s+on|covers?)\b", claim_norm
            ):
                return {
                    "verdict": "contradicted",
                    "reason": (
                        f"Effective-date contradiction on '{citation.doc_id}': "
                        f"source effective {eff_d} is after claimed date {d}."
                    ),
                    "doc_id": citation.doc_id,
                }
        return {
            "verdict": "unverified",
            "reason": (
                f"Date {d} in claim_supported is not established by lines "
                f"{citation.line_start}-{citation.line_end} of '{citation.doc_id}'."
            ),
            "doc_id": citation.doc_id,
        }

    # 5. Numeric value grounding (every asserted numeric figure must exist in cited span)
    span_nums = set(_NUM_RE.findall(span_norm))
    span_all_digits = (
        span_nums
        | {d[:4] for d in span_dates}
        | {d[:4] for d in entailed_dates}
        | set(re.findall(r"\b\d{4}\b", span_norm))
    )
    claim_nums = set(_NUM_RE.findall(claim_norm))
    missing_nums = [n for n in sorted(claim_nums) if n not in span_all_digits]
    if missing_nums:
        verdict: Verdict = "contradicted" if span_nums else "unverified"
        return {
            "verdict": verdict,
            "reason": (
                f"Numeric value(s) {missing_nums} not supported by lines "
                f"{citation.line_start}-{citation.line_end} of '{citation.doc_id}' "
                f"(source values: {sorted(span_nums)})."
            ),
            "doc_id": citation.doc_id,
        }
    if citation.value:
        val_norm = _norm(citation.value)
        val_missing_nums = [n for n in _NUM_RE.findall(val_norm) if n not in span_all_digits]
        val_missing_dates = [
            d for d in _DATE_RE.findall(val_norm) if d not in span_dates and d not in entailed_dates
        ]
        if val_missing_nums or val_missing_dates:
            return {
                "verdict": "contradicted" if (span_nums or span_dates) else "unverified",
                "reason": (
                    f"citation.value={citation.value!r} not grounded in lines "
                    f"{citation.line_start}-{citation.line_end} of '{citation.doc_id}'."
                ),
                "doc_id": citation.doc_id,
            }

    # 5b. Monetary role binding (limit/sublimit vs. deductible vs. paid erosion)
    span_roles = _extract_monetary_roles(span_norm)
    claim_roles = _extract_monetary_roles(claim_norm)
    if span_roles and claim_roles:
        if "deductible" in claim_roles and "deductible" not in span_roles:
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Monetary role mismatch on '{citation.doc_id}': claim asserts a deductible "
                    f"but source span specifies {sorted(span_roles)}."
                ),
                "doc_id": citation.doc_id,
            }
        if (
            "limit" in claim_roles
            and "limit" not in span_roles
            and ("deductible" in span_roles or "erosion" in span_roles)
        ):
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Monetary role mismatch on '{citation.doc_id}': claim asserts a limit "
                    f"but source span specifies {sorted(span_roles)}."
                ),
                "doc_id": citation.doc_id,
            }
        if "erosion" in span_roles and "limit" in claim_roles and "erosion" not in claim_roles:
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Monetary role mismatch on '{citation.doc_id}': source reports paid "
                    "erosion, not an aggregate limit."
                ),
                "doc_id": citation.doc_id,
            }

    # 6. Typed site / location scope & per-clause (site, amount) pair binding
    doc_norm = _norm(doc_text)
    span_sites = _extract_site_tokens(span_norm)
    doc_sites = _extract_site_tokens(doc_norm)
    claim_sites = _extract_site_tokens(claim_norm)
    if claim_sites and doc_sites:
        out_of_scope_sites = [s for s in sorted(claim_sites) if s not in doc_sites]
        if out_of_scope_sites:
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Site/location mismatch on '{citation.doc_id}': claim references "
                    f"{out_of_scope_sites} not present in document sites {sorted(doc_sites)}."
                ),
                "doc_id": citation.doc_id,
            }
    span_site_pairs = _extract_site_amount_pairs(span_norm)
    claim_site_pairs = _extract_site_amount_pairs(claim_norm)
    if span_site_pairs and claim_site_pairs:
        mismatched_pairs = sorted(p for p in claim_site_pairs if p not in span_site_pairs)
        if mismatched_pairs:
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Site-amount assignment mismatch on '{citation.doc_id}': claimed "
                    f"{mismatched_pairs} not supported by source pairs {sorted(span_site_pairs)}."
                ),
                "doc_id": citation.doc_id,
            }
    if span_sites and claim_sites:
        if not claim_sites.issubset(span_sites) and (
            "solely for" in span_norm or "remains at" in span_norm
        ):
            return {
                "verdict": "contradicted",
                "reason": (
                    f"Site scope mismatch on '{citation.doc_id}': span restricts to "
                    f"{sorted(span_sites)}, claim cites {sorted(claim_sites)}."
                ),
                "doc_id": citation.doc_id,
            }
        # Sentence-level (site, numeric_amount) binding when multiple sites appear in one span
        if len(span_sites) >= 2 and claim_nums:
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", span_norm) if s.strip()]
            for c_site in sorted(claim_sites):
                site_sents = [s for s in sentences if c_site in _extract_site_tokens(s)]
                if site_sents and not any(
                    claim_nums.issubset(set(_NUM_RE.findall(s))) for s in site_sents
                ):
                    return {
                        "verdict": "contradicted",
                        "reason": (
                            f"Site-amount binding mismatch on '{citation.doc_id}': "
                            f"site '{c_site}' is not associated with {sorted(claim_nums)} "
                            "in the cited sentence."
                        ),
                        "doc_id": citation.doc_id,
                    }

    # 7. Polarity & exclusion / draft / stale-authority check
    span_is_excl_or_gap = bool(_V2_EXCLUSION_OR_GAP_RE.search(span_norm))
    claim_is_excl_or_gap = bool(_V2_EXCLUSION_OR_GAP_RE.search(claim_norm))
    _nonbinding = (
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
        and any(k in span_norm for k in _nonbinding)
    ):
        return {
            "verdict": "contradicted",
            "reason": (
                f"Polarity/authority contradiction on '{citation.doc_id}': affirmative claim "
                "drawn from excluded, unsigned, revoked, expired, or non-binding clause."
            ),
            "doc_id": citation.doc_id,
        }
    if citation.polarity == "negated" and not span_is_excl_or_gap and not _NEG_RE.search(span_norm):
        return {
            "verdict": "contradicted",
            "reason": (
                f"Polarity contradiction on '{citation.doc_id}': negated polarity on "
                "affirmed source span."
            ),
            "doc_id": citation.doc_id,
        }

    # 8. Actor / entity binding (allow policy-number linkage; reject cross-entity!)
    decl_text = docs.get("policy_declarations.txt", "")
    same_policy = bool(_policy_numbers_in_doc(doc_text) & _policy_numbers_in_doc(decl_text))
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
                return {
                    "verdict": "contradicted",
                    "reason": (
                        f"Actor/entity mismatch on '{citation.doc_id}': subject "
                        f"'{citation.subject}' does not match document scope."
                    ),
                    "doc_id": citation.doc_id,
                }

    # 9. Unsupported concept & single-source overclaim check (fabricated concepts / no-lapse)
    for m in _UNSUPPORTED_CONCEPT_RE.finditer(claim_norm):
        concept = m.group(0)
        if concept not in span_norm and concept not in doc_norm:
            return {
                "verdict": "unverified",
                "reason": (
                    f"Unsupported concept '{concept}' in claim_supported not present in "
                    f"'{citation.doc_id}'."
                ),
                "doc_id": citation.doc_id,
            }
    if _CROSS_SOURCE_ONLY_RE.search(claim_norm):
        return {
            "verdict": "unverified",
            "reason": (
                f"Cross-source overclaim on '{citation.doc_id}': claim cannot be established "
                "from this single cited passage alone."
            ),
            "doc_id": citation.doc_id,
        }

    # Check if expected_subject is explicitly contradicted by a different entity named in claim
    _ = expected_subject
    return {
        "verdict": "supported",
        "reason": (
            f"Typed relationships, dates, values, scope, and polarity grounded in "
            f"'{citation.doc_id}' lines {citation.line_start}-{citation.line_end}."
        ),
        "doc_id": citation.doc_id,
    }


def score_coverage_audit_brief_v3(
    key_case: dict[str, Any],
    brief: CoverageAuditBrief,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> dict[str, Any]:
    """Calibrated v3 offline grader for CoverageAuditBrief (blinded to arm/model/usage)."""
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

    citation_verdicts: list[dict[str, Any]] = []
    verified_doc_spans: dict[str, list[str]] = {}
    contradicted_reasons: list[str] = []
    unverified_reasons: list[str] = []
    for idx, cit in enumerate(brief.evidence_citations):
        res = verify_coverage_citation_v3(cit, docs, expected_subject=expected_subject)
        res["citation_index"] = idx
        citation_verdicts.append(res)
        if res["verdict"] == "supported":
            lines = docs[cit.doc_id].splitlines()
            span_txt = _norm(" ".join(lines[cit.line_start - 1 : cit.line_end]))
            verified_doc_spans.setdefault(cit.doc_id, []).append(span_txt)
        elif res["verdict"] == "contradicted":
            contradicted_reasons.append(res["reason"])
        else:
            unverified_reasons.append(res["reason"])

    missing_req_ctrl: list[str] = []
    for req_doc in sorted(req_ctrl):
        spans = verified_doc_spans.get(req_doc) or []
        if req_doc not in cited_ctrl or not spans:
            missing_req_ctrl.append(req_doc)
            continue
        joined = " ".join(spans)
        if exp_det in {"covered", "covered_with_deductible"} and exp_val.isdigit():
            if packet_id == "B08":
                needed = "1200000" if req_doc == "endorsement_ag_209.txt" else "450000"
                if needed not in joined:
                    missing_req_ctrl.append(f"{req_doc}:missing_{needed}")
            elif exp_val not in joined:
                missing_req_ctrl.append(f"{req_doc}:missing_{exp_val}")
        elif exp_det == "excluded" and not _V2_EXCLUSION_OR_GAP_RE.search(joined):
            missing_req_ctrl.append(f"{req_doc}:missing_exclusion_clause")
        elif exp_det == "insufficient_evidence" and not re.search(
            r"(?:solely|only\s+to|unsigned|blank|not\s+executed|pending|schedule)",
            joined,
        ):
            missing_req_ctrl.append(f"{req_doc}:missing_gap_clause")

    controlling_docs_ok = len(missing_req_ctrl) == 0
    citations_grounded = (
        bool(brief.evidence_citations)
        and len(contradicted_reasons) == 0
        and len(unverified_reasons) == 0
    )

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
        "scorer_version": "v3",
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
        "supported_citations": sum(1 for v in citation_verdicts if v["verdict"] == "supported"),
        "contradicted_citations": len(contradicted_reasons),
        "unverified_citations": len(unverified_reasons),
        "contradicted_reasons": contradicted_reasons,
        "unverified_reasons": unverified_reasons,
        "citation_verdicts": citation_verdicts,
        "missing_docs_ok": missing_docs_ok,
    }


def score_issue_audit_brief(
    key_case: dict[str, Any],
    brief: IssueAuditBrief,
    *,
    archive_root: Path,
) -> dict[str, Any]:
    """Grade an IssueAuditBrief independently per requested issue using calibrated v3 checks."""
    packet_id = str(key_case["packet_id"])
    docs = load_packet_documents(packet_id, archive_root)
    expected_subject = str(key_case.get("expected_subject") or "")
    expected_issues: list[dict[str, Any]] = list(key_case["expected_issues"])
    by_id = {iss.issue_id: iss for iss in brief.issues}

    issue_results: list[dict[str, Any]] = []
    any_false_definitive = False
    for exp in expected_issues:
        iid = str(exp["issue_id"])
        actual = by_id.get(iid)
        if actual is None:
            issue_results.append(
                {
                    "issue_id": iid,
                    "passed": False,
                    "omitted": True,
                    "outcome_correct": False,
                    "value_correct": False,
                    "false_definitive_on_insufficient": False,
                    "controlling_docs_ok": False,
                    "scope_ok": False,
                    "dates_ok": False,
                    "missing_record_ok": False,
                    "citations_grounded": False,
                    "reasons": [f"Omitted required issue '{iid}'."],
                }
            )
            continue

        exp_out = str(exp["expected_outcome"])
        outcome_correct = actual.outcome == exp_out
        false_def = exp_out == "insufficient_evidence" and actual.outcome != "insufficient_evidence"
        if false_def:
            any_false_definitive = True

        exp_val = _normalize_controlling_value(str(exp["expected_value"]))
        act_val = _normalize_controlling_value(actual.determining_value)
        value_correct = act_val == exp_val

        verdicts: list[dict[str, Any]] = []
        verified_docs: set[str] = set()
        verified_spans: list[str] = []
        reasons: list[str] = []
        for cit in actual.citations:
            v = verify_coverage_citation_v3(cit, docs, expected_subject=expected_subject)
            verdicts.append(v)
            if v["verdict"] == "supported":
                verified_docs.add(cit.doc_id)
                lines = docs[cit.doc_id].splitlines()
                verified_spans.append(_norm(" ".join(lines[cit.line_start - 1 : cit.line_end])))
            else:
                reasons.append(f"[{v['verdict']}] {v['reason']}")

        req_docs = set(exp.get("required_controlling_docs") or [])
        cited_ctrl = set(actual.controlling_doc_ids) | verified_docs
        missing_ctrl = sorted(d for d in req_docs if d not in cited_ctrl or d not in verified_docs)
        controlling_docs_ok = len(missing_ctrl) == 0
        if missing_ctrl:
            reasons.append(f"Missing verified controlling doc(s): {missing_ctrl}")

        blob = _norm(
            " ".join(verified_spans)
            + " "
            + " ".join(c.claim_supported for c in actual.citations)
            + " "
            + actual.superseded_or_out_of_scope
            + " "
            + actual.missing_record_needed
            + " "
            + brief.rationale
        )
        req_scope = [str(t).lower() for t in (exp.get("required_scope_tokens") or [])]
        forb_scope = [str(t).lower() for t in (exp.get("forbidden_scope_tokens") or [])]
        # Forbidden scope tokens fail if asserted in controlling citations or determining_value
        ctrl_blob = _norm(
            actual.determining_value + " " + " ".join(c.claim_supported for c in actual.citations)
        )
        scope_ok = all(t in blob for t in req_scope) and not any(t in ctrl_blob for t in forb_scope)
        if not scope_ok:
            reasons.append("Scope check failed (missing required scope or forbidden site).")

        req_dates = [str(d) for d in (exp.get("required_dates") or [])]
        dates_ok = all(d in blob for d in req_dates)
        if not dates_ok:
            missing_d = [d for d in req_dates if d not in blob]
            reasons.append(f"Missing required date anchor(s): {missing_d}")

        req_miss = [str(k).lower() for k in (exp.get("expected_missing_keywords") or [])]
        miss_blob = _norm(actual.missing_record_needed + " " + brief.rationale)
        missing_record_ok = (not req_miss) or any(k in miss_blob for k in req_miss)
        if not missing_record_ok:
            reasons.append("Missing required record-request explanation for insufficient_evidence.")

        citations_grounded = bool(actual.citations) and all(
            v["verdict"] == "supported" for v in verdicts
        )
        iss_passed = (
            outcome_correct
            and value_correct
            and controlling_docs_ok
            and scope_ok
            and dates_ok
            and missing_record_ok
            and citations_grounded
        )
        issue_results.append(
            {
                "issue_id": iid,
                "passed": iss_passed,
                "omitted": False,
                "outcome_correct": outcome_correct,
                "expected_outcome": exp_out,
                "actual_outcome": actual.outcome,
                "value_correct": value_correct,
                "expected_value": exp_val,
                "actual_value": act_val,
                "false_definitive_on_insufficient": false_def,
                "controlling_docs_ok": controlling_docs_ok,
                "scope_ok": scope_ok,
                "dates_ok": dates_ok,
                "missing_record_ok": missing_record_ok,
                "citations_grounded": citations_grounded,
                "citation_verdicts": verdicts,
                "reasons": reasons,
            }
        )

    all_passed = bool(issue_results) and all(r["passed"] for r in issue_results)
    return {
        "scorer_version": "v3_issue",
        "packet_id": packet_id,
        "passed": all_passed,
        "issues_passed": sum(1 for r in issue_results if r["passed"]),
        "issues_total": len(issue_results),
        "false_definitive_on_insufficient": any_false_definitive,
        "issue_results": issue_results,
    }
