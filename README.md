# ContextStack Enterprise LangChain — Deep Agents Experiments Fork

> **Fork Purpose:** Experimental fork of [`contextstack/langchain`](https://github.com/contextstack/langchain) testing two concrete questions on a **Commercial Insurance Submission Triage** workflow sharing the same governed LangGraph substrate:
> 1. **Experiment 1 ([Results & 3-Trial Matrix](./docs/experiments/submission-triage-results.md)):** Does `deepagents` (`create_deep_agent` + `evidence_auditor` subagent) outperform the bounded LangChain v1 `create_agent` baseline on 12 synthetic submissions (4 clean, 8 messy × 3 trials) across both a frontier model (`anthropic/claude-sonnet-5-5`) and a local on-prem DGX Spark (`Qwen3-Coder-Next-FP8`)?
> 2. **Experiment 2 ([Recovery Trace](./docs/experiments/carrier-recovery-trace.md)):** Can the workflow safely recover when a downstream mock HTTP carrier commits a quote request and withholds its response (both with status lookup available and with outcome unverifiable → human review, proving zero duplicate `POST`s)?
> 3. **Engineering Decision Record ([ADR 0002](./docs/adr/0002-deep-agents-submission-triage-decision.md)):** Where Deep Agents helped, what it cost (2.80× cost / 3.68× tokens on frontier; 501-call runaway loop risk when unbounded on local open-weight models), and why we keep Deep Agents outside this bounded triage workflow.

---

## Base Reference Stack (`contextstack/langchain`)

A 100% Python, 100% open-source reference monorepo demonstrating an **enterprise-grade agent platform** built the LangChain stack way - using **LangChain v1**, **LangGraph**, native **MCP (`langchain[mcp]` + FastMCP)**, **PostgreSQL (`pgvector` + checkpoints + store)**, **Redis**, **OpenTelemetry**, and self-hosted **Langfuse**.

Everything so far runs locally at **\$0 in licences**. All CI tests, offline evaluations, and failure drills run deterministically against mock models with zero API spend.

---

## A bit more information about this project

- This is my latest experiment in using the LangChain stack, but all open source. So perhaps "enterprise" is the right word, or maybe it is ;)
- Traditionally, I've found LangChain a bit inflexible for how I want to run my own software factory / personal agent / anything factory. You can see my latest public release of that here: https://github.com/contextstack/contextstack if you are curious.
- I've used it on a few client projects, but it's been a minute, so I'm refamiliarizing myself with it via this project.

---

## Verified Results

All numbers below are from real runs against this repository (full tables, misses, and methodology in [docs/eval-results.md](./docs/eval-results.md)):

| Verification Layer | Result | Highlights |
|---|---|---|
| **Automated Suite (`pytest` + `mypy`)** | **89/89 passed** (>95% cov gate, 0 mypy errors) | Real `os._exit(137)` worker-kill crash recovery, PostgreSQL connection-drop recovery, Redis-outage fallback, 3-worker contention, fail-closed `api_rw` no-bypass RLS, live OTLP PII redaction, and MCP auth binding. |
| **Modern Live LLM Eval (8 models × 10 cases)** | **77/80 passed (96.3%)** | **0/80 unauthorized writes**, **0/16 prompt injections executed**, **100% idempotency** across commercial (`gpt-6.2-sol`, `claude-sonnet-5-5`, `gemini-4-hydrogen`), OpenRouter OSS (`qwen4-max`, `glm-5.3-flash`, `deepseek-v4.1-flash`), and **local on-prem DGX Spark** (`Qwen3-Coder-Next-FP8`, `Nemotron-Super-120B`). |
| **Local On-Prem DGX Spark Parity** | **10/10 in 10.97s/case** | `Qwen3-Coder-Next-FP8` running locally behind LiteLLM Proxy matched commercial flagships on safety and end-state while running faster per case than Claude Sonnet 5.5 and Gemini 4 Hydrogen at **\$0 external API spend**. |
| **Honest Misses Caught & Documented** | **4 real-world lessons** | `gemini-2.5-flash` over-remediating a read-only check (stopped cold by HITL `interrupt()`), `qwen-2.5-72b` dropping the `v` prefix (fixed via `normalize_service_version` at the write boundary), `gpt-6.2-sol` failing judge calibration at 4/8 vs. `gpt-4o` at 8/8 (proving why judge swaps must be gated on `calibration.jsonl`), and `Nemotron-Super-120B` hitting a 32k reasoning-token runaway. |

