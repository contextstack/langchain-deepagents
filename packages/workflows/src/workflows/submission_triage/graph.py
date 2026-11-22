"""Commercial Insurance Submission Triage StateGraph (Experiments 1 & 2).

Shares the exact same tools, output schema (`SubmissionBrief`), policy gate, HITL approval,
idempotent carrier submission, ambiguous-response reconciliation, and audit trail across both
planners:
  - `baseline`: LangChain v1 `create_agent` + middleware
  - `deep_agents`: `deepagents.create_deep_agent` + 1 specialist `evidence_auditor` subagent
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import httpx
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from core.audit import AuditEvent
from core.config import Settings, get_settings
from core.models import RunContext
from core.pii import PII_RULES, redact_text
from gateway.factory import get_chat_model
from workflows.submission_triage.fixtures import (
    CARRIER_APPETITE_GUIDES,
    SUBMISSIONS,
    serialize_carrier,
)
from workflows.submission_triage.models import (
    ContradictionFlag,
    EvidenceCitation,
    SubmissionBrief,
)

PLANNER_SYSTEM_PROMPT = (
    "You are a Commercial Insurance Underwriting Triage specialist. "
    "Use the read-only submission and carrier appetite tools to inspect ALL documents "
    "for the submission (including checking version numbers and superseded attachments) "
    "and compare them against all available carrier appetite guides.\n"
    "Rules:\n"
    "- If a later document explicitly supersedes an earlier draft (e.g. v2 supersedes v1), "
    "rely on the current v2 document for facts.\n"
    "- If the broker email contradicts the signed application or current loss run on any "
    "material underwriting fact (class, revenue, coverage limit, or loss history), flag the "
    "contradiction and recommend 'human_review'.\n"
    "- If the risk falls outside ALL available carrier appetite guides (prohibited class, "
    "limit above carrier max, or claim above max single loss), recommend 'human_review'.\n"
    "- If the risk is in appetite with no contradictions, but one or more required fields "
    "(e.g. FEIN, effective date, 3-year loss history, location schedule, or payroll breakdown) "
    "are missing, recommend 'ask_broker'.\n"
    "- Only recommend 'ready_to_quote' when all required fields are present, consistent, "
    "and within at least one carrier's appetite.\n"
    "- Cite exact doc_id values for every material fact."
)

EVIDENCE_AUDITOR_PROMPT = (
    "You are an Evidence Auditor specialist for commercial insurance submissions. "
    "Your ONLY job is to read every document in the submission, identify document versions "
    "and any superseded drafts, cross-check broker emails against signed applications and "
    "loss runs, verify every required field against the carrier appetite guides, and return "
    "a precise bulleted audit of:\n"
    "1. Active (non-superseded) documents and exact doc_ids\n"
    "2. Material facts with exact doc_id citations\n"
    "3. Any contradictions between documents (with exact field & both doc_ids)\n"
    "4. Any missing required fields\n"
    "5. Carrier appetite eligibility or exclusions."
)

BRIEF_SYNTHESIS_PROMPT = (
    "Produce a structured SubmissionBrief matching the schema from the triage investigation "
    "below.\n"
    "Disposition rules:\n"
    "- 'ready_to_quote': all required fields present, consistent across active documents, "
    "and within at least one carrier's appetite.\n"
    "- 'ask_broker': in appetite with no contradictions, but required fields are missing.\n"
    "- 'human_review': any material contradiction across documents, undisclosed loss, "
    "prohibited class, or coverage/loss outside all available carrier appetite rules.\n"
    "Cite exact doc_id values in material_facts.\n\n"
    "Submission ID: {submission_id}\n"
    "Investigation Findings:\n{investigation}"
)


class SubmissionTriageState(TypedDict, total=False):
    submission_id: str
    planner_variant: str
    investigation_summary: str
    submission_brief: dict[str, Any] | None
    policy_decision: str | None
    operation_id: str | None
    approval_decision: dict[str, Any] | None
    write_attempted: bool
    attempted_payload: dict[str, Any] | None
    carrier_response: dict[str, Any] | None
    carrier_ref: str | None
    timeout_error: str | None
    unresolved_state: dict[str, Any] | None
    reconciliation_resolution: dict[str, Any] | None
    final_status: str | None
    trace_events: list[dict[str, Any]]


@dataclass
class SubmissionTriageDeps:
    settings: Settings = field(default_factory=get_settings)
    planner_variant: Literal["baseline", "deep_agents"] = "baseline"
    primary_model_alias: str = "eval-anthropic"
    carrier_base_url: str | None = None
    carrier_timeout_s: float = 0.5
    callbacks: list[Any] = field(default_factory=list)
    audit_memory: list[AuditEvent] = field(default_factory=list)


def build_submission_read_tools() -> list[Any]:
    """Shared read-only tools given identically to BOTH Baseline and Deep Agents planners."""

    @tool
    def list_submission_documents(submission_id: str) -> str:
        """List all documents attached to a commercial insurance submission with version info."""
        sub = SUBMISSIONS.get(submission_id)
        if sub is None:
            return json.dumps({"error": f"Submission '{submission_id}' not found"})
        return json.dumps(
            [
                {
                    "doc_id": d.doc_id,
                    "doc_type": d.doc_type,
                    "version": d.version,
                    "timestamp": d.timestamp,
                    "title": d.title,
                    "supersedes": d.supersedes,
                }
                for d in sub.documents
            ]
        )

    @tool
    def read_submission_document(submission_id: str, doc_id: str) -> str:
        """Read the full text of a specific submission document by doc_id."""
        sub = SUBMISSIONS.get(submission_id)
        if sub is None:
            return json.dumps({"error": f"Submission '{submission_id}' not found"})
        for doc in sub.documents:
            if doc.doc_id == doc_id:
                return json.dumps(
                    {
                        "doc_id": doc.doc_id,
                        "doc_type": doc.doc_type,
                        "version": doc.version,
                        "timestamp": doc.timestamp,
                        "title": doc.title,
                        "supersedes": doc.supersedes,
                        "content": doc.content,
                    }
                )
        return json.dumps({"error": f"Document '{doc_id}' not found in '{submission_id}'"})

    @tool
    def list_carriers() -> str:
        """List all available commercial insurance carriers."""
        return json.dumps(
            [
                {"carrier_id": c.carrier_id, "carrier_name": c.carrier_name}
                for c in CARRIER_APPETITE_GUIDES.values()
            ]
        )

    @tool
    def lookup_carrier_appetite(carrier_id: str) -> str:
        """Read a carrier's underwriting appetite guide, limits, exclusions, and required fields."""
        guide = CARRIER_APPETITE_GUIDES.get(carrier_id)
        if guide is None:
            return json.dumps({"error": f"Carrier '{carrier_id}' not found"})
        return json.dumps(serialize_carrier(guide))

    return [
        list_submission_documents,
        read_submission_document,
        list_carriers,
        lookup_carrier_appetite,
    ]


