"""Commercial Insurance Submission Triage StateGraph (Experiments 1 & 2).

Shares the exact same tools, output schema (`SubmissionBrief`), policy gate, HITL approval,
durable pre-send intent checkpoint, idempotent carrier submission, ambiguous-response
reconciliation, and audit trail across both planners:
  - `baseline`: LangChain v1 `create_agent` + middleware
  - `deep_agents`: `deepagents.create_deep_agent` + 1 specialist `evidence_auditor` subagent
    (with implicit `general-purpose` delegate disabled and ONE shared call budget across
    coordinator + specialist)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import httpx
import psycopg
from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
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
from langgraph.types import Command, interrupt

from core.audit import AuditEvent
from core.auth import AuthError, verify_user_jwt
from core.config import Settings, get_settings
from core.db import set_tenant_context
from core.models import RunContext
from core.pii import PII_RULES, redact_text
from core.policy import PolicyDecision, PolicyEngine
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
    "- Cite exact doc_id values and verbatim passages from each cited document."
)

EVIDENCE_AUDITOR_PROMPT = (
    "You are an Evidence Auditor specialist for commercial insurance submissions. "
    "Your ONLY job is to read every document in the submission, identify document versions "
    "and any superseded drafts, cross-check broker emails against signed applications and "
    "loss runs, verify every required field against the carrier appetite guides, and return "
    "a precise bulleted audit of:\n"
    "1. Active (non-superseded) documents and exact doc_ids\n"
    "2. Material facts with exact doc_id citations and verbatim quotes\n"
    "3. Any contradictions between documents (with exact field & both doc_ids)\n"
    "4. Any missing required fields\n"
    "5. Carrier appetite eligibility or exclusions."
)

BRIEF_SYNTHESIS_PROMPT = (
    "Produce a structured SubmissionBrief matching the schema from the triage investigation "
    "below.\n"
    "Disposition rules:\n"
    "- 'ready_to_quote': all required fields present, consistent across active documents, "
    "and within at least one carrier's appetite. Must populate carrier_match, insured_name, "
    "annual_revenue_usd, requested_coverage_usd, and effective_date.\n"
    "- 'ask_broker': in appetite with no contradictions, but required fields are missing.\n"
    "- 'human_review': any material contradiction across documents, undisclosed loss, "
    "prohibited class, or coverage/loss outside all available carrier appetite rules.\n"
    "Cite exact doc_id values and verbatim passages in material_facts.\n\n"
    "Submission ID: {submission_id}\n"
    "Investigation Findings:\n{investigation}"
)


class SharedBudgetExceededError(RuntimeError):
    """Raised when coordinator + specialist combined exceed the single shared run budget."""


@dataclass
class SharedCallBudget:
    """Single shared model/tool call budget across a coordinator and its specialist subagent."""

    max_model_calls: int = 12
    max_tool_calls: int = 15
    model_calls: int = 0
    tool_calls: int = 0

    def record_model_call(self) -> None:
        self.model_calls += 1
        if self.model_calls > self.max_model_calls:
            raise SharedBudgetExceededError(
                f"Shared model call budget exceeded ({self.model_calls}/{self.max_model_calls})"
            )

    def record_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            raise SharedBudgetExceededError(
                f"Shared tool call budget exceeded ({self.tool_calls}/{self.max_tool_calls})"
            )


class SharedBudgetMiddleware(AgentMiddleware[Any, Any]):
    """AgentMiddleware enforcing ONE SharedCallBudget across coordinator and subagent."""

    def __init__(self, budget: SharedCallBudget) -> None:
        super().__init__()
        self._budget = budget

    def before_model(self, state: Any, runtime: Any) -> None:
        self._budget.record_model_call()

    async def abefore_model(self, state: Any, runtime: Any) -> None:
        self._budget.record_model_call()

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        self._budget.record_tool_call()
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        self._budget.record_tool_call()
        return await handler(request)


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalAuthorizationError(PermissionError):
    """Raised at the trusted resume boundary when caller authentication or policy fails."""


def _compute_approval_binding_mac(
    *,
    tenant_id: str,
    approver_id: str,
    decision: str,
    operation_id: str,
    approved_payload_hash: str,
    secret: str,
) -> str:
    msg = (
        f"{tenant_id}|{approver_id}|{decision}|{operation_id}|{approved_payload_hash}"
    ).encode()
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_verified_approval_command(
    *,
    bearer_token: str | None,
    decision: Literal["approve", "reject"],
    approval_tenant_id: str,
    requested_by: str,
    operation_id: str,
    approved_payload_hash: str,
    settings: Settings,
    policy: PolicyEngine | None = None,
) -> Command[Any]:
    """Authenticate caller & enforce PolicyEngine BEFORE constructing LangGraph Command(resume).

    Strips bearer credentials completely so raw JWTs never enter checkpoints, traces, or audits.
    """
    if not bearer_token or not bearer_token.strip():
        raise ApprovalAuthorizationError("Missing bearer token for approval decision")
    raw_tok = bearer_token.strip()
    if raw_tok.lower().startswith("bearer "):
        raw_tok = raw_tok[7:].strip()
    try:
        caller = verify_user_jwt(raw_tok, secret=settings.jwt_signing_secret)
    except AuthError as exc:
        raise ApprovalAuthorizationError(f"Invalid bearer token: {exc}") from exc

    engine = policy or PolicyEngine()
    authz = engine.check_approval_authority(
        caller=caller,
        approval_tenant_id=approval_tenant_id,
        requested_by=requested_by,
    )
    if authz.decision != PolicyDecision.ALLOW:
        raise ApprovalAuthorizationError(authz.reason)

    binding_mac = _compute_approval_binding_mac(
        tenant_id=approval_tenant_id,
        approver_id=caller.actor_id,
        decision=decision,
        operation_id=operation_id,
        approved_payload_hash=approved_payload_hash,
        secret=settings.mcp_token_secret,
    )
    # Credential-free server-derived record bound to (actor, decision, op_id, payload_hash)
    server_record = {
        "decision": decision,
        "approver_id": caller.actor_id,
        "operation_id": operation_id,
        "approved_payload_hash": approved_payload_hash,
        "binding_mac": binding_mac,
    }
    return Command(resume=server_record)


def build_deep_agents_planner(
    *,
    chat_model: Any,
    model_alias: str,
    read_tools: list[Any],
    budget: SharedCallBudget,
) -> Any:
    """Construct `create_deep_agent` with implicit general-purpose disabled & one shared budget."""
    from deepagents._models import get_model_identifier, get_model_provider  # noqa: PLC0415

    no_gp_profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    provider = get_model_provider(chat_model)
    identifier = get_model_identifier(chat_model)
    if provider and identifier:
        register_harness_profile(f"{provider}:{identifier}", no_gp_profile)
    elif provider:
        register_harness_profile(provider, no_gp_profile)
    register_harness_profile(f"openai:{model_alias}", no_gp_profile)

    shared_mw: list[Any] = [SharedBudgetMiddleware(budget)]
    evidence_subagent: dict[str, Any] = {
        "name": "evidence_auditor",
        "description": (
            "Reads all submission documents and carrier appetite guides, resolves "
            "superseded versions, verifies required fields, and flags contradictions."
        ),
        "system_prompt": EVIDENCE_AUDITOR_PROMPT,
        "tools": read_tools,
        "model": chat_model,
        "middleware": shared_mw,
    }
    return create_deep_agent(
        model=chat_model,
        tools=read_tools,
        system_prompt=(
            f"{PLANNER_SYSTEM_PROMPT}\n\n"
            "Delegate detailed document cross-checking and contradiction detection to "
            "your 'evidence_auditor' specialist subagent, then synthesize the final "
            "triage findings clearly."
        ),
        middleware=shared_mw,
        subagents=[evidence_subagent],  # type: ignore[list-item]
        context_schema=RunContext,
    )


class SubmissionTriageState(TypedDict, total=False):
    submission_id: str
    planner_variant: str
    investigation_summary: str
    submission_brief: dict[str, Any] | None
    policy_decision: str | None
    operation_id: str | None
    approved_payload: dict[str, Any] | None
    approved_payload_hash: str | None
    approval_decision: dict[str, Any] | None
    write_intent_recorded: bool
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
    use_postgres: bool = False
    # In-memory dispatch ledger used only when use_postgres=False
    dispatched_ops: set[str] = field(default_factory=set)
    # Optional fault-injection hook: raise right after carrier POST before checkpoint
    simulate_crash_mid_post: bool = False


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
                    section_or_quote=first_doc.content.splitlines()[0],
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
                    section_or_quote=first_doc.content.splitlines()[0],
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
                section_or_quote=first_doc.content.splitlines()[0],
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

        # Allow tests to inject an explicit pre-built brief directly in state
        if state.get("submission_brief") is not None:
            brief = SubmissionBrief.model_validate(state["submission_brief"])
            return {
                "planner_variant": variant,
                "investigation_summary": brief.summary,
                "submission_brief": brief.model_dump(mode="json"),
                "trace_events": _append_trace(
                    state,
                    {"node": "triage_submission", "variant": variant, "mode": "injected"},
                ),
            }

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
            shared_budget = SharedCallBudget(
                max_model_calls=wf_deps.settings.max_model_calls,
                max_tool_calls=wf_deps.settings.max_tool_calls,
            )
            deep_agent = build_deep_agents_planner(
                chat_model=chat_model,
                model_alias=wf_deps.primary_model_alias,
                read_tools=read_tools,
                budget=shared_budget,
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
                    model=chat_model,
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

    # Node 2: evaluate_policy (shared control gate — requires explicit carrier & coverage!)
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

        # Enforce explicit carrier & coverage BEFORE approval (never fill defaults post-approval!)
        missing_quote_fields: list[str] = []
        if not brief.carrier_match or brief.carrier_match not in CARRIER_APPETITE_GUIDES:
            missing_quote_fields.append("carrier_match")
        if brief.requested_coverage_usd is None or brief.requested_coverage_usd <= 0:
            missing_quote_fields.append("requested_coverage_usd")
        if not brief.insured_name.strip():
            missing_quote_fields.append("insured_name")

        operation_id = state.get("operation_id") or f"quote_{uuid.uuid4().hex[:16]}"
        if missing_quote_fields:
            _record_audit(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_id=ctx.requester_id,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                    workflow_version="submission_triage+0.1.0",
                    event_type="authz_check",
                    decision="deny",
                    step_or_op_key=f"policy:{operation_id}:incomplete",
                    operation_id=operation_id,
                    details={
                        "reason": (
                            "ready_to_quote brief missing explicit quote fields; "
                            "refusing to fabricate post-approval defaults"
                        ),
                        "missing_quote_fields": missing_quote_fields,
                    },
                )
            )
            return {
                "policy_decision": "deny",
                "operation_id": operation_id,
                "final_status": "denied",
                "trace_events": _append_trace(
                    state,
                    {
                        "node": "evaluate_policy",
                        "decision": "denied_incomplete_payload",
                        "missing_quote_fields": missing_quote_fields,
                    },
                ),
            }

        assert brief.carrier_match is not None and brief.requested_coverage_usd is not None
        approved_payload: dict[str, Any] = {
            "operation_id": operation_id,
            "submission_id": state["submission_id"],
            "carrier_id": brief.carrier_match,
            "insured_name": brief.insured_name.strip(),
            "requested_coverage_usd": int(brief.requested_coverage_usd),
        }
        approved_payload_hash = _canonical_payload_hash(approved_payload)

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
                    "approved_payload": approved_payload,
                    "approved_payload_hash": approved_payload_hash,
                },
            )
        )
        return {
            "policy_decision": "require_approval",
            "operation_id": operation_id,
            "approved_payload": approved_payload,
            "approved_payload_hash": approved_payload_hash,
            "trace_events": _append_trace(
                state,
                {
                    "node": "evaluate_policy",
                    "decision": "require_approval",
                    "operation_id": operation_id,
                    "approved_payload_hash": approved_payload_hash,
                },
            ),
        }

    def route_after_policy(state: SubmissionTriageState) -> str:
        if state.get("policy_decision") == "require_approval":
            return "await_approval"
        return END

    # Node 3: await_approval (HITL interrupt accepting ONLY server-derived, credential-free record)
    async def await_approval(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        raw_resume = interrupt(
            {
                "type": "quote_approval",
                "operation_id": state["operation_id"],
                "submission_id": state["submission_id"],
                "approved_payload": state["approved_payload"],
                "approved_payload_hash": state["approved_payload_hash"],
                "submission_brief": state["submission_brief"],
            }
        )
        resume_dict = raw_resume if isinstance(raw_resume, dict) else {}
        action = str(resume_dict.get("decision", ""))
        approver_id = str(resume_dict.get("approver_id", ""))
        resumed_op_id = str(resume_dict.get("operation_id", ""))
        resumed_hash = str(resume_dict.get("approved_payload_hash", ""))
        binding_mac = str(resume_dict.get("binding_mac", ""))

        refusal_reason: str | None = None
        if any(k in resume_dict for k in ("approver_token", "bearer_token", "token")):
            refusal_reason = (
                "Raw bearer credentials must be validated at the resume boundary, not passed "
                "inside graph resume payloads"
            )
        elif action not in {"approve", "reject"}:
            refusal_reason = f"Invalid approval decision '{action}'"
        elif not approver_id or approver_id == ctx.requester_id:
            refusal_reason = "Separation of duties: requester cannot approve own quote"
        elif resumed_op_id != str(state["operation_id"]):
            refusal_reason = "Approval record operation_id mismatch"
        elif resumed_hash != str(state["approved_payload_hash"]):
            refusal_reason = "Approved payload hash mismatch"
        else:
            expected_mac = _compute_approval_binding_mac(
                tenant_id=ctx.tenant_id,
                approver_id=approver_id,
                decision=action,
                operation_id=str(state["operation_id"]),
                approved_payload_hash=str(state["approved_payload_hash"]),
                secret=wf_deps.settings.mcp_token_secret,
            )
            if not binding_mac or not hmac.compare_digest(binding_mac, expected_mac):
                refusal_reason = (
                    "Unauthenticated or forged approval record: caller must be verified at the "
                    "trusted resume boundary"
                )

        # Sanitized, credential-free approval record persisted in checkpoint state
        sanitized_record = {
            "decision": action if refusal_reason is None else "refused",
            "approver_id": approver_id if refusal_reason is None else "",
            "operation_id": str(state["operation_id"]),
            "approved_payload_hash": str(state["approved_payload_hash"]),
        }

        if refusal_reason is not None:
            _record_audit(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_id=ctx.requester_id,
                    requested_by=ctx.requester_id,
                    approved_by=None,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                    workflow_version="submission_triage+0.1.0",
                    event_type="approval_refused",
                    decision="refused",
                    step_or_op_key=f"approval:{state['operation_id']}:refused",
                    operation_id=state["operation_id"],
                    details={"reason": refusal_reason},
                )
            )
            return {
                "approval_decision": sanitized_record,
                "final_status": "denied",
                "trace_events": _append_trace(
                    state,
                    {"node": "await_approval", "decision": "refused", "reason": refusal_reason},
                ),
            }
        if action != "approve":
            _record_audit(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_id=approver_id,
                    requested_by=ctx.requester_id,
                    approved_by=approver_id,
                    thread_id=ctx.thread_id,
                    run_id=ctx.run_id,
                    workflow_version="submission_triage+0.1.0",
                    event_type="approval_decided",
                    decision="rejected",
                    step_or_op_key=f"approval:{state['operation_id']}:rejected",
                    operation_id=state["operation_id"],
                    details={"decision": "rejected"},
                )
            )
            return {
                "approval_decision": sanitized_record,
                "final_status": "rejected",
                "trace_events": _append_trace(
                    state,
                    {
                        "node": "await_approval",
                        "decision": "rejected",
                        "approver_id": approver_id,
                    },
                ),
            }
        _record_audit(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=approver_id,
                requested_by=ctx.requester_id,
                approved_by=approver_id,
                thread_id=ctx.thread_id,
                run_id=ctx.run_id,
                workflow_version="submission_triage+0.1.0",
                event_type="approval_decided",
                decision="approved",
                step_or_op_key=f"approval:{state['operation_id']}:approved",
                operation_id=state["operation_id"],
                details={"approved_payload_hash": str(state["approved_payload_hash"])},
            )
        )
        return {
            "approval_decision": sanitized_record,
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
        return "record_quote_attempt"

    # Node 4: record_quote_attempt (Durable pre-send intent checkpoint BEFORE opening HTTP socket)
    async def record_quote_attempt(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        payload = state.get("approved_payload")
        expected_hash = state.get("approved_payload_hash")
        if not payload or _canonical_payload_hash(payload) != expected_hash:
            raise ValueError("Refusing to record quote attempt: approved_payload integrity failure")
        return {
            "write_intent_recorded": True,
            "attempted_payload": dict(payload),
            "trace_events": _append_trace(
                state,
                {
                    "node": "record_quote_attempt",
                    "operation_id": state["operation_id"],
                    "approved_payload_hash": expected_hash,
                },
            ),
        }

    async def _claim_carrier_dispatch(
        *, operation_id: str, tenant_id: str, payload_hash: str
    ) -> bool:
        """Atomically claim one-time dispatch permission across processes (Postgres or memory)."""
        if wf_deps.use_postgres:
            async with (
                await psycopg.AsyncConnection.connect(wf_deps.settings.database_url) as conn,
                conn.transaction(),
                conn.cursor() as cur,
            ):
                await set_tenant_context(conn, tenant_id)
                await cur.execute(
                    """
                    INSERT INTO carrier_dispatch_log (operation_id, tenant_id, payload_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (operation_id) DO NOTHING
                    RETURNING operation_id
                    """,
                    (operation_id, tenant_id, payload_hash),
                )
                return (await cur.fetchone()) is not None
        if operation_id in wf_deps.dispatched_ops:
            return False
        wf_deps.dispatched_ops.add(operation_id)
        return True

    # Node 5: submit_quote (Reconcile-before-resend across processes; exact approved payload only)
    async def submit_quote(
        state: SubmissionTriageState, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ctx = _ctx(runtime)
        op_id = str(state["operation_id"])
        approver_id = str((state.get("approval_decision") or {}).get("approver_id", ""))
        payload = dict(state["attempted_payload"] or {})
        payload_hash = _canonical_payload_hash(payload)
        if payload_hash != state.get("approved_payload_hash"):
            raise ValueError("Refusing to submit quote: attempted_payload hash mismatch")

        # Durable cross-process reconcile-before-resend guard: if a prior process already claimed
        # dispatch before crashing mid-POST, NEVER send a second POST — reconcile immediately!
        claimed = await _claim_carrier_dispatch(
            operation_id=op_id, tenant_id=ctx.tenant_id, payload_hash=payload_hash
        )
        if not claimed:
            return {
                "timeout_error": state.get("timeout_error") or "Recovered after mid-POST crash",
                "trace_events": _append_trace(
                    state,
                    {
                        "node": "submit_quote",
                        "outcome": "reconcile_before_resend",
                        "skipped_duplicate_post": True,
                        "operation_id": op_id,
                    },
                ),
            }

        if not wf_deps.carrier_base_url:
            return {
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
            if os.getenv("FAULT_INJECT") == "after_carrier_post":
                os._exit(137)
            if wf_deps.simulate_crash_mid_post:
                raise RuntimeError("Simulated worker crash after carrier POST before checkpoint")
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
        except RuntimeError:
            # Propagate simulated crash to prove restart reconciles without a second POST
            raise
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            return {
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

    # Node 6: reconcile_carrier (lookup by stable operation_id or escalate unverifiable outcome)
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
                "The quote request timed out or crashed after POST /v1/quotes was sent, and "
                "carrier status lookup could not verify whether the request committed. Automatic "
                "retry is blocked to prevent duplicate carrier submissions."
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

    # Node 7: await_reconciliation_review (Human review gate — NEVER re-posts to carrier!)
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
    builder.add_node("record_quote_attempt", record_quote_attempt)
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
        {"record_quote_attempt": "record_quote_attempt", END: END},
    )
    builder.add_edge("record_quote_attempt", "submit_quote")
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
    "AIMessage",
    "ApprovalAuthorizationError",
    "SharedBudgetExceededError",
    "SharedBudgetMiddleware",
    "SharedCallBudget",
    "SubmissionTriageDeps",
    "SubmissionTriageState",
    "build_deep_agents_planner",
    "build_submission_read_tools",
    "build_submission_triage_graph",
    "build_verified_approval_command",
]
