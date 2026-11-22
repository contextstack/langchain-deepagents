"""Mock HTTP Insurance Carrier server for Experiment 2 (ambiguous carrier response recovery)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class QuoteSubmissionRequest(BaseModel):
    operation_id: str
    submission_id: str
    carrier_id: str
    insured_name: str
    requested_coverage_usd: int


class MockCarrierState:
    """In-memory carrier ledger tracking both HTTP POST attempts and committed writes."""

    def __init__(
        self,
        *,
        fault_mode: Literal[
            "normal",
            "commit_then_hang_lookup_ok",
            "commit_then_hang_unverifiable",
        ] = "normal",
        hang_seconds: float = 5.0,
    ) -> None:
        self.fault_mode = fault_mode
        self.hang_seconds = hang_seconds
        self.http_post_attempts: int = 0
        self.lookup_attempts: int = 0
        self.operations: dict[str, dict[str, Any]] = {}

    @property
    def committed_writes(self) -> int:
        return len(self.operations)


def create_mock_carrier_app(state: MockCarrierState | None = None) -> FastAPI:
    carrier_state = state or MockCarrierState()
    app = FastAPI(title="Mock Insurance Carrier HTTP API", version="0.1.0")
    app.state.carrier = carrier_state

    @app.post("/v1/quotes")
    async def submit_quote(req: QuoteSubmissionRequest) -> dict[str, Any]:
        carrier_state.http_post_attempts += 1
        existing = carrier_state.operations.get(req.operation_id)
        if existing is None:
            record = {
                "operation_id": req.operation_id,
                "carrier_ref": f"CAR-{uuid.uuid4().hex[:8].upper()}",
                "submission_id": req.submission_id,
                "carrier_id": req.carrier_id,
                "insured_name": req.insured_name,
                "requested_coverage_usd": req.requested_coverage_usd,
                "status": "committed",
            }
            carrier_state.operations[req.operation_id] = record
        else:
            record = existing

        # Commit FIRST, then withhold response so caller times out with ambiguous outcome
        if carrier_state.fault_mode in {
            "commit_then_hang_lookup_ok",
            "commit_then_hang_unverifiable",
        }:
            await asyncio.sleep(carrier_state.hang_seconds)

        return dict(record)

    @app.get("/v1/quotes/{operation_id}")
    async def lookup_quote(operation_id: str) -> dict[str, Any]:
        carrier_state.lookup_attempts += 1
        if carrier_state.fault_mode == "commit_then_hang_unverifiable":
            raise HTTPException(
                status_code=501,
                detail=(
                    "Status lookup by operation_id is not supported for this carrier/line; "
                    "outcome cannot be verified automatically."
                ),
            )
        record = carrier_state.operations.get(operation_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Operation not found")
        return dict(record)

    @app.get("/admin/stats")
    async def admin_stats() -> dict[str, Any]:
        return {
            "fault_mode": carrier_state.fault_mode,
            "http_post_attempts": carrier_state.http_post_attempts,
            "lookup_attempts": carrier_state.lookup_attempts,
            "committed_writes": carrier_state.committed_writes,
            "operations": carrier_state.operations,
        }

    return app
