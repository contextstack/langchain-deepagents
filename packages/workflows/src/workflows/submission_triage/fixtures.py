"""Synthetic commercial insurance submissions & carrier appetite guides (4 clean, 8 messy).

Ground truth dispositions and required evidence are pre-registered in
`evals/datasets/submissions.jsonl` before running either planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SubmissionDocument:
    doc_id: str
    doc_type: str  # broker_email | application | loss_run | endorsement | schedule
    version: int
    timestamp: str
    title: str
    content: str
    supersedes: str | None = None


@dataclass(frozen=True)
class CarrierAppetiteGuide:
    carrier_id: str
    carrier_name: str
    eligible_classes: list[str]
    prohibited_classes: list[str]
    max_revenue_usd: int
    max_coverage_usd: int
    max_single_loss_usd: int
    required_fields: list[str]
    notes: str


@dataclass(frozen=True)
class SyntheticSubmission:
    submission_id: str
    tier: str  # clean | messy
    scenario_tag: str
    documents: list[SubmissionDocument] = field(default_factory=list)


CARRIER_APPETITE_GUIDES: dict[str, CarrierAppetiteGuide] = {
    "carrier-apex": CarrierAppetiteGuide(
        carrier_id="carrier-apex",
        carrier_name="Apex Commercial Tech & Professional",
        eligible_classes=[
            "SaaS / B2B Software",
            "IT & Management Consulting",
            "Accounting / Legal Professional Services",
        ],
        prohibited_classes=[
            "Roofing Contractor",
            "Cannabis Dispensary",
            "Fireworks / Explosives Manufacturing",
            "Crypto Exchange",
        ],
        max_revenue_usd=25_000_000,
        max_coverage_usd=5_000_000,
        max_single_loss_usd=100_000,
        required_fields=[
            "insured_name",
            "fein",
            "business_classification",
            "annual_revenue_usd",
            "requested_coverage_usd",
            "effective_date",
            "loss_history_3yr",
        ],
        notes=(
            "Requires clean 3-year loss runs (no single claim > $100,000). "
            "Any contradiction between broker email and signed application or loss run "
            "must be routed to human underwriting review."
        ),
    ),
    "carrier-ironclad": CarrierAppetiteGuide(
        carrier_id="carrier-ironclad",
        carrier_name="Ironclad Property & Light Industrial",
        eligible_classes=[
            "Light Manufacturing",
            "Commercial Warehouse / Distribution",
            "Wholesale Goods",
            "Commercial HVAC / Electrical (Non-Roofing)",
        ],
        prohibited_classes=[
            "Roofing Contractor",
            "Cannabis Dispensary",
            "Fireworks / Explosives Manufacturing",
            "Heavy Chemical Processing",
        ],
        max_revenue_usd=40_000_000,
        max_coverage_usd=5_000_000,
        max_single_loss_usd=150_000,
        required_fields=[
            "insured_name",
            "fein",
            "business_classification",
            "annual_revenue_usd",
            "requested_coverage_usd",
            "effective_date",
            "loss_history_3yr",
            "location_schedule",
        ],
        notes=(
            "Property and warehouse risks require a complete physical location schedule "
            "with square footage and sprinkler status. Workers Comp / payroll risks require "
            "a class-code payroll breakdown. Any undisclosed claim or limit above $5,000,000 "
            "must route to human review."
        ),
    ),
}


SUBMISSIONS: dict[str, SyntheticSubmission] = {
    # =========================================================================
    # 4 CLEAN CASES (expected: ready_to_quote)
    # =========================================================================
    "sub-01": SyntheticSubmission(
        submission_id="sub-01",
        tier="clean",
        scenario_tag="clean_saas_eo",
        documents=[
            SubmissionDocument(
                doc_id="sub-01-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-10T09:15:00Z",
                title="Broker Email: Northstar Analytics Tech E&O Quote Request",
                content=(
                    "Hi Underwriting, please quote Tech E&O for Northstar Analytics Inc. "
                    "(SaaS / B2B Software). Revenue is $4,200,000, requesting $2,000,000 limit "
                    "effective 2026-12-01. Clean 3-year loss history attached."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-01-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-10T09:15:00Z",
                title="ACORD Tech E&O Application - Northstar Analytics Inc.",
                content=(
                    "Insured: Northstar Analytics Inc.\n"
                    "FEIN: 84-1234567\n"
                    "Class: SaaS / B2B Software (cloud analytics dashboard)\n"
                    "Annual Revenue: $4,200,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "3-Year Loss History: 0 claims ($0 incurred, 2023-2026)."
                ),
            ),
        ],
    ),
    "sub-02": SyntheticSubmission(
        submission_id="sub-02",
        tier="clean",
        scenario_tag="clean_light_mfg",
        documents=[
            SubmissionDocument(
                doc_id="sub-02-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-11T10:00:00Z",
                title="Broker Email: Harbor Precision Parts Light Manufacturing",
                content=(
                    "Submitting Harbor Precision Parts LLC (Light Manufacturing - CNC metal "
                    "brackets). $9,500,000 revenue, $3,000,000 coverage limit, effective "
                    "2026-12-15. Full location schedule and 3-year loss run attached."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-02-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-11T10:00:00Z",
                title="Commercial Package Application - Harbor Precision Parts LLC",
                content=(
                    "Insured: Harbor Precision Parts LLC\n"
                    "FEIN: 47-9988776\n"
                    "Class: Light Manufacturing\n"
                    "Annual Revenue: $9,500,000\n"
                    "Requested Limit: $3,000,000\n"
                    "Effective Date: 2026-12-15\n"
                    "Location Schedule: 1200 Harbor Way, Toledo OH "
                    "(42,000 sq ft, 100% sprinklered)\n"
                    "3-Year Loss History: 1 minor property claim ($12,000 closed 2024)."
                ),
            ),
        ],
    ),
    "sub-03": SyntheticSubmission(
        submission_id="sub-03",
        tier="clean",
        scenario_tag="clean_consulting",
        documents=[
            SubmissionDocument(
                doc_id="sub-03-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-12T14:20:00Z",
                title="Broker Email: Crestview Advisory Consulting E&O",
                content=(
                    "Please quote Crestview Advisory Group LLC (IT & Management Consulting). "
                    "$6,800,000 annual revenue, $2,000,000 requested limit, effective 2027-01-01. "
                    "Zero claims over the last 3 years."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-03-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-12T14:20:00Z",
                title="Professional Liability Application - Crestview Advisory Group LLC",
                content=(
                    "Insured: Crestview Advisory Group LLC\n"
                    "FEIN: 31-5544332\n"
                    "Class: IT & Management Consulting\n"
                    "Annual Revenue: $6,800,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: 2027-01-01\n"
                    "3-Year Loss History: Loss-free (0 claims, 2023-2026)."
                ),
            ),
        ],
    ),
    "sub-04": SyntheticSubmission(
        submission_id="sub-04",
        tier="clean",
        scenario_tag="clean_warehouse",
        documents=[
            SubmissionDocument(
                doc_id="sub-04-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-13T11:05:00Z",
                title="Broker Email: Midland Cold-Chain Distribution Warehouse",
                content=(
                    "Requesting quote for Midland Distribution Corp (Commercial Warehouse / "
                    "Distribution). Revenue $14,000,000, requesting $4,000,000 limit effective "
                    "2026-12-01. Complete schedule and 3-year loss run enclosed."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-04-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-13T11:05:00Z",
                title="Property & GL Application - Midland Distribution Corp",
                content=(
                    "Insured: Midland Distribution Corp\n"
                    "FEIN: 62-7766554\n"
                    "Class: Commercial Warehouse / Distribution\n"
                    "Annual Revenue: $14,000,000\n"
                    "Requested Limit: $4,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "Location Schedule: 880 Logistics Blvd, Columbus OH "
                    "(85,000 sq ft, ESFR sprinkler)\n"
                    "3-Year Loss History: 0 claims ($0 incurred, 2023-2026)."
                ),
            ),
        ],
    ),
    # =========================================================================
    # 8 MESSY CASES (superseded docs, conflicts, missing fields, out-of-appetite)
    # =========================================================================
    "sub-05": SyntheticSubmission(
        submission_id="sub-05",
        tier="messy",
        scenario_tag="superseded_v2_clean_in_appetite",
        documents=[
            SubmissionDocument(
                doc_id="sub-05-email-v1",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-10T08:00:00Z",
                title="Broker Email (v1): Beacon Cloud Systems Initial Draft",
                content=(
                    "Sending draft app for Beacon Cloud Systems Inc. (SaaS / B2B Software). "
                    "Wait — please hold, I noticed the FEIN and effective date were missing in v1."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-05-app-v1",
                doc_type="application",
                version=1,
                timestamp="2026-11-10T08:00:00Z",
                title="Application v1 (DRAFT - SUPERSEDED) - Beacon Cloud Systems",
                content=(
                    "Insured: Beacon Cloud Systems Inc.\n"
                    "FEIN: TBD\n"
                    "Class: SaaS / B2B Software\n"
                    "Annual Revenue: $3,000,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: TBD\n"
                    "3-Year Loss History: Pending."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-05-email-v2",
                doc_type="broker_email",
                version=2,
                timestamp="2026-11-10T15:30:00Z",
                title="Broker Email (v2 FINAL): Beacon Cloud Systems Corrected Submission",
                content=(
                    "Apologies for the earlier draft! Please DISREGARD v1 completely. "
                    "Attached is the final signed v2 application for Beacon Cloud Systems Inc. "
                    "with FEIN 88-3344556, effective 2026-12-01, $3,400,000 revenue, $2,000,000 "
                    "limit, and confirmed 0 claims over 3 years."
                ),
                supersedes="sub-05-email-v1",
            ),
            SubmissionDocument(
                doc_id="sub-05-app-v2",
                doc_type="application",
                version=2,
                timestamp="2026-11-10T15:30:00Z",
                title="Application v2 (FINAL SIGNED) - Beacon Cloud Systems Inc.",
                content=(
                    "SUPERSEDES sub-05-app-v1.\n"
                    "Insured: Beacon Cloud Systems Inc.\n"
                    "FEIN: 88-3344556\n"
                    "Class: SaaS / B2B Software\n"
                    "Annual Revenue: $3,400,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "3-Year Loss History: 0 claims ($0 incurred, 2023-2026)."
                ),
                supersedes="sub-05-app-v1",
            ),
        ],
    ),
    "sub-06": SyntheticSubmission(
        submission_id="sub-06",
        tier="messy",
        scenario_tag="conflicting_business_classification",
        documents=[
            SubmissionDocument(
                doc_id="sub-06-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-11T09:00:00Z",
                title="Broker Email: Summit Exterior Tech Consulting Quote",
                content=(
                    "Please quote Summit Exterior Solutions LLC as IT & Management Consulting "
                    "for construction software. Revenue $5,100,000, limit $2,000,000, effective "
                    "2026-12-01. Clean loss history."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-06-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-11T09:00:00Z",
                title="Signed ACORD Application - Summit Exterior Solutions LLC",
                content=(
                    "Insured: Summit Exterior Solutions LLC\n"
                    "FEIN: 55-2211009\n"
                    "Primary Operations / Class: Commercial Roofing Contractor (85% revenue from "
                    "flat-roof tear-off and torch-down installation; 15% estimating software)\n"
                    "Annual Revenue: $5,100,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "3-Year Loss History: 0 claims."
                ),
            ),
        ],
    ),
    "sub-07": SyntheticSubmission(
        submission_id="sub-07",
        tier="messy",
        scenario_tag="missing_required_fields_effdate_lossrun",
        documents=[
            SubmissionDocument(
                doc_id="sub-07-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-11T16:40:00Z",
                title="Broker Email: Clearpath Accounting Professional Liability",
                content=(
                    "Hi team, please quote Clearpath Accounting LLP (Accounting / Legal "
                    "Professional Services). Revenue is $2,900,000 and they want $1,000,000 "
                    "coverage. Still waiting on the insured to confirm target effective date "
                    "and pull their 3-year loss runs from the prior carrier."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-07-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-11T16:40:00Z",
                title="Application (Incomplete) - Clearpath Accounting LLP",
                content=(
                    "Insured: Clearpath Accounting LLP\n"
                    "FEIN: 22-8877661\n"
                    "Class: Accounting / Legal Professional Services\n"
                    "Annual Revenue: $2,900,000\n"
                    "Requested Limit: $1,000,000\n"
                    "Effective Date: [BLANK]\n"
                    "3-Year Loss History: [NOT PROVIDED]"
                ),
            ),
        ],
    ),
    "sub-08": SyntheticSubmission(
        submission_id="sub-08",
        tier="messy",
        scenario_tag="outside_all_carrier_appetite",
        documents=[
            SubmissionDocument(
                doc_id="sub-08-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-12T08:30:00Z",
                title="Broker Email: Starburst Pyrotechnics Commercial Package",
                content=(
                    "Please quote Starburst Pyrotechnics Inc. (Fireworks / Explosives "
                    "Manufacturing). Revenue $8,000,000, requesting $3,000,000 limit effective "
                    "2026-12-01. Complete application and clean 3-year loss run attached."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-08-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-12T08:30:00Z",
                title="Commercial Package Application - Starburst Pyrotechnics Inc.",
                content=(
                    "Insured: Starburst Pyrotechnics Inc.\n"
                    "FEIN: 73-4411223\n"
                    "Class: Fireworks / Explosives Manufacturing\n"
                    "Annual Revenue: $8,000,000\n"
                    "Requested Limit: $3,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "Location Schedule: 400 Powder Mill Rd, Joplin MO (25,000 sq ft, sprinklered)\n"
                    "3-Year Loss History: 0 claims (2023-2026)."
                ),
            ),
        ],
    ),
    "sub-09": SyntheticSubmission(
        submission_id="sub-09",
        tier="messy",
        scenario_tag="conflicting_revenue_and_limit_over_cap",
        documents=[
            SubmissionDocument(
                doc_id="sub-09-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-12T13:15:00Z",
                title="Broker Email: Vector Robotics SaaS Quote",
                content=(
                    "Submitting Vector Robotics Software Inc. (SaaS / B2B Software). "
                    "Small startup with $2,000,000 annual revenue requesting a $1,000,000 limit "
                    "effective 2026-12-15. Clean loss history."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-09-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-12T13:15:00Z",
                title="Signed Application - Vector Robotics Software Inc.",
                content=(
                    "Insured: Vector Robotics Software Inc.\n"
                    "FEIN: 91-6655443\n"
                    "Class: SaaS / B2B Software\n"
                    "Annual Revenue: $18,500,000\n"
                    "Requested Limit: $10,000,000 (exceeds standard $5M carrier appetite cap)\n"
                    "Effective Date: 2026-12-15\n"
                    "3-Year Loss History: 0 claims."
                ),
            ),
        ],
    ),
    "sub-10": SyntheticSubmission(
        submission_id="sub-10",
        tier="messy",
        scenario_tag="missing_fein_and_location_schedule",
        documents=[
            SubmissionDocument(
                doc_id="sub-10-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-13T09:45:00Z",
                title="Broker Email: Riverbend Wholesale Warehouse Urgent Quote",
                content=(
                    "Need a quick quote for Riverbend Wholesale Goods Inc. (Wholesale Goods). "
                    "$11,000,000 revenue, $2,000,000 limit, effective 2026-12-01. Loss-free for "
                    "3 years. Note: I don't have their FEIN or warehouse location schedule yet."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-10-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-13T09:45:00Z",
                title="Application (Missing Schedule) - Riverbend Wholesale Goods Inc.",
                content=(
                    "Insured: Riverbend Wholesale Goods Inc.\n"
                    "FEIN: [MISSING]\n"
                    "Class: Wholesale Goods\n"
                    "Annual Revenue: $11,000,000\n"
                    "Requested Limit: $2,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "Location Schedule: [MISSING - warehouse addresses "
                    "& sprinkler status omitted]\n"
                    "3-Year Loss History: 0 claims (2023-2026)."
                ),
            ),
        ],
    ),
    "sub-11": SyntheticSubmission(
        submission_id="sub-11",
        tier="messy",
        scenario_tag="superseded_loss_run_contradicts_email",
        documents=[
            SubmissionDocument(
                doc_id="sub-11-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-13T15:00:00Z",
                title="Broker Email: Redrock HVAC Commercial GL Quote",
                content=(
                    "Submitting Redrock Mechanical & HVAC Inc. (Commercial HVAC / Electrical "
                    "(Non-Roofing)). $12,000,000 revenue, $3,000,000 limit, effective 2026-12-01. "
                    "Account has been 100% loss-free for the past 5 years!"
                ),
            ),
            SubmissionDocument(
                doc_id="sub-11-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-13T15:00:00Z",
                title="Application - Redrock Mechanical & HVAC Inc.",
                content=(
                    "Insured: Redrock Mechanical & HVAC Inc.\n"
                    "FEIN: 44-1199882\n"
                    "Class: Commercial HVAC / Electrical (Non-Roofing)\n"
                    "Annual Revenue: $12,000,000\n"
                    "Requested Limit: $3,000,000\n"
                    "Effective Date: 2026-12-01\n"
                    "Location Schedule: 210 Industrial Pkwy, Phoenix AZ (18,000 sq ft, sprinklered)"
                ),
            ),
            SubmissionDocument(
                doc_id="sub-11-loss-v1",
                doc_type="loss_run",
                version=1,
                timestamp="2026-11-01T12:00:00Z",
                title="Loss Run v1 (2023-2025, SUPERSEDED) - Redrock Mechanical",
                content="Policy Period 2023-2025: 0 claims reported as of 2025-12-31.",
            ),
            SubmissionDocument(
                doc_id="sub-11-loss-v2",
                doc_type="loss_run",
                version=2,
                timestamp="2026-11-12T17:00:00Z",
                title="Loss Run v2 (CURRENT AS OF 2026-11-12) - Redrock Mechanical",
                content=(
                    "SUPERSEDES sub-11-loss-v1.\n"
                    "Claim #CLM-2026-0884: Date of loss 2026-06-14, OPEN bodily injury & "
                    "equipment fire claim, $420,000 total incurred "
                    "($350,000 reserve + $70,000 paid)."
                ),
                supersedes="sub-11-loss-v1",
            ),
        ],
    ),
    "sub-12": SyntheticSubmission(
        submission_id="sub-12",
        tier="messy",
        scenario_tag="missing_payroll_breakdown_and_subsidiary_list",
        documents=[
            SubmissionDocument(
                doc_id="sub-12-email",
                doc_type="broker_email",
                version=1,
                timestamp="2026-11-14T10:10:00Z",
                title="Broker Email: Oakridge Industrial Light Mfg + Workers Comp",
                content=(
                    "Please quote Oakridge Industrial Holdings (Light Manufacturing) including "
                    "Workers Comp. Revenue $16,000,000, limit $3,000,000, effective 2027-01-01. "
                    "Loss-free 3 years. We still need to get the class-code payroll breakdown "
                    "from HR before binding."
                ),
            ),
            SubmissionDocument(
                doc_id="sub-12-app",
                doc_type="application",
                version=1,
                timestamp="2026-11-14T10:10:00Z",
                title="Application (Pending Payroll Schedule) - Oakridge Industrial Holdings",
                content=(
                    "Insured: Oakridge Industrial Holdings\n"
                    "FEIN: 39-7711220\n"
                    "Class: Light Manufacturing\n"
                    "Annual Revenue: $16,000,000\n"
                    "Requested Limit: $3,000,000\n"
                    "Effective Date: 2027-01-01\n"
                    "Location Schedule: 500 Oakridge Dr, Grand Rapids MI "
                    "(60,000 sq ft, sprinklered)\n"
                    "3-Year Loss History: 0 claims.\n"
                    "Workers Comp Payroll Breakdown by Class Code: [PENDING FROM HR]"
                ),
            ),
        ],
    ),
}


def serialize_carrier(guide: CarrierAppetiteGuide) -> dict[str, Any]:
    return {
        "carrier_id": guide.carrier_id,
        "carrier_name": guide.carrier_name,
        "eligible_classes": guide.eligible_classes,
        "prohibited_classes": guide.prohibited_classes,
        "max_revenue_usd": guide.max_revenue_usd,
        "max_coverage_usd": guide.max_coverage_usd,
        "max_single_loss_usd": guide.max_single_loss_usd,
        "required_fields": guide.required_fields,
        "notes": guide.notes,
    }
