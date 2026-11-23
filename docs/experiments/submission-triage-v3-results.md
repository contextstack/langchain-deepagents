# Submission Triage v3 — Paired A/B Rerun (Model Parity & Locked Scorer)

- **Pinned Commit:** `4822832e3e3a28d032d30852892593b55e09ba26`
- **Timestamp (UTC):** `2026-11-23T01:24:35.363164+00:00`
- **Fixtures SHA-256:** `db051e8b600b6a7383bc8693af89e7eb316a8c290dc2c5cbc653179563b32455`
- **Scorer:** `evals/submission_ab.py` (subject + full enclosing clause + typed facts)
- **Shared Budget & Controls:** Identical `chat_model` for investigation, summarization, Deep Agents coordinator/specialist, and synthesis; `general-purpose` disabled; alternating arm order; shared 12-model / 15-tool budget.

## Model Provenance Verification

| Model Alias | Arm | Unique Requested Models | Unique Responded Models |
|---|---|---|---|
| `eval-anthropic` | `baseline` | `eval-anthropic` | `eval-anthropic` |
| `eval-anthropic` | `deep_agents` | `eval-anthropic` | `eval-anthropic` |
| `eval-dgx-coder` | `baseline` | `eval-dgx-coder` | `eval-dgx-coder` |
| `eval-dgx-coder` | `deep_agents` | `eval-dgx-coder` | `eval-dgx-coder` |

## Aggregate Results (Attempted & Valid-Brief Denominators Reported Separately)

| Model | Arm | Tier | Attempted | Valid Briefs | Errors | Disp. Correct / Attempted | Facts Supported / Attempted | Facts Supported / Valid | Missed Esc. | Unnec. Esc. | Avg Calls | Avg Tokens | Total Cost | Avg Lat (s) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `eval-anthropic` | `baseline` | overall | 36 | 36 | 0 | 36/36 (100.0%) | 0/36 (0.0%) | 0/36 | 0 | 0 | 4.0 | 13,548 | \$3.3121 | 26.86 |
| `eval-anthropic` | `baseline` | clean | 12 | 12 | 0 | 12/12 (100.0%) | 0/12 (0.0%) | 0/12 | 0 | 0 | 4.0 | 13,472 | \$1.1142 | 27.45 |
| `eval-anthropic` | `baseline` | messy | 24 | 24 | 0 | 24/24 (100.0%) | 0/24 (0.0%) | 0/24 | 0 | 0 | 4.0 | 13,586 | \$2.1979 | 26.56 |
| `eval-anthropic` | `deep_agents` | overall | 36 | 34 | 2 | 34/36 (94.4%) | 0/36 (0.0%) | 0/34 | 0 | 0 | 6.67 | 42,477 | \$7.4841 | 42.7 |
| `eval-anthropic` | `deep_agents` | clean | 12 | 12 | 0 | 12/12 (100.0%) | 0/12 (0.0%) | 0/12 | 0 | 0 | 7.08 | 45,243 | \$2.6303 | 45.2 |
| `eval-anthropic` | `deep_agents` | messy | 24 | 22 | 2 | 22/24 (91.7%) | 0/24 (0.0%) | 0/22 | 0 | 0 | 6.46 | 41,095 | \$4.8537 | 41.45 |
| `eval-dgx-coder` | `baseline` | overall | 36 | 35 | 1 | 34/36 (94.4%) | 0/36 (0.0%) | 0/35 | 0 | 1 | 5.44 | 9,243 | \$0.0000 | 175.6 |
| `eval-dgx-coder` | `baseline` | clean | 12 | 12 | 0 | 11/12 (91.7%) | 0/12 (0.0%) | 0/12 | 0 | 1 | 4.33 | 7,595 | \$0.0000 | 109.03 |
| `eval-dgx-coder` | `baseline` | messy | 24 | 23 | 1 | 23/24 (95.8%) | 0/24 (0.0%) | 0/23 | 0 | 0 | 6.0 | 10,066 | \$0.0000 | 208.89 |
| `eval-dgx-coder` | `deep_agents` | overall | 36 | 15 | 21 | 10/36 (27.8%) | 0/36 (0.0%) | 0/15 | 0 | 2 | 8.22 | 35,676 | \$0.0000 | 167.33 |
| `eval-dgx-coder` | `deep_agents` | clean | 12 | 7 | 5 | 5/12 (41.7%) | 0/12 (0.0%) | 0/7 | 0 | 2 | 8.67 | 37,885 | \$0.0000 | 230.85 |
| `eval-dgx-coder` | `deep_agents` | messy | 24 | 8 | 16 | 5/24 (20.8%) | 0/24 (0.0%) | 0/8 | 0 | 0 | 8.0 | 34,571 | \$0.0000 | 135.58 |