def _deterministic_mock_brief(submission_id: str) -> SubmissionBrief:
    """Deterministic $0 mock brief for offline CI tests (never used in Experiment 1 A/B runs)."""
    sub = SUBMISSIONS[submission_id]
    first_doc = sub.documents[-1]
    if sub.tier == "clean" or sub.scenario_tag == "superseded_v2_clean_in_appetite":
        carrier = (
            "carrier-ironclad"
            if any(k in first_doc.content for k in ("Manufacturing", "Warehouse"))
            else "carrier-apex"
        )
        return SubmissionBrief(
            disposition="ready_to_quote",
            insured_name=first_doc.title,
            business_classification="In-Appetite Commercial",
            annual_revenue_usd=4_200_000,
            requested_coverage_usd=2_000_000,
            effective_date="2026-12-01",
            carrier_match=carrier,
            material_facts=[
                EvidenceCitation(
                    doc_id=first_doc.doc_id,
                    section_or_quote=first_doc.title,
                    fact_supported="Complete, in-appetite submission.",
                )
            ],
            summary=f"Submission {submission_id} is complete and ready to request a quote.",
        )
    if "missing" in sub.scenario_tag:
        return SubmissionBrief(
            disposition="ask_broker",
            insured_name=first_doc.title,
            missing_fields=["required_underwriting_field"],
            material_facts=[
                EvidenceCitation(
                    doc_id=first_doc.doc_id,
                    section_or_quote=first_doc.title,
                    fact_supported="Required underwriting field missing.",
                )
            ],
            summary=f"Submission {submission_id} is missing required underwriting fields.",
        )
    return SubmissionBrief(
        disposition="human_review",
        insured_name=first_doc.title,
        contradictions=[
            ContradictionFlag(
                field="underwriting_eligibility",
                source_a=sub.documents[0].doc_id,
                source_b=first_doc.doc_id,
                detail="Conflict or out-of-appetite risk detected.",
            )
        ],
        material_facts=[
            EvidenceCitation(
                doc_id=first_doc.doc_id,
                section_or_quote=first_doc.title,
                fact_supported="Requires human underwriting review.",
            )
        ],
        summary=f"Submission {submission_id} routed to human underwriting review.",
    )


