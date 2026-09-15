# Evidence index

This directory holds the repository-level evidence. Each study keeps its own raw
records, provenance and fail-closed builders; those artifacts are not duplicated
here.

| Artifact | What it proves | Boundary |
|---|---|---|
| [`deidentification-manifest.json`](deidentification-manifest.json) | Every numeric leaf of every de-identified measurement, dataset, config and replay file still matches the pinned pre-de-identification commit | Proves that removing identity changed no measurement; it does not re-verify the original Azure run |
| [`rule-results.json`](rule-results.json) | One evaluator-derived result for each `RUN-001` through `RUN-015` rule | `N/A` is allowed only with a type-based reason; every applicable rule must be `PASS` |
| [`exemplar-alignment.md`](exemplar-alignment.md) | The immutable layout exemplar and the slot-by-slot extraction applied here | Proves layout-contract adoption, not domain similarity |
| [`bilingual-audit.md`](bilingual-audit.md) | Independent native-Chinese and cross-language semantic audit | Material findings are zero; deterministic parity is enforced separately by the gate |

## Claim-to-evidence map

| Claim | Primary evidence | Executable gate |
|---|---|---|
| De-identification altered no number | Pinned baseline commit versus the current tree | `scripts/build_deid_manifest.py --check` |
| No customer identity or credential is published | Every tracked text file and path | `validate_public_boundary` in the repository gate |
| README numbers come from retained evidence | Per-study `outputs/` and report builders | The builder `--check` runs in the repository gate |
| Router selection shares trace to request rows | `model-router-validation/outputs/` | Router tests, full-text verifier and documentation gate |
| Replay shows the same rows as the reports | `live-benchmark-console/replay/replay_pack.json` | `build_replay_pack.py --check` plus console tests |
| Withheld cells still count in the aggregates | Four `outputs/public_redaction.json` records and response hashes | Full-text and results-integrity verifiers |

## Run the gate

```bash
cd Agents/Model-And-Router-Benchmark
python -m pip install -r requirements.txt
python scripts/validate_repo.py
```

The gate is fail-closed. A published credential, a customer identifier, a stale
generated report, an altered measurement, a broken local link or a failing test
exits non-zero. It makes no Azure or model call.
