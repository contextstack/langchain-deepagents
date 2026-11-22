"""Shared Pydantic output schema for Commercial Insurance Submission Triage."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EvidenceCitation(BaseModel, frozen=True):
    """Source citation grounding a single material underwriting fact."""

    doc_id: str
    section_or_quote: str
    fact_supported: str
    subject: str | None = None
    field: str | None = None
    value: str | None = None
    polarity: Literal["affirmed", "negated"] | None = None


class ContradictionFlag(BaseModel, frozen=True):
    """Explicit contradiction detected across submission documents."""

    field: str
    source_a: str
    source_b: str
    detail: str


class SubmissionBrief(BaseModel, frozen=True):
    """Structured submission brief returned by BOTH Baseline and Deep Agents planners."""

    disposition: Literal["ready_to_quote", "ask_broker", "human_review"]
    insured_name: str = ""
    business_classification: str = ""
    annual_revenue_usd: int | None = None
    requested_coverage_usd: int | None = None
    effective_date: str | None = None
    carrier_match: str | None = None
    material_facts: list[EvidenceCitation] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    contradictions: list[ContradictionFlag] = Field(default_factory=list)
    summary: str = ""
