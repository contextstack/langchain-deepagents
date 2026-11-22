"""Read-only Coverage Discrepancy Audit runner: Baseline vs. Deep Agents with equal budgets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain_core.runnables import RunnableConfig

from core.config import Settings, get_settings
from core.models import RunContext
from gateway.factory import get_chat_model
from workflows.coverage_audit.models import CoverageAuditBrief, CoverageCitation
from workflows.coverage_audit.tools import (
    ARCHIVE_ROOT,
    build_coverage_audit_tools,
    load_packet_documents,
)

AUDIT_PLANNER_PROMPT = (
    "You are a Commercial Insurance Coverage Discrepancy Auditor. "
    "Use ONLY the read-only policy-archive tools to inspect the packet documents, trace every "
    "endorsement replacement/revocation chain, verify execution/countersignature status, check "
    "effective dates against the date of loss, verify entity/location scope, and check for "
    "missing incorporated schedules.\n"
    "Rules:\n"
    "- Unsigned drafts, expired prior-year lists, marketing brochures, and informal broker "
    "summaries do NOT bind coverage.\n"
    "- If an executed endorsement expressly supersedes or revokes an earlier endorsement, follow "
    "the full executed chain to the controlling endorsement effective on the date of loss.\n"
    "- If coverage depends on an incorporated schedule or countersignature that is absent from "
    "the packet, you MUST abstain with determination='insufficient_evidence' and list the "
    "missing document(s).\n"
    "- Cite exact doc_id values, 1-indexed line numbers, and verbatim quotes for every finding."
)

CHAIN_AUDITOR_PROMPT = (
    "You are a Policy Endorsement Chain Auditor specialist. "
    "Read the packet documents using the read-only tools and return a concise structured audit:\n"
    "1. Chronological list of executed endorsements (noting any supersession/revocation links)\n"
    "2. Unsigned drafts, expired docs, or cross-entity distractors to ignore\n"
    "3. Effective date & entity applicability against the claim notice\n"
    "4. Any missing incorporated schedules or missing countersignatures\n"
    "5. Exact doc_ids, line numbers, and verbatim quotes supporting the controlling conclusion."
)

AUDIT_SYNTHESIS_PROMPT = (
    "Produce a structured CoverageAuditBrief matching the schema from the investigation below.\n"
    "Rules:\n"
    "- packet_id MUST be '{packet_id}'.\n"
    "- determination MUST be one of: 'covered', 'covered_with_deductible', 'excluded', "
    "'insufficient_evidence'.\n"
    "- controlling_value: exact controlling dollar amount (e.g. '$300,000'), 'excluded', or "
    "'insufficient_evidence'.\n"
    "- controlling_doc_ids: exact doc_id filename(s) of the controlling document(s).\n"
    "- supersession_chain: ordered doc_id filenames from base declarations through executed "
    "endorsements (exclude unsigned drafts).\n"
    "- evidence_citations: include exact doc_id, 1-indexed line_start/line_end, verbatim_quote "
    "copied verbatim from that document, atomic claim_supported, subject, value, and polarity.\n\n"
    "Audit Question: {prompt}\n"
    "Investigation Findings:\n{investigation}"
)


class CoverageBudgetExceededError(RuntimeError):
    """Raised when an attempt exceeds its shared model-call, tool-call, or token budget."""


@dataclass
class CoverageAuditBudget:
    """Equal whole-attempt budget shared across coordinator, specialist, and synthesis."""

    max_model_calls: int = 16
    max_tool_calls: int = 32
    max_total_tokens: int = 96_000
    model_calls: int = 0
    tool_calls: int = 0
    total_tokens: int = 0

    def record_model_call(self) -> None:
        self.model_calls += 1
        if self.model_calls > self.max_model_calls:
            raise CoverageBudgetExceededError(
                f"Model call budget exceeded ({self.model_calls}/{self.max_model_calls})"
            )

    def record_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            raise CoverageBudgetExceededError(
                f"Tool call budget exceeded ({self.tool_calls}/{self.max_tool_calls})"
            )

    def record_tokens(self, tokens: int) -> None:
        self.total_tokens += max(0, tokens)
        if self.total_tokens > self.max_total_tokens:
            raise CoverageBudgetExceededError(
                f"Token budget exceeded ({self.total_tokens}/{self.max_total_tokens})"
            )


class CoverageBudgetMiddleware(AgentMiddleware[Any, Any]):
    """Enforces CoverageAuditBudget across Baseline or Deep Agents coordinator + subagents."""

    def __init__(self, budget: CoverageAuditBudget) -> None:
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


def build_coverage_deep_agent(
    *,
    chat_model: Any,
    model_alias: str,
    read_tools: list[Any],
    budget: CoverageAuditBudget,
) -> Any:
    """Build Deep Agents coverage auditor with general-purpose disabled & one shared budget."""
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

    shared_mw: list[Any] = [CoverageBudgetMiddleware(budget)]
    specialist: dict[str, Any] = {
        "name": "policy_chain_auditor",
        "description": (
            "Inspects policy declarations, endorsements, drafts, and schedules to trace "
            "supersession chains, effective dates, entity scope, and missing schedules."
        ),
        "system_prompt": CHAIN_AUDITOR_PROMPT,
        "tools": read_tools,
        "model": chat_model,
        "middleware": shared_mw,
    }
    return create_deep_agent(
        model=chat_model,
        tools=read_tools,
        system_prompt=(
            f"{AUDIT_PLANNER_PROMPT}\n\n"
            "Delegate endorsement chain tracing and document verification to your "
            "'policy_chain_auditor' specialist, then synthesize the final audit findings."
        ),
        middleware=shared_mw,
        subagents=[specialist],  # type: ignore[list-item]
        context_schema=RunContext,
    )


@dataclass
class CoverageAuditDeps:
    settings: Settings = field(default_factory=get_settings)
    planner_variant: Literal["baseline", "deep_agents"] = "baseline"
    primary_model_alias: str = "eval-anthropic"
    archive_root: Path = ARCHIVE_ROOT
    max_model_calls: int = 16
    max_tool_calls: int = 32
    max_total_tokens: int = 96_000
    timeout_s: float = 180.0
    callbacks: list[Any] = field(default_factory=list)


def _deterministic_mock_audit_brief(
    packet_id: str,
    archive_root: Path,
) -> CoverageAuditBrief:
    """Offline $0 mock brief for dry-run CI tests (reads ONLY policy-archive/<packet_id>/)."""
    docs = load_packet_documents(packet_id, archive_root)
    first_doc = sorted(docs.keys())[0] if docs else "policy_declarations.txt"
    first_line = docs[first_doc].splitlines()[0] if docs else ""
    return CoverageAuditBrief(
        packet_id=packet_id,
        determination="insufficient_evidence",
        controlling_value="insufficient_evidence",
        controlling_doc_ids=[first_doc],
        supersession_chain=[first_doc],
        evidence_citations=[
            CoverageCitation(
                doc_id=first_doc,
                line_start=1,
                line_end=1,
                verbatim_quote=first_line,
                claim_supported=f"Mock audit read {first_doc}.",
            )
        ],
        missing_documents=[],
        rationale="Offline mock mode dry-run.",
    )


async def run_coverage_audit_attempt(
    *,
    packet_id: str,
    prompt: str,
    deps: CoverageAuditDeps,
    context: RunContext,
) -> dict[str, Any]:
    """Execute one isolated read-only Coverage Discrepancy Audit attempt under shared budget."""
    read_log: list[dict[str, Any]] = []
    read_tools = build_coverage_audit_tools(
        allowed_packet_id=packet_id,
        archive_root=deps.archive_root,
        read_log=read_log,
    )
    budget = CoverageAuditBudget(
        max_model_calls=deps.max_model_calls,
        max_tool_calls=deps.max_tool_calls,
        max_total_tokens=deps.max_total_tokens,
    )

    if deps.settings.model_mode == "mock":
        brief = _deterministic_mock_audit_brief(packet_id, deps.archive_root)
        return {
            "packet_id": packet_id,
            "variant": deps.planner_variant,
            "brief": brief.model_dump(mode="json"),
            "doc_reads": read_log,
            "budget_used": {
                "model_calls": budget.model_calls,
                "tool_calls": budget.tool_calls,
                "total_tokens": budget.total_tokens,
            },
        }

    chat_model = get_chat_model(
        alias=deps.primary_model_alias,
        tenant_id=context.tenant_id,
        settings=deps.settings,
    )
    invoke_cfg: RunnableConfig = {"callbacks": deps.callbacks} if deps.callbacks else {}

    async def _execute() -> CoverageAuditBrief:
        if deps.planner_variant == "deep_agents":
            agent = build_coverage_deep_agent(
                chat_model=chat_model,
                model_alias=deps.primary_model_alias,
                read_tools=read_tools,
                budget=budget,
            )
        else:
            agent = create_agent(
                model=chat_model,
                tools=read_tools,
                system_prompt=AUDIT_PLANNER_PROMPT,
                context_schema=RunContext,
                middleware=[
                    SummarizationMiddleware(
                        model=chat_model,
                        trigger=("tokens", 8000),
                        keep=("messages", 10),
                    ),
                    CoverageBudgetMiddleware(budget),
                ],
            )

        agent_res = await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=invoke_cfg,
            context=context,
        )
        # Check token budget before synthesis
        for cb in deps.callbacks:
            stats = getattr(cb, "stats", None)
            if stats is not None and getattr(stats, "total_tokens", 0) > budget.max_total_tokens:
                raise CoverageBudgetExceededError(
                    f"Token budget exceeded ({stats.total_tokens}/{budget.max_total_tokens})"
                )

        last_msg = agent_res["messages"][-1]
        investigation = str(getattr(last_msg, "content", last_msg))
        budget.record_model_call()
        structured = chat_model.with_structured_output(CoverageAuditBrief)
        brief_obj: CoverageAuditBrief = await structured.ainvoke(
            AUDIT_SYNTHESIS_PROMPT.format(
                packet_id=packet_id,
                prompt=prompt,
                investigation=investigation,
            ),
            config=invoke_cfg,
        )
        return brief_obj

    brief_out = await asyncio.wait_for(_execute(), timeout=deps.timeout_s)
    return {
        "packet_id": packet_id,
        "variant": deps.planner_variant,
        "brief": brief_out.model_dump(mode="json"),
        "doc_reads": read_log,
        "budget_used": {
            "model_calls": budget.model_calls,
            "tool_calls": budget.tool_calls,
            "total_tokens": budget.total_tokens,
        },
    }
