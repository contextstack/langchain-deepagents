# Coverage Discrepancy Audit — Two Missing Control Packets (`CTRL-C04` & `CTRL-B08`)

> **Separate Report:** Reported separately from the original 12-packet held-out corpus because these two control packets use the issue-level output schema (`IssueAuditBrief`) and new synthetic cases.

- **Run ID:** `cov-ctrl-v2-20261123`
- **Pinned Commit:** `02f1bc81f74b830265c7ab07539ba7fd43bb743b`
- **Timestamp (UTC):** `2026-11-23T08:51:56.486562+00:00`
- **Route / Model Alias:** `eval-anthropic` (`concurrency=1`, alternating arms)
- **Fixture Key SHA-256:** `dbfe84a9b80006463b85403c66c1ac22e3e5abd5c9dc84a9d58d9ea2569bfec2`
- **Rubric SHA-256 (`evals/coverage_audit_v3.py`):** `a83a2f30c73ab08134233894a6c09900f6dd8ec95e1b4f361acdec7ba5f970e0`
- **Effective Caps:** `max_model_calls=16`, `max_tool_calls=32`, `max_total_tokens=96000`, `timeout_s=180.0`, `model_timeout_s=60.0`, `model_max_tokens=4096`.
- **Total Actual Spend (12 attempts):** \$1.6360 (prior estimate: ~\$1.69)

## Model Provenance Verification

| Arm | Unique Requested Models | Unique Responded Models |
|---|---|---|
| `baseline` | `eval-anthropic` | `eval-anthropic` |
| `deep_agents` | `eval-anthropic` | `eval-anthropic` |

## Per-Packet Results (3 Trials per Arm)

| Packet | Tier | Arm | Attempted | Verified Pass | Issues Passed | False Def. | Errors | Avg Calls | Avg Tools | Avg Tokens | Cost | Avg Lat (s) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `CTRL-C04` | clean | `baseline` | 3 | 0/3 | 0/3 | 0 | 0 | 4.0 | 5.0 | 9,925 | \$0.1881 | 18.71 |
| `CTRL-C04` | clean | `deep_agents` | 3 | 0/3 | 0/3 | 0 | 0 | 7.0 | 11.0 | 38,367 | \$0.5211 | 33.74 |
| `CTRL-B08` | branching | `baseline` | 3 | 0/3 | 0/6 | 0 | 0 | 4.0 | 8.0 | 13,070 | \$0.2743 | 27.42 |
| `CTRL-B08` | branching | `deep_agents` | 3 | 0/3 | 1/6 | 0 | 0 | 7.0 | 17.0 | 43,647 | \$0.6526 | 44.27 |

## Per-Trial Breakdown

| Trial | Packet | Arm | Passed | Issues | Calls | Tools | Tokens | Cost | Lat (s) |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|
| 1 | `CTRL-C04` | `baseline` | `False` | 0/1 | 4 | 5 | 9,825 | \$0.0617 | 19.1 |
| 1 | `CTRL-C04` | `deep_agents` | `False` | 0/1 | 7 | 11 | 38,513 | \$0.1755 | 35.03 |
| 2 | `CTRL-C04` | `deep_agents` | `False` | 0/1 | 7 | 11 | 38,031 | \$0.1697 | 32.04 |
| 2 | `CTRL-C04` | `baseline` | `False` | 0/1 | 4 | 5 | 9,942 | \$0.0630 | 19.2 |
| 3 | `CTRL-C04` | `baseline` | `False` | 0/1 | 4 | 5 | 10,009 | \$0.0634 | 17.83 |
| 3 | `CTRL-C04` | `deep_agents` | `False` | 0/1 | 7 | 11 | 38,556 | \$0.1758 | 34.15 |
| 1 | `CTRL-B08` | `baseline` | `False` | 0/2 | 4 | 8 | 13,310 | \$0.0948 | 28.86 |
| 1 | `CTRL-B08` | `deep_agents` | `False` | 0/2 | 7 | 17 | 43,728 | \$0.2193 | 44.58 |
| 2 | `CTRL-B08` | `deep_agents` | `False` | 1/2 | 7 | 17 | 43,083 | \$0.2105 | 43.74 |
| 2 | `CTRL-B08` | `baseline` | `False` | 0/2 | 4 | 8 | 12,851 | \$0.0878 | 25.42 |
| 3 | `CTRL-B08` | `baseline` | `False` | 0/2 | 4 | 8 | 13,050 | \$0.0916 | 27.98 |
| 3 | `CTRL-B08` | `deep_agents` | `False` | 0/2 | 7 | 17 | 44,130 | \$0.2229 | 44.48 |

## Independent Arm-Blinded Source Review (Verified Requested Decisions & Controlling Evidence)

An independent arm-blinded audit reviewed all 12 anonymized briefs (`SC-01`–`SC-12`) from `cov-ctrl-v2-20261123` against the frozen packet sources with arm labels and automated pass flags hidden, validating the pinned commit/rubric/fixture hashes, alternating pair order, zero errors, and zero false definitive answers. **Scope note:** this review verifies the requested decisions and their controlling evidence against the combined citations for each issue; ancillary narrative assertions were outside this decisive-evidence grade, and the frozen automated all-citation pass (`0/12` briefs, `1/18` issues above) is preserved unmodified alongside it.

| Arm | Attempted | Automated All-Citation Pass (Frozen Scorer) | Independent Verified Requested Decisions & Controlling Evidence | False Def. | Total Tokens (All 6) | Branching Tokens (`CTRL-B08`, 3) | Token Ratio vs. Baseline |
|---|---:|---:|---:|---:|---:|---:|---:|
| `baseline` | 6 | 0/6 (0/9 issues) | **6/6 briefs (9/9 issues)** | 0 | 68,987 | 39,211 | 1.00× |
| `deep_agents` | 6 | 0/6 (1/9 issues) | **6/6 briefs (9/9 issues)** | 0 | 246,041 | 130,941 | **3.57×** (3.34× branching) |

- **`CTRL-C04` (clean abstention, 6/6 verified):** Every brief correctly abstained on optional machinery, identified signed Form MS-10 or a signed issuance record as missing, and quoted both the conditional grant in `policy_section_06_machinery.txt` and the absent-record statement in `intake_record_machinery.txt`.
- **`CTRL-B08` (branching dual-issue, 6/6 verified on both issues):** Every brief correctly applied theft coverage at Harbor (citing `endorsement_e18_a_theft.txt`, `site_schedule_2026.txt` mapping `HBR-4` to Harbor, and `endorsement_e18_b_deductible.txt` / `issuance_register_2026.txt` confirming theft applicability was unchanged) and correctly applied the `$5,000` deductible (citing E18-A `$5,000`, actual loss date `2026-08-27`, and E18-B prospective effective date `2026-09-01`; zero briefs used the `2026-09-05` report date or `$12,000`).

## CI Coverage Provenance (Live Commit vs. Final CI Commit)

- **Pinned live-execution commit (`02f1bc8`):** `137` passing tests, `94.64%` exact 2-decimal coverage (`coverage report --precision=2`), `ruff` clean, `mypy` clean, `10/10` offline evals.
- **Final CI commit (`aff2efc`, test-only coverage addition in `tests/test_coverage_v3_calibration.py`):** `138` passing tests, **`95.25%` exact 2-decimal coverage** (`coverage report --precision=2 --fail-under=95` passed), `ruff` clean, `mypy` clean, `10/10` offline evals.

