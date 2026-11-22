"""Stage 4 runner: Read-only Coverage Discrepancy Audit (dev pilot & 72-attempt held-out)."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import get_settings
from evals.coverage_audit import evaluate_routing_bar, run_coverage_audit_suite

DEV_KEY_PATH = Path("evals/datasets/coverage_audit_dev_key.json")
HELDOUT_KEY_PATH = Path("evals/datasets/coverage_audit_heldout_key.json")
MANIFEST_PATH = Path("evals/datasets/coverage_audit_manifest.json")

PILOT_OUT_JSON = Path("artifacts/experiments/coverage_audit_pilot.json")
HELDOUT_OUT_JSON = Path("artifacts/experiments/coverage_audit_heldout.json")
HELDOUT_OUT_MD = Path("docs/experiments/coverage-discrepancy-audit-results.md")


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _render_heldout_report(bundle: dict[str, Any]) -> str:
    runs = bundle["runs"]
    bar = bundle["routing_bar"]
    manifest = bundle["manifest"]
    lines = [
        "# Coverage Discrepancy Audit — Read-Only Held-Out Results",
        "",
        f"- **Pinned Commit:** `{bundle['commit_sha']}`",
        f"- **Timestamp (UTC):** `{bundle['timestamp_utc']}`",
        f"- **Model Alias:** `{bundle['model_alias']}` (`concurrency=1`, alternating arm order)",
        f"- **Held-Out Key SHA-256:** `{manifest['heldout_key_sha256']}`",
        f"- **Archive Docs Count:** {manifest['archive_file_count']} files under `policy-archive/`",
        (
            "- **Shared Equal Budgets:** 16 model calls, 32 tool calls, 96,000 total tokens, "
            "180s timeout per attempt; `general-purpose` disabled; identical `chat_model` across "
            "investigation, summarization, specialist, and synthesis."
        ),
        "",
        "## Model Provenance Verification",
        "",
        "| Arm | Unique Requested Models | Unique Responded Models |",
        "|---|---|---|",
    ]
    for variant in ("baseline", "deep_agents"):
        v_runs = [r for r in runs if r["variant"] == variant]
        req_m = sorted({m for r in v_runs for m in r.get("model_ids_requested", [])})
        resp_m = sorted({m for r in v_runs for m in r.get("model_ids_responded", [])})
        lines.append(f"| `{variant}` | `{', '.join(req_m)}` | `{', '.join(resp_m)}` |")

    lines.extend(
        [
            "",
            "## Aggregate Results (Clean vs. Branching — Separate from Submission Triage)",
            "",
            (
                "| Arm | Tier | Attempted | Passed | Det. Correct | False Def. on Insuff. | "
                "Errors | Avg Calls | Avg Tools | Avg Tokens | Total Cost | Avg Lat (s) |"
            ),
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in ("baseline", "deep_agents"):
        for tier in ("overall", "clean", "branching"):
            sub = [
                r
                for r in runs
                if r["variant"] == variant and (tier == "overall" or r["tier"] == tier)
            ]
            att = len(sub)
            passed = sum(1 for r in sub if r["passed"])
            det = sum(1 for r in sub if r["determination_correct"])
            fdef = sum(1 for r in sub if r.get("false_definitive_on_insufficient"))
            errs = sum(1 for r in sub if r.get("error"))
            ac = round(sum(r["llm_calls"] for r in sub) / att, 2) if att else 0.0
            at = round(sum(r["tool_calls"] for r in sub) / att, 2) if att else 0.0
            atok = round(sum(r["total_tokens"] for r in sub) / att) if att else 0
            cost = round(sum(r["cost_usd"] for r in sub), 4)
            lat = round(sum(r["duration_s"] for r in sub) / att, 2) if att else 0.0
            lines.append(
                f"| `{variant}` | {tier} | {att} | {passed}/{att} ({100.0 * passed / att:.1f}%) | "
                f"{det}/{att} | {fdef} | {errs} | {ac} | {at} | {atok:,} | \\${cost:.4f} | {lat} |"
            )

    lines.extend(
        [
            "",
            "## Per-Case Paired Table (3 Trials per Arm)",
            "",
            "| Packet | Tier | Baseline Pass | Deep Agents Pass | Baseline Avg Tok | DA Avg Tok |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    pids = sorted({r["packet_id"] for r in runs})
    for pid in pids:
        tier = next(r["tier"] for r in runs if r["packet_id"] == pid)
        b_runs = [r for r in runs if r["packet_id"] == pid and r["variant"] == "baseline"]
        d_runs = [r for r in runs if r["packet_id"] == pid and r["variant"] == "deep_agents"]
        b_pass = sum(1 for r in b_runs if r["passed"])
        d_pass = sum(1 for r in d_runs if r["passed"])
        b_tok = round(sum(r["total_tokens"] for r in b_runs) / max(1, len(b_runs)))
        d_tok = round(sum(r["total_tokens"] for r in d_runs) / max(1, len(d_runs)))
        lines.append(
            f"| `{pid}` | {tier} | {b_pass}/{len(b_runs)} | {d_pass}/{len(d_runs)} | "
            f"{b_tok:,} | {d_tok:,} |"
        )

    lines.extend(
        [
            "",
            "## Pre-Registered Routing Bar Evaluation",
            "",
            f"- **Baseline branching cases passed (majority of 3):** "
            f"{bar['baseline']['branching_cases_passed']}/{bar['baseline']['branching_cases_total']}",
            f"- **Deep Agents branching cases passed (majority of 3):** "
            f"{bar['deep_agents']['branching_cases_passed']}/{bar['deep_agents']['branching_cases_total']}",
            f"- **Delta (Deep Agents − Baseline, target ≥ +2):** {bar['delta_branching_cases']:+d}",
            (
                "- **Deep Agents false definitive on insufficient-evidence (target 0):** "
                f"{bar['deep_agents']['false_definitive_on_insufficient']}"
            ),
            (
                "- **Token cost per verified branching attempt vs. Baseline (target ≤ 2.0×):** "
                f"{bar['token_cost_ratio_vs_baseline']}×"
            ),
            f"- **Routing Bar Met:** `{bar['routing_bar_met']}`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


async def _main(mode: str, model_alias: str) -> None:
    settings = get_settings()
    settings.model_mode = "gateway"
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    commit_sha = _git_sha()

    if mode == "pilot":
        print(
            f"=== Running 6-attempt Dev Pilot (D01-D03 x 2 arms) on {model_alias} ===",
            flush=True,
        )
        res = await run_coverage_audit_suite(
            key_path=DEV_KEY_PATH,
            model_alias=model_alias,
            trials=1,
            settings=settings,
            checkpoint_path=PILOT_OUT_JSON,
        )
        runs = res["runs"]
        pilot_cost = sum(r["cost_usd"] for r in runs)
        pilot_tokens = sum(r["total_tokens"] for r in runs)
        est_72_cost = round(pilot_cost * (72 / max(1, len(runs))), 4)
        est_72_tokens = round(pilot_tokens * (72 / max(1, len(runs))))
        bundle = {
            "mode": "dev_pilot_6",
            "commit_sha": commit_sha,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "model_alias": model_alias,
            "manifest": manifest,
            "pilot_total_tokens": pilot_tokens,
            "pilot_total_cost_usd": round(pilot_cost, 4),
            "estimated_heldout_72_tokens": est_72_tokens,
            "estimated_heldout_72_cost_usd": est_72_cost,
            "runs": runs,
        }
        PILOT_OUT_JSON.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        print(
            f"\nPILOT COMPLETE: tokens={pilot_tokens:,} cost=${pilot_cost:.4f} | "
            f"ESTIMATED 72-ATTEMPT HELDOUT: tokens={est_72_tokens:,} cost=${est_72_cost:.4f}",
            flush=True,
        )
        return

    print(
        f"=== Running 72-attempt Held-Out Coverage Audit (12 x 3 x 2) on {model_alias} ===",
        flush=True,
    )
    res = await run_coverage_audit_suite(
        key_path=HELDOUT_KEY_PATH,
        model_alias=model_alias,
        trials=3,
        settings=settings,
        checkpoint_path=HELDOUT_OUT_JSON,
    )
    bundle = {
        "mode": "heldout_72",
        "commit_sha": commit_sha,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "model_alias": model_alias,
        "manifest": manifest,
        "routing_bar": evaluate_routing_bar(res["runs"]),
        "runs": res["runs"],
    }
    HELDOUT_OUT_JSON.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    HELDOUT_OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    HELDOUT_OUT_MD.write_text(_render_heldout_report(bundle), encoding="utf-8")
    print(f"\nWrote {HELDOUT_OUT_JSON} and {HELDOUT_OUT_MD}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["pilot", "heldout"], required=True)
    parser.add_argument("--model", default="eval-anthropic")
    args = parser.parse_args()
    asyncio.run(_main(mode=args.mode, model_alias=args.model))
