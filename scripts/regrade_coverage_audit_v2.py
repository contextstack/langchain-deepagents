"""Offline dual-version (v1 + v2) re-grader for saved Coverage Discrepancy Audit outputs.

Preserves all raw model outputs and v1 scores intact while computing v2 scores side-by-side.
Makes zero LLM calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.coverage_audit import evaluate_routing_bar, score_coverage_audit_brief_v2
from workflows.coverage_audit.models import CoverageAuditBrief

DEV_KEY_PATH = Path("evals/datasets/coverage_audit_dev_key.json")
HELDOUT_KEY_PATH = Path("evals/datasets/coverage_audit_heldout_key.json")
PILOT_IN = Path("artifacts/experiments/coverage_audit_pilot.json")
HELDOUT_IN = Path("artifacts/experiments/coverage_audit_heldout.json")
PILOT_OUT = Path("artifacts/experiments/coverage_audit_pilot_v1_v2.json")
HELDOUT_OUT = Path("artifacts/experiments/coverage_audit_heldout_v1_v2.json")
REPORT_MD = Path("docs/experiments/coverage-discrepancy-audit-results.md")


def _regrade_bundle(in_path: Path, key_path: Path, out_path: Path) -> dict[str, Any]:
    bundle: dict[str, Any] = json.loads(in_path.read_text(encoding="utf-8"))
    keys = {
        row["packet_id"]: row for row in json.loads(key_path.read_text(encoding="utf-8"))
    }
    dual_runs: list[dict[str, Any]] = []
    v2_rows_for_bar: list[dict[str, Any]] = []
    for row in bundle["runs"]:
        brief_raw = row.get("brief")
        if brief_raw is not None:
            brief_obj = CoverageAuditBrief.model_validate(brief_raw)
            v2 = score_coverage_audit_brief_v2(keys[row["packet_id"]], brief_obj)
        else:
            v2 = {
                "scorer_version": "v2",
                "passed": False,
                "determination_correct": False,
                "false_definitive_on_insufficient": False,
                "ordered_chain_ok": False,
                "controlling_docs_ok": False,
                "citations_grounded": False,
                "citation_failures": [],
            }
        merged = {
            **row,
            "v1_passed": row["passed"],
            "v2_scores": v2,
            "v2_passed": v2["passed"],
        }
        dual_runs.append(merged)
        v2_rows_for_bar.append(
            {
                "packet_id": row["packet_id"],
                "tier": row["tier"],
                "variant": row["variant"],
                "passed": v2["passed"],
                "false_definitive_on_insufficient": v2["false_definitive_on_insufficient"],
                "total_tokens": row["total_tokens"],
            }
        )

    observed_max = {}
    for variant in ("baseline", "deep_agents"):
        vr = [r for r in dual_runs if r["variant"] == variant]
        observed_max[variant] = {
            "max_llm_calls": max((r["llm_calls"] for r in vr), default=0),
            "max_tool_calls": max((r["tool_calls"] for r in vr), default=0),
            "max_total_tokens": max((r["total_tokens"] for r in vr), default=0),
            "max_duration_s": max((r["duration_s"] for r in vr), default=0.0),
        }

    out = {
        **bundle,
        "routing_bar_v1": bundle.get("routing_bar") or evaluate_routing_bar(bundle["runs"]),
        "routing_bar_v2": evaluate_routing_bar(v2_rows_for_bar),
        "observed_max_usage": observed_max,
        "runs": dual_runs,
    }
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def _append_v2_section(heldout_dual: dict[str, Any], pilot_dual: dict[str, Any]) -> None:
    base_md = REPORT_MD.read_text(encoding="utf-8")
    marker = "## Offline Grader v2 Re-Grade (Preserving v1 & v2 Side-by-Side)"
    if marker in base_md:
        base_md = base_md.split(marker)[0].rstrip() + "\n"

    runs = heldout_dual["runs"]
    bar2 = heldout_dual["routing_bar_v2"]
    obs = heldout_dual["observed_max_usage"]
    p_runs = pilot_dual["runs"]
    p_v1 = sum(1 for r in p_runs if r["v1_passed"])
    p_v2 = sum(1 for r in p_runs if r["v2_passed"])

    lines = [
        "",
        marker,
        "",
        (
            "- **Dual-Version Artifacts (zero new live calls):** "
            "`artifacts/experiments/coverage_audit_pilot_v1_v2.json` and "
            "`artifacts/experiments/coverage_audit_heldout_v1_v2.json` preserve all raw model "
            "outputs, v1 scores, and v2 scores side-by-side."
        ),
        (
            f"- **Dev Pilot Validation (`D01`–`D03`):** v1 passed `{p_v1}/{len(p_runs)}`, "
            f"v2 passed `{p_v2}/{len(p_runs)}` (v2 enforces exact line-span entailment, ordered "
            "operative supersession chains, separate evidence-only docs on B07/B08, exclusion "
            "polarity, and allows rule-out citations while rejecting B01 heading-only/empty-chain "
            "and unrelated-claim negative controls)."
        ),
        "",
        "### Held-Out v1 vs. v2 Aggregate Comparison",
        "",
        (
            "| Arm | Tier | Attempted | v1 Passed | v2 Passed | v2 Ordered Chain OK | "
            "v2 Controlling Clause OK | v2 Citations Grounded | False Def. |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ("baseline", "deep_agents"):
        for tier in ("overall", "clean", "branching"):
            sub = [
                r
                for r in runs
                if r["variant"] == variant and (tier == "overall" or r["tier"] == tier)
            ]
            att = len(sub)
            v1_p = sum(1 for r in sub if r["v1_passed"])
            v2_p = sum(1 for r in sub if r["v2_passed"])
            ch_ok = sum(1 for r in sub if r["v2_scores"]["ordered_chain_ok"])
            ct_ok = sum(1 for r in sub if r["v2_scores"]["controlling_docs_ok"])
            ci_ok = sum(1 for r in sub if r["v2_scores"]["citations_grounded"])
            fdef = sum(1 for r in sub if r["v2_scores"]["false_definitive_on_insufficient"])
            lines.append(
                f"| `{variant}` | {tier} | {att} | {v1_p}/{att} ({100.0 * v1_p / att:.1f}%) | "
                f"{v2_p}/{att} ({100.0 * v2_p / att:.1f}%) | {ch_ok}/{att} | {ct_ok}/{att} | "
                f"{ci_ok}/{att} | {fdef} |"
            )

    lines.extend(
        [
            "",
            "### Observed Usage Maxima vs. Enforced Equal Caps (16 calls / 32 tools / 96k tok)",
            "",
            "| Arm | Max LLM Calls | Max Tool Calls | Max Total Tokens | Max Latency (s) |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for variant in ("baseline", "deep_agents"):
        o = obs[variant]
        lines.append(
            f"| `{variant}` | {o['max_llm_calls']} | {o['max_tool_calls']} | "
            f"{o['max_total_tokens']:,} | {o['max_duration_s']} |"
        )

    lines.extend(
        [
            "",
            "### Routing Bar Under v2 Grader",
            "",
            f"- **Baseline branching cases passed (majority of 3):** "
            f"{bar2['baseline']['branching_cases_passed']}/{bar2['baseline']['branching_cases_total']}",
            f"- **Deep Agents branching cases passed (majority of 3):** "
            f"{bar2['deep_agents']['branching_cases_passed']}/"
            f"{bar2['deep_agents']['branching_cases_total']}",
            f"- **Delta (Deep Agents − Baseline, target ≥ +2):** "
            f"{bar2['delta_branching_cases']:+d}",
            (
                "- **Deep Agents false definitive on insufficient-evidence (target 0):** "
                f"{bar2['deep_agents']['false_definitive_on_insufficient']}"
            ),
            (
                "- **Token cost per verified branching attempt vs. Baseline (target ≤ 2.0×):** "
                f"{bar2['token_cost_ratio_vs_baseline']}×"
            ),
            f"- **Routing Bar Met (v2):** `{bar2['routing_bar_met']}`",
            "",
            "### Protocol Substitution Notes (Flagged Before Any Corpus Expansion)",
            "",
            (
                "1. **Clean abstention case:** The frozen 12-packet held-out corpus placed both "
                "abstention cases in the branching tier (`B03`, `B07`) and has no clean-tier "
                "abstention case (`C01`–`C04` are all definitive)."
            ),
            (
                "2. **Separately graded dual outcome on `B08`:** `B08` currently grades a single "
                "remaining-aggregate value (`$750,000`) rather than separate applicability "
                "and deductible outcomes. Adding those scenarios would require a new corpus freeze "
                "and is flagged here rather than launching a new live run."
            ),
            "",
        ]
    )
    REPORT_MD.write_text(base_md + "\n".join(lines), encoding="utf-8")


def main() -> None:
    pilot_dual = _regrade_bundle(PILOT_IN, DEV_KEY_PATH, PILOT_OUT)
    heldout_dual = _regrade_bundle(HELDOUT_IN, HELDOUT_KEY_PATH, HELDOUT_OUT)
    _append_v2_section(heldout_dual, pilot_dual)
    print(
        f"Pilot v1={sum(r['v1_passed'] for r in pilot_dual['runs'])}/6 "
        f"v2={sum(r['v2_passed'] for r in pilot_dual['runs'])}/6"
    )
    for variant in ("baseline", "deep_agents"):
        vr = [r for r in heldout_dual["runs"] if r["variant"] == variant]
        print(
            f"Held-out {variant:11s}: "
            f"v1={sum(r['v1_passed'] for r in vr)}/36 "
            f"v2={sum(r['v2_passed'] for r in vr)}/36 "
            f"chain={sum(r['v2_scores']['ordered_chain_ok'] for r in vr)}/36 "
            f"ctrl={sum(r['v2_scores']['controlling_docs_ok'] for r in vr)}/36"
        )
    print("Routing bar v2:", json.dumps(heldout_dual["routing_bar_v2"], indent=2))


if __name__ == "__main__":
    main()
