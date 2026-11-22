# Experiment 2 Recovery Trace: Ambiguous Carrier HTTP Response

This trace documents two real live-HTTP recovery drills executed against [`packages/mcp_servers/src/mcp_servers/mock_carrier.py`](../../packages/mcp_servers/src/mcp_servers/mock_carrier.py) (`tests/test_exp2_carrier_recovery.py`).

In both behaviors:
1. A human approver (`bob`, distinct from requester `alice`) approves a `ready_to_quote` submission (`sub-01`) with a stable `operation_id`.
2. `submit_quote` sets durable `write_attempted = True` and sends `POST /v1/quotes`.
3. The mock HTTP carrier **commits the quote request first**, then withholds its HTTP response so the workflow times out (`ReadTimeout` at `0.3s`) before receiving the `carrier_ref`.
4. Downstream write counts (`http_post_attempts` and `committed_writes`) are verified directly via `GET /admin/stats` on the mock carrier.

---

## Behavior 1: Status Lookup Available (`commit_then_hang_lookup_ok`)

- **Operation ID:** `quote_38fe71d0b4b244e6`
- **Timeout Observed:** `ReadTimeout` on `POST /v1/quotes`
- **Reconciliation Action:** `GET /v1/quotes/quote_38fe71d0b4b244e6` → `HTTP 200 OK`
- **Recovered Carrier Reference:** `CAR-E43F576F`
- **Final Workflow Status:** `completed`
- **Direct Mock Carrier Verification (`/admin/stats`):**
  - `http_post_attempts`: **`1`**
  - `committed_writes`: **`1`**
  - `lookup_attempts`: **`1`**

### Execution Trace
```json
[
  {"node": "triage_submission", "variant": "deep_agents", "mode": "mock"},
  {"node": "evaluate_policy", "decision": "require_approval", "operation_id": "quote_38fe71d0b4b244e6"},
  {"node": "await_approval", "decision": "approved", "approver_id": "bob"},
  {"node": "submit_quote", "outcome": "ambiguous_timeout", "operation_id": "quote_38fe71d0b4b244e6", "error": "ReadTimeout: "},
  {"node": "reconcile_carrier", "outcome": "reconciled_committed", "operation_id": "quote_38fe71d0b4b244e6", "carrier_ref": "CAR-E43F576F"}
]
```

---

## Behavior 2: Outcome Unverifiable (`commit_then_hang_unverifiable`)

- **Operation ID:** `quote_3519cef280864cfe`
- **Timeout Observed:** `ReadTimeout` on `POST /v1/quotes`
- **Reconciliation Action:** `GET /v1/quotes/quote_3519cef280864cfe` → `HTTP 501 Not Implemented`
- **Escalation:** Persisted `unresolved_state`, recorded `reconciliation (outcome_unknown)` audit event, and paused at `await_reconciliation_review` (`interrupt()`).
- **Restart & Resume Test:** Compiled a fresh graph instance sharing the checkpointer and resumed with reviewer `bob`'s offline confirmation (`CAR-VERIFIED-OFFLINE`).
- **Final Workflow Status:** `resolved_by_human`
- **Direct Mock Carrier Verification (`/admin/stats`):**
  - `http_post_attempts`: **`1`** *(proves restart + human resume NEVER sent a second POST)*
  - `committed_writes`: **`1`**
  - `lookup_attempts`: **`1`**

### Exact State & Guidance Surfaced to the Human Reviewer (`interrupt` Payload)
```json
{
  "type": "reconciliation_human_review",
  "operation_id": "quote_3519cef280864cfe",
  "attempted_payload": {
    "operation_id": "quote_3519cef280864cfe",
    "submission_id": "sub-01",
    "carrier_id": "carrier-apex",
    "insured_name": "ACORD Tech E&O Application - Northstar Analytics Inc.",
    "requested_coverage_usd": 2000000
  },
  "timeout_error": "ReadTimeout: ",
  "lookup_error": "HTTP 501: {\"detail\":\"Status lookup by operation_id is not supported for this carrier/line; outcome cannot be verified automatically.\"}",
  "why_uncertain": "The quote request timed out after POST /v1/quotes was sent, and carrier status lookup could not verify whether the request committed. Automatic retry is blocked to prevent duplicate carrier submissions.",
  "safe_action_guidance": "Verify directly with carrier portal/underwriter using operation_id before taking action. Resuming this workflow will NOT send a second POST request."
}
```

### Execution Trace (Approval → Timeout → Escalation → Restart → Human Resolution)
```json
[
  {"node": "triage_submission", "variant": "deep_agents", "mode": "mock"},
  {"node": "evaluate_policy", "decision": "require_approval", "operation_id": "quote_3519cef280864cfe"},
  {"node": "await_approval", "decision": "approved", "approver_id": "bob"},
  {"node": "submit_quote", "outcome": "ambiguous_timeout", "operation_id": "quote_3519cef280864cfe", "error": "ReadTimeout: "},
  {"node": "reconcile_carrier", "outcome": "unverifiable_escalated", "operation_id": "quote_3519cef280864cfe", "lookup_error": "HTTP 501: ..."},
  {"node": "await_reconciliation_review", "outcome": "resolved_by_human", "resolution": {"decision": "confirmed_committed_offline", "carrier_ref": "CAR-VERIFIED-OFFLINE", "reviewer_id": "bob"}}
]
```
