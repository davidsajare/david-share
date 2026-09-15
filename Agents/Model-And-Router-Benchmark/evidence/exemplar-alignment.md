# Layout exemplar alignment

| Field | Value |
|---|---|
| Repository type | Public, executable, bilingual Azure benchmark with retained evidence and a self-hosted console |
| Immutable exemplar | [`AI-Foundry-Custom-Code-Training@3ffd2b1`](https://github.com/david-xinyuwei/david-share/tree/3ffd2b1fffa19ce1315851e07b7ae854ffd340e3/Deep-Learning/AI-Foundry-Custom-Code-Training) |
| Exemplar version read | `README.md` at `3ffd2b1fffa19ce1315851e07b7ae854ffd340e3`, 620 lines |
| Report chapter standard | Executive Summary with a test-condition column, then Background, Methodology, Results, Cost, Configuration, Reproducing, Evidence |
| Adaptation rule | Take the reader path and the evidence discipline; take no domain content, no screenshot count and no chapter numbering that this project does not need |

## Slot extraction

| Slot | Content here | Evidence entry | Native gate |
|---|---|---|---|
| S0 first screen | Accurate project H1, 5 fact badges, a positioning paragraph ending in a measured spread, confirmed author, language switch, 5-item navigation | Root `README.md` / `README-CN.md` | `validate_reader_contract`: badge count, element order, H2 sequence, navigation anchors |
| S0.5 use entry | `6. Reproducing` gives one offline acceptance command per platform with a single Done-When and an explicit "no cloud resource" statement | `#reproducing` | Required command tokens and bilingual code-fence equality |
| S1 responsibility boundary | `2. Methodology` opens with a control table whose second column states what each control does **not** prove | `#methodology` | Required boundary tokens; 6 declared control rows |
| S2 validation matrix | `3. Results` separates the direct matrix, router selection and production behaviour, each with its sample size | `#results` | Component report builders and full-text verifiers |
| S3 product walkthrough | Deliberately absent. This repository publishes no hosted instance, so a console screenshot would prove nothing about a measurement | — | `RUN-011` records the N/A with its reason |
| S4 executable assets | `7. Evidence and boundaries` maps every directory to what it holds | `#evidence` | Local link resolution |
| S5 measured matrix | Executive Summary table plus per-study run IDs and terminal counts | `#executive-summary` | Bilingual numeric parity and builder `--check` |
| S6 deep dive | API-path effect, reasoning-effort effect and the in-stream rate-limit shape, each stated with its boundary | `#methodology`, `#results` | Report builders regenerate these from retained rows |
| S7 Quick Start | Offline path has no side effects; the paid path is separated and never carries access details | `#reproducing` | Code-fence equality between languages |
| S8 tests | Component suites plus repository-gate mutation tests that remove one guarantee at a time | `tests/`, per-study `tests/` | `validate_repo.py` runs all of them |
| S9 tail | Public-data boundary, withheld cells, and what the evidence does not cover | `#evidence` | Redaction contract and public-boundary gate |
| S10 directory | Only directories that exist and carry distinct responsibility | `Repository layout` | Layout gate |

## Principles taken, not copied

1. The first screen compresses a decision, not the archive.
2. The only onboarding path sits immediately after the first screen and separates
   the no-side-effect path from the paid one.
3. Every result points at retained rows or an executable builder.
4. `scripts/`, `tests/` and `evidence/` are load-bearing and are never traded for prose.
5. Boundaries sit next to the claims they limit, not in a closing disclaimer.
6. A public repository carries no customer identity, no endpoint and no credential;
   that rule is enforced by a gate rather than by review habit.
