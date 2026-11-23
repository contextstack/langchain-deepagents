"""Step 2b: Offline adjudication of the 12 historical triage spot-review briefs (R01-R12).

Adjudicates all 91 claims across R01.json..R12.json strictly blinded (does not read arm-key.json),
incorporating the source-based findings from independent-review.md and recording the exact
reviewed denominator (12/126 briefs, 91/960 claims). Remaining 114 briefs / 869 claims stay
explicitly provisional. Makes zero live LLM calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evals.submission_ab import _source_texts_for_case, verify_citation_against_doc
from workflows.submission_triage.models import EvidenceCitation

SPOT_DIR = Path("/tmp/contextstack-historical-review.Rp92QI")
OUT_JSON = Path("artifacts/experiments/historical_triage_spot_adjudication.json")
REPORT_MD = Path("docs/experiments/historical-v2-regrade.md")

# Explicit source-based overrides from independent-review.md for the flagged spot-check items
_SPOT_NOTES: dict[str, str] = {
    "R01": (
        "Financial fields and clean loss history are corroborated; broad claim that all required "
        "fields are present requires both the application and carrier guide (partial/cross-doc)."
    ),
    "R02": (
        "Claim 1 misattributes minor property claim details ($12k closed 2024) to sub-02-email "
        "(email only says loss run attached); Claim 2 on sub-02-app substantiates the $12k claim."
    ),
    "R03": (
        "Application substantiates $6.8M revenue; $25M carrier maximum requires carrier-apex "
        "guide (separate single-source fact from cross-source within-appetite conclusion)."
    ),
    "R04": (
        "Application substantiates $14M revenue; $40M carrier maximum requires carrier-ironclad "
        "guide (separate single-source fact from cross-source cap comparison)."
    ),
    "R05": (
        "v2 application and email explicitly supersede v1 drafts; v2 supports revenue, limit, "
        "and FEIN."
    ),
    "R06": (
        "Email vs. signed application genuinely conflict on business classification; carrier "
        "guides substantiate roofing prohibition."
    ),
    "R07": (
        "Application placeholders and email support withholding definitive quote due to missing "
        "effective date and loss history; carrier cap/required-field assertions need the guide."
    ),
    "R08": (
        "Absence of loss-run file is a packet-level observation; broker email only proves the "
        "broker claimed it was attached, not that it was received."
    ),
    "R09": (
        "Conflicting revenue and limit figures exist in separate files; discrepancy requires "
        "both sources and carrier caps require the guide."
    ),
    "R10": (
        "Email confirms missing FEIN/location schedule, but finance-team attribution and Friday "
        "deadline are unsupported; cited 2024-09-01 contradicts application's 2026-12-01; "
        "wholesale class is supported while fabricated product description & Apex prohibition fail."
    ),
    "R11": (
        "Current loss run supports $420,000 open claim and supersedes prior clean report; old "
        "report is valid evidence only for what the old report stated."
    ),
    "R12": (
        "Missing payroll schedule is supported by both operational sources; its status as a "
        "carrier requirement needs the guide, and broad consistency claims need all records."
    ),
}

_CROSS_DOC_RE = re.compile(
    r"\b(?:all\s+required\s+fields|within\s+(?:apex|ironclad|carrier)|under\s+(?:apex|ironclad)|"
    r"below\s+(?:apex|ironclad)|exceeds\s+(?:apex|ironclad)|satisf(?:y|ies|ying)\s+.*required|"
    r"maximum|max\s+(?:revenue|limit|coverage)|matches\s+the\s+application|"
    r"consistent\s+across|same\s+as\s+the\s+application)\b",
    re.I,
)


def _adjudicate_claim(
    review_id: str,
    case_id: str,
    idx: int,
    raw_cit: dict[str, Any],
    doc_texts: dict[str, str],
) -> dict[str, Any]:
    doc_id = str(raw_cit.get("doc_id") or "")
    quote = str(raw_cit.get("section_or_quote") or "")
    fact = str(raw_cit.get("fact_supported") or "")

    # Apply explicit independent-review.md findings first
    if review_id == "R02" and idx == 1 and doc_id == "sub-02-email":
        return {
            "verdict": "contradicted",
            "reason": (
                "Email only states a loss run is attached and does not state the minor property "
                "claim amount or date (those appear in sub-02-app, not sub-02-email)."
            ),
        }
    if review_id == "R10" and "2024-09-01" in fact:
        return {
            "verdict": "contradicted",
            "reason": (
                "Cited 2024-09-01 effective date contradicts the application's 2026-12-01; "
                "finance-team attribution and Friday deadline are unsupported."
            ),
        }
    if review_id == "R10" and ("friday" in fact.lower() or "finance" in fact.lower()):
        return {
            "verdict": "unverified",
            "reason": (
                "Email confirms missing FEIN/location schedule, but finance-team attribution "
                "and Friday deadline are unsupported by the source."
            ),
        }
    if review_id == "R10" and doc_id == "sub-10-app" and "prohibited" in fact.lower():
        return {
            "verdict": "contradicted",
            "reason": (
                "Application supports wholesale class, but fabricated product description and "
                "asserted Apex prohibition are unsupported/contradicted."
            ),
        }
    if review_id == "R08" and doc_id == "sub-08-email" and "missing" in fact.lower():
        return {
            "verdict": "partial_missing_second_source",
            "reason": (
                "Broker email shows the broker claimed a loss run was attached; absence of the "
                "loss run is a packet-level observation requiring the document list."
            ),
        }

    cit = EvidenceCitation(doc_id=doc_id, section_or_quote=quote, fact_supported=fact)
    ok, reason = verify_citation_against_doc(cit, doc_texts)
    if ok:
        return {"verdict": "supported", "reason": "Verified against cited source clause."}

    # Distinguish cross-document / missing-second-source claims from outright falsehoods
    if _CROSS_DOC_RE.search(fact) and not doc_id.startswith("carrier-"):
        return {
            "verdict": "partial_missing_second_source",
            "reason": (
                f"Single-source operational citation '{doc_id}' blended with cross-document "
                f"carrier-guide or consistency assertion (requires second source). Detail: {reason}"
            ),
        }
    if "not supported by resolved span" in reason or "mismatch" in reason.lower():
        return {"verdict": "contradicted", "reason": reason}
    return {"verdict": "unverified", "reason": reason}


def main() -> None:
    adjudicated_briefs: list[dict[str, Any]] = []
    totals = {
        "supported": 0,
        "partial_missing_second_source": 0,
        "contradicted": 0,
        "unverified": 0,
    }
    total_claims = 0

    for i in range(1, 13):
        rid = f"R{i:02d}"
        data = json.loads((SPOT_DIR / f"{rid}.json").read_text(encoding="utf-8"))
        case_id = str(data["case_id"])
        brief = data.get("submission_brief") or data.get("brief") or data
        doc_texts = _source_texts_for_case(case_id)
        claims_out: list[dict[str, Any]] = []
        for idx, raw_cit in enumerate(brief.get("material_facts") or [], 1):
            res = _adjudicate_claim(rid, case_id, idx, raw_cit, doc_texts)
            totals[res["verdict"]] += 1
            total_claims += 1
            claims_out.append(
                {
                    "claim_index": idx,
                    "doc_id": raw_cit.get("doc_id"),
                    "section_or_quote": raw_cit.get("section_or_quote"),
                    "fact_supported": raw_cit.get("fact_supported"),
                    **res,
                }
            )
        adjudicated_briefs.append(
            {
                "review_id": rid,
                "case_id": case_id,
                "spot_review_note": _SPOT_NOTES[rid],
                "claim_count": len(claims_out),
                "claims": claims_out,
            }
        )

    payload = {
        "source_review_file": str(SPOT_DIR / "independent-review.md"),
        "blinded": True,
        "reviewed_briefs": len(adjudicated_briefs),
        "total_generated_briefs_in_v2": 126,
        "total_attempts_in_v2": 144,
        "no_brief_attempts_in_v2": 18,
        "reviewed_claims": total_claims,
        "total_claims_in_v2": 960,
        "unreviewed_provisional_briefs": 126 - len(adjudicated_briefs),
        "unreviewed_provisional_claims": 960 - total_claims,
        "verdict_totals": totals,
        "briefs": adjudicated_briefs,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # Append spot-review adjudication section to docs/experiments/historical-v2-regrade.md
    base_md = REPORT_MD.read_text(encoding="utf-8")
    marker = "## Blinded Spot-Review Adjudication (`R01`–`R12`)"
    if marker in base_md:
        base_md = base_md.split(marker)[0].rstrip() + "\n"

    lines = [
        "",
        marker,
        "",
        (
            f"- **Exact Reviewed Denominator:** `{len(adjudicated_briefs)}/126` generated briefs "
            f"(`12/144` total attempts, `{total_claims}/960` claims). The remaining "
            f"`114/126` briefs (`869/960` claims) and `18` no-brief attempts remain **explicitly "
            "provisional**."
        ),
        (
            "- **Artifact:** `artifacts/experiments/historical_triage_spot_adjudication.json` "
            "(blinded; `arm-key.json` not opened)."
        ),
        (
            f"- **Reviewed Claim Verdicts (`{total_claims}` claims):** "
            f"`supported={totals['supported']}`, "
            f"`partial_missing_second_source={totals['partial_missing_second_source']}`, "
            f"`contradicted={totals['contradicted']}`, "
            f"`unverified={totals['unverified']}`."
        ),
        "",
        (
            "| Review ID | Case | Claims | Supported | "
            "Partial (Missing 2nd Source) | Contradicted | Unverified |"
        ),
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for b in adjudicated_briefs:
        c_list = b["claims"]
        lines.append(
            f"| `{b['review_id']}` | `{b['case_id']}` | {len(c_list)} | "
            f"{sum(1 for c in c_list if c['verdict'] == 'supported')} | "
            f"{sum(1 for c in c_list if c['verdict'] == 'partial_missing_second_source')} | "
            f"{sum(1 for c in c_list if c['verdict'] == 'contradicted')} | "
            f"{sum(1 for c in c_list if c['verdict'] == 'unverified')} |"
        )
    REPORT_MD.write_text(base_md + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"Adjudicated {len(adjudicated_briefs)}/126 briefs ({total_claims}/960 claims):", totals)


if __name__ == "__main__":
    main()
