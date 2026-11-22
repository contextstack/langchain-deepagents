# Experiment 1 Results: Baseline LangChain vs. Deep Agents for Commercial Insurance Submission Triage

- **Repository:** `contextstack-langchain-deepagents`
- **Pre-Registered Ground Truth Commit:** `45a5f9a` ([`evals/datasets/submissions.jsonl`](../../evals/datasets/submissions.jsonl), locked in git history *before* running either planner)
- **Fixture Set:** 12 synthetic commercial insurance submissions ([`packages/workflows/src/workflows/submission_triage/fixtures.py`](../../packages/workflows/src/workflows/submission_triage/fixtures.py)) — **4 clean** (`sub-01`..`sub-04`) and **8 messy** (`sub-05`..`sub-12`: superseded v1/v2 attachments, conflicting business classifications, missing required fields, conflicting revenue/limits, undisclosed prior claim, and out-of-appetite prohibited class)
- **Trials:** 3 trials per case per planner (36 runs/planner/model)
- **Grading:** 100% deterministic code grading ([`evals/submission_ab.py`](../../evals/submission_ab.py)) — no LLM-as-judge noise added on top of the 12-case sample.

---

## 1. Paired Summary Table (Clean vs. Messy)

### 1A. Frontier Model — `anthropic/claude-sonnet-5-5` (72 / 72 runs completed)

| Planner | Slice | Runs | Disposition Accuracy | Facts Supported | Missed Escalations | Unnecessary Escalations | Errors | Avg Calls | Calls / Correct | Tokens / Correct | Avg Latency | Cost / Correct (USD) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Baseline** (`create_agent`) | **Overall** | 36 | **36/36 (100.0%)** | **36/36 (100.0%)** | **0** | **0** | 0 | 4.00 | **4.00** | **11,369** | **19.53s** | **\$0.0678** |
| **Baseline** (`create_agent`) | Clean (4×3) | 12 | 12/12 (100.0%) | 12/12 (100.0%) | 0 | 0 | 0 | 4.00 | 4.00 | 11,276 | 21.02s | \$0.0691 |
| **Baseline** (`create_agent`) | Messy (8×3) | 24 | 24/24 (100.0%) | 24/24 (100.0%) | 0 | 0 | 0 | 4.00 | 4.00 | 11,416 | 18.79s | \$0.0672 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | **Overall** | 36 | **36/36 (100.0%)** | **36/36 (100.0%)** | **0** | **0** | 0 | 6.89 | **6.89** | **41,869** | **36.42s** | **\$0.1899** |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | Clean (4×3) | 12 | 12/12 (100.0%) | 12/12 (100.0%) | 0 | 0 | 0 | 7.33 | 7.33 | 44,773 | 38.39s | \$0.2004 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | Messy (8×3) | 24 | 24/24 (100.0%) | 24/24 (100.0%) | 0 | 0 | 0 | 6.67 | 6.67 | 40,416 | 35.44s | \$0.1846 |

**Frontier Delta (Deep Agents vs. Baseline):**
- Accuracy & evidence grounding: **0.0 pp gain** (both 36/36 = 100%)
- Model calls per correct disposition: **+72%** (`6.89` vs `4.00`)
- Tokens per correct disposition: **3.68×** (`41,869` vs `11,369`)
- Latency: **1.86× slower** (`36.42s` vs `19.53s`)
- Cost per correct disposition: **2.80× more expensive** (`\$0.1899` vs `\$0.0678`; `\$6.84` vs `\$2.44` total)

---

### 1B. Local On-Prem DGX Spark — `Qwen3-Coder-Next-FP8` (36 Baseline + 11 Deep Agents runs before runaway abort)

> [!NOTE]
> Latency on a single local DGX Spark under 3-way concurrency is not directly comparable to a hosted API; **model calls and tokens per correct disposition**, plus **separate timeout/schema error counts**, are the comparable operational metrics.

