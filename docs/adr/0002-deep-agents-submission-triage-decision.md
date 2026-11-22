# ADR 0002: Deep Agents vs. Bounded LangChain Planner for Commercial Insurance Submission Triage

- **Status:** Accepted
- **Date:** 2026-11-22
- **Evidence:** [`docs/experiments/submission-triage-results.md`](../experiments/submission-triage-results.md), [`docs/experiments/carrier-recovery-trace.md`](../experiments/carrier-recovery-trace.md)

---

## 1. Decision

**Keep `deepagents` (`create_deep_agent`) outside this bounded submission-triage workflow.** Use the existing bounded LangChain v1 `create_agent` planner (with `PIIMiddleware`, `ModelCallLimitMiddleware`, and `ToolCallLimitMiddleware`) inside the shared LangGraph governed `StateGraph` for both clean and messy submissions.

Reserve `deepagents` for open-ended, multi-file, long-horizon tasks (deep research, repository-scale coding, multi-hour autonomous analysis) where dynamic planning, virtual filesystems, and isolated subagent context windows are genuinely required — and always attach hard call/token guardrails when running `deepagents` on local or open-weight models.

---

## 2. Why (Empirical Evidence)

1. **Zero accuracy gain on a frontier model at 2.8× the cost and 1.86× the latency (`anthropic/claude-sonnet-5-5`, 72 runs):**
   - Both Baseline (`create_agent`) and Deep Agents (`create_deep_agent` + `evidence_auditor` subagent) scored **36/36 (100%)** on disposition accuracy and **36/36 (100%)** on deterministic evidence-supported facts across all 4 clean and 8 messy cases (`0` missed escalations, `0` unnecessary escalations).
   - Deep Agents required **6.89 vs. 4.00 model calls**, **41,869 vs. 11,369 tokens per correct disposition (3.68×)**, **36.42s vs. 19.53s latency (1.86×)**, and **\$0.1899 vs. \$0.0678 per correct disposition (2.80×)**.

2. **Severe harness overhead and runaway loop risk on a local open-weight model (`Qwen3-Coder-Next-FP8` on DGX Spark):**
   - Baseline scored **35/36 (97.2%)** overall (**24/24 = 100% on messy cases**, `0` missed escalations, `0` errors, `11,328` tokens per correct disposition at **\$0 external API spend**).
   - Deep Agents inflated context ~3.5× with its built-in filesystem/todo/subagent harness prompt, triggering **5/11 request timeouts** under 3-way concurrency, **2 unnecessary escalations on clean `sub-03`**, and a **501-call / 31.69M-token / 54.5-minute runaway loop** on `sub-05` t1 when `ModelCallLimitMiddleware` was omitted.

3. **Asymmetric underwriting error cost favors deterministic outer governance over heavier planners:**
   - In commercial insurance triage, a **missed escalation** (marking a conflicting or out-of-appetite submission `ready_to_quote`) is far more expensive than an unnecessary escalation. Both Baseline models achieved **0 missed escalations across all 72 Baseline runs** (36 Claude 5.5 + 36 DGX Qwen3), and the shared LangGraph HITL approval gate still sits in front of every external carrier `POST /v1/quotes`.

4. **Ambiguous downstream writes are a state-machine problem, not a planner problem (Experiment 2):**
   - Recovering from a carrier that commits then withholds its HTTP response required a durable `write_attempted` flag, a stable `operation_id`, a `GET /v1/quotes/{operation_id}` reconciliation node, and a human-review `interrupt()` gate (`await_reconciliation_review`) that structurally bars duplicate `POST` retries (`http_post_attempts == 1`, `committed_writes == 1` across both behaviors). Neither planner changes that guarantee.

---

## 3. Experimental Limits to Note

- **Confounded variable if Deep Agents *had* won:** Our Deep Agents arm combined both the `deepagents` harness *and* a second `evidence_auditor` verification pass, while Baseline had one pass. Because Baseline already achieved **100% messy-case accuracy on both Claude Sonnet 5.5 (24/24) and DGX Qwen3 (24/24)**, we did not need a third `baseline_plus_verify` arm to refute a Deep Agents advantage here. On a harder, multi-dozen-document dataset where Baseline misses contradictions, a `baseline_plus_verify` arm should be run first before adopting `deepagents`.
- **Small sample size (12 cases: 4 clean, 8 messy):** Each messy case represents 12.5 percentage points of messy accuracy; we therefore report the full 3-trial per-case matrix alongside aggregate tables.
- **No A2A adapter yet:** Experiment 2 intentionally validated HTTP recovery first; an A2A partner adapter should be evaluated only if a partner requires asynchronous task identity/status semantics beyond HTTP `operation_id` lookup.
