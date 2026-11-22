# ADR 0002: Deep Agents vs. Bounded LangChain Planner for Commercial Insurance Submission Triage

- **Status:** Accepted
- **Date:** 2026-11-22
- **Evidence:** [`docs/experiments/submission-triage-results.md`](../experiments/submission-triage-results.md), [`docs/experiments/carrier-recovery-trace.md`](../experiments/carrier-recovery-trace.md)

---

## 1. Decision

**Keep `deepagents` (`create_deep_agent`) outside this bounded submission-triage workflow.** Use the existing bounded LangChain v1 `create_agent` planner (with `PIIMiddleware`, `ModelCallLimitMiddleware`, and `ToolCallLimitMiddleware`) inside the shared LangGraph governed `StateGraph` for both clean and messy submissions.

Reserve `deepagents` for open-ended, multi-file, long-horizon tasks (deep research, repository-scale coding, multi-hour autonomous analysis) where dynamic planning, virtual filesystems, and isolated subagent context windows are genuinely required — and always enforce a **single shared call budget** (`SharedCallBudget`) across coordinator and subagents.

---

## 2. Why (Empirical Evidence from 144 Hardened Interleaved Live Runs)

1. **Lower disposition accuracy and 2.82× higher cost on a frontier model under an equal shared budget (`anthropic/claude-sonnet-5-5`, 72 interleaved runs):**
   - Baseline (`create_agent`) scored **35/36 (97.2%)** overall (**24/24 = 100% on messy cases**, `0` missed escalations, `0` errors) at **4.11 calls/correct**, **12,042 tokens/correct**, **19.87s avg**, and **\$0.0726 / correct disposition**.
   - Deep Agents (`create_deep_agent` + `evidence_auditor`, `general-purpose` disabled, equal shared 12-model / 15-tool budget) scored **33/36 (91.7%)** overall (**22/24 on messy cases**, `2` `SharedBudgetExceededError` failures on 4-document `sub-11` where coordinator + specialist duplicated tool reads) at **7.18 calls/correct**, **43,997 tokens/correct (3.65×)**, **36.81s avg (1.85×)**, and **\$0.2048 / correct disposition (2.82×)**.
   - **Where Deep Agents appeared to help (and grader caveat):** Under the `1ae0749` lexical-overlap + polarity grader, Deep Agents' `evidence_auditor` subagent scored higher on verbatim quote grounding (`23/36 = 63.9%` vs. Baseline `15/36 = 41.7%`). However, re-scoring all 144 saved briefs offline with the fixed deterministic source-span + typed-fact `(field, value, polarity)` verifier yields `0/36` across all arms because the saved briefs used free-text comparative prose in `fact_supported` (now strictly treated as unverified). Either way, paying 2.82× cost, 3.65× tokens, and losing disposition accuracy under a shared tool budget is a poor trade compared to typed-claim extraction or a lightweight verification pass.

2. **Severe budget exhaustion and runaway risk on a local open-weight model (`Qwen3-Coder-Next-FP8` on DGX Spark, 72 interleaved runs):**
   - Baseline scored **35/36 (97.2%)** overall (**24/24 = 100% on messy cases**, `0` missed escalations, `0` errors, **5.54 calls/correct**, **8,463 tokens/correct**, **\$0.00 external API spend**).
   - Deep Agents scored **16/36 (44.4%)** with **16 errors** (15 `SharedBudgetExceededError`s as coordinator + specialist duplicated reads on multi-document cases) and **78,939 tokens/correct (9.3× Baseline)**. Before adding `SharedCallBudget`, an unbounded run hit a **501-call / 31.69M-token / 54.5-minute runaway loop** on `sub-05` t1.

3. **Asymmetric underwriting error cost favors deterministic outer governance over heavier planners:**
   - In commercial insurance triage, a **missed escalation** (marking a conflicting or out-of-appetite submission `ready_to_quote`) is far more expensive than an unnecessary escalation. Both Baseline models achieved **0 missed escalations across all 72 Baseline runs** (36 Claude 5.5 + 36 DGX Qwen3), and the shared LangGraph policy + HITL approval gate enforces explicit carrier/coverage fields, authenticated approver roles (`APPROVER_ROLES`), and separation of duties before any external carrier `POST /v1/quotes`.

4. **Ambiguous downstream writes are a state-machine problem, not a planner problem (Experiment 2):**
   - Recovering safely from a carrier that commits then withholds its response — including a real OS process kill (`os._exit(137)`) and fresh-process PostgreSQL resume — required a durable pre-send `carrier_dispatch_log` table, a stable `operation_id`, a `GET /v1/quotes/{operation_id}` reconciliation node, and a human-review `interrupt()` gate (`await_reconciliation_review`) that structurally bars duplicate `POST` retries (`http_post_attempts == 1`, `committed_writes == 1` across all recovery drills).

---

## 3. Experimental Limits & Provenance

- **Baseline summarizer provenance:** The 144-trial live run started before Baseline switched its `SummarizationMiddleware` model from `ops-fast` to the primary `chat_model` (fixed in `1ae0749`). A live model-parity confirmation run is left pending as a separate run if requested.
- **Confounded variable on verbatim quoting:** Because Deep Agents combined the `deepagents` harness with a dedicated `evidence_auditor` pass, any quoting lift under the `1ae0749` grader could likely be matched by a much cheaper `baseline_plus_verify` single-call verification step. On a harder, multi-dozen-document dataset where Baseline misses contradictions, run `baseline_plus_verify` before adopting `deepagents`.
- **Small sample size (12 cases: 4 clean, 8 messy):** Each messy case represents 12.5 percentage points of messy accuracy; we therefore publish the full 3-trial per-case matrix alongside aggregate tables.
- **No A2A adapter yet:** Experiment 2 intentionally validated HTTP recovery first; an A2A partner adapter should be evaluated only if a partner requires asynchronous task identity/status semantics beyond HTTP `operation_id` lookup.
