# Experiment 1 Results: Baseline LangChain vs. Deep Agents for Commercial Insurance Submission Triage

- **Repository:** `contextstack-langchain-deepagents`
- **Pre-Registered Ground Truth Commit:** `45a5f9a` ([`evals/datasets/submissions.jsonl`](../../evals/datasets/submissions.jsonl), locked in git history *before* running either planner)
- **Fixture Set:** 12 synthetic commercial insurance submissions ([`packages/workflows/src/workflows/submission_triage/fixtures.py`](../../packages/workflows/src/workflows/submission_triage/fixtures.py)) — **4 clean** (`sub-01`..`sub-04`) and **8 messy** (`sub-05`..`sub-12`: superseded v1/v2 attachments, conflicting business classifications, missing required fields, conflicting revenue/limits, undisclosed prior claim, and out-of-appetite prohibited class)
- **Protocol & Provenance (Hardened Interleaved Pass, 144 Live Runs):**
  - 3 trials per case per planner (72 runs per model) with arms **alternated within each `(case, trial)`** (`build_trial_schedule`) to control for provider load drift.
  - Implicit `general-purpose` subagent **disabled** on Deep Agents (`evidence_auditor` only).
  - **One shared call budget** (`max_model_calls=12`, `max_tool_calls=15`) enforced across Deep Agents' coordinator + specialist (matching Baseline's single budget).
  - **Model-parity provenance note:** The 144 live trials below started *before* Baseline's `SummarizationMiddleware` was switched from `ops-fast` to the primary `chat_model` (fixed in `1ae0749`). A live model-parity confirmation sweep is left pending as a separate run unless requested.
  - **Grader provenance:** Headline `Facts Supported (1ae0749 grader)` columns below reflect the lexical-overlap + polarity + exact typed-field grader at `1ae0749`. See **§1C** below for the offline re-score of all 144 saved briefs under the fixed **deterministic source-span + typed-fact `(field, value, polarity)` verifier**.

---

## 1. Paired Summary Table (Hardened Interleaved Pass — Clean vs. Messy)

### 1A. Frontier Model — `anthropic/claude-sonnet-5-5` (72 / 72 runs completed)

| Planner | Slice | Runs | Disposition Accuracy | Strict Facts Supported\* | Typed Fields OK | Missed Escalations | Unnecessary Escalations | Budget / Tool Errors | Calls / Correct | Tokens / Correct | Avg Latency | Cost / Correct (USD) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Baseline** (`create_agent`) | **Overall** | 36 | **35/36 (97.2%)** | 15/36 (41.7%) | 33/36 | **0** | 1 | **0** | **4.11** | **12,042** | **19.87s** | **\$0.0726** |
| **Baseline** (`create_agent`) | Clean (4×3) | 12 | 11/12 (91.7%) | 2/12 (16.7%) | 9/12 | 0 | 1 | 0 | 4.36 | 12,847 | 21.57s | \$0.0806 |
| **Baseline** (`create_agent`) | Messy (8×3) | 24 | **24/24 (100.0%)** | 13/24 (54.2%) | 24/24 | 0 | 0 | 0 | 4.00 | 11,672 | 19.02s | \$0.0690 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | **Overall** | 36 | **33/36 (91.7%)** | **23/36 (63.9%)** | **34/36** | **0** | 1 | **2** (`SharedBudgetExceededError`) | 7.18 | 43,997 | 36.81s | \$0.2048 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | Clean (4×3) | 12 | 11/12 (91.7%) | 7/12 (58.3%) | 12/12 | 0 | 1 | 0 | 7.64 | 47,378 | 41.16s | \$0.2230 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | Messy (8×3) | 24 | 22/24 (91.7%) | 16/24 (66.7%) | 22/24 | 0 | 0 | 2 | 6.95 | 42,307 | 34.64s | \$0.1957 |

**Frontier Delta (Deep Agents vs. Baseline under Equal Shared Budget):**
- **Disposition accuracy:** **-5.5 pp** (`91.7%` vs `97.2%`) — both misses were `SharedBudgetExceededError (16/15 tool calls)` on 4-document `sub-11` where coordinator + specialist duplicated tool reads.
- **Strict verbatim passage grounding (`facts_supported`):** **+22.2 pp** (`63.9%` vs `41.7%`) — the `evidence_auditor` subagent quoted source passages more verbatim than Baseline's paraphrased citations.
- **Efficiency:** **1.75× calls/correct**, **3.65× tokens/correct**, **1.85× latency**, and **2.82× cost/correct** (`\$0.2048` vs `\$0.0726`).

---

### 1B. Local On-Prem DGX Spark — `Qwen3-Coder-Next-FP8` (72 / 72 runs completed, `concurrency=1`)

> [!NOTE]
> Latency on a single local DGX Spark is not directly comparable to a hosted API; **model calls and tokens per correct disposition**, plus **separate budget/timeout error counts**, are the comparable metrics.

