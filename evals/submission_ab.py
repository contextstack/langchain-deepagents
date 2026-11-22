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
_ENTITY_SUFFIX_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&\-\']+(?:\s+[A-Z][A-Za-z0-9&\-\']+){0,4}\s+"
    r"(?:Inc\.?|LLC|L\.L\.C\.|Corp\.?|Corporation|Co\.?|Ltd\.?|LLP|LP|Partners|Group|"
    r"Holdings|Enterprises|Industries|Solutions|Services|Systems|Technologies|Logistics|"
    r"Analytics|Manufacturing|Roofing|Mechanical|Consulting|Accounting|Fireworks|"
    r"Pyrotechnics|Insurance|Specialty|Mutual|Underwriters))\b"
)
_LEADING_SUBJECT_RE = re.compile(
    r"^\s*([A-Z][A-Za-z0-9&\-\']+(?:\s+[A-Z][A-Za-z0-9&\-\']+){0,3})\s+"
    r"(?:has|have|had|is|are|was|were|does|do|did|reported|reports|requested|requests|"
    r"shows|stated|states|operates|carries|maintains|earned|seeks)\b"
)
_NON_ENTITY_LEADERS = frozenset(
    {
        "annual",
        "application",
        "broker",
        "business",
        "carrier",
        "claim",
        "claims",
        "class",
        "classification",
        "coverage",
        "document",
        "effective",
        "email",
        "fein",
        "guideline",
        "insured",
        "limit",
        "location",
        "loss",
        "named",
        "payroll",
        "policy",
        "prohibited",
        "requested",
        "revenue",
        "risk",
        "schedule",
        "signed",
        "submission",
        "the",
        "this",
        "these",
        "underwriting",
        "version",
    }
)
_CLAUSE_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "been",
        "were",
        "also",
        "their",
        "they",
        "them",
        "does",
        "into",
        "only",
        "said",
        "says",
        "stated",
        "states",
        "shows",
        "shown",
        "lists",
        "listed",
        "reports",
        "reported",
        "notes",
        "noted",
        "named",
        "insured",
        "company",
        "document",
        "application",
        "email",
        "submission",
        "under",
        "over",
        "than",
        "more",
        "less",
        "both",
        "each",
        "same",
        "field",
        "value",
        "source",
        "carrier",
        "required",
        "provided",
    }
)
_APPENDED_SPLIT_RE = re.compile(
    r"[;\n]+|(?<=[.!?])\s+|,\s*(?:and|plus|also|which)\s+"
    r"|\s+(?:and|plus|also)\s+(?=(?:it|they|the|has|have|had|is|are|was|were|does|do|did|"
    r"no|not|zero|under|above|below|exceeds|matches|contradicts|waived|approved|verified|"
    r"confirmed|includes|excludes|carries|maintains|reported|requested|operates|holds|"
    r"meets|satisfies|violates|lacks|missing|omits|supersedes|\$?\d))"
)

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
    """Resolve `section_or_quote` to its FULL enclosing source clause inside `doc_text`.

    Returning the full enclosing line/sentence prevents truncated excerpts from cutting off
    leading/trailing negation or subject attribution in the source document.
    """
    norm_quote = _normalize_text(section_or_quote)
    if not norm_quote:
        return None
    raw_units = [u.strip() for u in re.split(r"[\n]+|(?<=[.!?])\s+", doc_text) if u.strip()]

    # 1. Exact substring match inside a 1..3 unit enclosing clause -> return FULL clause
    for window in (1, 2, 3):
        for i in range(len(raw_units) - window + 1):
            cand = _normalize_text(" ".join(raw_units[i : i + window]))
            if norm_quote in cand:
                return cand

    norm_doc = _normalize_text(doc_text)
    if norm_quote in norm_doc:
        idx = norm_doc.find(norm_quote)
        lo = max(0, idx - 120)
        hi = min(len(norm_doc), idx + len(norm_quote) + 120)
        return norm_doc[lo:hi].strip()

    # 2. Fall back to matching a contiguous 1-3 line/sentence span in doc_text where every
    # concrete numeric/date/ID token in the quote exists and >=80% of content tokens match.
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


