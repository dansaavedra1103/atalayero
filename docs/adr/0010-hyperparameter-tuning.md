# ADR-0010: Hyperparameter tuning with Optuna

- Status: accepted
- Date: 2026-10-05

## Context

- The defaults of `config/models.yaml` v1.0 (ADR-0009) give these validation PR-AUCs: LightGBM
  0.506, logistic regression 0.091, Isolation Forest 0.004.
- Hyperparameters matter here. In an early probe, keeping only 10% of the clean rows raised
  LightGBM's PR-AUC from 0.27 to 0.49 with everything else equal.
- Validation is two days with 1,036 laundering transactions, so a search can overfit it. Test
  stays reserved for one final run (ADR-0005).
- A fit takes 18–80 s for logistic regression and 25–50 s for LightGBM with early stopping. A
  search therefore has a budget of tens of trials, not thousands.

## Decision

1. **Optuna** becomes a runtime dependency (MIT licence, runs locally). The SQLAlchemy and alembic
   it needs already come with MLflow. scikit-learn's `RandomizedSearchCV` was the alternative,
   but it uses random K-fold by default, which breaks the temporal split, and it cannot prune.
2. **Objective: PR-AUC on validation, family by family.** It is smooth and uses every laundering
   transaction. The winner across families is still chosen by detection without hubs at the
   rules' volume (ADR-0007), in `make train`. Optimising the primary metric directly would be
   noisier: in the probe, PR-AUC moved from 0.27 to 0.49 while detection only moved from 16.6% to
   17.5%.
3. **A seeded TPE study per family.** The first trial is the current configuration, so a search
   never ends below its starting point in PR-AUC; point 8 protects the primary metric. The
   number of trials per family is set in `models.tuning_trials`: 15 for logistic regression, 40
   for LightGBM and 15 for Isolation Forest.
4. **Search spaces** (`models/tune.py`):

   | Family | Parameter | Range |
   | --- | --- | --- |
   | Logistic regression | `C` | 1e-4 to 10, log scale |
   | | `class_weight` | none or balanced |
   | | `negative_rate` | 0.01 to 0.3, log scale |
   | LightGBM | `learning_rate` | 0.01 to 0.3, log scale |
   | | `num_leaves` | 7 to 255, log scale |
   | | `min_child_samples` | 10 to 2,000, log scale |
   | | `subsample` | 0.5 to 1 |
   | | `colsample_bytree` | 0.3 to 1 |
   | | `reg_lambda` | 1e-3 to 30, log scale |
   | | `negative_rate` | 0.01 to 1, log scale |
   | Isolation Forest | `n_estimators` | 50 to 500, log scale |
   | | `max_samples` | 64 to 8,192, log scale |
   | | `max_features` | 0.3 to 1 |

   Logistic regression keeps at most 30% of the clean rows: in the probe, more clean rows made the
   fit four times slower with no gain in PR-AUC.
5. **LightGBM** grows up to 3,000 trees and stops after 100 rounds without a better validation
   PR-AUC. The number of trees it reaches becomes `n_estimators`. A median pruner stops trials that
   fall behind: it starts after 5 trials and from round 100, with a report every 50 rounds.
6. **Isolation Forest is tuned with labels on validation.** That makes its model selection
   semi-supervised, although the fit itself never sees a label.
7. **`make tune` writes a new minor version of `config/models.yaml`** with a `history` entry that
   gives the trials and the outcome for each family. `make train` then fits that version with a
   fixed tree count, and validation plays no part in the fit itself. Each family's study is logged
   to MLflow as a `tune-<family>` run, with the PR-AUC and the detection of every trial.
8. **Veto on the primary metric.** Every trial also measures detection without hubs at the rules'
   volume. The trial with the best PR-AUC replaces the current parameters only if it does not
   detect less than the first trial, which is the current configuration. Otherwise the family
   keeps its parameters and the history says why. `make tune` therefore also needs the rule
   alerts (`make rules`). The veto was added after the first search; see Consequences.
9. **Isolation Forest runs on 4 threads.** With every core, each thread held its own copy of the
   data, and the search peaked at 14.9 GB on a 15 GB machine. Four threads are just as fast.

## Consequences

- **The first search, without the veto,** raised every family's PR-AUC but made LightGBM worse
  where it counts:
  - Its best trial reached a PR-AUC of 0.572, against 0.506 for v1.0. That trial used a learning
    rate of 0.018, 12 leaves, 82% of the clean rows and 1,315 trees.
  - At the rules' volume it detected 17.0% without hubs, against 19.4% for v1.0.
  - At 400 alerts a day it was better: 49.3% detected with 18% false positives, against 47.1%
    with 26%.

  The registry kept the v1.0 model as champion, because it only promotes a better primary metric
  (ADR-0009). A rebuild from scratch, however, would have crowned the tuned model. Hence the veto.
- **The final search, with the veto, gives `config/models.yaml` v1.1.** Validation, at the rules'
  volume of 75.5 alerts a day:

  | Family | PR-AUC v1.0 → v1.1 | Without hubs v1.0 → v1.1 | Outcome |
  | --- | --- | --- | --- |
  | Logistic regression | 0.091 → 0.246 | 5.7% → 7.8% | adopted |
  | LightGBM | 0.506 (kept) | 19.4% (kept) | vetoed |
  | Isolation Forest | 0.004 → 0.0044 | 0.0% → 1.1% | adopted |

  - **Logistic regression** adopts `C` = 6.2, no class weights and 8% of the clean rows. It still
    does not beat the rules (8.3%).
  - **LightGBM's** best trial detected 17.0% inside the study, against 17.8% for the first trial
    (v1.0 with early stopping). The study compares like with like; `make train` fits v1.0 with 500
    trees and gets 19.4%.
  - **The champion does not change:** LightGBM v1.0 at 19.4% against the rules' 8.3%.
- **Cost:**
  - `make tune`: 42 min with a peak of 7.0 GB. The first search peaked at 14.9 GB because of
    Isolation Forest's threads.
  - 26 of LightGBM's 40 trials were pruned.
  - `make train` on v1.1: 73 s with a peak of 5.9 GB.
- **Validation is now used for early stopping, the search, the veto and the choice of family.**
  Its figures are optimistic. The honest number is the single test run at the end of phase 2.
- **PR-AUC and detection at a small budget disagree for LightGBM.** A smoother version of the
  primary metric, such as detection averaged over several budgets, could replace PR-AUC as the
  objective later. That would mean a new ADR.
- Registered models are cloudpickled, so they refer to the functions in their pipelines by name
  (ADR-0009). Renaming one, such as the LightGBM category helper, stops those models from
  loading. Such a rename needs a retrain and a new champion.
