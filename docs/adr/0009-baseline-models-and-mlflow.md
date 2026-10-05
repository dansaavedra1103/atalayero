# ADR-0009: Baseline models, hyperparameters as config, and MLflow

- Status: accepted
- Date: 2026-10-05

## Context

- The design compares four detectors:
  - the rules alone, as the benchmark;
  - logistic regression, as an interpretable GLM;
  - LightGBM, on tabular and graph features;
  - Isolation Forest, as an unsupervised reference.

  MLflow logs every run and the champion.
- The protocol is set (ADR-0007). The primary metric is detection without hubs, on validation,
  at the rules' volume. The rules-only baseline detects 8.3% at 75.5 alerts a day.
- The features are 29 tabular and 14 graph ones (ADR-0008). Train runs 2–6 Sep after the warm-up:
  2,134,000 transactions, 2,208 of them laundering (0.10%). Validation holds 965,524 transactions
  and 1,036 laundering ones.
- LightGBM's wheels need the system OpenMP runtime (`libgomp.so.1`). Ubuntu 26.04 on WSL does not
  ship it; scikit-learn's wheels bundle their own copy.

## Decision

1. **Three families** (`models/families.py`), each a scikit-learn `Pipeline` that outputs a score
   per transaction (higher means more suspicious):
   - **Logistic regression.** Numeric features get median imputation with indicators of what
     was missing, then `log1p` and standardisation. Hour is standardised, flags pass through and
     categories are one-hot encoded.
   - **LightGBM**, with native categories, deterministic and row-wise.
   - **Isolation Forest**, on the numeric features (imputed and `log1p`) and the flags. It is fit
     without labels; categories have no distance to isolate on.
2. **Negative sampling** for the supervised families: every laundering row is kept, plus a share
   `negative_rate` of the clean rows, drawn with a fixed seed. With 0.1% positives, most clean
   rows add training time, not information.
3. **Hyperparameters live in `config/models.yaml`**, versioned like a rule: a `version` and a
   `history` entry for each change. Each family must list exactly its own parameters. Version 1.0
   holds hand-picked defaults; tuning comes in the next ADR.
4. **`make train`** does the following:
   - fits each family on train;
   - scores validation and evaluates it with the protocol of ADR-0007: at the rules' volume, at
     the fixed budgets, with curves and PR-AUC;
   - logs one MLflow run per family, plus one for the rules-only baseline;
   - registers the best family, ranked by detection without hubs (ties go to PR-AUC). It becomes
     the `champion` only if it beats the current champion's validation metric, which is kept as
     a tag on each model version;
   - writes `data/reports/models_validation.json`, and logs a warning for every family that does
     not beat the rules.
5. **MLflow runs locally.** Tracking and registry use SQLite in `data/mlflow/mlflow.db`, and the
   artifacts go to `data/mlflow/artifacts/`. Both can be rebuilt like the rest of `data/`.
   `make mlflow-ui` serves them. Models are saved with cloudpickle and their requirements are
   listed explicitly, since inferring them spawns a slow subprocess. Unpickling runs code, which
   is acceptable for artifacts this project produced on the same machine.
6. **New runtime dependencies:** `lightgbm`, `mlflow` and `pandas`. MLflow brings pandas anyway,
   and the model inputs use it directly. **New system prerequisite:** `libgomp1`
   (`sudo apt-get install libgomp1`). GitHub's `ubuntu-latest` runner already has it.

## Consequences

- Validation, 7–8 Sep, with the defaults of `config/models.yaml` v1.0, at the rules' volume
  (75.5 alerts a day):

  | Detector | Without hubs | Detection | False positives | Attempts | PR-AUC |
  | --- | --- | --- | --- | --- | --- |
  | Rules R01–R04 | 8.3% | 21.1% | 60% | 44/157 | — |
  | **LightGBM** | **19.4%** | 19.4% | 0% | 54/157 | 0.506 |
  | Logistic regression | 5.7% | 5.7% | 74% | 32/157 | 0.091 |
  | Isolation Forest | 0.0% | 0.0% | 100% | 0/157 | 0.004 |

  At 400 alerts a day, LightGBM detects 47.1% of the laundering (132 of 157 attempts) with 26%
  false positives. No family spends alerts on hubs.
- LightGBM more than doubles the rules' detection at the same volume. Logistic regression does
  not beat the rules, and Isolation Forest finds nothing at that volume. In this data laundering
  is not anomalous in a generic sense, so an unsupervised detector does not help. The report
  states all of this as it is (CLAUDE.md rule 7).
- A false-positive share of 0% means the simulator's patterns are learnable. It says nothing
  about performance on real data.
- These are validation numbers for a model chosen on validation, so they are optimistic. Test
  stays untouched until the single final run.
- `make train` takes about 55 s, with a peak of about 5.2 GB.