_ALLOWED_CITATION_FIELDS = frozenset(
    {
        "insured_name",
        "annual_revenue",
        "requested_coverage",
        "effective_date",
        "fein",
        "business_classification",
        "loss_history",
        "missing_field",
        "supersedes",
        "prohibited_class",
        "carrier_cap",
        "carrier_rule",
    }
)
_DIFFERENT_SUBJECT_RE = re.compile(
    r"\b(?:different|another|other)\s+(?:insured|company|entity|applicant|business|account)\b",
    re.I,
)
_TITLE_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9&\-\']+(?:\s+[A-Z][a-zA-Z0-9&\-\']+)+)\b")
_NON_ENTITY_PHRASES = frozenset(
    {
        "annual revenue",
        "requested limit",
        "requested coverage",
        "effective date",
        "loss history",
        "loss run",
        "loss runs",
        "signed application",
        "broker email",
        "business classification",
        "named insured",
        "b2b software",
        "management consulting",
        "it & management consulting",
        "roofing contractor",
        "light manufacturing",
        "wholesale goods",
        "accounting & legal",
        "legal professional",
        "location schedule",
        "payroll breakdown",
        "human review",
        "underwriting review",
        "carrier apex",
        "carrier ironclad",
    }
)
_STRUCTURAL_GLUE_WORDS = _CLAUSE_STOPWORDS | frozenset(
    {
        "annual",
        "revenue",
        "requested",
        "coverage",
        "limit",
        "effective",
        "date",
        "fein",
        "loss",
        "history",
        "claims",
        "claim",
        "clean",
        "zero",
        "free",
        "class",
        "classification",
        "business",
        "missing",
        "blank",
        "omitted",
        "pending",
        "supersedes",
        "prohibited",
        "maximum",
        "single",
        "dollars",
        "names",
        "account",
        "broker",
        "signed",
        "requires",
        "routed",
        "human",
        "underwriting",
        "review",
        "contradiction",
        "between",
        "sets",
        "asks",
        "attached",
        "within",
        "above",
        "below",
        "major",
        "open",
        "bodily",
        "injury",
        "minor",
        "property",
        "version",
        "runs",
    }
)


def _extract_named_entities(text: str) -> list[str]:
    """Extract proper-noun company/subject names anywhere in raw (cased) text."""
    found: list[str] = []
    for m in _ENTITY_SUFFIX_RE.finditer(text):
        ent = _normalize_text(m.group(1)).rstrip(".")
        if ent and ent not in found:
            found.append(ent)
    lead = _LEADING_SUBJECT_RE.search(text)
    if lead:
        cand = _normalize_text(lead.group(1)).rstrip(".")
        first_word = cand.split()[0] if cand else ""
        if cand and first_word not in _NON_ENTITY_LEADERS and cand not in found:
            found.append(cand)
    # Catch proper-noun company phrases anywhere in the claim (e.g. "named Moonbeam Labs")
    for m in _TITLE_PHRASE_RE.finditer(text):
        cand = _normalize_text(m.group(1)).rstrip(".")
        words = cand.split()
        if (
            not cand
            or cand in _NON_ENTITY_PHRASES
            or all(w in _NON_ENTITY_LEADERS for w in words)
            or any(cand in existing for existing in found)
        ):
            continue
        found.append(cand)
    return found


def _canonicalize_structured_fact(
    field: str,
    value: str,
    polarity: Literal["affirmed", "negated"],
) -> TypedFact | None:
    """Convert structured (field, value, polarity) into a canonical TypedFact for exact matching."""
    norm_val = _normalize_text(value).rstrip(".")
    if not norm_val:
        return None
    if field in {"annual_revenue", "requested_coverage", "carrier_cap"}:
        m = re.search(r"\b(\d{5,9})\b", norm_val)
        return TypedFact(field, m.group(1) if m else norm_val, polarity)
    if field == "effective_date":
        m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", norm_val)
        return TypedFact(field, m.group(1) if m else norm_val, polarity)
    if field == "fein":
        m = re.search(r"\b(\d{2}-\d{7})\b", norm_val)
        return TypedFact(field, m.group(1) if m else norm_val, polarity)
    if field in {"business_classification", "prohibited_class"}:
        for canon_cls, aliases in _CLASS_CANONICAL:
            if norm_val == canon_cls or any(a in norm_val for a in aliases):
                return TypedFact(field, canon_cls, polarity)
        return TypedFact(field, norm_val, polarity)
    if field == "loss_history":
        if re.search(r"\b(?:0|zero|no\s+claims|loss-free|loss_free|clean)\b", norm_val):
            return TypedFact("loss_history", "loss_free", polarity)
        m = re.search(r"\b(\d{4,7})\b", norm_val)
        return TypedFact("loss_history", m.group(1) if m else norm_val, polarity)
    return TypedFact(field, norm_val, polarity)