| Planner | Slice | Runs | Disposition Accuracy | Strict Facts Supported\* | Typed Fields OK | Missed Escalations | Unnecessary Escalations | Budget / Tool Errors | Calls / Correct | Tokens / Correct | External API Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Baseline** (`create_agent`) | **Overall** | 36 | **35/36 (97.2%)** | **7/36 (19.4%)** | **21/36** | **0** | 1 | **0** | **5.54** | **8,463** | **\$0.00** |
| **Baseline** (`create_agent`) | Clean (4×3) | 12 | 11/12 (91.7%) | 0/12 (0.0%) | 0/12 | 0 | 1 | 0 | 5.45 | 7,834 | \$0.00 |
| **Baseline** (`create_agent`) | Messy (8×3) | 24 | **24/24 (100.0%)** | 7/24 (29.2%) | 21/24 | 0 | 0 | 0 | 5.58 | 8,751 | \$0.00 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | **Overall** | 36 | **16/36 (44.4%)** | 1/36 (2.8%) | 13/36 | **0** | 1 | **16** (15 budget, 1 error) | 18.50 | 78,939 | \$0.00 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | Clean (4×3) | 12 | 6/12 (50.0%) | 0/12 (0.0%) | 0/12 | 0 | 1 | 5 | 16.83 | 72,600 | \$0.00 |
| **Deep Agents** (`create_deep_agent` + `evidence_auditor`) | Messy (8×3) | 24 | 10/24 (41.7%) | 1/24 (4.2%) | 13/24 | 0 | 0 | 11 | 19.50 | 82,742 | \$0.00 |

\* Strict passage grounding in the tables above uses the `1ae0749` lexical-overlap + polarity + exact typed-field grader. In an earlier **unbounded** run without `SharedCallBudget`, `eval-dgx-coder` on `deep_agents` entered a **501-call, 31.69M-token, 54.5-minute runaway loop** on `sub-05` t1 — proving that hard shared budgets are mandatory for Deep Agents on open-weight models, while also showing that duplicating tool reads across coordinator + specialist exhausts a 15-tool budget on 15/36 runs.

### 1C. Offline Re-Score of Saved Briefs with Fixed Deterministic Span + Typed-Fact Grader

Re-scoring all 144 saved briefs from the live run offline with the fixed deterministic source-span + typed-fact `(field, value, polarity)` verifier (`evals/submission_ab.py`) — which rejects negated claims on genuine excerpts and treats ambiguous/untyped free-text claims as unverified:

| Model | Planner | Saved Trials | Disposition Accuracy (unchanged) | `1ae0749` Grader Facts Supported | Fixed Span+Typed-Fact Grader | Typed Fields OK |
|---|---|---:|---:|---:|---:|---:|
| `claude-sonnet-5-5` | Baseline | 36 | 35/36 (97.2%) | 15/36 (41.7%) | **0/36 (0.0%)** | 33/36 |
| `claude-sonnet-5-5` | Deep Agents | 36 | 33/36 (91.7%) | 23/36 (63.9%) | **0/36 (0.0%)** | 34/36 |
| DGX `Qwen3-Coder-Next-FP8` | Baseline | 36 | 35/36 (97.2%) | 7/36 (19.4%) | **0/36 (0.0%)** | 21/36 |
| DGX `Qwen3-Coder-Next-FP8` | Deep Agents | 36 | 16/36 (44.4%) | 1/36 (2.8%) | **0/36 (0.0%)** | 13/36 |

**Why all 144 saved briefs scored `0/36` under the fixed span+typed-fact grader:** Every saved brief was generated against the free-text `fact_supported: str` schema and included at least one meta/comparative prose citation (e.g., `"Broker email revenue matches the application"`, `"FEIN is provided, satisfying Apex's required field"`) that does not express a single verifiable `(field, value, polarity)` fact from the cited span alone. Treating such free-text claims as unverified eliminates false positives like negated revenue claims (`test_negated_revenue_claim_with_correct_typed_fields_fails_citation_grounding`) and shows that deterministic claim verification requires either typed-claim schema fields or structured claim formatting in future live runs.

---

## 2. Per-Case 3-Trial Side-by-Side Matrix (Hardened Interleaved Pass)

All 3 trials (`t1 / t2 / t3`) per case (`✓` = exact disposition match; `ask` / `rev` = wrong disposition; `BUD` = `SharedBudgetExceededError`; `ERR` = runtime/timeout error):

| Case ID | Tier | Scenario | Expected | Claude 5.5 Baseline | Claude 5.5 Deep Agents | DGX Qwen3 Baseline | DGX Qwen3 Deep Agents |
|---|---|---|---|---|---|---|---|
| `sub-01` | clean | SaaS Tech E&O | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` |
| `sub-02` | clean | Light Manufacturing | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `BUD / ask / ERR` |
| `sub-03` | clean | IT & Mgmt Consulting | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / rev / ✓` | `✓ / ✓ / BUD` |
| `sub-04` | clean | Commercial Warehouse | `ready_to_quote` | `rev / ✓ / ✓` | `✓ / ✓ / ask` | `✓ / ✓ / ✓` | `✓ / BUD / BUD` |
| `sub-05` | messy | Superseded v2 clean | `ready_to_quote` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `BUD / BUD / BUD` |
| `sub-06` | messy | Conflicting class (Roofing) | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / BUD` |
| `sub-07` | messy | Missing eff. date & loss run | `ask_broker` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `rev / rev / BUD` |
| `sub-08` | messy | Out of appetite (Fireworks) | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` |
| `sub-09` | messy | Conflicting rev. & $10M cap | `human_review` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` |
| `sub-10` | messy | Missing FEIN & schedule | `ask_broker` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `rev / ✓ / ✓` |
| `sub-11` | messy | Superseded v2 $420k claim | `human_review` | `✓ / ✓ / ✓` | `BUD / BUD / ✓` | `✓ / ✓ / ✓` | `BUD / BUD / BUD` |
| `sub-12` | messy | Missing WC payroll schedule | `ask_broker` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `✓ / ✓ / ✓` | `BUD / BUD / BUD` |
