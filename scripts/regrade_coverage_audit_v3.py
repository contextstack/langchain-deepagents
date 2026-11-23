"""Step 2: Arm-blinded offline v3 re-grade of all 72 saved Coverage Discrepancy Audit outputs.

Preserves v1, v2, and all raw outputs intact. Blinds arm, model, trial, and usage until all
72 v3 grades are fixed, then unblinds and evaluates the pre-registered routing bar.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from evals.coverage_audit import evaluate_routing_bar
from evals.coverage_audit_v3 import score_coverage_audit_brief_v3
from workflows.coverage_audit.models import CoverageAuditBrief

HELDOUT_KEY_PATH = Path("evals/datasets/coverage_audit_heldout_key.json")
IN_PATH = Path("artifacts/experiments/coverage_audit_heldout_v1_v2.json")
BLIND_PACKET_PATH = Path("artifacts/experiments/coverage_audit_heldout_blinded_v3.jsonl")
BLIND_KEY_PATH = Path("artifacts/experiments/coverage_audit_heldout_blind_key_v3.json")
OUT_PATH = Path("artifacts/experiments/coverage_audit_heldout_v1_v2_v3.json")
REPORT_MD = Path("docs/experiments/coverage-discrepancy-audit-results.md")
SCORER_PATH = Path("evals/coverage_audit_v3.py")


def main() -> None:
    bundle: dict[str, Any] = json.loads(IN_PATH.read_text(encoding="utf-8"))
    keys = {
        row["packet_id"]: row for row in json.loads(HELDOUT_KEY_PATH.read_text(encoding="utf-8"))
    }
    runs: list[dict[str, Any]] = list(bundle["runs"])

    # 1. Build strictly arm-blinded packet (randomized order, zero arm/model/trial/usage metadata)
    indices = list(range(len(runs)))
    rng = random.Random(20261123)
    rng.shuffle(indices)

    blind_records: list[dict[str, Any]] = []
    blind_key: dict[str, dict[str, Any]] = {}
    for pos, orig_idx in enumerate(indices, 1):
        bid = f"COV-BLIND-{pos:03d}"
        row = runs[orig_idx]
        blind_records.append(
            {
                "blind_id": bid,
                "packet_id": row["packet_id"],
                "brief": row["brief"],
            }
        )
        blind_key[bid] = {
            "orig_index": orig_idx,
            "packet_id": row["packet_id"],
            "tier": row["tier"],
            "variant": row["variant"],
            "trial": row["trial"],
        }

    BLIND_PACKET_PATH.write_text(
        "\n".join(json.dumps(rec) for rec in blind_records) + "\n",
        encoding="utf-8",
    )
    BLIND_KEY_PATH.write_text(json.dumps(blind_key, indent=2) + "\n", encoding="utf-8")

    # 2. Grade strictly from the blinded packet (no arm/model/usage visible)
    blind_grades: dict[str, dict[str, Any]] = {}
    for line in BLIND_PACKET_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        bid = rec["blind_id"]
        brief_raw = rec["brief"]
        if brief_raw is not None:
            brief_obj = CoverageAuditBrief.model_validate(brief_raw)
            v3 = score_coverage_audit_brief_v3(keys[rec["packet_id"]], brief_obj)
        else:
            v3 = {
                "scorer_version": "v3",
                "passed": False,
                "determination_correct": False,
                "false_definitive_on_insufficient": False,
                "ordered_chain_ok": False,
                "controlling_docs_ok": False,
                "citations_grounded": False,
                "supported_citations": 0,
                "contradicted_citations": 0,
                "unverified_citations": 0,
                "contradicted_reasons": [],
                "unverified_reasons": [],
                "citation_verdicts": [],
            }
        blind_grades[bid] = v3

    # 3. Unblind ONLY after all 72 grades are fixed
    idx_to_v3: dict[int, dict[str, Any]] = {}
    for bid, meta in blind_key.items():
        idx_to_v3[int(meta["orig_index"])] = blind_grades[bid]

    merged_runs: list[dict[str, Any]] = []
    v3_rows_for_bar: list[dict[str, Any]] = []
    for i, row in enumerate(runs):
        v3 = idx_to_v3[i]
        merged_runs.append(
            {
                **row,
                "v3_scores": v3,
                "v3_passed": v3["passed"],
            }
        )
        v3_rows_for_bar.append(
            {
                "packet_id": row["packet_id"],
                "tier": row["tier"],
                "variant": row["variant"],
                "passed": v3["passed"],
                "false_definitive_on_insufficient": v3["false_definitive_on_insufficient"],
                "total_tokens": row["total_tokens"],
            }
        )

    bar3 = evaluate_routing_bar(v3_rows_for_bar)
    scorer_sha = hashlib.sha256(SCORER_PATH.read_bytes()).hexdigest()
    out = {
        **bundle,
        "v3_scorer_sha256": scorer_sha,
        "routing_bar_v3": bar3,
        "runs": merged_runs,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    # 4. Append v3 section to docs/experiments/coverage-discrepancy-audit-results.md
    base_md = REPORT_MD.read_text(encoding="utf-8")
    marker = "## Calibrated Grader v3 Re-Grade (Arm-Blinded, Preserving v1 & v2)"
    if marker in base_md:
        base_md = base_md.split(marker)[0].rstrip() + "\n"

    lines = [
        "",
        marker,
        "",
        f"- **Frozen v3 Scorer SHA-256 (`evals/coverage_audit_v3.py`):** `{scorer_sha}`",
        (
            "- **Blinded Packet & Key:** "
            "`artifacts/experiments/coverage_audit_heldout_blinded_v3.jsonl` & "
            "`artifacts/experiments/coverage_audit_heldout_blind_key_v3.json` "
            "(all 72 attempts graded with arm, model, trial, and usage hidden before unblinding)."
        ),
        (
            "- **Combined v1/v2/v3 Artifact:** "
            "`artifacts/experiments/coverage_audit_heldout_v1_v2_v3.json`."
        ),
        "",
        "### Held-Out v1 vs. v2 vs. v3 Aggregate Comparison",
        "",
        (
            "| Arm | Tier | Attempted | v1 Pass | v2 Pass | v3 Verified Pass | "
            "v3 Citations Sup / Con / Unv | False Def. |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ("baseline", "deep_agents"):
        for tier in ("overall", "clean", "branching"):
            sub = [
                r
                for r in merged_runs
                if r["variant"] == variant and (tier == "overall" or r["tier"] == tier)
            ]
            att = len(sub)
            v1_p = sum(1 for r in sub if r["v1_passed"])
            v2_p = sum(1 for r in sub if r["v2_passed"])
            v3_p = sum(1 for r in sub if r["v3_passed"])
            sup = sum(r["v3_scores"]["supported_citations"] for r in sub)
            con = sum(r["v3_scores"]["contradicted_citations"] for r in sub)
            unv = sum(r["v3_scores"]["unverified_citations"] for r in sub)
            fdef = sum(1 for r in sub if r["v3_scores"]["false_definitive_on_insufficient"])
            lines.append(
                f"| `{variant}` | {tier} | {att} | {v1_p}/{att} | {v2_p}/{att} | "
                f"**{v3_p}/{att} ({100.0 * v3_p / att:.1f}%)** | {sup} / {con} / {unv} | {fdef} |"
            )

    lines.extend(
        [
            "",
            "### Paired Per-Case Outcomes Under Calibrated v3 (3 Trials per Arm)",
            "",
            "| Packet | Tier | Baseline v3 | Deep Agents v3 | Baseline Maj. | DA Maj. | Paired |",
            "|---|---|---:|---:|---|---|---|",
        ]
    )
    pids = sorted({r["packet_id"] for r in merged_runs})
    for pid in pids:
        tier = next(r["tier"] for r in merged_runs if r["packet_id"] == pid)
        b_runs = [r for r in merged_runs if r["packet_id"] == pid and r["variant"] == "baseline"]
        d_runs = [r for r in merged_runs if r["packet_id"] == pid and r["variant"] == "deep_agents"]
        bp = sum(1 for r in b_runs if r["v3_passed"])
        dp = sum(1 for r in d_runs if r["v3_passed"])
        bm = bp >= 2
        dm = dp >= 2
        pair = "tie" if bm == dm else ("DA win" if dm else "Base win")
        lines.append(
            f"| `{pid}` | {tier} | {bp}/3 | {dp}/3 | {'PASS' if bm else 'FAIL'} | "
            f"{'PASS' if dm else 'FAIL'} | {pair} |"
        )

    tok_ratio_str = (
        f"{bar3['token_cost_ratio_vs_baseline']}×"
        if bar3["token_cost_ratio_vs_baseline"] is not None
        else "indeterminate (0 baseline verified branching attempts)"
    )
    lines.extend(
        [
            "",
            "### Pre-Registered Routing Bar Under Calibrated v3",
            "",
            f"- **Baseline branching majorities:** "
            f"{bar3['baseline']['branching_cases_passed']}/"
            f"{bar3['baseline']['branching_cases_total']} "
            f"({bar3['baseline']['branching_attempts_passed']}/24 verified attempts; "
            f"{bar3['baseline']['total_branching_tokens']:,} total branching tokens)",
            f"- **Deep Agents branching majorities:** "
            f"{bar3['deep_agents']['branching_cases_passed']}/"
            f"{bar3['deep_agents']['branching_cases_total']} "
            f"({bar3['deep_agents']['branching_attempts_passed']}/24 verified attempts; "
            f"{bar3['deep_agents']['total_branching_tokens']:,} total branching tokens)",
            f"- **Delta (Deep Agents − Baseline, target ≥ +2):** "
            f"{bar3['delta_branching_cases']:+d}",
            (
                "- **Deep Agents false definitive on insufficient-evidence (target 0):** "
                f"{bar3['deep_agents']['false_definitive_on_insufficient']}"
            ),
            (
                "- **Token cost per verified branching attempt vs. Baseline (target ≤ 2.0×):** "
                f"{tok_ratio_str}"
            ),
            f"- **Routing Bar Met (v3):** `{bar3['routing_bar_met']}`",
            "",
        ]
    )
    REPORT_MD.write_text(base_md + "\n".join(lines), encoding="utf-8")

    for variant in ("baseline", "deep_agents"):
        vr = [r for r in merged_runs if r["variant"] == variant]
        print(
            f"{variant:11s}: v1={sum(r['v1_passed'] for r in vr)}/36 "
            f"v2={sum(r['v2_passed'] for r in vr)}/36 "
            f"v3={sum(r['v3_passed'] for r in vr)}/36 "
            f"sup={sum(r['v3_scores']['supported_citations'] for r in vr)} "
            f"con={sum(r['v3_scores']['contradicted_citations'] for r in vr)} "
            f"unv={sum(r['v3_scores']['unverified_citations'] for r in vr)}"
        )
    print("Routing bar v3:", json.dumps(bar3, indent=2))


if __name__ == "__main__":
    main()