def _entity_matches_source(entity: str, allowed_blob: str) -> bool:
    """Return True if `entity` (or its distinctive tokens) is grounded in `allowed_blob`."""
    norm_ent = _normalize_text(entity).rstrip(".")
    if not norm_ent:
        return True
    if norm_ent in allowed_blob:
        return True
    generic_suffixes = {
        "inc",
        "llc",
        "corp",
        "corporation",
        "co",
        "ltd",
        "llp",
        "lp",
        "company",
        "group",
        "partners",
        "insurance",
        "specialty",
        "carrier",
        "the",
    }
    distinctive = [
        w for w in _WORD_RE.findall(norm_ent) if len(w) >= 3 and w not in generic_suffixes
    ]
    if not distinctive:
        return False
    return all(w in allowed_blob for w in distinctive)


def _allowed_subject_blob(doc_id: str, doc_texts: dict[str, str]) -> str:
    """Build normalized subject text for `doc_id` (doc itself + same-submission Insured header)."""
    parts = [_normalize_text(doc_texts.get(doc_id, ""))]
    # Submission docs share the insured's name from the submission's application/email headers
    if not doc_id.startswith("carrier-"):
        for other_id, other_text in doc_texts.items():
            if other_id.startswith("carrier-"):
                continue
            for line in other_text.splitlines()[:6]:
                if re.match(r"^\s*(?:Insured|Named\s+Insured|Account|Re)\s*:", line, re.I):
                    parts.append(_normalize_text(line))
    return " ".join(parts)


def extract_typed_facts(text: str) -> set[TypedFact]:
    """Deterministically parse verifiable (field, value, polarity) facts from a span or claim."""
    norm = _normalize_text(text)
    facts: set[TypedFact] = set()
    # Split into sentence/clause units so negation stays scoped to its clause
    clauses = [c.strip() for c in re.split(r"[;\n]+|(?<=[.!?])\s+", norm) if c.strip()]
    for clause in clauses:
        pol = _clause_polarity(clause)

        # 1. Annual revenue (skip carrier max-revenue caps, handled in #9)
        if not re.search(r"\b(?:max(?:imum)?|cap)\b", clause):
            for m in re.finditer(
                r"(?:annual\s+)?revenue(?::|\s+(?:is|of|at))?\s*(\d{5,9})\b"
                r"|\b(\d{5,9})\s+(?:annual\s+)?revenue\b",
                clause,
            ):
                val = m.group(1) or m.group(2)
                facts.add(TypedFact("annual_revenue", val, pol))

        # 2. Requested coverage / limit (skip carrier max-coverage caps, handled in #9)
        if not re.search(r"\b(?:max(?:imum)?|cap)\b", clause):
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
        if re.search(
            r"\b(?:0\s+claims|zero\s+claims|loss-free|no\s+claims|"
            r"clean\s+(?:3-year\s+)?loss(?:\s+history|\s+runs?)?|"
            r"loss\s+history\s+is\s+clean)\b",
            clause,
        ):
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

        # 9. Carrier appetite caps, prohibited classes, and escalation rules
        if "prohibited_classes" in clause or "prohibited" in clause:
            for canon_cls, aliases in _CLASS_CANONICAL:
                if any(a in clause for a in aliases):
                    facts.add(TypedFact("prohibited_class", canon_cls, pol))
        if any(
            k in clause
            for k in (
                "max_coverage_usd",
                "max_single_loss_usd",
                "max_revenue_usd",
                "maximum single loss",
                "maximum annual revenue",
                "maximum revenue",
                "maximum coverage",
                "maximum limit",
                "max single loss",
                "max revenue",
                "max coverage",
                "max limit",
            )
        ):
            for m in re.finditer(r"\b(\d{5,8})\b", clause):
                facts.add(TypedFact("carrier_cap", m.group(1), pol))
        if "contradiction" in clause and "human" in clause and "review" in clause:
            facts.add(TypedFact("carrier_rule", "contradiction_human_review", pol))

    # 10. Insured entity naming (case-insensitive; legal entity suffixes only)
    for m in re.finditer(
        r"(?:named\s+)?insured(?:\s+(?:is|as|name))?(?::|\s+is|\s+as)\s+"
        r"([a-z0-9][a-z0-9&\-\'\s]{1,50}?"
        r"(?:inc|llc|corp|corporation|co|ltd|llp|lp|partners|group))\b",
        norm,
    ):
        ent = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".")
        facts.add(TypedFact("insured_name", ent, _clause_polarity(norm)))

    return facts