---

## Architecture (3-Plane Separation)

- **Control Plane (`packages/core`):** Signed JWT auth, frozen `RunContext` (`context_schema`), deterministic `PolicyEngine` with separation of duties, workflow `release_version` + `checkpoint_compat`, and resource bounds.
- **Execution Plane (`services/api`, `services/worker`, `packages/workflows`, `packages/mcp_servers`):**
  - FastAPI **never** executes a graph - it authenticates, writes to PostgreSQL `runs`, and streams reconnectable SSE (`Last-Event-ID`) from Redis Streams.
  - A separate worker process claims runs via `FOR UPDATE SKIP LOCKED`, holds fenced leases `(lease_owner, attempt)` with heartbeats, runs LangGraph with `durability="sync"`, and automatically reclaims crashed workers' runs.
  - Workflows registered via `WORKFLOW_REGISTRY`: **IT / Ops Change-Request & Incident Desk** (`incident_desk`) and **Customer Billing / Refund Operations Desk** (`refund_ops`) — proving *"swap the tools, change the factory"* on the exact same Control, Execution, and Evidence substrate (`StateGraph` outer orchestrator + LangChain v1 `create_agent` with `PIIMiddleware`, `SummarizationMiddleware`, `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware`, `interrupt()`/`Command(resume=...)` HITL approval, idempotent writes, and deterministic end-state verification).
- **Evidence Plane (`packages/telemetry`, `evals/`):**
  - OpenTelemetry spans with mandatory PII masking → self-hosted Langfuse (with a zero-code env-var toggle to commercial LangSmith).
  - Separate, DB-enforced append-only PostgreSQL `audit_log` (role grants + trigger).
  - Offline & real-provider evaluation suite (`evals/datasets/core.jsonl`, 10 cases across `incident_desk` + `refund_ops`) with deterministic ground-truth evaluators and an 8-sample judge calibration set (`evals/datasets/calibration.jsonl`). See [docs/eval-results.md](./docs/eval-results.md) for live multi-provider and local DGX Spark benchmark results (160 live runs across 3 rounds), including both passes and misses.

See [docs/oss-vs-commercial.md](./docs/oss-vs-commercial.md) for the complete build-vs-buy breakdown against **LangSmith Platform** and **LangSmith Deployments / Agent Server**, and [docs/adr/0001-oss-control-execution-evidence-architecture.md](./docs/adr/0001-oss-control-execution-evidence-architecture.md) for the full Architecture Decision Record (ADR).

### What This Reference Proves (and Doesn't)
- **Proven at \$0 (automated, repeatable):** Crash recovery across real `SIGKILL`/`os._exit(137)` worker kills, exactly-once external mutations under retries and 3 parallel workers, fail-closed PostgreSQL Row-Level Security (RLS), separation of duties, MCP outage resilience, PII masking at the live OTLP/HTTP export boundary, and reusable runtime dispatch across two distinct domains (`incident_desk` and `refund_ops`).
- **Known limits (local reference scope):**
  - `/v1/dev/token` is disabled by default in code (`ENABLE_DEV_TOKENS=false`) and explicitly opted into in `.env.example` for local demos; replace with a real IdP before any shared deployment.
  - Redis is best-effort only: during a Redis outage, SSE wake-ups degrade to Postgres polling and the per-tenant rate limiter deliberately fails open to preserve availability.
  - `api_rw` (strict no-bypass RLS) and `app_rw` (worker queue/seed) are provisioned in Postgres and wired via `API_DATABASE_URL`; production deployments should also add RLS on LangGraph's checkpoint/store tables.
  - Write reconciliation covers cancellation and tool-level write timeouts; whole-run deadlines and exhausted-attempt failures do not currently run post-mortem reconciliation, and cancelling a run after `operation_id` is minted (before any write) records `outcome_unknown`.
  - In `MODEL_MODE=mock`, injection drills key off request text rather than live model parsing of poisoned runbooks, and per-tenant LLM rate limits/budgets are local stubs.

---

## Quickstart (Core Profile - \$0, No API Keys Required)

```bash
cp .env.example .env
docker compose up -d --wait      # Starts Postgres (pgvector) on 5439 + Redis on 6389
uv sync                          # Installs the Python workspace
uv run ops-cli db migrate        # Creates schemas, append-only audit_log grants, LangGraph tables
uv run ops-cli seed              # Seeds tenant-alpha & tenant-beta runbooks, services, tickets
uv run pytest -v                 # Runs all unit, integration, and real-process failure drills
uv run ops-cli eval              # Runs the offline evaluation regression gate
```

