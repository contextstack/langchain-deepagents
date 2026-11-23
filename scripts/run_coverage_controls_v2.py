"""Step 3 runner: 2 missing control packets x 3 trials x 2 arms = 12 hosted eval-anthropic runs.

Strictly separate run ID, artifacts, and report from the original 72-attempt corpus.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.config import get_settings
from core.models import RunContext
from evals.coverage_audit import build_coverage_schedule
from evals.coverage_audit_v3 import score_issue_audit_brief
from evals.metrics import UsageTrackingCallback
from workflows.coverage_audit.graph import CoverageAuditDeps, run_issue_audit_attempt
from workflows.coverage_audit.models import IssueAuditBrief

RUN_ID = "cov-ctrl-v2-20261123"
ARCHIVE_ROOT = Path("packages/workflows/src/workflows/coverage_audit/policy-archive-controls")
KEY_PATH = Path("evals/datasets/coverage_audit_controls_v2_key.json")
MANIFEST_PATH = Path("evals/datasets/coverage_audit_controls_v2_manifest.json")
SCORER_PATH = Path("evals/coverage_audit_v3.py")
OUT_JSON = Path("artifacts/experiments/coverage_audit_controls_v2_12runs.json")
OUT_MD = Path("docs/experiments/coverage-discrepancy-controls-v2-results.md")


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _render_report(bundle: dict[str, Any]) -> str:
    runs = bundle["runs"]
    lines = [
        "# Coverage Discrepancy Audit — Two Missing Control Packets (`CTRL-C04` & `CTRL-B08`)",
        "",
        (
            "> **Separate Report:** Reported separately from the original 12-packet held-out "
            "corpus because these two control packets use the issue-level output schema "
            "(`IssueAuditBrief`) and new synthetic cases."
        ),
        "",
        f"- **Run ID:** `{bundle['run_id']}`",
        f"- **Pinned Commit:** `{bundle['commit_sha']}`",
        f"- **Timestamp (UTC):** `{bundle['timestamp_utc']}`",
        f"- **Route / Model Alias:** `{bundle['model_alias']}` (`concurrency=1`, alternating arms)",
        f"- **Fixture Key SHA-256:** `{bundle['manifest']['key_sha256']}`",
        f"- **Rubric SHA-256 (`evals/coverage_audit_v3.py`):** `{bundle['rubric_sha256']}`",
        (
            "- **Effective Caps:** `max_model_calls=16`, `max_tool_calls=32`, "
            "`max_total_tokens=96000`, `timeout_s=180.0`, `model_timeout_s=60.0`, "
            "`model_max_tokens=4096`."
        ),
        f"- **Total Actual Spend (12 attempts):** \\${bundle['total_cost_usd']:.4f} "
        f"(prior estimate: ~\\$1.69)",
        "",
        "## Model Provenance Verification",
        "",
        "| Arm | Unique Requested Models | Unique Responded Models |",
        "|---|---|---|",
    ]
    for variant in ("baseline", "deep_agents"):
        vr = [r for r in runs if r["variant"] == variant]
        req = sorted({m for r in vr for m in r.get("model_ids_requested", [])})
        resp = sorted({m for r in vr for m in r.get("model_ids_responded", [])})
        lines.append(f"| `{variant}` | `{', '.join(req)}` | `{', '.join(resp)}` |")

    lines.extend(
        [
            "",
            "## Per-Packet Results (3 Trials per Arm)",
            "",
            (
                "| Packet | Tier | Arm | Attempted | Verified Pass | Issues Passed | "
                "False Def. | Errors | Avg Calls | Avg Tools | Avg Tokens | Cost | Avg Lat (s) |"
            ),
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for pid in ("CTRL-C04", "CTRL-B08"):
        tier = next(r["tier"] for r in runs if r["packet_id"] == pid)
        for variant in ("baseline", "deep_agents"):
            sub = [r for r in runs if r["packet_id"] == pid and r["variant"] == variant]
            att = len(sub)
            vp = sum(1 for r in sub if r["passed"])
            ip = sum(r["issues_passed"] for r in sub)
            it = sum(r["issues_total"] for r in sub)
            fdef = sum(1 for r in sub if r["false_definitive_on_insufficient"])
            errs = sum(1 for r in sub if r["error"])
            ac = round(sum(r["llm_calls"] for r in sub) / att, 2)
            at = round(sum(r["tool_calls"] for r in sub) / att, 2)
            atok = round(sum(r["total_tokens"] for r in sub) / att)
            cost = round(sum(r["cost_usd"] for r in sub), 4)
            lat = round(sum(r["duration_s"] for r in sub) / att, 2)
            lines.append(
                f"| `{pid}` | {tier} | `{variant}` | {att} | {vp}/{att} | {ip}/{it} | "
                f"{fdef} | {errs} | {ac} | {at} | {atok:,} | \\${cost:.4f} | {lat} |"
            )

    lines.extend(
        [
            "",
            "## Per-Trial Breakdown",
            "",
            "| Trial | Packet | Arm | Passed | Issues | Calls | Tools | Tokens | Cost | Lat (s) |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for r in runs:
        lines.append(
            f"| {r['trial']} | `{r['packet_id']}` | `{r['variant']}` | `{r['passed']}` | "
            f"{r['issues_passed']}/{r['issues_total']} | {r['llm_calls']} | {r['tool_calls']} | "
            f"{r['total_tokens']:,} | \\${r['cost_usd']:.4f} | {r['duration_s']} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    settings = get_settings()
    settings.model_mode = "gateway"
    cases: list[dict[str, Any]] = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    commit_sha = _git_sha()
    rubric_sha = hashlib.sha256(SCORER_PATH.read_bytes()).hexdigest()
    model_alias = "eval-anthropic"

    variants: tuple[Literal["baseline", "deep_agents"], ...] = ("baseline", "deep_agents")
    schedule = build_coverage_schedule(cases, trials=3, variants=variants)
    assert len(schedule) == 12

    runs: list[dict[str, Any]] = []
    for idx, (variant, case, trial) in enumerate(schedule, 1):
        cb = UsageTrackingCallback()
        doc_reads: list[dict[str, Any]] = []
        deps = CoverageAuditDeps(
            settings=settings,
            planner_variant=variant,
            primary_model_alias=model_alias,
            archive_root=ARCHIVE_ROOT,
            callbacks=[cb],
        )
        ctx = RunContext(
            tenant_id="tenant-alpha",
            requester_id="auditor",
            requester_roles=frozenset({"underwriter"}),
            thread_id=f"{RUN_ID}-{variant}-{case['packet_id']}-t{trial}",
            run_id=f"{RUN_ID}-{variant}-{case['packet_id']}-t{trial}",
            request_id=f"req-{RUN_ID}-{idx:02d}",
        )
        t0 = time.monotonic()
        brief_dump: dict[str, Any] | None = None
        error: str | None = None
        try:
            out = await run_issue_audit_attempt(
                packet_id=case["packet_id"],
                prompt=case["prompt"],
                deps=deps,
                context=ctx,
                read_log=doc_reads,
            )
            brief_obj = IssueAuditBrief.model_validate(out["brief"])
            brief_dump = brief_obj.model_dump(mode="json")
            scores = score_issue_audit_brief(case, brief_obj, archive_root=ARCHIVE_ROOT)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            scores = {
                "scorer_version": "v3_issue",
                "packet_id": case["packet_id"],
                "passed": False,
                "issues_passed": 0,
                "issues_total": len(case["expected_issues"]),
                "false_definitive_on_insufficient": False,
                "issue_results": [],
            }

        dt = round(time.monotonic() - t0, 2)
        cost = cb.stats.estimate_cost_usd(model_alias)
        row = {
            "run_id": RUN_ID,
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
            f"[{tag} {idx:02d}/12] {variant:11s} | {case['packet_id']} t{trial}: "
            f"issues={scores['issues_passed']}/{scores['issues_total']} "
            f"calls={cb.stats.llm_calls} tools={cb.stats.tool_calls} "
            f"tok={cb.stats.total_tokens} cost=${cost:.4f} ({dt}s)"
            + (f" ERR={error[:70]}" if error else ""),
            flush=True,
        )

    total_cost = round(sum(r["cost_usd"] for r in runs), 4)
    bundle = {
        "run_id": RUN_ID,
        "commit_sha": commit_sha,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "model_alias": model_alias,
        "concurrency": 1,
        "effective_settings": {
            "max_model_calls": 16,
            "max_tool_calls": 32,
            "max_total_tokens": 96_000,
            "timeout_s": 180.0,
            "model_timeout_s": settings.model_timeout_s,
            "model_max_tokens": settings.model_max_tokens,
        },
        "manifest": manifest,
        "rubric_sha256": rubric_sha,
        "total_cost_usd": total_cost,
        "total_tokens": sum(r["total_tokens"] for r in runs),
        "runs": runs,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(_render_report(bundle), encoding="utf-8")
    print(f"\nCOMPLETE: total_tokens={bundle['total_tokens']:,} actual_cost=${total_cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
