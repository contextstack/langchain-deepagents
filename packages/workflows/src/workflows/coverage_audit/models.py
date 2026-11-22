"""Shared Pydantic output schema for the read-only Coverage Discrepancy Audit."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CoverageCitation(BaseModel, frozen=True):
    """Source-grounded citation for a coverage audit finding."""

    doc_id: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    verbatim_quote: str
    claim_supported: str
    subject: str | None = None
    value: str | None = None
    polarity: Literal["affirmed", "negated"] = "affirmed"


class CoverageAuditBrief(BaseModel, frozen=True):
    """Structured coverage audit output shared identically by Baseline and Deep Agents."""

    packet_id: str
    determination: Literal[
        "covered",
        "covered_with_deductible",
        "excluded",
        "insufficient_evidence",
    ]
    controlling_value: str
    controlling_doc_ids: list[str] = Field(default_factory=list)
    supersession_chain: list[str] = Field(default_factory=list)
    evidence_citations: list[CoverageCitation] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    rationale: str = ""
