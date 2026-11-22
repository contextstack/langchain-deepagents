"""Experiment 2 boundary tests: ambiguous carrier HTTP response recovery & unverifiable review."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from core.config import Settings
from core.models import RunContext
from mcp_servers.mock_carrier import MockCarrierState, create_mock_carrier_app
from workflows.submission_triage.graph import (
    SubmissionTriageDeps,
    build_submission_triage_graph,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def _running_carrier(state: MockCarrierState) -> AsyncIterator[str]:
    port = _free_port()
    app = create_mock_carrier_app(state)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(srv.serve())
    try:
        while not srv.started:
            await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        await asyncio.gather(task, return_exceptions=True)


async def test_exp2_behavior1_timeout_reconciles_via_status_lookup_exactly_once() -> None:
    """Carrier commits then withholds response; status lookup recovers carrier_ref with 1 write."""
    carrier_state = MockCarrierState(
        fault_mode="commit_then_hang_lookup_ok",
        hang_seconds=1.2,
    )
    async with _running_carrier(carrier_state) as base_url:
        saver = InMemorySaver()
        deps = SubmissionTriageDeps(
            settings=Settings(model_mode="mock"),
            planner_variant="baseline",
            carrier_base_url=base_url,
            carrier_timeout_s=0.3,
        )
        graph = build_submission_triage_graph(deps=deps, checkpointer=saver)
        ctx = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="exp2-lookup-ok",
            run_id="run-exp2-1",
            request_id="req-exp2-1",
        )
        cfg = {"configurable": {"thread_id": ctx.thread_id}}

        # 1. Triage -> pauses at HITL approval
        await graph.ainvoke(
            {"submission_id": "sub-01", "planner_variant": "baseline"},
            config=cfg,
            context=ctx,
            durability="sync",
        )
        snap = await graph.aget_state(cfg)
        assert snap.interrupts and snap.interrupts[0].value["type"] == "quote_approval"
        op_id = snap.interrupts[0].value["operation_id"]

        # 2. Approve -> POST times out after carrier commits -> reconciles via GET /{op_id}
        final = await graph.ainvoke(
            Command(resume={"decision": "approve", "approver_id": "bob"}),
            config=cfg,
            context=ctx,
            durability="sync",
        )
        assert final["final_status"] == "completed"
        assert final["carrier_ref"] and str(final["carrier_ref"]).startswith("CAR-")
        assert final["timeout_error"] is not None

        # 3. Direct boundary verification on Mock Carrier HTTP stats
        async with httpx.AsyncClient() as client:
            stats = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats["committed_writes"] == 1
        assert stats["http_post_attempts"] == 1
        assert stats["lookup_attempts"] == 1
        assert stats["operations"][op_id]["carrier_ref"] == final["carrier_ref"]


async def test_exp2_behavior2_unverifiable_outcome_routes_to_human_without_second_post() -> None:
    """Carrier commits then hangs & lookup returns 501; routes to human review, NEVER re-POSTs."""
    carrier_state = MockCarrierState(
        fault_mode="commit_then_hang_unverifiable",
        hang_seconds=1.2,
    )
    async with _running_carrier(carrier_state) as base_url:
        saver = InMemorySaver()
        deps = SubmissionTriageDeps(
            settings=Settings(model_mode="mock"),
            planner_variant="deep_agents",
            carrier_base_url=base_url,
            carrier_timeout_s=0.3,
        )
        graph = build_submission_triage_graph(deps=deps, checkpointer=saver)
        ctx = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="exp2-unverifiable",
            run_id="run-exp2-2",
            request_id="req-exp2-2",
        )
        cfg = {"configurable": {"thread_id": ctx.thread_id}}

        # 1. Triage -> pauses at quote approval
        await graph.ainvoke(
            {"submission_id": "sub-01", "planner_variant": "deep_agents"},
            config=cfg,
            context=ctx,
            durability="sync",
        )
        snap = await graph.aget_state(cfg)
        op_id = snap.interrupts[0].value["operation_id"]

        # 2. Approve -> POST times out -> GET returns 501 -> pauses at human reconciliation review
        await graph.ainvoke(
            Command(resume={"decision": "approve", "approver_id": "bob"}),
            config=cfg,
            context=ctx,
            durability="sync",
        )
        review_snap = await graph.aget_state(cfg)
        assert review_snap.interrupts
        review_payload = review_snap.interrupts[0].value
        assert review_payload["type"] == "reconciliation_human_review"
        assert review_payload["operation_id"] == op_id
        assert review_payload["attempted_payload"]["submission_id"] == "sub-01"
        assert "501" in review_payload["lookup_error"]
        assert "why_uncertain" in review_payload

        # 3. Simulate restart with a fresh compiled graph instance sharing the checkpointer,
        #    then resume with human reviewer decision -> MUST NOT send a second POST!
        restarted_graph = build_submission_triage_graph(deps=deps, checkpointer=saver)
        final = await restarted_graph.ainvoke(
            Command(
                resume={
                    "decision": "confirmed_committed_offline",
                    "carrier_ref": "CAR-VERIFIED-OFFLINE",
                    "reviewer_id": "bob",
                }
            ),
            config=cfg,
            context=ctx,
            durability="sync",
        )
        assert final["final_status"] == "resolved_by_human"
        assert final["carrier_ref"] == "CAR-VERIFIED-OFFLINE"

        # 4. Direct boundary verification: carrier saw EXACTLY ONE POST and ONE committed write
        async with httpx.AsyncClient() as client:
            stats = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats["http_post_attempts"] == 1
        assert stats["committed_writes"] == 1
        assert stats["lookup_attempts"] == 1


async def test_exp2_crash_mid_post_reconciles_on_restart_without_resend() -> None:
    """Crash after carrier commits POST (before checkpoint) -> restart reconciles, 1 POST total."""
    carrier_state = MockCarrierState(fault_mode="normal")
    async with _running_carrier(carrier_state) as base_url:
        saver = InMemorySaver()
        shared_dispatched: set[str] = set()
        crashing_deps = SubmissionTriageDeps(
            settings=Settings(model_mode="mock"),
            planner_variant="baseline",
            carrier_base_url=base_url,
            carrier_timeout_s=2.0,
            dispatched_ops=shared_dispatched,
            simulate_crash_mid_post=True,
        )
        graph = build_submission_triage_graph(deps=crashing_deps, checkpointer=saver)
        ctx = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="exp2-crash-mid-post",
            run_id="run-exp2-3",
            request_id="req-exp2-3",
        )
        cfg = {"configurable": {"thread_id": ctx.thread_id}}

        await graph.ainvoke(
            {"submission_id": "sub-01", "planner_variant": "baseline"},
            config=cfg,
            context=ctx,
            durability="sync",
        )
        snap = await graph.aget_state(cfg)
        op_id = snap.interrupts[0].value["operation_id"]

        # Approve -> carrier commits POST, then worker crashes before submit_quote checkpoints!
        import pytest  # noqa: PLC0415

        with pytest.raises(RuntimeError, match="Simulated worker crash"):
            await graph.ainvoke(
                Command(resume={"decision": "approve", "approver_id": "bob"}),
                config=cfg,
                context=ctx,
                durability="sync",
            )
        assert op_id in shared_dispatched

        # Restart fresh graph sharing checkpointer + durable dispatched ledger -> MUST reconcile,
        # NOT send a second POST!
        recovered_deps = SubmissionTriageDeps(
            settings=Settings(model_mode="mock"),
            planner_variant="baseline",
            carrier_base_url=base_url,
            carrier_timeout_s=2.0,
            dispatched_ops=shared_dispatched,
            simulate_crash_mid_post=False,
        )
        restarted_graph = build_submission_triage_graph(deps=recovered_deps, checkpointer=saver)
        final = await restarted_graph.ainvoke(
            None,
            config=cfg,
            context=ctx,
            durability="sync",
        )
        assert final["final_status"] == "completed"
        assert final["carrier_ref"] and str(final["carrier_ref"]).startswith("CAR-")

        async with httpx.AsyncClient() as client:
            stats = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats["http_post_attempts"] == 1
        assert stats["committed_writes"] == 1
        assert stats["lookup_attempts"] == 1


async def test_exp2_rejects_missing_explicit_quote_fields_and_unauthorized_approver() -> None:
    """Explicit carrier & coverage are required pre-approval; approver role is enforced."""
    from workflows.submission_triage.models import SubmissionBrief  # noqa: PLC0415

    saver = InMemorySaver()
    deps = SubmissionTriageDeps(settings=Settings(model_mode="mock"))
    graph = build_submission_triage_graph(deps=deps, checkpointer=saver)
    ctx = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="exp2-incomplete-brief",
        run_id="run-exp2-4",
        request_id="req-exp2-4",
    )

    # 1. Incomplete ready_to_quote brief (no carrier_match, no requested_coverage_usd) -> DENIED
    incomplete = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        carrier_match=None,
        requested_coverage_usd=None,
        summary="Incomplete ready_to_quote brief.",
    )
    res = await graph.ainvoke(
        {
            "submission_id": "sub-01",
            "submission_brief": incomplete.model_dump(mode="json"),
        },
        config={"configurable": {"thread_id": ctx.thread_id}},
        context=ctx,
        durability="sync",
    )
    assert res["final_status"] == "denied"
    assert res["policy_decision"] == "deny"

    # 2. Valid brief, but approver lacks ops-approver/admin role -> REFUSED
    ctx2 = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="exp2-unauth-approver",
        run_id="run-exp2-5",
        request_id="req-exp2-5",
    )
    cfg2 = {"configurable": {"thread_id": ctx2.thread_id}}
    await graph.ainvoke(
        {"submission_id": "sub-01"},
        config=cfg2,
        context=ctx2,
        durability="sync",
    )
    denied = await graph.ainvoke(
        Command(
            resume={
                "decision": "approve",
                "approver_id": "charlie",
                "approver_roles": ["engineer"],
            }
        ),
        config=cfg2,
        context=ctx2,
        durability="sync",
    )
    assert denied["final_status"] == "denied"
