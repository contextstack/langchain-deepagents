"""Stage 3 runner: Fresh 144-attempt Triage Matrix (v3) with pinned commit & model provenance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import get_settings
from evals.submission_ab import run_submission_ab

DATASET_PATH = Path("evals/datasets/submissions.jsonl")
SMOKE_DATASET_PATH = Path("/tmp/triage_v3_smoke.jsonl")
OUT_JSON = Path("artifacts/experiments/triage_v3_matrix.json")
OUT_SMOKE_JSON = Path("artifacts/experiments/triage_v3_smoke.json")
OUT_MD = Path("docs/experiments/submission-triage-v3-results.md")


def _git_sha() -> str:
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render_report(bundle: dict[str, Any]) -> str:
    lines = [
        "# Submission Triage v3 — Paired A/B Rerun (Model Parity & Locked Scorer)",
        "",
        f"- **Pinned Commit:** `{bundle['commit_sha']}`",
        f"- **Timestamp (UTC):** `{bundle['timestamp_utc']}`",
        f"- **Fixtures SHA-256:** `{bundle['fixtures_sha256']}`",
        "- **Scorer:** `evals/submission_ab.py` (subject + full enclosing clause + typed facts)",
        (
            "- **Shared Budget & Controls:** Identical `chat_model` for investigation, "
            "summarization, Deep Agents coordinator/specialist, and synthesis; "
            "`general-purpose` disabled; alternating arm order; shared 12-model / 15-tool budget."
        ),
        "",
        "## Model Provenance Verification",
        "",
        "| Model Alias | Arm | Unique Requested Models | Unique Responded Models |",
        "|---|---|---|---|",
    ]
    for model_alias, res in bundle["results"].items():
        for variant in ("baseline", "deep_agents"):
            v_runs = [r for r in res["runs"] if r["variant"] == variant]
            req_m = sorted({m for r in v_runs for m in r.get("model_ids_requested", [])})
            resp_m = sorted({m for r in v_runs for m in r.get("model_ids_responded", [])})
            lines.append(
                f"| `{model_alias}` | `{variant}` | `{', '.join(req_m)}` | `{', '.join(resp_m)}` |"
            )

    lines.extend(
        [
            "",
            "## Aggregate Results (Attempted & Valid-Brief Denominators Reported Separately)",
            "",
            (
                "| Model | Arm | Tier | Attempted | Valid Briefs | Errors | "
                "Disp. Correct / Attempted | Facts Supported / Attempted | "
                "Facts Supported / Valid | Missed Esc. | Unnec. Esc. | "
                "Avg Calls | Avg Tokens | Total Cost | Avg Lat (s) |"
            ),
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for model_alias, res in bundle["results"].items():
        for variant in ("baseline", "deep_agents"):
            for tier in ("overall", "clean", "messy"):
                sub = [
                    r
                    for r in res["runs"]
                    if r["variant"] == variant and (tier == "overall" or r["tier"] == tier)
                ]
                att = len(sub)
                valid = sum(1 for r in sub if r.get("submission_brief") is not None)
                errs = sum(1 for r in sub if r.get("error"))
                corr = sum(1 for r in sub if r.get("disposition_correct"))
                facts = sum(1 for r in sub if r.get("facts_supported"))
                missed = sum(1 for r in sub if r.get("missed_escalation"))
                unnec = sum(1 for r in sub if r.get("unnecessary_escalation"))
                avg_calls = round(sum(r["llm_calls"] for r in sub) / att, 2) if att else 0.0
                avg_tok = round(sum(r["total_tokens"] for r in sub) / att) if att else 0
                tot_cost = round(sum(r["cost_usd"] for r in sub), 4)
                avg_lat = round(sum(r["duration_s"] for r in sub) / att, 2) if att else 0.0
                lines.append(
                    f"| `{model_alias}` | `{variant}` | {tier} | {att} | {valid} | {errs} | "
                    f"{corr}/{att} ({100.0 * corr / att:.1f}%) | "
                    f"{facts}/{att} ({100.0 * facts / att:.1f}%) | "
                    f"{facts}/{valid} | {missed} | {unnec} | "
                    f"{avg_calls} | {avg_tok:,} | \\${tot_cost:.4f} | {avg_lat} |"
                )
    return "\n".join(lines) + "\n"


async def _main(smoke: bool, resume_dgx: bool) -> None:
    settings = get_settings()
    settings.model_mode = "gateway"
    commit_sha = _git_sha()
    fixtures_sha = _sha256_file(DATASET_PATH)

    if smoke:
        raw_lines = [
            ln for ln in DATASET_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        subset = [ln for ln in raw_lines if json.loads(ln)["case_id"] in {"sub-01", "sub-06"}]
        SMOKE_DATASET_PATH.write_text("\n".join(subset) + "\n", encoding="utf-8")
        res = await run_submission_ab(
            dataset_path=SMOKE_DATASET_PATH,
            model_alias="eval-anthropic",
            trials=1,
            variants=("baseline", "deep_agents"),
            concurrency=2,
            settings=settings,
        )
        bundle = {
            "mode": "smoke",
            "commit_sha": commit_sha,
            "fixtures_sha256": fixtures_sha,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "results": {"eval-anthropic": res},
        }
        OUT_SMOKE_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_SMOKE_JSON.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        for r in res["runs"]:
            print(
                f"SMOKE {r['variant']:11s} {r['case_id']} | disp={r['disposition_correct']} "
                f"facts={r['facts_supported']} req={r['model_ids_requested']} "
                f"resp={r['model_ids_responded']} fail={r['citation_failures'][:1]}",
                flush=True,
            )
        return

    results: dict[str, Any] = {}
    route_provenance: dict[str, Any] = {}
    runner_sha = _sha256_file(Path(__file__))
    targets: tuple[tuple[str, int, float], ...] = (
        ("eval-anthropic", 4, 60.0),
        ("eval-dgx-coder", 1, 180.0),
    )
    if resume_dgx and OUT_JSON.exists():
        existing = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        if existing.get("fixtures_sha256") != fixtures_sha:
            raise ValueError("Refusing to merge: fixtures_sha256 mismatch with saved hosted block")
        hosted = (existing.get("results") or {}).get("eval-anthropic") or {}
        if len(hosted.get("runs") or []) != 72:
            raise ValueError("Refusing to merge: saved eval-anthropic block does not have 72 rows")
        results = dict(existing.get("results") or {})
        route_provenance = dict(existing.get("route_provenance") or {})
        commit_sha = str(existing.get("commit_sha") or commit_sha)
        targets = (("eval-dgx-coder", 1, 180.0),)

    exec_sha = _git_sha()
    for model_alias, conc, timeout_s in targets:
        settings.model_timeout_s = timeout_s
        print(
            f"\n=== Running full 72-attempt matrix on {model_alias} "
            f"(conc={conc}, timeout={timeout_s}s) ===",
            flush=True,
        )
        results[model_alias] = await run_submission_ab(
            dataset_path=DATASET_PATH,
            model_alias=model_alias,
            trials=3,
            variants=("baseline", "deep_agents"),
            concurrency=conc,
            settings=settings,
        )
        route_provenance[model_alias] = {
            "execution_code_sha": exec_sha,
            "runner_sha256": runner_sha,
            "effective_timeout_s": timeout_s,
            "concurrency": conc,
        }
        # Checkpoint partial results after each model
        bundle = {
            "mode": "full_144",
            "commit_sha": commit_sha,
            "fixtures_sha256": fixtures_sha,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "route_provenance": route_provenance,
            "results": results,
        }
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(_render_report(bundle), encoding="utf-8")
    print(f"\nWrote {OUT_JSON} and {OUT_MD}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--resume-dgx", action="store_true")
    args = parser.parse_args()
    asyncio.run(_main(smoke=args.smoke, resume_dgx=args.resume_dgx))
