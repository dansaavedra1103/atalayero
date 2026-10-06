# ADR-0014: The single test run

- Status: accepted
- Date: 2026-10-06

## Context

- Every choice in phase 2 was made on validation (7–8 Sep):
  - the rule thresholds, calibrated on train + validation (ADR-0006);
  - the features (ADR-0008, ADR-0011, ADR-0013);
  - the hyperparameters (`config/models.yaml` v1.3, ADR-0010);
  - the family: LightGBM, chosen by detection without hubs at the rules' volume (ADR-0009).
- ADR-0005 keeps test (9–10 Sep) for a single read per reported result, and allows the final
  models to be refit on train + validation before it.
- Validation adds 1,036 laundering transactions to the 2,208 of train (2–6 Sep), and they are the
  most recent: the laundering rate grows over time (ADR-0005).

## Decision

1. **Refit on train + validation.** Each family of `config/models.yaml` is refit on
   2–8 Sep: `models.train_start` up to `splits.validation_end`. The hyperparameters stay
   frozen at v1.3, `n_estimators` and `negative_rate` included, as does the seed. The rules
   need no refit: their thresholds already come from train + validation.
2. **The protocol of ADR-0007, on test:**
   - hubs are still the top senders of train;
   - models get, day by day, as many alerts as the rules raised on test;
   - everything else ADR-0007 reports is reported too: the fixed budgets, the curves, the
     typologies, the untyped laundering and the PR-AUC.
3. **Nothing is chosen on test.** The headline is the family chosen on validation, LightGBM,
   and every family is reported next to the rules. The refit models are logged in MLflow
   (metrics `test/…`, parameter `fit_end`) and never registered: they have no validation metric
   to compete on, and the `champion` alias stays with the validation line.
4. **`make holdout`** runs, in order:
   - the rules-only baseline on test, rule by rule (`rules_only_test.json`);
   - the refit families against the rules (`holdout_test.json`), with the test score of every
     transaction (`holdout_scores.parquet`);
   - drift on test (`drift_test.json`).

   The error analysis (`notebooks/03_error_analysis.ipynb`) and the golden set of phase 3 read
   those scores, so nothing scores test again.
5. **`make pipeline`** rebuilds everything from the download to `make holdout`, in a single
   command. Every step is deterministic, so running it again reproduces the same numbers. That
   is not a second read of test, as long as nothing is changed after seeing them. Changing a
   rule, a feature or a hyperparameter now would need a new holdout period, which this dataset
   does not have (the simulation ends on 10 Sep, ADR-0002).

## Consequences

- **Test, 9–10 Sep** (956 laundering transactions, 174 documented attempts), at the rules'
  volume of 85.5 alerts a day, with validation alongside (ADR-0013, models fit on train only):

  | Detector | Without hubs | False positives | Attempts | Untyped | PR-AUC | Validation: without hubs, PR-AUC |
  | --- | --- | --- | --- | --- | --- | --- |
  | Rules R01–R04 | 7.8% | 70% | 45/174 | 112/371 | — | 8.3%, — |
  | **LightGBM** | **21.1%** | 1% | 72/174 | 0/371 | 0.475 | 19.0%, 0.553 |
  | Logistic regression | 12.7% | 31% | 58/174 | 3/371 | 0.236 | 12.0%, 0.286 |
  | Isolation Forest | 0.7% | 95% | 6/174 | 50/371 | 0.003 | 7.7%, 0.013 |

  - **LightGBM** detects 2.7 times what the rules detect without hubs, at the same volume.
  - **Logistic regression** also beats the rules on test.
  - **Isolation Forest loses to them.** Its 7.7% on validation does not hold: on test, 95% of its
    alerts are false positives. The untyped laundering it catches comes through hubs (it
    detects 6.0% with hubs and 0.7% without).
- **LightGBM's detection without hubs, by alerts a day:**

  | Alerts a day | 25 | 50 | 75 | 100 | 150 | 200 | 400 | 1,000 |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | Validation (ADR-0013) | 8.3% | 13.4% | 18.7% | 23.0% | 30.1% | 36.2% | 49.0% | 60.5% |
  | Test | 7.2% | 12.6% | 18.4% | 24.6% | 33.7% | 37.3% | 47.5% | 56.5% |

  Detection holds from validation to test across the budgets, even though PR-AUC falls from
  0.553 to 0.475. The ranking of account-days transfers better than the ranking of single
  transactions.
- **Untyped laundering stays undetected** by every supervised family: 0 of 371 for LightGBM at
  the rules' volume. All the untyped laundering the rules catch (112) comes through hubs. This is
  the hardest group for the phase 3 agent.
- **Typologies at the rules' volume, LightGBM on test:**
  - the strongest are gather-scatter (57/127), scatter-gather (56/110) and random (23/43);
  - cycles reach 21/55;
  - the weakest are stack (5/84) and bipartite (4/44).
- **Drift on test** (`drift_test.json`):
  - **9 Sep (Friday):** no significant feature; the highest PSI is 0.247
    (`sender_graph_in_degree`).
  - **10 Sep (Saturday):** drift on `receiver_graph_in_degree` (0.39) and
    `sender_graph_in_amount` (0.28). The cause is the reference, not the day. The train weekend
    (3–4 Sep) is the only weekend reference, and its three-day snapshots still hold the 1–2 Sep
    burst. For example, the median `sender_graph_in_amount` is 24–25k USD on 3–4 Sep and 5.5k
    on 10 Sep. A longer dataset would give a weekend reference outside the burst.
  - The rule alerts stay within their band on both days.
- **Reproducibility.** `make pipeline` from an empty `data/` (keeping only the two downloaded
  files) takes 16 min 51 s. `make holdout` is about 4 minutes of that, and it sets the peak:
  9.7 GB, with train + validation, test and LightGBM in memory at once.
  - It rebuilds `rules_only_validation.json` and `drift_validation.json` exactly, and every
    alert metric of `models_validation.json`. Logistic regression's PR-AUC alone moves in the
    sixth decimal (0.286329 against 0.286328).
  - Running `models holdout` a second time gives the same test report and the same scores,
    bit for bit.
- These are the numbers that phase 2 publishes, in `docs/model_card.md`. The data is synthetic,
  and labels are known as soon as a transaction happens (ADR-0005). They show that the method
  works on this simulator, not how it would perform on real data.