| Planner | Slice | Runs | Disposition Accuracy | Facts Supported | Missed Escalations | Unnecessary Escalations | Timeout / Tool Errors | Calls / Correct | Tokens / Correct | External API Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Baseline** (`create_agent`) | **Overall** | 36 | **35/36 (97.2%)** | 23/36 (63.9%) | **0** | 1 | **0** | **7.74** | **11,328** | **\$0.00** |
| **Baseline** (`create_agent`) | Clean (4×3) | 12 | 11/12 (91.7%) | 2/12 (16.7%)\* | 0 | 1 | 0 | 8.36 | 11,764 | \$0.00 |
| **Baseline** (`create_agent`) | Messy (8×3) | 24 | **24/24 (100.0%)** | 21/24 (87.5%) | 0 | 0 | 0 | 7.46 | 11,128 | \$0.00 |
| **Deep Agents** (unbounded, aborted on run #11) | **Observed (11)** | 11 | **3/11 (27.3%)** | 4/11 (36.4%) | **0** | 3 | **5** (`OpenAITimeoutError`) | 192.33\*\* | 10,659,846\*\* | \$0.00 |
| **Deep Agents** | Clean (10/12) | 10 | 3/10 (30.0%) | 3/10 (30.0%) | 0 | 2 | 5 | 25.33 | 96,881 | \$0.00 |
| **Deep Agents** | Messy (1/24) | 1 | 0/1 (0.0%) | 1/1 (100.0%) | 0 | 1 | 0 (runaway loop) | — | — | \$0.00 |

\* On clean cases, `Qwen3-Coder-Next-FP8` often formatted dollar figures cleanly as integers in structured fields (`annual_revenue_usd=4200000`) while writing `"$4.2M"` instead of `"4,200,000"` in prose, causing our strict comma-formatted keyword substring check (`"4,200,000"`) to mark `facts=False` despite valid citations and correct dispositions.
\*\* On `sub-05` t1 (superseded v1/v2 attachment), `create_deep_agent` without explicit `ModelCallLimitMiddleware` entered a **501-call, 31,688,895-token, 54.5-minute (`3,267.5s`) runaway loop** and still returned the wrong disposition (`human_review` instead of `ready_to_quote`). The sweep was aborted after run #11 and `ModelCallLimitMiddleware` + `ToolCallLimitMiddleware` were subsequently attached to `create_deep_agent` and its `evidence_auditor` subagent.

---

## 2. Per-Case 3-Trial Side-by-Side Matrix

Showing all 3 trials (`t1 / t2 / t3`) per case exposes single-trial variance directly instead of hiding it behind averages (`✓` = exact disposition match; `ask` / `rev` = wrong disposition; `TO` = `OpenAITimeoutError`; `—` = not run after runaway abort):

| Case ID | Tier | Scenario | Expected | Claude 5.5 Baseline | Claude 5.5 Deep Agents | DGX Qwen3 Baseline | DGX Qwen3 Deep Agents |
|---|---|---|---|---|---|---|---|
| `sub-01` | clean | SaaS Tech E&O | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `TO / TO / TO` |
| `sub-02` | clean | Light Manufacturing | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ask / ✓` | `— / TO / TO` |
| `sub-03` | clean | IT & Mgmt Consulting | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `ask / ask / ✓` |
| `sub-04` | clean | Commercial Warehouse | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / —` |
| `sub-05` | messy | Superseded v2 clean | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `rev (501 calls!) / — / —` |
| `sub-06` | messy | Conflicting class (Roofing) | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |
| `sub-07` | messy | Missing eff. date & loss run | `ask_broker` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |
| `sub-08` | messy | Out of appetite (Fireworks) | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |
| `sub-09` | messy | Conflicting rev. & $10M cap | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |
| `sub-10` | messy | Missing FEIN & schedule | `ask_broker` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |
| `sub-11` | messy | Superseded v2 $420k claim | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |
| `sub-12` | messy | Missing WC payroll schedule | `ask_broker` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `—` |

---

## 3. Reproducing Locally

```bash
# Run Experiment 2 HTTP carrier recovery tests (offline, $0)
uv run pytest -v tests/test_exp2_carrier_recovery.py

# Run Experiment 1 A/B via LiteLLM Proxy
MODEL_MODE=gateway uv run python -c "
import asyncio
from pathlib import Path
from evals.submission_ab import run_submission_ab
print(asyncio.run(run_submission_ab(
    dataset_path=Path('evals/datasets/submissions.jsonl'),
    model_alias='eval-anthropic',
    trials=3,
))['summary'])
"
```
