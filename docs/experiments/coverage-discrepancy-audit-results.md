# Coverage Discrepancy Audit — Read-Only Held-Out Results

- **Pinned Commit:** `7dc6bf2112d779e83ac8065c8edfa96f46fd9bbd`
- **Timestamp (UTC):** `2026-11-22T21:56:14.061672+00:00`
- **Model Alias:** `eval-anthropic` (`concurrency=1`, alternating arm order)
- **Held-Out Key SHA-256:** `76816a5246ab901e45b9725fc4f9c932a35d2a398ed2b55aa0b719f619f7164c`
- **Archive Docs Count:** 91 files under `policy-archive/`
- **Shared Equal Budgets:** 16 model calls, 32 tool calls, 96,000 total tokens, 180s timeout per attempt; `general-purpose` disabled; identical `chat_model` across investigation, summarization, specialist, and synthesis.

## Model Provenance Verification

| Arm | Unique Requested Models | Unique Responded Models |
|---|---|---|
| `baseline` | `eval-anthropic` | `eval-anthropic` |
| `deep_agents` | `eval-anthropic` | `eval-anthropic` |

## Aggregate Results (Clean vs. Branching — Separate from Submission Triage)

| Arm | Tier | Attempted | Passed | Det. Correct | False Def. on Insuff. | Errors | Avg Calls | Avg Tools | Avg Tokens | Total Cost | Avg Lat (s) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `baseline` | overall | 36 | 7/36 (19.4%) | 36/36 | 0 | 0 | 4.0 | 7.33 | 11,723 | \$2.8845 | 23.73 |
| `baseline` | clean | 12 | 2/12 (16.7%) | 12/12 | 0 | 0 | 4.0 | 6.0 | 10,572 | \$0.8592 | 21.6 |
| `baseline` | branching | 24 | 5/24 (20.8%) | 24/24 | 0 | 0 | 4.0 | 8.0 | 12,299 | \$2.0253 | 24.79 |
| `deep_agents` | overall | 36 | 7/36 (19.4%) | 36/36 | 0 | 0 | 7.06 | 15.11 | 41,936 | \$7.2223 | 40.61 |
| `deep_agents` | clean | 12 | 4/12 (33.3%) | 12/12 | 0 | 0 | 7.0 | 13.0 | 38,861 | \$2.1635 | 35.68 |
| `deep_agents` | branching | 24 | 3/24 (12.5%) | 24/24 | 0 | 0 | 7.08 | 16.17 | 43,473 | \$5.0588 | 43.08 |

## Per-Case Paired Table (3 Trials per Arm)

| Packet | Tier | Baseline Pass | Deep Agents Pass | Baseline Avg Tok | DA Avg Tok |
|---|---|---:|---:|---:|---:|
| `B01` | branching | 2/3 | 2/3 | 13,325 | 45,743 |
| `B02` | branching | 3/3 | 0/3 | 11,641 | 41,596 |
| `B03` | branching | 0/3 | 0/3 | 12,307 | 43,444 |
| `B04` | branching | 0/3 | 0/3 | 12,666 | 43,177 |
| `B05` | branching | 0/3 | 0/3 | 11,978 | 44,262 |
| `B06` | branching | 0/3 | 1/3 | 11,853 | 44,020 |
| `B07` | branching | 0/3 | 0/3 | 12,923 | 42,917 |
| `B08` | branching | 0/3 | 0/3 | 11,696 | 42,628 |
| `C01` | clean | 0/3 | 2/3 | 10,322 | 38,528 |
| `C02` | clean | 2/3 | 1/3 | 11,024 | 39,201 |
| `C03` | clean | 0/3 | 0/3 | 10,362 | 39,067 |
| `C04` | clean | 0/3 | 1/3 | 10,580 | 38,649 |

## Pre-Registered Routing Bar Evaluation

- **Baseline branching cases passed (majority of 3):** 2/8
- **Deep Agents branching cases passed (majority of 3):** 1/8
- **Delta (Deep Agents − Baseline, target ≥ +2):** -1
- **Deep Agents false definitive on insufficient-evidence (target 0):** 0
- **Token cost per verified branching attempt vs. Baseline (target ≤ 2.0×):** 5.89×
- **Routing Bar Met:** `False`