def _is_unreviewable_reason(reason: str) -> bool:
    return reason.startswith("unreviewable:")


def verify_citation_against_doc(
    citation: EvidenceCitation,
    doc_texts: dict[str, str],
) -> tuple[bool, str]:
    """Resolve `section_or_quote` to its FULL enclosing clause and verify subject + typed facts."""
    raw_doc = doc_texts.get(citation.doc_id)
    if raw_doc is None:
        return False, f"unreviewable: Unknown doc_id '{citation.doc_id}'"

    if not citation.section_or_quote.strip():
        return (
            False,
            f"unreviewable: empty section_or_quote does not resolve on '{citation.doc_id}'",
        )

    resolved_span = resolve_source_span(raw_doc, citation.section_or_quote)
    if resolved_span is None:
        return (
            False,
            f"unreviewable: cited excerpt '{citation.section_or_quote}' does not resolve to a "
            f"source span in '{citation.doc_id}'",
        )

    # 1. Subject / entity binding: reject attribution to a different company/entity
    if _DIFFERENT_SUBJECT_RE.search(citation.fact_supported):
        return (
            False,
            f"Entity/subject mismatch on '{citation.doc_id}': claim explicitly attributes "
            "fact to a different insured/company",
        )
    allowed_blob = _allowed_subject_blob(citation.doc_id, doc_texts)
    claimed_entities: list[str] = []
    if citation.subject and citation.subject.strip():
        claimed_entities.append(citation.subject.strip())
    for ent in _extract_named_entities(citation.fact_supported):
        if ent not in claimed_entities:
            claimed_entities.append(ent)
    for ent in claimed_entities:
        if not _entity_matches_source(ent, allowed_blob):
            return (
                False,
                f"Entity/subject mismatch on '{citation.doc_id}': claimed subject '{ent}' "
                "is not present in source document or submission subject",
            )

    # 2. Strict structured (field, value, polarity) tuple verification against span_facts
    span_facts = extract_typed_facts(resolved_span)
    span_polarity = _clause_polarity(resolved_span)
    if citation.polarity is not None and citation.polarity != span_polarity:
        is_loss_free = TypedFact("loss_history", "loss_free", span_polarity) in span_facts
        if not (is_loss_free and citation.field == "loss_history"):
            return (
                False,
                f"Structured polarity mismatch on '{citation.doc_id}': "
                f"citation.polarity={citation.polarity!r} != source clause {span_polarity!r}",
            )
    if citation.field is not None:
        norm_field = citation.field.strip().lower()
        if norm_field not in _ALLOWED_CITATION_FIELDS:
            return (
                False,
                f"unreviewable: structured field '{citation.field}' on '{citation.doc_id}' "
                "is outside allowed field vocabulary",
            )
        if citation.value is not None:
            eff_pol = citation.polarity or span_polarity
            structured_fact = _canonicalize_structured_fact(norm_field, citation.value, eff_pol)
            if structured_fact is None or structured_fact not in span_facts:
                return (
                    False,
                    f"Structured tuple {structured_fact} not supported by resolved span on "
                    f"'{citation.doc_id}' (span facts: {sorted(span_facts, key=str)})",
                )

    # 3. Typed fact verification + whole-claim extra-assertion / qualifier grounding
    claim_facts = extract_typed_facts(citation.fact_supported)
    if not claim_facts:
        return (
            False,
            f"unreviewable: claim '{citation.fact_supported}' on '{citation.doc_id}' contains "
            "no verifiable typed fact (ambiguous/unsupported free text)",
        )

    unsupported = [cf for cf in sorted(claim_facts, key=str) if cf not in span_facts]
    if unsupported:
        return (
            False,
            f"Claimed fact(s) {unsupported} not supported by resolved span on "
            f"'{citation.doc_id}' (span facts: {sorted(span_facts, key=str)})",
        )

    # Check ALL extra substantive tokens across the whole claim (even inside the same clause!)
    span_words = set(_WORD_RE.findall(resolved_span))
    span_nums = set(_NUM_OR_ID_RE.findall(resolved_span))
    norm_claim = _normalize_text(citation.fact_supported)
    claim_nums = set(_NUM_OR_ID_RE.findall(norm_claim))
    if claim_nums and not claim_nums.issubset(span_nums):
        return (
            False,
            f"Unsupported numeric/date/ID value(s) {sorted(claim_nums - span_nums)} on "
            f"'{citation.doc_id}'",
        )

    ent_tokens = set(_WORD_RE.findall(" ".join(claimed_entities).lower()))
    # Include canonical class/value words already verified in span_facts
    verified_fact_tokens = set(_WORD_RE.findall(" ".join(f.value for f in claim_facts).lower()))
    extra_ungrounded = [
        w
        for w in _WORD_RE.findall(norm_claim)
        if len(w) >= 4
        and w not in span_words
        and w not in ent_tokens
        and w not in verified_fact_tokens
        and w not in _STRUCTURAL_GLUE_WORDS
        and not w.isdigit()
    ]
    if extra_ungrounded:
        return (
            False,
            f"Unsupported appended/extra assertion on '{citation.doc_id}': "
            f"ungrounded qualifier(s) {sorted(set(extra_ungrounded))}",
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

    # Passage-level verification: EVERY cited passage must actually be grounded in its cited doc
    citation_failures: list[str] = []
    unreviewable_citations: list[str] = []
    verified_doc_ids: set[str] = set()
    for cit in brief.material_facts:
        ok, reason = verify_citation_against_doc(cit, doc_texts)
        if ok:
            verified_doc_ids.add(cit.doc_id)
        else:
            citation_failures.append(reason)
            if _is_unreviewable_reason(reason):
                unreviewable_citations.append(reason)

    # Controlling documents must exist in doc_texts AND have at least one verified citation
    missing_controlling = sorted(d for d in must_cite if d not in verified_doc_ids)
    must_cite_covered = must_cite.issubset(set(doc_texts)) and len(missing_controlling) == 0
    if missing_controlling:
        citation_failures.append(
            f"Absent controlling document(s) without verified citation: {missing_controlling}"
        )
    citations_grounded = (
        citations_valid
        and must_cite_covered
        and (len(citation_failures) == 0)
        and (len(unreviewable_citations) == 0)
    )

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
        "unreviewable_citations": unreviewable_citations,
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
            trace_events: list[dict[str, Any]] = []
            try:
                final = await graph.ainvoke(
                    {"submission_id": case["case_id"], "planner_variant": variant},
                    config={"configurable": {"thread_id": ctx.thread_id}},
                    context=ctx,
                    durability="sync",
                )
                trace_events = list(final.get("trace_events") or [])
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
                    "unreviewable_citations": [],
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
                "tool_calls": cb.stats.tool_calls,
                "tools_called": list(cb.stats.tools_called),
                "model_ids_requested": cb.stats.unique_requested_models,
                "model_ids_responded": cb.stats.unique_response_models,
                "prompt_tokens": cb.stats.prompt_tokens,
                "completion_tokens": cb.stats.completion_tokens,
                "total_tokens": cb.stats.total_tokens,
                "cost_usd": cost,
                "error": error,
                "trace_events": trace_events,
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
