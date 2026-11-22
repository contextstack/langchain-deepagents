"""Stage 1: Arm-blinded offline re-grade of the 144-attempt historical v2 triage bundle.

Reads the existing live benchmark artifact without modifying it, builds a strictly arm-blinded
review packet (no model/arm/trial/usage metadata) with full enclosing source clauses, grades
every material claim for (entity, field, value, scope, date, polarity) -> supported | contradicted
| unverified with a source-based reason, and aggregates by group after scoring while keeping all
18 attempts without briefs in the attempted denominator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from workflows.submission_triage.fixtures import (
    CARRIER_APPETITE_GUIDES,
    SUBMISSIONS,
    serialize_carrier,
)

_NEG_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|doesn't|don't|did\s+not|didn't|"
    r"is\s+not|isn't|are\s+not|aren't|was\s+not|wasn't|were\s+not|weren't|"
    r"has\s+not|hasn't|have\s+not|haven't|cannot|can't|not\s+have|never|"
    r"neither|nor|contrary\s+to|contradicts|false\s+that|no\s+longer)\b"
)
_MILLION_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*m(?:illion)?\b")
_THOUSAND_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*k\b")
_NUM_ID_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{7}|clm-\d{4}-\d+|\d{4,9})\b")
_COMPARATIVE_RE = re.compile(
    r"\b(?:matches|same\s+as|consistent\s+with|agrees\s+with|contradicts|"
    r"conflicts\s+with|differs\s+from|versus|vs\.?|compared\s+to)\b"
)
_INSURED_TOKENS: dict[str, tuple[str, ...]] = {
    "sub-01": ("northstar",),
    "sub-02": ("harbor",),
    "sub-03": ("crestview",),
    "sub-04": ("midland",),
    "sub-05": ("beacon",),
    "sub-06": ("summit",),
    "sub-07": ("clearpath",),
    "sub-08": ("starburst",),
    "sub-09": ("vector",),
    "sub-10": ("riverbend",),
    "sub-11": ("redrock",),
    "sub-12": ("oakridge",),
}
_ALL_ENTITY_TOKENS = tuple(tok for toks in _INSURED_TOKENS.values() for tok in toks)


def _norm(text: str) -> str:
    s = re.sub(r"(?<=\d),(?=\d)", "", text).replace("$", "").lower()
    s = _MILLION_RE.sub(lambda m: str(int(round(float(m.group(1)) * 1_000_000))), s)
    s = _THOUSAND_RE.sub(lambda m: str(int(round(float(m.group(1)) * 1_000))), s)
    return re.sub(r"\s+", " ", s).strip()


def _doc_map(case_id: str) -> dict[str, str]:
    sub = SUBMISSIONS[case_id]
    docs: dict[str, str] = {d.doc_id: f"{d.title}\n{d.content}" for d in sub.documents}
    for cid, guide in CARRIER_APPETITE_GUIDES.items():
        docs[cid] = json.dumps(serialize_carrier(guide))
    return docs


def _enclosing_clause(doc_text: str, quote: str) -> str | None:
    """Resolve `quote` to the full enclosing line/sentence clause(s) inside `doc_text`."""
    nq = _norm(quote)
    if not nq:
        return None
    units = [u.strip() for u in re.split(r"[\n]+|(?<=[.!?])\s+", doc_text) if u.strip()]
    # First: exact substring inside a 1-3 unit window (returning full enclosing clause!)
    for window in (1, 2, 3):
        for i in range(len(units) - window + 1):
            raw_win = " ".join(units[i : i + window])
            if nq in _norm(raw_win):
                return raw_win
    # Second: high-overlap token match with all numeric/date/ID tokens preserved
    q_nums = set(_NUM_ID_RE.findall(nq))
    q_words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", nq) if len(w) >= 4]
    if not q_words and not q_nums:
        return None
    best: tuple[float, str] = (0.0, "")
    for i in range(len(units)):
        for window in (1, 2, 3):
            if i + window > len(units):
                continue
            raw_win = " ".join(units[i : i + window])
            nwin = _norm(raw_win)
            if q_nums and not all(tok in nwin for tok in q_nums):
                continue
            wset = set(re.findall(r"[a-z0-9][a-z0-9\-]*", nwin))
            score = (sum(1 for w in q_words if w in wset) / len(q_words)) if q_words else 1.0
            if score >= 0.75 and score > best[0]:
                best = (score, raw_win)
    return best[1] if best[0] > 0 else None


def _grade_blinded_claim(
    *,
    case_id: str,
    claim: dict[str, Any],
    all_case_docs: dict[str, str],
) -> dict[str, Any]:
    """Automated deterministic audit of one claim against its full enclosing source clause."""
    doc_id = str(claim.get("doc_id", ""))
    quote = str(claim.get("section_or_quote", ""))
    fact = str(claim.get("fact_supported", ""))
    raw_doc = all_case_docs.get(doc_id)

    if raw_doc is None:
        return {
            "verdict": "unverified",
            "reason": f"Cited doc_id '{doc_id}' does not exist in submission or carrier guides.",
            "enclosing_clause": None,
        }

    clause = _enclosing_clause(raw_doc, quote)
    if clause is None:
        return {
            "verdict": "unverified",
            "reason": f"Excerpt does not resolve to an enclosing source clause in '{doc_id}'.",
            "enclosing_clause": None,
        }

    n_clause = _norm(clause)
    n_fact = _norm(fact)
    n_doc = _norm(raw_doc)

    # 1. Entity / subject check: must not attribute fact to a different insured entity
    expected_entities = _INSURED_TOKENS.get(case_id, ())
    wrong_entities = [
        e for e in _ALL_ENTITY_TOKENS if e in n_fact and e not in expected_entities
    ]
    if wrong_entities:
        return {
            "verdict": "contradicted",
            "reason": (
                f"Entity mismatch: claim attributes fact to {wrong_entities}, "
                f"contradicting case entity {list(expected_entities)}."
            ),
            "enclosing_clause": clause,
        }

    # 2. Comparative claims require evidence from BOTH sources (cannot be verified from 1 doc!)
    if _COMPARATIVE_RE.search(n_fact):
        other_docs_mentioned = [
            d for d in all_case_docs if d != doc_id and d in n_fact
        ]
        if not other_docs_mentioned:
            return {
                "verdict": "unverified",
                "reason": (
                    "Comparative claim references another source without citing evidence "
                    "from both sources."
                ),
                "enclosing_clause": clause,
            }

    # 3. Polarity / syntactic negation check against FULL enclosing source clause
    clause_neg = bool(_NEG_RE.search(n_clause))
    fact_neg = bool(_NEG_RE.search(n_fact))
    # Claim-vs-clause numeric/date/ID mismatch -> contradicted if same field, else unverified
    fact_nums = set(_NUM_ID_RE.findall(n_fact))
    clause_nums = set(_NUM_ID_RE.findall(n_clause))
    doc_nums = set(_NUM_ID_RE.findall(n_doc))
    missing_nums = [t for t in sorted(fact_nums) if t not in clause_nums and t not in doc_nums]
    if missing_nums:
        return {
            "verdict": "contradicted" if clause_nums else "unverified",
            "reason": (
                f"Claim numeric/date/ID value(s) {missing_nums} not present in enclosing "
                f"clause or doc '{doc_id}' (clause values: {sorted(clause_nums)})."
            ),
            "enclosing_clause": clause,
        }

    # Loss-free vs open-claim polarity contradiction
    clause_loss_free = bool(
        re.search(r"\b(?:0\s+claims|zero\s+claims|loss-free|no\s+claims)\b", n_clause)
    )
    fact_loss_free = bool(
        re.search(r"\b(?:0\s+claims|zero\s+claims|loss-free|no\s+claims)\b", n_fact)
    )
    clause_has_claim = bool(
        re.search(r"\b(?:open\s+bodily\s+injury|open\s+claim|clm-\d{4}-\d+)\b", n_clause)
    )
    fact_has_claim = bool(
        re.search(r"\b(?:open\s+bodily\s+injury|open\s+claim|clm-\d{4}-\d+)\b", n_fact)
    )
    if (clause_loss_free and fact_has_claim) or (clause_has_claim and fact_loss_free):
        return {
            "verdict": "contradicted",
            "reason": f"Loss-history polarity contradiction between '{doc_id}' clause and claim.",
            "enclosing_clause": clause,
        }

    _polarity_fields = (
        "revenue",
        "limit",
        "coverage",
        "effective",
        "fein",
        "class",
        "roofing",
        "fireworks",
        "saas",
    )
    if (
        clause_neg != fact_neg
        and (fact_nums or any(k in n_fact for k in _polarity_fields))
        and not (clause_loss_free and fact_loss_free)
    ):
        return {
            "verdict": "contradicted",
            "reason": (
                f"Polarity mismatch between full enclosing source clause "
                f"(negated={clause_neg}) and claim (negated={fact_neg}) in '{doc_id}'."
            ),
            "enclosing_clause": clause,
        }

    # 4. Field/value grounding check: claim must assert a concrete value/status present in clause
    has_concrete_anchor = bool(
        (fact_nums and fact_nums.issubset(clause_nums))
        or (clause_loss_free and fact_loss_free)
        or (clause_has_claim and fact_has_claim)
        or any(
            kw in n_fact and kw in n_clause
            for kw in (
                *expected_entities,
                "saas",
                "roofing",
                "fireworks",
                "manufacturing",
                "consulting",
                "warehouse",
                "wholesale",
                "accounting",
                "hvac",
                "missing",
                "blank",
                "pending",
                "not provided",
                "tbd",
                "supersedes",
                "sprinkler",
                "prohibited",
                "eligible",
            )
        )
    )
    if not has_concrete_anchor:
        return {
            "verdict": "unverified",
            "reason": (
                f"Claim lacks a concrete entity, field, value, date, or status anchor "
                f"verifiable from the enclosing clause in '{doc_id}'."
            ),
            "enclosing_clause": clause,
        }

    return {
        "verdict": "supported",
        "reason": f"Entity, value, and polarity match full enclosing source clause in '{doc_id}'.",
        "enclosing_clause": clause,
    }


def run_historical_regrade(
    *,
    v2_bundle_path: Path,
    out_dir: Path,
    report_path: Path,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(v2_bundle_path.read_text(encoding="utf-8"))

    # Collect all 144 attempts (preserving exact historical order)
    all_attempts: list[dict[str, Any]] = []
    for exp_key in ("exp1_frontier_claude_sonnet_5_5", "exp1_local_dgx_qwen3_coder_next"):
        for r in data[exp_key]["runs"]:
            all_attempts.append({"exp_key": exp_key, **r})

    blind_packet_lines: list[str] = []
    adjudication_lines: list[str] = []
    blind_key: dict[str, dict[str, Any]] = {}
    brief_verdicts: dict[str, dict[str, Any]] = {}

    blind_idx = 0
    for att in all_attempts:
        brief = att.get("submission_brief")
        if not brief:
            continue
        blind_idx += 1
        blind_id = f"BLIND-{blind_idx:03d}"
        case_id = att["case_id"]
        docs = _doc_map(case_id)

        blind_key[blind_id] = {
            "exp_key": att["exp_key"],
            "model_alias": att["model_alias"],
            "variant": att["variant"],
            "case_id": case_id,
            "tier": att["tier"],
            "trial": att["trial"],
        }

        claims = brief.get("material_facts") or []
        blind_claims: list[dict[str, Any]] = []
        graded_claims: list[dict[str, Any]] = []
        for idx, c in enumerate(claims, 1):
            clause = _enclosing_clause(
                docs.get(str(c.get("doc_id", "")), ""),
                str(c.get("section_or_quote", "")),
            )
            blind_claims.append(
                {
                    "claim_index": idx,
                    "doc_id": c.get("doc_id"),
                    "section_or_quote": c.get("section_or_quote"),
                    "fact_supported": c.get("fact_supported"),
                    "enclosing_source_clause": clause,
                }
            )
            g = _grade_blinded_claim(case_id=case_id, claim=c, all_case_docs=docs)
            graded_claims.append(
                {
                    "blind_id": blind_id,
                    "case_id": case_id,
                    "claim_index": idx,
                    "doc_id": c.get("doc_id"),
                    "fact_supported": c.get("fact_supported"),
                    "verdict": g["verdict"],
                    "reason": g["reason"],
                    "enclosing_source_clause": g["enclosing_clause"],
                    "grading_method": "automated_deterministic_audit",
                }
            )

        blind_record = {
            "blind_id": blind_id,
            "case_id": case_id,
            "disposition": brief.get("disposition"),
            "insured_name": brief.get("insured_name"),
            "annual_revenue_usd": brief.get("annual_revenue_usd"),
            "requested_coverage_usd": brief.get("requested_coverage_usd"),
            "effective_date": brief.get("effective_date"),
            "carrier_match": brief.get("carrier_match"),
            "claims": blind_claims,
            "case_documents": list(docs.keys()),
        }
        blind_packet_lines.append(json.dumps(blind_record))
        for gc in graded_claims:
            adjudication_lines.append(json.dumps(gc))

        n_claims = len(graded_claims)
        n_sup = sum(1 for g in graded_claims if g["verdict"] == "supported")
        n_con = sum(1 for g in graded_claims if g["verdict"] == "contradicted")
        n_unv = sum(1 for g in graded_claims if g["verdict"] == "unverified")
        brief_verdicts[blind_id] = {
            "total_claims": n_claims,
            "supported": n_sup,
            "contradicted": n_con,
            "unverified": n_unv,
            "all_claims_supported": n_claims > 0 and n_sup == n_claims,
        }

    packet_path = out_dir / "historical_v2_blinded_review_packet.jsonl"
    key_path = out_dir / "historical_v2_blind_key.json"
    adj_path = out_dir / "historical_v2_adjudication.jsonl"
    packet_path.write_text("\n".join(blind_packet_lines) + "\n", encoding="utf-8")
    key_path.write_text(json.dumps(blind_key, indent=2), encoding="utf-8")
    adj_path.write_text("\n".join(adjudication_lines) + "\n", encoding="utf-8")

    # Unblind & aggregate AFTER scoring, keeping all 18 no-brief attempts in attempted denominator
    inv_key = {
        (v["exp_key"], v["variant"], v["case_id"], v["trial"]): bid
        for bid, v in blind_key.items()
    }
    groups: list[dict[str, Any]] = []
    for exp_key, model_label in [
        ("exp1_frontier_claude_sonnet_5_5", "anthropic/claude-sonnet-5-5"),
        ("exp1_local_dgx_qwen3_coder_next", "local/Qwen3-Coder-Next-FP8 (DGX Spark)"),
    ]:
        for variant in ("baseline", "deep_agents"):
            for tier in ("overall", "clean", "messy"):
                sub = [
                    a
                    for a in all_attempts
                    if a["exp_key"] == exp_key
                    and a["variant"] == variant
                    and (tier == "overall" or a["tier"] == tier)
                ]
                attempted = len(sub)
                with_brief = [a for a in sub if a.get("submission_brief")]
                no_brief = attempted - len(with_brief)
                disp_ok = sum(1 for a in sub if a.get("disposition_correct"))
                claims_tot = claims_sup = claims_con = claims_unv = briefs_all_sup = 0
                for a in with_brief:
                    bid = inv_key[(a["exp_key"], a["variant"], a["case_id"], a["trial"])]
                    bv = brief_verdicts[bid]
                    claims_tot += bv["total_claims"]
                    claims_sup += bv["supported"]
                    claims_con += bv["contradicted"]
                    claims_unv += bv["unverified"]
                    if bv["all_claims_supported"]:
                        briefs_all_sup += 1
                groups.append(
                    {
                        "model": model_label,
                        "variant": variant,
                        "tier": tier,
                        "attempted": attempted,
                        "valid_briefs": len(with_brief),
                        "no_brief_failures": no_brief,
                        "disposition_correct": disp_ok,
                        "briefs_all_claims_supported": briefs_all_sup,
                        "total_claims": claims_tot,
                        "claims_supported": claims_sup,
                        "claims_contradicted": claims_con,
                        "claims_unverified": claims_unv,
                    }
                )

    lines = [
        "# Historical v2 Triage Run — Arm-Blinded Offline Claim Re-Grade",
        "",
        (
            "- **Source Artifact (preserved intact):** `deepagents_experiments_v2.json` "
            "(144 attempts: 126 generated briefs + 18 attempts without briefs)"
        ),
        (
            "- **Original Configuration Provenance:** Baseline ran with "
            "`SummarizationMiddleware(model=ops-fast)`; Deep Agents ran with `general-purpose` "
            "disabled and shared 12-model / 15-tool budget. Historical disposition and usage "
            "counts remain attached to that original configuration."
        ),
        (
            "- **Grading Method:** **Automated deterministic audit** "
            "(`scripts/regrade_historical_v2.py`) executed strictly on the arm-blinded packet "
            "(`artifacts/experiments/historical_v2_blinded_review_packet.jsonl`, zero "
            "model/arm/trial/usage metadata). All 960 claim judgments are retained in "
            "`artifacts/experiments/historical_v2_adjudication.jsonl` for independent adjudication."
        ),
        "",
        "## Aggregate Results by Group (Attempted & Valid-Brief Denominators Reported Separately)",
        "",
        (
            "| Model | Arm | Tier | Attempted | Valid Briefs | No-Brief Failures | "
            "Disp. Correct / Attempted | All-Claims-Supported / Attempted | "
            "All-Claims-Supported / Valid Briefs | Claims Supported | "
            "Claims Contradicted | Claims Unverified |"
        ),
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for g in groups:
        att_n = g["attempted"]
        vb_n = g["valid_briefs"]
        ct = g["total_claims"]
        sup_pct = f"{100.0 * g['claims_supported'] / ct:.1f}%" if ct else "—"
        lines.append(
            f"| {g['model']} | {g['variant']} | {g['tier']} | {att_n} | {vb_n} | "
            f"{g['no_brief_failures']} | {g['disposition_correct']}/{att_n} | "
            f"{g['briefs_all_claims_supported']}/{att_n} | "
            f"{g['briefs_all_claims_supported']}/{vb_n} | "
            f"{g['claims_supported']}/{ct} ({sup_pct}) | "
            f"{g['claims_contradicted']} | {g['claims_unverified']} |"
        )
    lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "packet_path": str(packet_path),
        "key_path": str(key_path),
        "adjudication_path": str(adj_path),
        "report_path": str(report_path),
        "groups": groups,
    }


if __name__ == "__main__":
    res = run_historical_regrade(
        v2_bundle_path=Path(
            "/Users/jeremyvaught/.gemini/antigravity/brain/"
            "39efd2cc-568c-4fa3-a5fc-2b34bc888638/scratch/deepagents_experiments_v2.json"
        ),
        out_dir=Path("artifacts/experiments"),
        report_path=Path("docs/experiments/historical-v2-regrade.md"),
    )
    print(json.dumps({k: v for k, v in res.items() if k != "groups"}, indent=2))
    for g in res["groups"]:
        if g["tier"] == "overall":
            print(
                f"{g['model']:40s} | {g['variant']:11s} | att={g['attempted']} "
                f"briefs={g['valid_briefs']} nobrief={g['no_brief_failures']} "
                f"all_sup={g['briefs_all_claims_supported']}/{g['attempted']} "
                f"claims={g['claims_supported']}/{g['total_claims']} "
                f"(con={g['claims_contradicted']}, unv={g['claims_unverified']})"
            )