def build_submission_triage_graph(
    *,
    deps: SubmissionTriageDeps | None = None,
    checkpointer: Any = None,
):
    """Compile the governed Commercial Insurance Submission Triage StateGraph."""
    wf_deps = deps or SubmissionTriageDeps()
    read_tools = build_submission_read_tools()

    def _ctx(runtime: Runtime[RunContext]) -> RunContext:
        return runtime.context

    def _append_trace(state: SubmissionTriageState, event: dict[str, Any]) -> list[dict[str, Any]]:
        return [*list(state.get("trace_events") or []), event]

    def _record_audit(event: AuditEvent) -> None:
        wf_deps.audit_memory.append(event)

    # Node 1: triage_submission (pluggable Baseline vs Deep Agents planner)
    async def triage_submission(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        sub_id = state["submission_id"]
        variant = state.get("planner_variant") or wf_deps.planner_variant

        if wf_deps.settings.model_mode == "mock":
            brief = _deterministic_mock_brief(sub_id)
            return {
                "planner_variant": variant,
                "investigation_summary": brief.summary,
                "submission_brief": brief.model_dump(mode="json"),
                "trace_events": _append_trace(
                    state,
                    {"node": "triage_submission", "variant": variant, "mode": "mock"},
                ),
            }

        chat_model = get_chat_model(
            alias=wf_deps.primary_model_alias,
            tenant_id=ctx.tenant_id,
            settings=wf_deps.settings,
        )
        fast_model = get_chat_model(
            alias="ops-fast",
            tenant_id=ctx.tenant_id,
            settings=wf_deps.settings,
        )
        invoke_cfg: RunnableConfig = (
            {"callbacks": wf_deps.callbacks} if wf_deps.callbacks else {}
        )
        user_prompt = (
            f"Triage commercial insurance submission '{sub_id}'. "
            "Inspect all attached documents, check for superseded versions, missing required "
            "fields, and contradictions, and evaluate against all available carrier appetite "
            "guides before summarizing your findings and recommended disposition."
        )

        if variant == "deep_agents":
            deep_guardrails: list[Any] = [
                ModelCallLimitMiddleware(run_limit=wf_deps.settings.max_model_calls),
                ToolCallLimitMiddleware(run_limit=wf_deps.settings.max_tool_calls),
            ]
            evidence_subagent: dict[str, Any] = {
                "name": "evidence_auditor",
                "description": (
                    "Reads all submission documents and carrier appetite guides, resolves "
                    "superseded versions, verifies required fields, and flags contradictions."
                ),
                "system_prompt": EVIDENCE_AUDITOR_PROMPT,
                "tools": read_tools,
                "model": chat_model,
                "middleware": deep_guardrails,
            }
            deep_agent = create_deep_agent(
                model=chat_model,
                tools=read_tools,
                system_prompt=(
                    f"{PLANNER_SYSTEM_PROMPT}\n\n"
                    "Delegate detailed document cross-checking and contradiction detection to "
                    "your 'evidence_auditor' specialist subagent, then synthesize the final "
                    "triage findings clearly."
                ),
                middleware=deep_guardrails,
                subagents=[evidence_subagent],  # type: ignore[list-item]
                context_schema=RunContext,
            )
            agent_res = await deep_agent.ainvoke(
                {"messages": [{"role": "user", "content": user_prompt}]},
                config=invoke_cfg,
                context=ctx,
            )
        else:
            agent_middleware: list[Any] = [
                *[
                    PIIMiddleware(name, detector=pattern.pattern, strategy="redact")
                    for name, pattern, _ in PII_RULES
                ],
                SummarizationMiddleware(
                    model=fast_model,
                    trigger=("tokens", 4000),
                    keep=("messages", 10),
                ),
                ModelCallLimitMiddleware(run_limit=wf_deps.settings.max_model_calls),
                ToolCallLimitMiddleware(run_limit=wf_deps.settings.max_tool_calls),
            ]
            baseline_agent = create_agent(
                model=chat_model,
                tools=read_tools,
                system_prompt=PLANNER_SYSTEM_PROMPT,
                context_schema=RunContext,
                middleware=agent_middleware,
            )
            agent_res = await baseline_agent.ainvoke(
                {"messages": [{"role": "user", "content": user_prompt}]},
                config=invoke_cfg,
                context=ctx,
            )

        last_msg = agent_res["messages"][-1]
        investigation_summary = str(getattr(last_msg, "content", last_msg))

        structured = chat_model.with_structured_output(SubmissionBrief)
        brief_obj: SubmissionBrief = await structured.ainvoke(
            BRIEF_SYNTHESIS_PROMPT.format(
                submission_id=sub_id,
                investigation=redact_text(investigation_summary),
            ),
            config=invoke_cfg,
        )
        return {
            "planner_variant": variant,
            "investigation_summary": investigation_summary,
            "submission_brief": brief_obj.model_dump(mode="json"),
            "trace_events": _append_trace(
                state,
                {
                    "node": "triage_submission",
                    "variant": variant,
                    "disposition": brief_obj.disposition,
                },
            ),
        }

    # Node 2: evaluate_policy (shared control gate — planner only proposes!)
    async def evaluate_policy(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        brief = SubmissionBrief.model_validate(state["submission_brief"])

        if brief.disposition == "ask_broker":
            return {
                "policy_decision": "ask_broker",
                "final_status": "broker_clarification_requested",
                "trace_events": _append_trace(
                    state, {"node": "evaluate_policy", "decision": "ask_broker"}
                ),
            }
        if brief.disposition == "human_review":
            return {
                "policy_decision": "human_review",
                "final_status": "routed_to_human_review",
                "trace_events": _append_trace(
                    state, {"node": "evaluate_policy", "decision": "human_review"}
                ),
            }

        operation_id = state.get("operation_id") or f"quote_{uuid.uuid4().hex[:16]}"
        _record_audit(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.requester_id,
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
                workflow_version="submission_triage+0.1.0",
                event_type="authz_check",
                decision="allow",
                step_or_op_key=f"policy:{operation_id}",
                operation_id=operation_id,
                details={
                    "disposition": brief.disposition,
                    "carrier_match": brief.carrier_match,
                },
            )
        )
        return {
            "policy_decision": "require_approval",
            "operation_id": operation_id,
            "trace_events": _append_trace(
                state,
                {
                    "node": "evaluate_policy",
                    "decision": "require_approval",
                    "operation_id": operation_id,
                },
            ),
        }

    def route_after_policy(state: SubmissionTriageState) -> str:
        if state.get("policy_decision") == "require_approval":
            return "await_approval"
        return END

    # Node 3: await_approval (HITL interrupt with separation of duties)
    async def await_approval(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        decision = interrupt(
            {
                "type": "quote_approval",
                "operation_id": state["operation_id"],
                "submission_id": state["submission_id"],
                "submission_brief": state["submission_brief"],
            }
        )
        approver_id = str(decision.get("approver_id", ""))
        action = str(decision.get("decision", "reject"))
        if action == "approve" and (not approver_id or approver_id == ctx.requester_id):
            _record_audit(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_id=approver_id or ctx.requester_id,
                    requested_by=ctx.requester_id,
                    approved_by=approver_id or None,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                    workflow_version="submission_triage+0.1.0",
                    event_type="approval_refused",
                    decision="refused",
                    step_or_op_key=f"approval:{state['operation_id']}:refused",
                    operation_id=state["operation_id"],
                    details={"reason": "Separation of duties: requester cannot approve own quote"},
                )
            )
            return {
                "approval_decision": decision,
                "final_status": "denied",
                "trace_events": _append_trace(
                    state, {"node": "await_approval", "decision": "refused_self_approval"}
                ),
            }
        if action != "approve":
            return {
                "approval_decision": decision,
                "final_status": "rejected",
                "trace_events": _append_trace(
                    state, {"node": "await_approval", "decision": "rejected"}
                ),
            }
        return {
            "approval_decision": decision,
            "trace_events": _append_trace(
                state,
                {
                    "node": "await_approval",
                    "decision": "approved",
                    "approver_id": approver_id,
                },
            ),
        }

    def route_after_approval(state: SubmissionTriageState) -> str:
        if state.get("final_status") in {"denied", "rejected"}:
            return END
        return "submit_quote"

    # Node 4: submit_quote (Experiment 2 durable write boundary with anti-duplicate guard)
    async def submit_quote(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        op_id = str(state["operation_id"])
        approver_id = str((state.get("approval_decision") or {}).get("approver_id", ""))

        # Structural anti-duplicate guard: NEVER send a second POST if already attempted!
        if state.get("write_attempted"):
            return {
                "trace_events": _append_trace(
                    state,
                    {
                        "node": "submit_quote",
                        "skipped_duplicate_post": True,
                        "operation_id": op_id,
                    },
                )
            }

        brief = SubmissionBrief.model_validate(state["submission_brief"])
        payload = {
            "operation_id": op_id,
            "submission_id": state["submission_id"],
            "carrier_id": brief.carrier_match or "carrier-apex",
            "insured_name": brief.insured_name or state["submission_id"],
            "requested_coverage_usd": int(brief.requested_coverage_usd or 1_000_000),
        }
        if not wf_deps.carrier_base_url:
            # In-memory fallback when no HTTP carrier URL is configured
            return {
                "write_attempted": True,
                "attempted_payload": payload,
                "carrier_ref": f"CAR-MOCK-{op_id[-6:].upper()}",
                "final_status": "completed",
                "trace_events": _append_trace(
                    state,
                    {"node": "submit_quote", "mode": "in_memory", "operation_id": op_id},
                ),
            }

        try:
            async with httpx.AsyncClient(timeout=wf_deps.carrier_timeout_s) as client:
                resp = await client.post(f"{wf_deps.carrier_base_url}/v1/quotes", json=payload)
                resp.raise_for_status()
                body = resp.json()
            _record_audit(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_id=approver_id or ctx.requester_id,
                    requested_by=ctx.requester_id,
                    approved_by=approver_id or None,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                    workflow_version="submission_triage+0.1.0",
                    event_type="tool_write",
                    tool_id="carrier_submit_quote",
                    operation_id=op_id,
                    decision="succeeded",
                    step_or_op_key=f"write:{op_id}",
                    details=body,
                )
            )
            return {
                "write_attempted": True,
                "attempted_payload": payload,
                "carrier_response": body,
                "carrier_ref": body.get("carrier_ref"),
                "final_status": "completed",
                "trace_events": _append_trace(
                    state,
                    {
                        "node": "submit_quote",
                        "outcome": "committed_ok",
                        "operation_id": op_id,
                        "carrier_ref": body.get("carrier_ref"),
                    },
                ),
            }
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            return {
                "write_attempted": True,
                "attempted_payload": payload,
                "timeout_error": err_msg,
                "trace_events": _append_trace(
                    state,
                    {
                        "node": "submit_quote",
                        "outcome": "ambiguous_timeout",
                        "operation_id": op_id,
                        "error": err_msg,
                    },
                ),
            }

    def route_after_submit(state: SubmissionTriageState) -> str:
        if state.get("final_status") == "completed":
            return END
        return "reconcile_carrier"

    # Node 5: reconcile_carrier (lookup by stable operation_id or escalate unverifiable outcome)
    async def reconcile_carrier(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        op_id = str(state["operation_id"])
        approver_id = str((state.get("approval_decision") or {}).get("approver_id", ""))
        assert wf_deps.carrier_base_url is not None

        lookup_error = ""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{wf_deps.carrier_base_url}/v1/quotes/{op_id}")
            if resp.status_code == 200:
                body = resp.json()
                _record_audit(
                    AuditEvent(
                        tenant_id=ctx.tenant_id,
                        actor_id=approver_id or ctx.requester_id,
                        requested_by=ctx.requester_id,
                        approved_by=approver_id or None,
                        thread_id=ctx.thread_id,
                        run_id=ctx.run_id,
                        workflow_version="submission_triage+0.1.0",
                        event_type="reconciliation",
                        tool_id="carrier_lookup_quote",
                        operation_id=op_id,
                        decision="succeeded",
                        step_or_op_key=f"reconcile:{op_id}",
                        details=body,
                    )
                )
                return {
                    "carrier_response": body,
                    "carrier_ref": body.get("carrier_ref"),
                    "final_status": "completed",
                    "trace_events": _append_trace(
                        state,
                        {
                            "node": "reconcile_carrier",
                            "outcome": "reconciled_committed",
                            "operation_id": op_id,
                            "carrier_ref": body.get("carrier_ref"),
                        },
                    ),
                }
            lookup_error = f"HTTP {resp.status_code}: {resp.text}"
        except Exception as exc:
            lookup_error = f"{type(exc).__name__}: {exc}"

        unresolved = {
            "operation_id": op_id,
            "attempted_payload": state.get("attempted_payload"),
            "timeout_error": state.get("timeout_error"),
            "lookup_error": lookup_error,
            "why_uncertain": (
                "The quote request timed out after POST /v1/quotes was sent, and carrier status "
                "lookup could not verify whether the request committed. Automatic retry is "
                "blocked to prevent duplicate carrier submissions."
            ),
        }
        _record_audit(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=approver_id or ctx.requester_id,
                requested_by=ctx.requester_id,
                approved_by=approver_id or None,
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
                workflow_version="submission_triage+0.1.0",
                event_type="reconciliation",
                tool_id="carrier_lookup_quote",
                operation_id=op_id,
                decision="outcome_unknown",
                step_or_op_key=f"reconcile:{op_id}:unknown",
                details=unresolved,
            )
        )
        return {
            "unresolved_state": unresolved,
            "trace_events": _append_trace(
                state,
                {
                    "node": "reconcile_carrier",
                    "outcome": "unverifiable_escalated",
                    "operation_id": op_id,
                    "lookup_error": lookup_error,
                },
            ),
        }

    def route_after_reconcile(state: SubmissionTriageState) -> str:
        if state.get("final_status") == "completed":
            return END
        return "await_reconciliation_review"

    # Node 6: await_reconciliation_review (Human review gate — NEVER re-posts to carrier!)
    async def await_reconciliation_review(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        unresolved = state.get("unresolved_state") or {}
        resolution = interrupt(
            {
                "type": "reconciliation_human_review",
                "operation_id": state["operation_id"],
                "attempted_payload": unresolved.get("attempted_payload"),
                "timeout_error": unresolved.get("timeout_error"),
                "lookup_error": unresolved.get("lookup_error"),
                "why_uncertain": unresolved.get("why_uncertain"),
                "safe_action_guidance": (
                    "Verify directly with carrier portal/underwriter using operation_id before "
                    "taking action. Resuming this workflow will NOT send a second POST request."
                ),
            }
        )
        reviewer_id = str(resolution.get("reviewer_id", ""))
        _record_audit(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=reviewer_id or ctx.requester_id,
                requested_by=ctx.requester_id,
                approved_by=reviewer_id or None,
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
                workflow_version="submission_triage+0.1.0",
                event_type="reconciliation",
                tool_id="human_reconciliation_review",
                operation_id=state["operation_id"],
                decision="succeeded",
                step_or_op_key=f"reconcile_review:{state['operation_id']}",
                details=resolution,
            )
        )
        return {
            "reconciliation_resolution": resolution,
            "carrier_ref": resolution.get("carrier_ref"),
            "final_status": "resolved_by_human",
            "trace_events": _append_trace(
                state,
                {
                    "node": "await_reconciliation_review",
                    "outcome": "resolved_by_human",
                    "resolution": resolution,
                },
            ),
        }

    builder = StateGraph(SubmissionTriageState, context_schema=RunContext)
    builder.add_node("triage_submission", triage_submission)
    builder.add_node("evaluate_policy", evaluate_policy)
    builder.add_node("await_approval", await_approval)
    builder.add_node("submit_quote", submit_quote)
    builder.add_node("reconcile_carrier", reconcile_carrier)
    builder.add_node("await_reconciliation_review", await_reconciliation_review)

    builder.add_edge(START, "triage_submission")
    builder.add_edge("triage_submission", "evaluate_policy")
    builder.add_conditional_edges(
        "evaluate_policy",
        route_after_policy,
        {"await_approval": "await_approval", END: END},
    )
    builder.add_conditional_edges(
        "await_approval",
        route_after_approval,
        {"submit_quote": "submit_quote", END: END},
    )
    builder.add_conditional_edges(
        "submit_quote",
        route_after_submit,
        {"reconcile_carrier": "reconcile_carrier", END: END},
    )
    builder.add_conditional_edges(
        "reconcile_carrier",
        route_after_reconcile,
        {"await_reconciliation_review": "await_reconciliation_review", END: END},
    )
    builder.add_edge("await_reconciliation_review", END)

    return builder.compile(checkpointer=checkpointer)


__all__ = [
    "SubmissionTriageDeps",
    "SubmissionTriageState",
    "build_submission_read_tools",
    "build_submission_triage_graph",
    "AIMessage",
]
