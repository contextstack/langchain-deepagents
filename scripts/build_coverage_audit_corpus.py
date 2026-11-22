# ruff: noqa: E501
"""Build the read-only Coverage Discrepancy Audit corpus and separate answer keys.

All planner-accessible files are written strictly under:
  packages/workflows/src/workflows/coverage_audit/policy-archive/<packet_id>/

All ground-truth keys, tiers, and rubrics are written strictly OUTSIDE policy-archive/ under:
  evals/datasets/coverage_audit_dev_key.json
  evals/datasets/coverage_audit_heldout_key.json
  evals/datasets/coverage_audit_manifest.json
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

ARCHIVE_ROOT = Path("packages/workflows/src/workflows/coverage_audit/policy-archive")
DEV_KEY_PATH = Path("evals/datasets/coverage_audit_dev_key.json")
HELDOUT_KEY_PATH = Path("evals/datasets/coverage_audit_heldout_key.json")
MANIFEST_PATH = Path("evals/datasets/coverage_audit_manifest.json")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_packets() -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (packet_files, dev_keys, heldout_keys)."""
    files: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # DEVELOPMENT PILOT PACKETS (D01 - D03)
    # ------------------------------------------------------------------
    files["D01"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY & LIABILITY POLICY DECLARATIONS\n"
            "Policy Number: CPL-2026-4401\n"
            "Named Insured: Harborview Cold Storage LLC\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Per-Occurrence Limit: $1,000,000\n"
            "Base Deductible: $25,000\n"
            "Note: See attached executed endorsements for modified limits.\n"
        ),
        "endorsement_gl_105.txt": (
            "ENDORSEMENT GL-105 — LIMIT OF LIABILITY AMENDMENT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CPL-2026-4401\n"
            "Named Insured: Harborview Cold Storage LLC\n"
            "Effective Date: 2026-01-01\n"
            "This endorsement modifies the Base Per-Occurrence Limit from $1,000,000 to $2,000,000.\n"
            "All other terms and conditions remain unchanged.\n"
        ),
        "broker_renewal_note.txt": (
            "From: M. Alvarez <malvarez@harborbroker.example>\n"
            "Date: 2026-01-05\n"
            "Subject: Harborview Cold Storage LLC renewal file\n"
            "Attached is the executed GL-105 endorsement reflecting the $2,000,000 per-occurrence limit.\n"
            "Billing invoice #8812 has been sent to accounting.\n"
        ),
        "marketing_overview_2025.txt": (
            "GENERAL BROKERAGE PRODUCT OVERVIEW (2025)\n"
            "Standard commercial cold storage accounts typically carry $1,000,000 to $5,000,000 limits.\n"
            "This brochure is for informational purposes only and confers no coverage.\n"
        ),
        "claim_notice_2026_04.txt": (
            "FIRST NOTICE OF LOSS\n"
            "Claim ID: FNOL-D01-2026\n"
            "Insured: Harborview Cold Storage LLC\n"
            "Date of Loss: 2026-04-12\n"
            "Loss Description: Ammonia compressor failure damaging third-party inventory.\n"
            "Question: What is the controlling Per-Occurrence limit for this loss?\n"
        ),
    }

    files["D02"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY POLICY DECLARATIONS\n"
            "Policy Number: CPP-2026-7720\n"
            "Named Insured: Cascadia Timberworks Inc.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Water Damage Sublimit: $25,000\n"
        ),
        "draft_endorsement_water_2026.txt": (
            "PRELIMINARY DRAFT — UNSIGNED / NOT EXECUTED\n"
            "Proposed Endorsement ED-412 (Water Damage Sublimit Proposal)\n"
            "Named Insured: Cascadia Timberworks Inc.\n"
            "Proposed Water Damage Sublimit: $250,000\n"
            "WARNING: FOR DISCUSSION ONLY. THIS DRAFT DOES NOT BIND COVERAGE.\n"
        ),
        "endorsement_ed_401.txt": (
            "ENDORSEMENT ED-401 — WATER DAMAGE SUBLIMIT EXTENSION\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CPP-2026-7720\n"
            "Named Insured: Cascadia Timberworks Inc.\n"
            "Effective Date: 2026-01-01\n"
            "This endorsement replaces the Base Water Damage Sublimit of $25,000 with $100,000.\n"
        ),
        "endorsement_ed_409.txt": (
            "ENDORSEMENT ED-409 — AMENDED WATER DAMAGE SUBLIMIT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CPP-2026-7720\n"
            "Named Insured: Cascadia Timberworks Inc.\n"
            "Effective Date: 2026-02-15\n"
            "This endorsement expressly supersedes and replaces Endorsement ED-401 in its entirety.\n"
            "Controlling Water Damage Sublimit: $75,000 per occurrence.\n"
        ),
        "broker_correspondence_feb.txt": (
            "From: D. Chen <dchen@pnwbroker.example>\n"
            "Date: 2026-02-20\n"
            "Subject: Cascadia Timberworks water sublimit\n"
            "We floated a $250,000 draft earlier, but underwriting declined it and issued ED-409.\n"
            "Please verify the executed endorsements in the file.\n"
        ),
        "loss_notice_march.txt": (
            "CLAIM LOSS NOTICE\n"
            "Claim ID: FNOL-D02-2026\n"
            "Insured: Cascadia Timberworks Inc.\n"
            "Date of Loss: 2026-03-18\n"
            "Loss Description: Roof drain backup caused interior water damage at Mill #1.\n"
            "Question: What is the controlling Water Damage Sublimit for this 2026-03-18 loss?\n"
        ),
    }

    files["D03"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY POLICY DECLARATIONS\n"
            "Policy Number: CPP-2026-9914\n"
            "Named Insured: Redrock Distribution Co.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Covered Locations: Per Scheduled Locations Endorsement SL-204.\n"
        ),
        "endorsement_sl_204.txt": (
            "ENDORSEMENT SL-204 — SCHEDULED LOCATIONS LIMITATION\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CPP-2026-9914\n"
            "Named Insured: Redrock Distribution Co.\n"
            "Effective Date: 2026-01-01\n"
            "Coverage applies solely to locations explicitly enumerated in Schedule A-2026 "
            "(attached separately and incorporated by reference). Unscheduled locations are excluded.\n"
        ),
        "expired_2024_location_list.txt": (
            "EXPIRED 2024 RENEWAL WORKSHEET (SUPERSEDED)\n"
            "Policy Year: 2024-2025 (NOT VALID FOR 2026 POLICY TERM)\n"
            "1. 100 Main Terminal Way\n"
            "2. 412 Dockside Rd (Warehouse Annex B)\n"
        ),
        "broker_email_jan.txt": (
            "From: L. Patel <lpatel@redrockbroker.example>\n"
            "Date: 2026-01-12\n"
            "Subject: Schedule A-2026 upload pending\n"
            "We will forward the finalized Schedule A-2026 once the insured signs off on the new annex.\n"
        ),
        "claim_notice_annex_b.txt": (
            "CLAIM INQUIRY\n"
            "Claim ID: FNOL-D03-2026\n"
            "Insured: Redrock Distribution Co.\n"
            "Date of Loss: 2026-05-02\n"
            "Loss Location: Warehouse Annex B, 412 Dockside Rd.\n"
            "Question: Is Warehouse Annex B covered under the 2026 policy?\n"
        ),
    }

    # ------------------------------------------------------------------
    # HELD-OUT CLEAN PACKETS (C01 - C04)
    # ------------------------------------------------------------------
    files["C01"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PACKAGE POLICY DECLARATIONS\n"
            "Policy Number: PKG-2026-1011\n"
            "Named Insured: Beacon Health Systems LLC\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Cyber Incident Response Sublimit: $100,000\n"
        ),
        "endorsement_cy_102.txt": (
            "ENDORSEMENT CY-102 — CYBER INCIDENT SUBLIMIT INCREASE\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PKG-2026-1011\n"
            "Named Insured: Beacon Health Systems LLC\n"
            "Effective Date: 2026-01-01\n"
            "The Cyber Incident Response Sublimit is increased from $100,000 to $500,000.\n"
        ),
        "billing_receipt_q1.txt": (
            "PREMIUM PAYMENT RECEIPT\n"
            "Policy: PKG-2026-1011 | Paid in Full: $42,800 | Date: 2026-01-03\n"
        ),
        "it_security_questionnaire.txt": (
            "UNDERWRITING CYBER QUESTIONNAIRE\n"
            "Insured: Beacon Health Systems LLC\n"
            "MFA Enabled: Yes | Offline Backups: Daily\n"
        ),
        "claim_notice_cyber.txt": (
            "INCIDENT REPORT\n"
            "Insured: Beacon Health Systems LLC\n"
            "Date of Incident: 2026-04-08\n"
            "Description: Ransomware forensics and breach notification expenses.\n"
            "Question: What is the controlling Cyber Incident Response Sublimit?\n"
        ),
    }

    files["C02"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY DECLARATIONS\n"
            "Policy Number: PRO-2026-2042\n"
            "Named Insured: Gulfcrest Hospitality Group Inc.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Standard All-Other-Perils Deductible: $50,000\n"
            "Location: 88 Oceanfront Blvd (Tier-1 Coastal Property)\n"
        ),
        "endorsement_ns_201.txt": (
            "ENDORSEMENT NS-201 — NAMED STORM DEDUCTIBLE\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PRO-2026-2042\n"
            "Named Insured: Gulfcrest Hospitality Group Inc.\n"
            "Effective Date: 2026-01-01\n"
            "For any loss caused by a Named Storm at a Tier-1 Coastal Property, "
            "the controlling deductible is $150,000 per occurrence.\n"
        ),
        "inspection_report_2026.txt": (
            "LOSS CONTROL INSPECTION SUMMARY\n"
            "Property: 88 Oceanfront Blvd | Roof age: 4 years | Impact shutters installed.\n"
        ),
        "broker_welcome_email.txt": (
            "From: S. Ortiz <sortiz@coastalbroker.example>\n"
            "Subject: Welcome packet — Gulfcrest Hospitality Group Inc.\n"
            "Policy bound with standard AOP deductible and executed NS-201 coastal endorsement.\n"
        ),
        "claim_notice_storm.txt": (
            "FIRST NOTICE OF LOSS\n"
            "Insured: Gulfcrest Hospitality Group Inc.\n"
            "Date of Loss: 2026-08-22\n"
            "Peril: Named Storm (Tropical Storm Elena) at 88 Oceanfront Blvd.\n"
            "Question: What is the controlling deductible for this Named Storm loss?\n"
        ),
    }

    files["C03"] = {
        "policy_declarations.txt": (
            "COMMERCIAL GENERAL LIABILITY DECLARATIONS\n"
            "Policy Number: CGL-2026-3088\n"
            "Named Insured: Heartland Chemical Blending Corp.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Per-Occurrence Limit: $2,000,000\n"
        ),
        "cgl_coverage_form_exclusions.txt": (
            "COMMERCIAL GENERAL LIABILITY COVERAGE FORM — SECTION I EXCLUSIONS\n"
            "Policy Number: CGL-2026-3088\n"
            "Exclusion (f) Total Pollution Exclusion: This insurance does NOT apply to bodily "
            "injury, property damage, or cleanup costs arising out of the actual, alleged, or "
            "threatened discharge, dispersal, seepage, migration, release, or escape of pollutants "
            "at any time, including sudden and accidental releases. No pollution buyback applies.\n"
        ),
        "carrier_product_brochure.txt": (
            "OPTIONAL ENVIRONMENTAL LIABILITY PRODUCT SHEET (NOT PART OF POLICY)\n"
            "Separate standalone Environmental Impairment policies are available upon application.\n"
            "This marketing sheet does not amend any CGL policy.\n"
        ),
        "safety_audit_2026.txt": (
            "ANNUAL EHS SAFETY AUDIT\n"
            "Facility: Heartland Chemical Blending Corp. | Secondary containment inspected.\n"
        ),
        "claim_notice_spill.txt": (
            "CLAIM NOTICE\n"
            "Insured: Heartland Chemical Blending Corp.\n"
            "Date of Loss: 2026-06-11\n"
            "Description: Sudden solvent tank valve rupture releasing 400 gallons into storm drain.\n"
            "Question: Is this sudden pollutant release covered or excluded under CGL-2026-3088?\n"
        ),
    }

    files["C04"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY DECLARATIONS\n"
            "Policy Number: PRO-2026-4099\n"
            "Named Insured: Midwest Precision Stamping LLC\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Form: Equipment Breakdown excluded unless added by endorsement.\n"
        ),
        "endorsement_eb_305.txt": (
            "ENDORSEMENT EB-305 — EQUIPMENT BREAKDOWN COVERAGE\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PRO-2026-4099\n"
            "Named Insured: Midwest Precision Stamping LLC\n"
            "Effective Date: 2026-01-01\n"
            "This endorsement adds Equipment Breakdown coverage with a controlling limit of $750,000 "
            "per breakdown.\n"
        ),
        "maintenance_log_presses.txt": (
            "PREVENTIVE MAINTENANCE LOG — STAMPING PRESS #4\n"
            "Last serviced: 2026-02-14 | Bearings and hydraulic seals within tolerance.\n"
        ),
        "premium_schedule.txt": (
            "ENDORSEMENT PREMIUM ALLOCATION\n"
            "EB-305 Equipment Breakdown Charge: $3,200 (Paid).\n"
        ),
        "claim_notice_press.txt": (
            "CLAIM NOTICE\n"
            "Insured: Midwest Precision Stamping LLC\n"
            "Date of Loss: 2026-05-19\n"
            "Description: Mechanical breakdown of hydraulic main cylinder on Stamping Press #4.\n"
            "Question: What is the controlling Equipment Breakdown limit for this loss?\n"
        ),
    }

    # ------------------------------------------------------------------
    # HELD-OUT BRANCHING PACKETS (B01 - B08)
    # ------------------------------------------------------------------
    files["B01"] = {
        "policy_declarations.txt": (
            "INLAND MARINE & PROPERTY DECLARATIONS\n"
            "Policy Number: IM-2026-5101\n"
            "Named Insured: Pacific Freightways Inc.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Property-in-Transit Limit: $100,000 per conveyance\n"
        ),
        "endorsement_tr_101.txt": (
            "ENDORSEMENT TR-101 — TRANSIT LIMIT AMENDMENT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: IM-2026-5101\n"
            "Named Insured: Pacific Freightways Inc.\n"
            "Effective Date: 2026-01-01\n"
            "Replaces Base Property-in-Transit Limit of $100,000 with $250,000 per conveyance.\n"
        ),
        "endorsement_tr_108.txt": (
            "ENDORSEMENT TR-108 — REVISED TRANSIT LIMIT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: IM-2026-5101\n"
            "Named Insured: Pacific Freightways Inc.\n"
            "Effective Date: 2026-02-01\n"
            "Expressly supersedes and replaces Endorsement TR-101.\n"
            "Amended Property-in-Transit Limit: $400,000 per conveyance.\n"
        ),
        "endorsement_tr_115.txt": (
            "ENDORSEMENT TR-115 — FINAL TRANSIT LIMIT MODIFICATION\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: IM-2026-5101\n"
            "Named Insured: Pacific Freightways Inc.\n"
            "Effective Date: 2026-03-10\n"
            "Expressly supersedes and replaces Endorsement TR-108.\n"
            "Controlling Property-in-Transit Limit: $300,000 per conveyance.\n"
        ),
        "broker_draft_tr_120.txt": (
            "UNSIGNED BROKER PROPOSAL — DRAFT TR-120\n"
            "Status: DRAFT ONLY — NOT EXECUTED BY CARRIER\n"
            "Named Insured: Pacific Freightways Inc.\n"
            "Requested Property-in-Transit Limit: $500,000 per conveyance.\n"
        ),
        "carrier_underwriting_memo.txt": (
            "INTERNAL UNDERWRITING NOTE (2026-03-12)\n"
            "Account: Pacific Freightways Inc.\n"
            "Broker asked for $500,000 (draft TR-120), which was declined due to cargo theft trends.\n"
            "TR-115 was bound at $300,000 effective 2026-03-10.\n"
        ),
        "fleet_safety_certificate.txt": (
            "DOT SAFETY RATING CERTIFICATE\n"
            "Carrier: Pacific Freightways Inc. | Rating: Satisfactory | Date: 2026-01-20\n"
        ),
        "claim_notice_transit.txt": (
            "CARGO LOSS NOTICE\n"
            "Insured: Pacific Freightways Inc.\n"
            "Date of Loss: 2026-04-22\n"
            "Description: Trailer rollover destroying electronics shipment in transit.\n"
            "Question: What is the controlling Property-in-Transit limit on 2026-04-22?\n"
        ),
    }

    files["B02"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY DECLARATIONS\n"
            "Policy Number: PRO-2026-5202\n"
            "Named Insured: Riverbend Millworks LLC\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Policy: Flood peril is EXCLUDED unless added by executed endorsement.\n"
        ),
        "endorsement_fl_201.txt": (
            "ENDORSEMENT FL-201 — PROSPECTIVE FLOOD COVERAGE\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PRO-2026-5202\n"
            "Named Insured: Riverbend Millworks LLC\n"
            "Effective Date: 2026-04-01\n"
            "Effective strictly for flood losses occurring on or after 2026-04-01, "
            "this endorsement provides a $500,000 Flood Sublimit. Does not apply retroactively.\n"
        ),
        "endorsement_fl_205.txt": (
            "ENDORSEMENT FL-205 — INTERIM FLOOD SUBLIMIT BINDER\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PRO-2026-5202\n"
            "Named Insured: Riverbend Millworks LLC\n"
            "Effective Date: 2026-03-01 (Applies to losses from 2026-03-01 through 2026-03-31)\n"
            "Provides a controlling Flood Sublimit of $200,000 for losses occurring between "
            "2026-03-01 and 2026-03-31.\n"
        ),
        "broker_summary_april.txt": (
            "From: R. Kim <rkim@riverbroker.example>\n"
            "Date: 2026-04-02\n"
            "Subject: Riverbend Flood Endorsements\n"
            "Good news — FL-201 ($500,000) took effect April 1. Note that March losses are governed "
            "by the interim binder FL-205.\n"
        ),
        "elevation_certificate.txt": (
            "FEMA FLOOD ELEVATION CERTIFICATE\n"
            "Site: Riverbend Millworks LLC | Flood Zone: AE | Base Flood Elevation: 112 ft.\n"
        ),
        "prior_year_2025_dec.txt": (
            "EXPIRED 2025 POLICY DECLARATIONS (2025-01-01 to 2026-01-01)\n"
            "Named Insured: Riverbend Millworks LLC | Flood Sublimit: $100,000 (EXPIRED).\n"
        ),
        "claim_notice_march_flood.txt": (
            "FIRST NOTICE OF LOSS\n"
            "Insured: Riverbend Millworks LLC\n"
            "Date of Loss: 2026-03-15\n"
            "Description: Spring river overflow flooded ground-floor lumber storage.\n"
            "Question: What is the controlling Flood Sublimit for the 2026-03-15 loss?\n"
        ),
    }

    files["B03"] = {
        "policy_declarations.txt": (
            "BUSINESS AUTO POLICY DECLARATIONS\n"
            "Policy Number: BAP-2026-5303\n"
            "Named Insured: Crestline Concrete Pumping Inc.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Covered Auto Symbol: Symbol 7 (Specifically Described Autos ONLY).\n"
        ),
        "endorsement_ba_701.txt": (
            "ENDORSEMENT BA-701 — SYMBOL 7 SCHEDULE REQUIREMENT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: BAP-2026-5303\n"
            "Named Insured: Crestline Concrete Pumping Inc.\n"
            "Effective Date: 2026-01-01\n"
            "Coverage under Symbol 7 applies ONLY to vehicles explicitly listed by VIN on "
            "Schedule of Covered Vehicles V-2026. Vehicles omitted from Schedule V-2026 have no coverage.\n"
        ),
        "expired_2025_fleet_draft.txt": (
            "2025 FLEET WORKSHEET (EXPIRED — NOT SCHEDULE V-2026)\n"
            "Unit #12 | VIN 1HTMMAAL45H001212\n"
            "Unit #14 | VIN 1HTMMAAL45H001414 (Note: leased unit, verify 2026 renewal inclusion)\n"
        ),
        "broker_email_schedule_pending.txt": (
            "From: T. Brooks <tbrooks@autobroker.example>\n"
            "Date: 2026-01-18\n"
            "Subject: Schedule V-2026 still pending\n"
            "Insured is confirming whether Unit #14 stays on the 2026 fleet. Schedule V-2026 to follow.\n"
        ),
        "driver_mvr_summary.txt": (
            "DRIVER MVR CLEARANCE LOG (2026)\n"
            "All 9 commercial drivers hold valid Class A CDL licenses.\n"
        ),
        "maintenance_invoice_unit14.txt": (
            "FLEET REPAIR INVOICE\n"
            "Vehicle: Unit #14 (VIN 1HTMMAAL45H001414) | Brake service completed 2026-02-11.\n"
        ),
        "claim_notice_unit14.txt": (
            "AUTO COLLISION LOSS NOTICE\n"
            "Insured: Crestline Concrete Pumping Inc.\n"
            "Date of Loss: 2026-04-05\n"
            "Vehicle Involved: Truck #14 (VIN 1HTMMAAL45H001414)\n"
            "Question: Is Truck #14 covered under BAP-2026-5303?\n"
        ),
    }

    files["B04"] = {
        "policy_declarations.txt": (
            "MASTER COMMERCIAL PROPERTY DECLARATIONS\n"
            "Policy Number: MCP-2026-5404\n"
            "First Named Insured: Summit Holdings Corp.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Policy: Temperature-Change / Spoilage is excluded unless endorsed by entity.\n"
        ),
        "endorsement_bl_402.txt": (
            "ENDORSEMENT BL-402 — SPOILAGE COVERAGE (PARENT ENTITY ONLY)\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: MCP-2026-5404\n"
            "Applicable Entity: Summit Holdings Corp. ONLY (subsidiaries and affiliates expressly excluded).\n"
            "Effective Date: 2026-01-01\n"
            "Spoilage Sublimit: $1,000,000 per occurrence for Summit Holdings Corp. operations.\n"
        ),
        "endorsement_bl_408.txt": (
            "ENDORSEMENT BL-408 — SUBSIDIARY SPOILAGE EXTENSION\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: MCP-2026-5404\n"
            "Applicable Entity: Summit BioLabs LLC (Wholly Owned Subsidiary)\n"
            "Effective Date: 2026-01-15\n"
            "Grants Spoilage Coverage specifically to Summit BioLabs LLC with a controlling "
            "sublimit of $250,000 per occurrence.\n"
        ),
        "corporate_org_chart.txt": (
            "SUMMIT CORPORATE STRUCTURE (2026)\n"
            "Parent: Summit Holdings Corp.\n"
            "Subsidiary: Summit BioLabs LLC (100% owned, operates cold-chain reagent lab)\n"
        ),
        "broker_correspondence_spoilage.txt": (
            "From: A. Lin <alin@lifescibroker.example>\n"
            "Date: 2026-01-20\n"
            "Subject: Summit BioLabs spoilage coverage\n"
            "Confirmed BL-408 is executed for Summit BioLabs LLC; note BL-402 applies only to parent.\n"
        ),
        "freezer_alarm_log.txt": (
            "COLD STORAGE TELEMETRY REPORT\n"
            "Facility: Summit BioLabs LLC | Ultra-low freezer excursion logged 2026-05-03.\n"
        ),
        "claim_notice_spoilage.txt": (
            "SPOILAGE LOSS NOTICE\n"
            "Claimant Entity: Summit BioLabs LLC\n"
            "Date of Loss: 2026-05-03\n"
            "Description: Power surge caused failure of reagent storage freezers.\n"
            "Question: What is the controlling Spoilage Sublimit for Summit BioLabs LLC?\n"
        ),
    }

    files["B05"] = {
        "policy_declarations.txt": (
            "COMMERCIAL GENERAL LIABILITY DECLARATIONS\n"
            "Policy Number: CGL-2026-5505\n"
            "Named Insured: Granite Ridge Builders LLC\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Form Exclusion: Contractors' Errors & Omissions (Faulty Workmanship) is EXCLUDED.\n"
        ),
        "endorsement_eo_101.txt": (
            "ENDORSEMENT EO-101 — CONTRACTORS' E&O LIMITED BUYBACK\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CGL-2026-5505\n"
            "Named Insured: Granite Ridge Builders LLC\n"
            "Effective Date: 2026-01-01\n"
            "Buys back Contractors' Errors & Omissions coverage up to a $300,000 sublimit.\n"
        ),
        "endorsement_eo_109.txt": (
            "ENDORSEMENT EO-109 — REVOCATION OF CONTRACTORS' E&O BUYBACK\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CGL-2026-5505\n"
            "Named Insured: Granite Ridge Builders LLC\n"
            "Effective Date: 2026-02-01\n"
            "This endorsement expressly revokes and deletes Endorsement EO-101 in its entirety "
            "effective 2026-02-01. The Base Form Contractors' Errors & Omissions Exclusion is "
            "reinstated in full.\n"
        ),
        "broker_renewal_summary_jan.txt": (
            "From: K. Walsh <kwalsh@buildbroker.example>\n"
            "Date: 2026-01-04\n"
            "Subject: Granite Ridge policy bound\n"
            "Policy bound with EO-101 ($300,000 Contractors' E&O buyback).\n"
        ),
        "underwriting_cancellation_notice_feb.txt": (
            "MID-TERM ENDORSEMENT NOTICE (2026-02-01)\n"
            "Following adverse QA audit findings, Endorsement EO-109 has been issued and accepted "
            "by the insured, removing EO-101 effective 2026-02-01 with a $4,100 return premium.\n"
        ),
        "project_contract_summary.txt": (
            "SUBCONTRACT SUMMARY — OAKWOOD OFFICE PARK\n"
            "Contractor: Granite Ridge Builders LLC | Scope: Structural framing.\n"
        ),
        "claim_notice_workmanship.txt": (
            "CLAIM NOTICE\n"
            "Insured: Granite Ridge Builders LLC\n"
            "Date of Loss: 2026-05-10\n"
            "Description: Claim solely for cost to tear out and redo insured's own faulty framing "
            "(Contractors' Errors & Omissions / faulty workmanship).\n"
            "Question: Is this 2026-05-10 Contractors' E&O claim covered or excluded?\n"
        ),
    }

    files["B06"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY DECLARATIONS\n"
            "Policy Number: PRO-2026-5606\n"
            "Named Insured: Ironworks Fabrication Inc.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Locations: Plant 1 (Detroit), Plant 2 (Toledo)\n"
            "Base Deductible: $25,000 per occurrence\n"
        ),
        "broker_summary_email.txt": (
            "From: J. Morales <jmorales@indbroker.example>\n"
            "Date: 2026-01-08\n"
            "Subject: Quick summary of Ironworks deductible\n"
            "Hi team, I believe Ironworks has a $10,000 deductible across plants — please check the "
            "actual executed endorsements before quoting claim reserves.\n"
        ),
        "endorsement_dd_301.txt": (
            "ENDORSEMENT DD-301 — DEDUCTIBLE INCREASE\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PRO-2026-5606\n"
            "Named Insured: Ironworks Fabrication Inc.\n"
            "Effective Date: 2026-01-01\n"
            "Replaces Base Deductible of $25,000 with $50,000 per occurrence at all locations.\n"
        ),
        "endorsement_dd_304.txt": (
            "ENDORSEMENT DD-304 — PLANT 2 DEDUCTIBLE AMENDMENT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: PRO-2026-5606\n"
            "Named Insured: Ironworks Fabrication Inc.\n"
            "Effective Date: 2026-02-10\n"
            "Amends Endorsement DD-301 solely for Plant 2 (Toledo): controlling deductible for "
            "Plant 2 is $35,000 per occurrence. Plant 1 remains at $50,000.\n"
        ),
        "plant2_sprinkler_cert.txt": (
            "NFPA 25 SPRINKLER INSPECTION CERTIFICATE\n"
            "Location: Plant 2 (Toledo) | Status: Passed | Date: 2026-02-01\n"
        ),
        "claim_notice_plant2.txt": (
            "FIRST NOTICE OF LOSS\n"
            "Insured: Ironworks Fabrication Inc.\n"
            "Location: Plant 2 (Toledo)\n"
            "Date of Loss: 2026-04-17\n"
            "Description: Electrical panel fire causing $180,000 in covered property damage.\n"
            "Question: What is the controlling deductible for this Plant 2 loss?\n"
        ),
    }

    files["B07"] = {
        "policy_declarations.txt": (
            "COMMERCIAL PROPERTY DECLARATIONS\n"
            "Policy Number: PRO-2026-5707\n"
            "Named Insured: Evergreen Senior Living LLC\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Policy Exclusion: Fungi, Wet Rot, Dry Rot, and Mold are EXCLUDED.\n"
        ),
        "unsigned_endorsement_mx_502.txt": (
            "ENDORSEMENT MX-502 — LIMITED MOLD REMEDIATION BUYBACK\n"
            "Status: UNSIGNED / PENDING CARRIER COUNTERSIGNATURE\n"
            "Policy Number: PRO-2026-5707\n"
            "Named Insured: Evergreen Senior Living LLC\n"
            "Proposed Effective Date: 2026-05-15\n"
            "Proposed Mold Remediation Sublimit: $150,000\n"
            "Carrier Authorized Signature: [BLANK — NOT EXECUTED]\n"
        ),
        "broker_email_may.txt": (
            "From: N. Gupta <ngupta@carebroker.example>\n"
            "Date: 2026-05-18\n"
            "Subject: MX-502 sent to underwriter for signature\n"
            "We submitted MX-502 to the carrier on May 15 and are still awaiting written confirmation "
            "of whether underwriting countersigned and bound it prior to June 1.\n"
        ),
        "hvac_service_report.txt": (
            "HVAC HUMIDITY INSPECTION REPORT\n"
            "Facility: Evergreen Senior Living LLC | Wing C ductwork condensation noted.\n"
        ),
        "billing_ledger_2026.txt": (
            "ACCOUNT BILLING LEDGER (AS OF 2026-05-30)\n"
            "Base Policy Premium: $68,500 (Paid). No endorsement billing posted for MX-502.\n"
        ),
        "claim_notice_mold.txt": (
            "CLAIM INQUIRY\n"
            "Insured: Evergreen Senior Living LLC\n"
            "Date of Loss: 2026-06-01\n"
            "Description: Mold remediation in Wing C ($95,000 estimate).\n"
            "Question: Was MX-502 executed to provide $150,000 mold coverage for the 2026-06-01 loss?\n"
        ),
    }

    files["B08"] = {
        "policy_declarations.txt": (
            "COMMERCIAL GENERAL LIABILITY DECLARATIONS\n"
            "Policy Number: CGL-2026-5808\n"
            "Named Insured: Atlas Industrial Services Corp.\n"
            "Policy Period: 2026-01-01 to 2027-01-01\n"
            "Base Annual Aggregate Limit: $1,000,000\n"
            "Per-Occurrence Limit: $1,000,000\n"
        ),
        "endorsement_ag_201.txt": (
            "ENDORSEMENT AG-201 — ANNUAL AGGREGATE INCREASE\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CGL-2026-5808\n"
            "Named Insured: Atlas Industrial Services Corp.\n"
            "Effective Date: 2026-01-01\n"
            "Increases Annual Aggregate Limit from $1,000,000 to $1,500,000.\n"
        ),
        "endorsement_ag_209.txt": (
            "ENDORSEMENT AG-209 — AMENDED ANNUAL AGGREGATE LIMIT\n"
            "Status: EXECUTED & COUNTERSIGNED\n"
            "Policy Number: CGL-2026-5808\n"
            "Named Insured: Atlas Industrial Services Corp.\n"
            "Effective Date: 2026-02-01\n"
            "Expressly supersedes and replaces Endorsement AG-201.\n"
            "Controlling Annual Aggregate Limit for the 2026 policy term: $1,200,000.\n"
        ),
        "loss_run_2026.txt": (
            "OFFICIAL CARRIER LOSS RUN — POLICY CGL-2026-5808\n"
            "Valued As Of: 2026-07-01\n"
            "Named Insured: Atlas Industrial Services Corp.\n"
            "Claim CLM-2026-011 (Closed, Paid Indemnity): $450,000 (erodes 2026 Annual Aggregate)\n"
            "Total Paid Erosion Against 2026 Annual Aggregate: $450,000\n"
        ),
        "broker_draft_ag_215.txt": (
            "UNSIGNED DRAFT REQUEST — AG-215\n"
            "Status: DRAFT ONLY — NOT BOUND\n"
            "Requested Annual Aggregate: $2,000,000.\n"
        ),
        "safety_training_roster.txt": (
            "OSHA 30 TRAINING COMPLETION LOG (2026)\n"
            "42 field technicians certified.\n"
        ),
        "claim_notice_july.txt": (
            "NEW CLAIM RESERVE INQUIRY\n"
            "Insured: Atlas Industrial Services Corp.\n"
            "Date of Loss: 2026-07-15\n"
            "Description: Third-party bodily injury claim at jobsite.\n"
            "Question: What is the remaining available Annual Aggregate limit after prior 2026 erosion?\n"
        ),
    }

    dev_keys: list[dict[str, Any]] = [
        {
            "packet_id": "D01",
            "tier": "clean",
            "prompt": (
                "Audit packet D01 (Harborview Cold Storage LLC, loss 2026-04-12). "
                "Determine coverage status and the controlling Per-Occurrence limit."
            ),
            "expected_determination": "covered",
            "expected_value": "2000000",
            "required_controlling_docs": ["endorsement_gl_105.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_gl_105.txt"],
            "expected_subject": "Harborview Cold Storage LLC",
            "decoy_docs": ["marketing_overview_2025.txt"],
        },
        {
            "packet_id": "D02",
            "tier": "branching",
            "prompt": (
                "Audit packet D02 (Cascadia Timberworks Inc., loss 2026-03-18). "
                "Determine coverage status, controlling Water Damage Sublimit, and supersession chain."
            ),
            "expected_determination": "covered",
            "expected_value": "75000",
            "required_controlling_docs": ["endorsement_ed_409.txt"],
            "expected_chain": [
                "policy_declarations.txt",
                "endorsement_ed_401.txt",
                "endorsement_ed_409.txt",
            ],
            "expected_subject": "Cascadia Timberworks Inc.",
            "decoy_docs": ["draft_endorsement_water_2026.txt"],
        },
        {
            "packet_id": "D03",
            "tier": "branching",
            "prompt": (
                "Audit packet D03 (Redrock Distribution Co., loss 2026-05-02 at Warehouse Annex B, "
                "412 Dockside Rd). Determine whether coverage can be confirmed from the archive."
            ),
            "expected_determination": "insufficient_evidence",
            "expected_value": "insufficient_evidence",
            "required_controlling_docs": ["endorsement_sl_204.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_sl_204.txt"],
            "expected_subject": "Redrock Distribution Co.",
            "decoy_docs": ["expired_2024_location_list.txt"],
            "expected_missing_keywords": ["schedule a-2026"],
        },
    ]

    heldout_keys: list[dict[str, Any]] = [
        {
            "packet_id": "C01",
            "tier": "clean",
            "prompt": (
                "Audit packet C01 (Beacon Health Systems LLC, incident 2026-04-08). "
                "Determine coverage status and controlling Cyber Incident Response Sublimit."
            ),
            "expected_determination": "covered",
            "expected_value": "500000",
            "required_controlling_docs": ["endorsement_cy_102.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_cy_102.txt"],
            "expected_subject": "Beacon Health Systems LLC",
            "decoy_docs": [],
        },
        {
            "packet_id": "C02",
            "tier": "clean",
            "prompt": (
                "Audit packet C02 (Gulfcrest Hospitality Group Inc., Named Storm loss 2026-08-22 "
                "at 88 Oceanfront Blvd). Determine coverage status and controlling deductible."
            ),
            "expected_determination": "covered_with_deductible",
            "expected_value": "150000",
            "required_controlling_docs": ["endorsement_ns_201.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_ns_201.txt"],
            "expected_subject": "Gulfcrest Hospitality Group Inc.",
            "decoy_docs": [],
        },
        {
            "packet_id": "C03",
            "tier": "clean",
            "prompt": (
                "Audit packet C03 (Heartland Chemical Blending Corp., sudden solvent release "
                "2026-06-11). Determine whether the release is covered or excluded."
            ),
            "expected_determination": "excluded",
            "expected_value": "excluded",
            "required_controlling_docs": ["cgl_coverage_form_exclusions.txt"],
            "expected_chain": ["policy_declarations.txt", "cgl_coverage_form_exclusions.txt"],
            "expected_subject": "Heartland Chemical Blending Corp.",
            "decoy_docs": ["carrier_product_brochure.txt"],
        },
        {
            "packet_id": "C04",
            "tier": "clean",
            "prompt": (
                "Audit packet C04 (Midwest Precision Stamping LLC, press breakdown 2026-05-19). "
                "Determine coverage status and controlling Equipment Breakdown limit."
            ),
            "expected_determination": "covered",
            "expected_value": "750000",
            "required_controlling_docs": ["endorsement_eb_305.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_eb_305.txt"],
            "expected_subject": "Midwest Precision Stamping LLC",
            "decoy_docs": [],
        },
        {
            "packet_id": "B01",
            "tier": "branching",
            "prompt": (
                "Audit packet B01 (Pacific Freightways Inc., transit loss 2026-04-22). "
                "Determine coverage status, controlling Property-in-Transit limit, and chain."
            ),
            "expected_determination": "covered",
            "expected_value": "300000",
            "required_controlling_docs": ["endorsement_tr_115.txt"],
            "expected_chain": [
                "policy_declarations.txt",
                "endorsement_tr_101.txt",
                "endorsement_tr_108.txt",
                "endorsement_tr_115.txt",
            ],
            "expected_subject": "Pacific Freightways Inc.",
            "decoy_docs": ["broker_draft_tr_120.txt"],
        },
        {
            "packet_id": "B02",
            "tier": "branching",
            "prompt": (
                "Audit packet B02 (Riverbend Millworks LLC, flood loss 2026-03-15). "
                "Determine coverage status and controlling Flood Sublimit for this loss date."
            ),
            "expected_determination": "covered",
            "expected_value": "200000",
            "required_controlling_docs": ["endorsement_fl_205.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_fl_205.txt"],
            "expected_subject": "Riverbend Millworks LLC",
            "decoy_docs": ["endorsement_fl_201.txt", "prior_year_2025_dec.txt"],
        },
        {
            "packet_id": "B03",
            "tier": "branching",
            "prompt": (
                "Audit packet B03 (Crestline Concrete Pumping Inc., auto loss 2026-04-05 involving "
                "Truck #14). Determine whether coverage can be confirmed from the archive."
            ),
            "expected_determination": "insufficient_evidence",
            "expected_value": "insufficient_evidence",
            "required_controlling_docs": ["endorsement_ba_701.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_ba_701.txt"],
            "expected_subject": "Crestline Concrete Pumping Inc.",
            "decoy_docs": ["expired_2025_fleet_draft.txt"],
            "expected_missing_keywords": ["v-2026"],
        },
        {
            "packet_id": "B04",
            "tier": "branching",
            "prompt": (
                "Audit packet B04 (Summit BioLabs LLC, spoilage loss 2026-05-03). "
                "Determine coverage status and controlling Spoilage Sublimit for Summit BioLabs LLC."
            ),
            "expected_determination": "covered",
            "expected_value": "250000",
            "required_controlling_docs": ["endorsement_bl_408.txt"],
            "expected_chain": ["policy_declarations.txt", "endorsement_bl_408.txt"],
            "expected_subject": "Summit BioLabs LLC",
            "decoy_docs": ["endorsement_bl_402.txt"],
        },
        {
            "packet_id": "B05",
            "tier": "branching",
            "prompt": (
                "Audit packet B05 (Granite Ridge Builders LLC, faulty workmanship claim 2026-05-10). "
                "Determine whether the Contractors' E&O claim is covered or excluded."
            ),
            "expected_determination": "excluded",
            "expected_value": "excluded",
            "required_controlling_docs": ["endorsement_eo_109.txt"],
            "expected_chain": [
                "policy_declarations.txt",
                "endorsement_eo_101.txt",
                "endorsement_eo_109.txt",
            ],
            "expected_subject": "Granite Ridge Builders LLC",
            "decoy_docs": ["broker_renewal_summary_jan.txt"],
        },
        {
            "packet_id": "B06",
            "tier": "branching",
            "prompt": (
                "Audit packet B06 (Ironworks Fabrication Inc., fire loss 2026-04-17 at Plant 2 "
                "Toledo). Determine coverage status and controlling deductible for Plant 2."
            ),
            "expected_determination": "covered_with_deductible",
            "expected_value": "35000",
            "required_controlling_docs": ["endorsement_dd_304.txt"],
            "expected_chain": [
                "policy_declarations.txt",
                "endorsement_dd_301.txt",
                "endorsement_dd_304.txt",
            ],
            "expected_subject": "Ironworks Fabrication Inc.",
            "decoy_docs": ["broker_summary_email.txt"],
        },
        {
            "packet_id": "B07",
            "tier": "branching",
            "prompt": (
                "Audit packet B07 (Evergreen Senior Living LLC, mold remediation claim 2026-06-01). "
                "Determine whether MX-502 coverage can be confirmed from the archive."
            ),
            "expected_determination": "insufficient_evidence",
            "expected_value": "insufficient_evidence",
            "required_controlling_docs": ["unsigned_endorsement_mx_502.txt"],
            "expected_chain": ["policy_declarations.txt", "unsigned_endorsement_mx_502.txt"],
            "expected_subject": "Evergreen Senior Living LLC",
            "decoy_docs": [],
            "expected_missing_keywords": ["executed", "signature", "mx-502"],
        },
        {
            "packet_id": "B08",
            "tier": "branching",
            "prompt": (
                "Audit packet B08 (Atlas Industrial Services Corp., bodily injury claim 2026-07-15). "
                "Determine the remaining available 2026 Annual Aggregate limit after prior erosion."
            ),
            "expected_determination": "covered",
            "expected_value": "750000",
            "required_controlling_docs": ["endorsement_ag_209.txt", "loss_run_2026.txt"],
            "expected_chain": [
                "policy_declarations.txt",
                "endorsement_ag_201.txt",
                "endorsement_ag_209.txt",
                "loss_run_2026.txt",
            ],
            "expected_subject": "Atlas Industrial Services Corp.",
            "decoy_docs": ["broker_draft_ag_215.txt"],
        },
    ]
    return files, dev_keys, heldout_keys


