# Evidence index

This directory contains the repository-level evidence for the split. The four
study folders retain their own raw/numerical evidence, provenance files and
fail-closed builders; those artifacts are not duplicated here.

| Artifact | What it proves | Boundary |
|---|---|---|
| [`split-manifest.json`](split-manifest.json) | Pinned source commit, complete five-folder inventory, declared path-only edits, unchanged-tree digest and scientific-artifact digest | Proves repository content identity, not that Azure measurements are universally repeatable |
| [`rule-results.json`](rule-results.json) | One evaluator-derived result for every SOP-68 `RUN-001` through `RUN-015` rule | `N/A` is allowed only with a type-based reason; every applicable rule must be `PASS` |
| [`exemplar-alignment.md`](exemplar-alignment.md) | Immutable exemplar, type adaptation and four-column S0–S10 extraction | Proves layout-contract adoption, not training-domain similarity |
| [`ui-evidence.json`](ui-evidence.json) | [Desktop](../images/qira-live-console-desktop.png) / [mobile](../images/qira-live-console-mobile.png) capture hashes, LIVE/Runner labels, source/deployed CSS hash and overflow differential | UI proves product surface and current status; behavior/numbers require tests and run evidence |
| [`bilingual-audit.md`](bilingual-audit.md) | Independent native-Chinese and cross-language semantic audit after remediation | Material findings are zero; deterministic parity remains enforced separately |

## Claim-to-evidence map

| Claim | Primary evidence | Executable gate |
|---|---|---|
| The split did not alter retained measurements | Per-study `outputs/`, `datasets/`, `config/`, console `replay/` | `scripts/build_split_manifest.py --check` |
| README numbers come from retained evidence | Per-study report builders and provenance JSON | `scripts/validate_repo.py` report checks |
| Model Router served-model shares are traceable to request rows | `qira-model-router-validation/outputs/` | Router tests, full-text verifier and documentation gate |
| The demo replay uses the same recorded rows as the reports | `qira-live-benchmark-console/replay/replay_pack.json` | `build_replay_pack.py --check` plus console tests |
| Withheld public cells remain in numerical aggregates | Four `outputs/public_redaction.json` records and response hashes | Full-text and results-integrity verifiers |
| The new root is independently usable | Root README, requirements, component-relative links | Layout, links, stale-path and bilingual gates |

## Run the gate

```bash
cd Agents/Model-And-Router-Benchmark
python -m pip install -r requirements.txt
python scripts/validate_repo.py
```

The gate is fail-closed. Missing evidence, a stale generated report, an
undeclared byte change, a broken local link, a public-redaction drift or a test
failure exits non-zero. It makes no Azure/model calls.