### Run Individual Failure & Security Drills

```bash
uv run ops-cli drill crash-mid-inference
uv run ops-cli drill crash-after-write
uv run ops-cli drill restart-hitl
uv run ops-cli drill tenant-isolation
uv run ops-cli drill prompt-injection-in-tenant
uv run ops-cli drill version-upgrade
uv run ops-cli drill self-approval
uv run ops-cli drill double-approve
uv run ops-cli drill edit-recheck
uv run ops-cli drill cancel-after-write
```

---

## 5-Minute Guided Walkthrough (Submit → Approve → Verify → Audit → Crash Drill)

1. **Start the infrastructure, migrate, and seed:**
   ```bash
   cp .env.example .env
   docker compose up -d --wait
   uv sync
   uv run ops-cli db migrate
   uv run ops-cli seed
   ```

2. **Start the authenticated FastMCP servers, worker, and API** (in separate terminals):
   ```bash
   uv run ops-mcp-infra                      # Streamable HTTP MCP server on :8101 (incident_desk)
   uv run ops-mcp-ticketing                  # Streamable HTTP MCP server on :8102 (incident_desk)
   uv run ops-mcp-billing                    # Streamable HTTP MCP server on :8103 (refund_ops)
   MCP_TRANSPORT=http uv run ops-worker      # Claims runs & calls MCP servers with signed JWTs
   uv run ops-api                            # FastAPI + single-file Demo UI on :8000
   ```
   *(For quick zero-daemon local runs without starting the MCP servers, omit `MCP_TRANSPORT=http` to use direct Postgres store mode.)*

3. **Run the human-in-the-loop workflow in the Demo UI (`http://127.0.0.1:8000`):**
   - Pick **`incident_desk`** (or switch to **`refund_ops`**) and select **`dana@tenant-alpha [engineer, ops-approver]`**.
   - Click **Submit Request** — watch the live SSE stream investigate via read-only tools, propose a typed `ChangePlan`, pass `PolicyEngine`, and pause at `interrupt` (`awaiting_approval`).
   - Stay as **`dana`** and click **Approve** in the HITL Approval Inbox — it is refused (`403`) by separation of duties because the requester cannot approve their own change.
   - Switch persona to **`bob@tenant-alpha [ops-approver]`** and click **Approve** — the worker resumes the checkpoint, executes the idempotent write over authenticated MCP HTTP, verifies the post-write state, and streams `done`.

4. **Inspect the append-only PostgreSQL audit trail:**
   ```bash
   docker compose exec postgres psql -U postgres -d contextstack -c \
     "SELECT created_at, event_type, requested_by, approved_by, tool_id, decision, operation_id
        FROM audit_log ORDER BY created_at DESC LIMIT 8;"
   ```
   Notice that `audit_log` records both `requested_by = dana` and `approved_by = bob` alongside the `operation_id`, and `app_rw` cannot `UPDATE` or `DELETE` rows (enforced by both role grants and a DB trigger).

5. **Run a real worker-kill crash drill (disposable local DB only):**
   > **Note:** `ops-cli drill` truncates `runs`, `approvals`, `app_threads`, and `mock_operations` in the configured database before running. Only run drills against a disposable local dev stack.
   ```bash
   uv run ops-cli drill crash-after-write
   ```
   This spawns a worker with `FAULT_INJECT=after_write` (`os._exit(137)` right after the external write commits, before checkpointing), lets a replacement worker reclaim the expired lease automatically, and proves the downstream service mutated **exactly once**. See [docs/oss-vs-commercial.md](./docs/oss-vs-commercial.md) for what this drill teaches about building vs. buying an Agent Server.

### Local LangGraph Studio

```bash
uv run langgraph dev
```

### Full Stack (LiteLLM Proxy + Self-Hosted Langfuse)

```bash
docker compose --profile full up -d
```

- **Langfuse UI:** `http://localhost:3000`
- **LiteLLM Proxy:** `http://localhost:4000`

---

## Note on Data Handling

Raw request text is stored unredacted in PostgreSQL (`runs` and checkpoints) as the tenant-scoped system of record. PII redaction is enforced at two independent boundaries: `PIIMiddleware` before any LLM call, and a span-masking processor/hook before any trace export.