def main() -> None:
    files, dev_keys, heldout_keys = _build_packets()
    if ARCHIVE_ROOT.exists():
        shutil.rmtree(ARCHIVE_ROOT)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)

    file_hashes: dict[str, str] = {}
    for packet_id, doc_map in sorted(files.items()):
        pdir = ARCHIVE_ROOT / packet_id
        pdir.mkdir(parents=True, exist_ok=True)
        for fname, content in sorted(doc_map.items()):
            fpath = pdir / fname
            raw = content.encode("utf-8")
            fpath.write_bytes(raw)
            file_hashes[f"{packet_id}/{fname}"] = _sha256_bytes(raw)

    DEV_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    dev_bytes = (json.dumps(dev_keys, indent=2) + "\n").encode("utf-8")
    held_bytes = (json.dumps(heldout_keys, indent=2) + "\n").encode("utf-8")
    DEV_KEY_PATH.write_bytes(dev_bytes)
    HELDOUT_KEY_PATH.write_bytes(held_bytes)

    manifest: dict[str, Any] = {
        "archive_root": str(ARCHIVE_ROOT),
        "dev_packets": [k["packet_id"] for k in dev_keys],
        "heldout_packets": [k["packet_id"] for k in heldout_keys],
        "dev_key_sha256": _sha256_bytes(dev_bytes),
        "heldout_key_sha256": _sha256_bytes(held_bytes),
        "archive_file_count": len(file_hashes),
        "archive_files_sha256": file_hashes,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Built {len(files)} packets ({len(file_hashes)} docs) in {ARCHIVE_ROOT}; "
        f"dev_key={manifest['dev_key_sha256'][:12]} heldout_key={manifest['heldout_key_sha256'][:12]}"
    )


if __name__ == "__main__":
    main()
