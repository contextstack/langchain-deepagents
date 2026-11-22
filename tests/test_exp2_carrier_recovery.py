"""Experiment 2 boundary tests: authenticated approval binding, process-kill recovery, & review."""

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
import psycopg
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
    ApprovalAuthorizationError,
    SubmissionTriageDeps,
    build_submission_triage_graph,
    build_verified_approval_command,
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
        intr = snap.interrupts[0].value
        op_id = intr["operation_id"]

        cmd = build_verified_approval_command(
            bearer_token=f"Bearer {_bob_jwt(settings)}",
            decision="approve",
            approval_tenant_id="tenant-alpha",
            requested_by="alice",
            operation_id=op_id,
            approved_payload_hash=intr["approved_payload_hash"],
            settings=settings,
        )
        final = await graph.ainvoke(cmd, config=cfg, context=ctx, durability="sync")
        assert final["final_status"] == "completed"
        assert final["carrier_ref"] and str(final["carrier_ref"]).startswith("CAR-")
        assert final["timeout_error"] is not None
        assert any(
            a.event_type == "reconciliation" and a.approved_by == "bob" for a in deps.audit_memory
        )

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
        intr = snap.interrupts[0].value
        op_id = intr["operation_id"]

        cmd = build_verified_approval_command(
            bearer_token=_bob_jwt(settings),
            decision="approve",
            approval_tenant_id="tenant-alpha",
            requested_by="alice",
            operation_id=op_id,
            approved_payload_hash=intr["approved_payload_hash"],
            settings=settings,
        )
        await graph.ainvoke(cmd, config=cfg, context=ctx, durability="sync")
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

        child1_code = (
            "import asyncio\n"
            "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver\n"
            "from core.config import Settings\n"
            "from core.models import RunContext\n"
            "from workflows.submission_triage.graph import (\n"
            "    SubmissionTriageDeps, build_submission_triage_graph,\n"
            "    build_verified_approval_command,\n"
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
            "        snap = await g.aget_state(cfg)\n"
            "        intr = snap.interrupts[0].value\n"
            "        cmd = build_verified_approval_command(\n"
            f"            bearer_token={bob_token!r}, decision='approve',\n"
            "            approval_tenant_id='tenant-alpha', requested_by='alice',\n"
            "            operation_id=intr['operation_id'],\n"
            "            approved_payload_hash=intr['approved_payload_hash'], settings=s,\n"
            "        )\n"
            "        await g.ainvoke(cmd, config=cfg, context=ctx, durability='sync')\n"
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

        # Inspect serialized PostgreSQL checkpoints & writes for ABSENCE of bearer credentials!
        async with (
            await psycopg.AsyncConnection.connect(settings.database_admin_url) as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "SELECT checkpoint::text, metadata::text FROM checkpoints WHERE thread_id = %s",
                (thread_id,),
            )
            cp_rows = await cur.fetchall()
            assert len(cp_rows) >= 2
            for cp_txt, meta_txt in cp_rows:
                assert bob_token not in (cp_txt or "")
                assert bob_token not in (meta_txt or "")
                assert "eyJ" not in (cp_txt or "")
                assert "eyJ" not in (meta_txt or "")

            await cur.execute(
                "SELECT blob FROM checkpoint_writes WHERE thread_id = %s",
                (thread_id,),
            )
            write_rows = await cur.fetchall()
            for (blob,) in write_rows:
                raw_bytes = bytes(blob) if blob is not None else b""
                assert bob_token.encode("utf-8") not in raw_bytes
                assert b"eyJ" not in raw_bytes


async def test_exp2_authenticated_approval_boundary_rejections_and_zero_carrier_posts() -> None:
    """Unauthenticated, forged, cross-tenant, self-approval, & wrong-hash attempts -> 0 POSTs."""
    settings = Settings(model_mode="mock")
    carrier_state = MockCarrierState(fault_mode="normal")

    async with _running_carrier(carrier_state) as base_url:
        saver = InMemorySaver()
        deps = SubmissionTriageDeps(
            settings=settings,
            carrier_base_url=base_url,
            carrier_timeout_s=2.0,
        )
        graph = build_submission_triage_graph(deps=deps, checkpointer=saver)

        # 1. Incomplete ready_to_quote brief -> DENIED before approval
        ctx0 = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="auth-incomplete",
            run_id="r-a0",
            request_id="req-a0",
        )
        incomplete = SubmissionBrief(
            disposition="ready_to_quote",
            insured_name="Northstar Analytics Inc.",
            carrier_match=None,
            requested_coverage_usd=None,
            summary="Incomplete.",
        )
        res0 = await graph.ainvoke(
            {"submission_id": "sub-01", "submission_brief": incomplete.model_dump(mode="json")},
            config={"configurable": {"thread_id": ctx0.thread_id}},
            context=ctx0,
            durability="sync",
        )
        assert res0["final_status"] == "denied"

        # 2. Boundary helper rejections (missing token, invalid token, cross-tenant, wrong role,
        #    self-approval)
        alice_approver_tok = mint_user_jwt(
            CallerIdentity(
                tenant_id="tenant-alpha", actor_id="alice", roles=frozenset({"ops-approver"})
            ),
            secret=settings.jwt_signing_secret,
        )
        charlie_eng_tok = mint_user_jwt(
            CallerIdentity(
                tenant_id="tenant-alpha", actor_id="charlie", roles=frozenset({"engineer"})
            ),
            secret=settings.jwt_signing_secret,
        )
        beta_admin_tok = mint_user_jwt(
            CallerIdentity(
                tenant_id="tenant-beta", actor_id="carol", roles=frozenset({"admin"})
            ),
            secret=settings.jwt_signing_secret,
        )
        for bad_tok in (None, "", "not-a-jwt", alice_approver_tok, charlie_eng_tok, beta_admin_tok):
            with pytest.raises(ApprovalAuthorizationError):
                build_verified_approval_command(
                    bearer_token=bad_tok,
                    decision="approve",
                    approval_tenant_id="tenant-alpha",
                    requested_by="alice",
                    operation_id="quote_1",
                    approved_payload_hash="abc",
                    settings=settings,
                )

        # 3. Direct graph bypass attempts: self-asserted roles, raw bearer token, & wrong hash
        bob_tok = _bob_jwt(settings)
        bypass_cases = [
            (
                "bypass-self-asserted",
                Command(
                    resume={
                        "decision": "approve",
                        "approver_id": "bob",
                        "approver_roles": ["ops-approver"],
                    }
                ),
            ),
            (
                "bypass-raw-token",
                Command(resume={"decision": "approve", "approver_token": bob_tok}),
            ),
        ]
        for tid, bad_cmd in bypass_cases:
            ctx = RunContext(
                tenant_id="tenant-alpha",
                requester_id="alice",
                requester_roles=frozenset({"underwriter"}),
                thread_id=tid,
                run_id=f"r-{tid}",
                request_id=f"req-{tid}",
            )
            cfg = {"configurable": {"thread_id": tid}}
            await graph.ainvoke({"submission_id": "sub-01"}, config=cfg, context=ctx)
            denied = await graph.ainvoke(bad_cmd, config=cfg, context=ctx)
            assert denied["final_status"] == "denied"
            # Verify raw token never persisted in state or trace
            assert bob_tok not in json.dumps(denied, default=str)

        # 4. Valid bob JWT bound to an INCORRECT payload hash -> denied at await_approval
        ctx_hash = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="bypass-wrong-hash",
            run_id="r-wh",
            request_id="req-wh",
        )
        cfg_hash = {"configurable": {"thread_id": ctx_hash.thread_id}}
        await graph.ainvoke({"submission_id": "sub-01"}, config=cfg_hash, context=ctx_hash)
        snap_h = await graph.aget_state(cfg_hash)
        wrong_hash_cmd = build_verified_approval_command(
            bearer_token=bob_tok,
            decision="approve",
            approval_tenant_id="tenant-alpha",
            requested_by="alice",
            operation_id=snap_h.interrupts[0].value["operation_id"],
            approved_payload_hash="0" * 64,
            settings=settings,
        )
        denied_hash = await graph.ainvoke(wrong_hash_cmd, config=cfg_hash, context=ctx_hash)
        assert denied_hash["final_status"] == "denied"

        # Assert ZERO carrier POSTs across ALL denied/bypassed approvals!
        async with httpx.AsyncClient() as client:
            stats = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats["http_post_attempts"] == 0
        assert stats["committed_writes"] == 0

        # 5. Authorized path succeeds with authenticated actor 'bob' in downstream audit & no JWT
        ctx_ok = RunContext(
            tenant_id="tenant-alpha",
            requester_id="alice",
            requester_roles=frozenset({"underwriter"}),
            thread_id="auth-ok",
            run_id="r-ok",
            request_id="req-ok",
        )
        cfg_ok = {"configurable": {"thread_id": ctx_ok.thread_id}}
        await graph.ainvoke({"submission_id": "sub-01"}, config=cfg_ok, context=ctx_ok)
        snap_ok = await graph.aget_state(cfg_ok)
        intr_ok = snap_ok.interrupts[0].value
        ok_cmd = build_verified_approval_command(
            bearer_token=bob_tok,
            decision="approve",
            approval_tenant_id="tenant-alpha",
            requested_by="alice",
            operation_id=intr_ok["operation_id"],
            approved_payload_hash=intr_ok["approved_payload_hash"],
            settings=settings,
        )
        assert bob_tok not in json.dumps(ok_cmd.resume, default=str)
        done = await graph.ainvoke(ok_cmd, config=cfg_ok, context=ctx_ok)
        assert done["final_status"] == "completed"
        assert bob_tok not in json.dumps(done, default=str)
        assert any(
            a.event_type == "tool_write"
            and a.approved_by == "bob"
            and a.operation_id == intr_ok["operation_id"]
            for a in deps.audit_memory
        )

        async with httpx.AsyncClient() as client:
            stats_after = (await client.get(f"{base_url}/admin/stats")).json()
        assert stats_after["http_post_attempts"] == 1
        assert stats_after["committed_writes"] == 1
