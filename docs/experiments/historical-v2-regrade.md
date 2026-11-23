# Historical v2 Triage Run — Arm-Blinded Offline Claim Re-Grade (Provisional Automated Triage)

> **Status: Provisional Automated Triage Only.** The blinded packet counts (144 attempts, 126 briefs, 960 citations, 18 no-brief attempts) are verified, but `_grade_blinded_claim` in `scripts/regrade_historical_v2.py` (`f06349b`) is a heuristic first pass that does not yet bind `(subject, field, value, polarity)` tuples or separate comparative claims from missing-citation vs. false claims. Treat the claim counts below as provisional triage pending independent adjudication on the full-source review packet.

- **Source Artifact (preserved intact):** `deepagents_experiments_v2.json` (144 attempts: 126 generated briefs + 18 attempts without briefs)
- **Original Configuration Provenance:** Baseline ran with `SummarizationMiddleware(model=ops-fast)`; Deep Agents ran with `general-purpose` disabled and shared 12-model / 15-tool budget. Historical disposition and usage counts remain attached to that original configuration.
- **Grading Method:** **Automated deterministic audit** (`scripts/regrade_historical_v2.py`) executed strictly on the arm-blinded packet (`artifacts/experiments/historical_v2_blinded_review_packet.jsonl`, zero model/arm/trial/usage metadata). All 960 claim judgments are retained in `artifacts/experiments/historical_v2_adjudication.jsonl` for independent adjudication.

## Aggregate Results by Group (Attempted & Valid-Brief Denominators Reported Separately)

| Model | Arm | Tier | Attempted | Valid Briefs | No-Brief Failures | Disp. Correct / Attempted | All-Claims-Supported / Attempted | All-Claims-Supported / Valid Briefs | Claims Supported | Claims Contradicted | Claims Unverified |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| anthropic/claude-sonnet-5-5 | baseline | overall | 36 | 36 | 0 | 35/36 | 0/36 | 0/36 | 107/356 (30.1%) | 74 | 175 |
| anthropic/claude-sonnet-5-5 | baseline | clean | 12 | 12 | 0 | 11/12 | 0/12 | 0/12 | 29/135 (21.5%) | 30 | 76 |
| anthropic/claude-sonnet-5-5 | baseline | messy | 24 | 24 | 0 | 24/24 | 0/24 | 0/24 | 78/221 (35.3%) | 44 | 99 |
| anthropic/claude-sonnet-5-5 | deep_agents | overall | 36 | 34 | 2 | 33/36 | 0/36 | 0/34 | 105/354 (29.7%) | 52 | 197 |
| anthropic/claude-sonnet-5-5 | deep_agents | clean | 12 | 12 | 0 | 11/12 | 0/12 | 0/12 | 26/133 (19.5%) | 22 | 85 |
| anthropic/claude-sonnet-5-5 | deep_agents | messy | 24 | 22 | 2 | 22/24 | 0/24 | 0/22 | 79/221 (35.7%) | 30 | 112 |
| local/Qwen3-Coder-Next-FP8 (DGX Spark) | baseline | overall | 36 | 36 | 0 | 35/36 | 0/36 | 0/36 | 33/158 (20.9%) | 11 | 114 |
| local/Qwen3-Coder-Next-FP8 (DGX Spark) | baseline | clean | 12 | 12 | 0 | 11/12 | 0/12 | 0/12 | 2/37 (5.4%) | 1 | 34 |
| local/Qwen3-Coder-Next-FP8 (DGX Spark) | baseline | messy | 24 | 24 | 0 | 24/24 | 0/24 | 0/24 | 31/121 (25.6%) | 10 | 80 |
| local/Qwen3-Coder-Next-FP8 (DGX Spark) | deep_agents | overall | 36 | 20 | 16 | 16/36 | 0/36 | 0/20 | 4/92 (4.3%) | 7 | 81 |
| local/Qwen3-Coder-Next-FP8 (DGX Spark) | deep_agents | clean | 12 | 7 | 5 | 6/12 | 0/12 | 0/7 | 1/32 (3.1%) | 6 | 25 |
| local/Qwen3-Coder-Next-FP8 (DGX Spark) | deep_agents | messy | 24 | 13 | 11 | 10/24 | 0/24 | 0/13 | 3/60 (5.0%) | 1 | 56 |

## Blinded Spot-Review Adjudication (`R01`–`R12`)

- **Exact Reviewed Denominator:** `12/126` generated briefs (`12/144` total attempts, `91/960` claims). The remaining `114/126` briefs (`869/960` claims) and `18` no-brief attempts remain **explicitly provisional**.
- **Artifact:** `artifacts/experiments/historical_triage_spot_adjudication.json` (blinded; `arm-key.json` not opened).
- **Reviewed Claim Verdicts (`91` claims):** `supported=8`, `partial_missing_second_source=19`, `contradicted=13`, `unverified=51`.

| Review ID | Case | Claims | Supported | Partial (Missing 2nd Source) | Contradicted | Unverified |
|---|---|---:|---:|---:|---:|---:|
| `R01` | `sub-01` | 2 | 0 | 0 | 0 | 2 |
| `R02` | `sub-02` | 2 | 0 | 0 | 1 | 1 |
| `R03` | `sub-03` | 12 | 3 | 3 | 1 | 5 |
| `R04` | `sub-04` | 11 | 0 | 6 | 1 | 4 |
| `R05` | `sub-05` | 14 | 1 | 3 | 1 | 9 |
| `R06` | `sub-06` | 14 | 3 | 2 | 0 | 9 |
| `R07` | `sub-07` | 6 | 1 | 3 | 2 | 0 |
| `R08` | `sub-08` | 10 | 0 | 0 | 0 | 10 |
| `R09` | `sub-09` | 7 | 0 | 1 | 2 | 4 |
| `R10` | `sub-10` | 3 | 0 | 0 | 1 | 2 |
| `R11` | `sub-11` | 6 | 0 | 0 | 4 | 2 |
| `R12` | `sub-12` | 4 | 0 | 1 | 0 | 3 |
