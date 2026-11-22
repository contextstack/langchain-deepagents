"""Experiment 2 boundary tests: real process-kill PostgreSQL recovery & unverifiable review."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from core.auth import mint_user_jwt
from core.config import Settings, get_settings
from core.db import apply_app_migrations
from core.models import CallerIdentity, RunContext
from mcp_servers.mock_carrier import MockCarrierState, create_mock_carrier_app
from workflows.submission_triage.graph import (
    SubmissionTriageDeps,
    build_submission_triage_graph,
)
from workflows.submission_triage.models import SubmissionBrief


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


def _bob_jwt(settings: Settings) -> str:
    return mint_user_jwt(
        CallerIdentity(
            tenant_id="tenant-alpha",
            actor_id="bob",
            roles=frozenset({"ops-approver"}),
        ),
        secret=settings.jwt_signing_secret,
    )


async def test_exp2_behavior1_timeout_reconciles_via_status_lookup_exactly_once() -> None:
    """Carrier commits then withholds response; status lookup recovers carrier_ref with 1 write."""
    carrier_state = MockCarrierState(
        fault_mode="commit_then_hang_lookup_ok",
        hang_seconds=1.2,
    )
    async with _running_carrier(carrier_state) as base_url:
        saver = InMemorySaver()
        settings = Settings(model_mode="mock")
        deps = SubmissionTriageDeps(
            settings=settings,
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

        await graph.ainvoke(
            {"submission_id": "sub-01", "planner_variant": "baseline"},
            config=cfg,
            context=ctx,
            durability="sync",
        )
        snap = await graph.aget_state(cfg)
        assert snap.interrupts and snap.interrupts[0].value["type"] == "quote_approval"
        op_id = snap.interrupts[0].value["operation_id"]

        final = await graph.ainvoke(
            Command(resume={"decision": "approve", "approver_token": _bob_jwt(settings)}),
            config=cfg,
            context=ctx,
            durability="sync",
        )
        assert final["final_status"] == "completed"
        assert final["carrier_ref"] and str(final["carrier_ref"]).startswith("CAR-")
        assert final["timeout_error"] is not None

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
        settings = Settings(model_mode="mock")
        deps = SubmissionTriageDeps(
            settings=settings,
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

        await graph.ainvoke(
            {"submission_id": "sub-01", "planner_variant": "deep_agents"},
            config=cfg,
            context=ctx,
            durability="sync",
        )
        snap = await graph.aget_state(cfg)
        op_id = snap.interrupts[0].value["operation_id"]

        await graph.ainvoke(
            Command(resume={"decision": "approve", "approver_token": _bob_jwt(settings)}),
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

        async with httpx.AsyncClient() as client:
            stats = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats["http_post_attempts"] == 1
        assert stats["committed_writes"] == 1
        assert stats["lookup_attempts"] == 1


async def test_exp2_real_process_kill_postgres_recovery_sends_exactly_one_post() -> None:
    """Real OS process kill (os._exit(137)) after carrier commit -> fresh process reconciles."""
    settings = get_settings()
    await apply_app_migrations(settings.database_admin_url)
    async with AsyncPostgresSaver.from_conn_string(settings.database_admin_url) as saver:
        await saver.setup()

    thread_id = f"exp2-prockill-{uuid.uuid4().hex[:10]}"
    carrier_state = MockCarrierState(fault_mode="normal")

    async with _running_carrier(carrier_state) as base_url:
        bob_token = _bob_jwt(settings)

        # Child process 1: triage -> approve -> POST commits to carrier -> os._exit(137)!
        child1_code = (
            "import asyncio\n"
            "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver\n"
            "from langgraph.types import Command\n"
            "from core.config import Settings\n"
            "from core.models import RunContext\n"
            "from workflows.submission_triage.graph import (\n"
            "    SubmissionTriageDeps, build_submission_triage_graph\n"
            ")\n"
            "async def main():\n"
            "    s = Settings(model_mode='mock')\n"
            "    deps = SubmissionTriageDeps(\n"
            f"        settings=s, use_postgres=True, carrier_base_url={base_url!r},\n"
            "        carrier_timeout_s=5.0,\n"
            "    )\n"
            "    async with AsyncPostgresSaver.from_conn_string(s.database_admin_url) as cp:\n"
            "        g = build_submission_triage_graph(deps=deps, checkpointer=cp)\n"
            f"        ctx = RunContext(tenant_id='tenant-alpha', requester_id='alice',\n"
            f"            requester_roles=frozenset({{'underwriter'}}), thread_id={thread_id!r},\n"
            "            run_id='r-pk-1', request_id='req-pk-1')\n"
            f"        cfg = {{'configurable': {{'thread_id': {thread_id!r}}}}}\n"
            "        await g.ainvoke({'submission_id': 'sub-01'}, config=cfg, context=ctx,\n"
            "            durability='sync')\n"
            "        await g.ainvoke(\n"
            f"            Command(resume={{'decision': 'approve',\n"
            f"                'approver_token': {bob_token!r}}}),\n"
            "            config=cfg, context=ctx, durability='sync',\n"
            "        )\n"
            "asyncio.run(main())\n"
        )
        env1 = {**os.environ, "FAULT_INJECT": "after_carrier_post", "MODEL_MODE": "mock"}
        proc1 = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", child1_code],
            env=env1,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc1.returncode == 137, (
            f"Expected 137 crash, got {proc1.returncode}: {proc1.stderr}"
        )

        # Fresh OS Process 2: ZERO shared memory! Resumes from Postgres -> MUST reconcile,
        # NOT send a second POST!
        child2_code = (
            "import asyncio, json\n"
            "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver\n"
            "from core.config import Settings\n"
            "from core.models import RunContext\n"
            "from workflows.submission_triage.graph import (\n"
            "    SubmissionTriageDeps, build_submission_triage_graph\n"
            ")\n"
            "async def main():\n"
            "    s = Settings(model_mode='mock')\n"
            "    deps = SubmissionTriageDeps(\n"
            f"        settings=s, use_postgres=True, carrier_base_url={base_url!r},\n"
            "        carrier_timeout_s=5.0,\n"
            "    )\n"
            "    async with AsyncPostgresSaver.from_conn_string(s.database_admin_url) as cp:\n"
            "        g = build_submission_triage_graph(deps=deps, checkpointer=cp)\n"
            f"        ctx = RunContext(tenant_id='tenant-alpha', requester_id='alice',\n"
            f"            requester_roles=frozenset({{'underwriter'}}), thread_id={thread_id!r},\n"
            "            run_id='r-pk-2', request_id='req-pk-1')\n"
            f"        cfg = {{'configurable': {{'thread_id': {thread_id!r}}}}}\n"
            "        res = await g.ainvoke(None, config=cfg, context=ctx, durability='sync')\n"
            "        print(json.dumps({'final_status': res.get('final_status'),\n"
            "            'carrier_ref': res.get('carrier_ref')}))\n"
            "asyncio.run(main())\n"
        )
        env2 = {k: v for k, v in os.environ.items() if k != "FAULT_INJECT"}
        env2["MODEL_MODE"] = "mock"
        proc2 = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", child2_code],
            env=env2,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc2.returncode == 0, f"Recovery failed: {proc2.stderr}"
        out = json.loads(proc2.stdout.strip().splitlines()[-1])
        assert out["final_status"] == "completed"
        assert out["carrier_ref"] and str(out["carrier_ref"]).startswith("CAR-")

        # Direct HTTP boundary check: EXACTLY ONE POST across both OS processes!
        async with httpx.AsyncClient() as client:
            stats = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats["http_post_attempts"] == 1
        assert stats["committed_writes"] == 1
        assert stats["lookup_attempts"] == 1


async def test_exp2_rejects_missing_quote_fields_and_fail_closed_approval_roles() -> None:
    """Explicit carrier/coverage required; omitted approver roles MUST fail closed."""
    saver = InMemorySaver()
    settings = Settings(model_mode="mock")
    deps = SubmissionTriageDeps(settings=settings)
    graph = build_submission_triage_graph(deps=deps, checkpointer=saver)

    # 1. Incomplete ready_to_quote brief -> DENIED
    ctx1 = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="exp2-incomplete-brief",
        run_id="run-exp2-4",
        request_id="req-exp2-4",
    )
    incomplete = SubmissionBrief(
        disposition="ready_to_quote",
        insured_name="Northstar Analytics Inc.",
        carrier_match=None,
        requested_coverage_usd=None,
        summary="Incomplete ready_to_quote brief.",
    )
    res = await graph.ainvoke(
        {"submission_id": "sub-01", "submission_brief": incomplete.model_dump(mode="json")},
        config={"configurable": {"thread_id": ctx1.thread_id}},
        context=ctx1,
        durability="sync",
    )
    assert res["final_status"] == "denied"

    # 2. Omitting approver_roles MUST fail closed (never default to ops-approver!)
    ctx2 = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="exp2-omitted-roles",
        run_id="run-exp2-5",
        request_id="req-exp2-5",
    )
    cfg2 = {"configurable": {"thread_id": ctx2.thread_id}}
    await graph.ainvoke({"submission_id": "sub-01"}, config=cfg2, context=ctx2, durability="sync")
    denied_omitted = await graph.ainvoke(
        Command(resume={"decision": "approve", "approver_id": "bob"}),
        config=cfg2,
        context=ctx2,
        durability="sync",
    )
    assert denied_omitted["final_status"] == "denied"

    # 3. Cross-tenant approver JWT MUST be refused
    ctx3 = RunContext(
        tenant_id="tenant-alpha",
        requester_id="alice",
        requester_roles=frozenset({"underwriter"}),
        thread_id="exp2-cross-tenant-jwt",
        run_id="run-exp2-6",
        request_id="req-exp2-6",
    )
    cfg3 = {"configurable": {"thread_id": ctx3.thread_id}}
    await graph.ainvoke({"submission_id": "sub-01"}, config=cfg3, context=ctx3, durability="sync")
    beta_tok = mint_user_jwt(
        CallerIdentity(tenant_id="tenant-beta", actor_id="carol", roles=frozenset({"admin"})),
        secret=settings.jwt_signing_secret,
    )
    denied_beta = await graph.ainvoke(
        Command(resume={"decision": "approve", "approver_token": beta_tok}),
        config=cfg3,
        context=ctx3,
        durability="sync",
    )
    assert denied_beta["final_status"] == "denied"

    # 4. In-memory crash-mid-post recovery check
    carrier_state = MockCarrierState(fault_mode="normal")
    async with _running_carrier(carrier_state) as base_url:
        dispatched: set[str] = set()
        d_crash = SubmissionTriageDeps(
            settings=settings,
            carrier_base_url=base_url,
            carrier_timeout_s=2.0,
            dispatched_ops=dispatched,
            simulate_crash_mid_post=True,
        )
        g_crash = build_submission_triage_graph(deps=d_crash, checkpointer=saver)
        ctx4 = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="exp2-inmem-crash",
            run_id="run-exp2-7",
            request_id="req-exp2-7",
        )
        cfg4 = {"configurable": {"thread_id": ctx4.thread_id}}
        await g_crash.ainvoke({"submission_id": "sub-01"}, config=cfg4, context=ctx4)
        with pytest.raises(RuntimeError, match="Simulated worker crash"):
            await g_crash.ainvoke(
                Command(resume={"decision": "approve", "approver_token": _bob_jwt(settings)}),
                config=cfg4,
                context=ctx4,
            )
        d_rec = SubmissionTriageDeps(
            settings=settings,
            carrier_base_url=base_url,
            carrier_timeout_s=2.0,
            dispatched_ops=dispatched,
        )
        g_rec = build_submission_triage_graph(deps=d_rec, checkpointer=saver)
        done = await g_rec.ainvoke(None, config=cfg4, context=ctx4)
        assert done["final_status"] == "completed"
