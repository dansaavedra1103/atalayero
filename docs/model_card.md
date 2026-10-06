# Model card: Atalayero transaction scorer (phase 2)

A LightGBM model that scores bank transactions for money laundering, on top of four versioned
rules. It was built and evaluated on a **synthetic** dataset (IBM AML, HI-Small).

On the held-out test days, it detects **21.1% of the laundering** with the same number of daily
alerts as the rules. The rules detect **7.8%**, both counted without the hub accounts. The model
never detects the laundering that belongs to no documented attempt. None of this measures
performance on real transactions.

- Date: 2026-10-06 · Phase 2 of Atalayero, a public portfolio project · License: MIT
- Decisions behind every number below: [ADRs 0005–0014](adr/)
- Error analysis: [`notebooks/03_error_analysis.ipynb`](../notebooks/03_error_analysis.ipynb)

## Model details

| | |
| --- | --- |
| Family | LightGBM 4.7.0 (gradient-boosted trees, native categories), inside a scikit-learn 1.9.1 pipeline |
| Hyperparameters | `config/models.yaml` v1.3: 2,272 trees, learning rate 0.0127, 9 leaves; tuned with Optuna on validation (ADR-0010) |
| Training rows | Every laundering transaction and 91% of the clean ones (`negative_rate`), drawn with seed 0 |
| Inputs | 44 point-in-time features of one transaction (feature signature `ab0bbda57d09`) |
| Output | A score per transaction, higher means more suspicious; an account-day takes the highest score among its transactions |
| Selection | Chosen among three families on validation, by detection without hubs at the rules' volume (ADR-0009) |
| Final fit | Refit on 2–8 Sep 2022 (train + validation) with frozen hyperparameters, then scored once on 9–10 Sep (ADR-0014) |
| Tracking | MLflow, local (`data/mlflow/`): run `lightgbm on test`; the validation line is the registered `champion` |

The other two families are reported as references: logistic regression, an interpretable GLM, and
Isolation Forest, an unsupervised detector.

## Intended use

- **Intended:** to show, in a portfolio project, how to build and evaluate a transaction monitor
  with no temporal leakage, with versioned rules as the baseline and a fixed daily alert budget.
- **Phase 3:** the scores feed the alerts that an LLM investigator agent receives. The agent's
  golden set comes from the test split.
- **Out of scope:** any decision about real people, accounts or transactions. The model has never
  seen real data, and its numbers do not carry over to a real bank.

## Data

*IBM Transactions for Anti Money Laundering*, HI-Small variant (Kaggle, CDLA-Sharing-1.0). It
comes from a multi-agent simulation, with no real people or institutions. Accounts are identified
by bank and account. Labels are per transaction. A patterns file documents 370 laundering attempts
in 8 typologies, but only 62% of the laundering belongs to one of them; the rest is called
*untyped* here (ADR-0002).

Temporal split (ADR-0005), with half-open day boundaries:

| Split | Days (2022) | Transactions | Laundering | Patterned / untyped | Attempts |
| --- | --- | --- | --- | --- | --- |
| Train | 1–6 Sep, rows from 2 Sep | 2,134,000 rows | 2,208 | — | — |
| Validation | 7–8 Sep | 965,524 | 1,036 | 657 / 379 | 157 |
| Test | 9–10 Sep | 862,792 | 956 | 585 / 371 | 174 |

- 1 Sep is warm-up only: it serves as history for the features, and its rows are not trained on
  (ADR-0008).
- 11–18 Sep is never used. It holds only the tails of attempts, 59% of them laundering.

## Features

There are 44 features, none of them a label. Each one uses only transactions **strictly before**
the scored one, and looks back at most 96 h (ADR-0008, ADR-0013). Each family of features has a
test that removes every transaction from the scored one's minute onwards (from its day, for the
graph) and checks that the features do not change.

- **Tabular (29):**
  - the transaction's own attributes: amount, currencies, payment format, hour, same bank;
  - the sender's and receiver's activity over 1 h and 24 h: counts, amounts, counterparties;
  - minutes since each account's previous transaction;
  - pair counts over 24 h;
  - the amount against the sender's mean over 24 h.
- **Graph (10):** for each account, in a daily snapshot of the three previous days: in/out degree,
  in/out amounts, and whether it sits on a short cycle.
- **Motifs (5):**
  - the shortest cycle the transaction closes with amounts close to its own, and how fast it
    closes;
  - relaying of recent incoming funds;
  - fan paths.

## Evaluation protocol

The protocol was fixed before any model was trained (ADR-0007):

- **The alert is an account-day:** everything one account sent and received on one day.
- **Models fill a daily budget:** the top N account-days by score.
- **A laundering transaction is detected** when the account-day of its sender or of its receiver
  is alerted that day.
- **The primary metric is detection without hubs, at the rules' volume.** Each day, models get as
  many alerts as the rules raised that day.
  - Hubs are the 15 accounts that sent the most transactions in train. Alerting them every day
    would "detect" about 13% of the laundering, while handing the analyst thousands of
    transactions per alert. Laundering detected only through a hub therefore does not count.
- **Also reported:** detection with hubs, false-positive share, documented attempts, untyped
  laundering, typologies, fixed budgets, curves and PR-AUC.
- **Test was read once**, after every choice had been made on validation (ADR-0014).

## Results

At the rules' volume. On test that is 85.5 alerts a day; on validation, 75.5.

| Detector | Test: without hubs | False positives | Attempts | Untyped | PR-AUC | Validation: without hubs | PR-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Rules R01–R04 | 7.8% | 70% | 45/174 | 112/371 | — | 8.3% | — |
| **LightGBM** | **21.1%** | 1% | 72/174 | 0/371 | 0.475 | 19.0% | 0.553 |
| Logistic regression | 12.7% | 31% | 58/174 | 3/371 | 0.236 | 12.0% | 0.286 |
| Isolation Forest | 0.7% | 95% | 6/174 | 50/371 | 0.003 | 7.7% | 0.013 |

**Against the baseline (CLAUDE.md rule 7):**
- LightGBM and logistic regression beat the rules on both splits.
- **Isolation Forest loses to the rules on test.** On validation it came close (7.7% against
  8.3%), but on test 95% of its alerts are false positives, and what little it detects comes
  through hubs. Laundering in this simulator is not anomalous in a generic sense.

LightGBM's detection without hubs by daily budget (the rules raise 75.5 alerts a day on
validation and 85.5 on test):

| Alerts a day | 25 | 50 | 75 | 100 | 150 | 200 | 400 | 1,000 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Validation | 8.3% | 13.4% | 18.7% | 23.0% | 30.1% | 36.2% | 49.0% | 60.5% |
| Test | 7.2% | 12.6% | 18.4% | 24.6% | 33.7% | 37.3% | 47.5% | 56.5% |

At 400 alerts a day on test, LightGBM detects 47.5% of the laundering and 140 of the 174 attempts,
with 33% false positives.

**By typology**, LightGBM at the rules' volume on test (from the error analysis):

| Typology | Transactions | Rules without hubs | LightGBM | LightGBM at 400/day |
| --- | --- | --- | --- | --- |
| random | 43 | 14.0% | 53.5% | 83.7% |
| scatter_gather | 110 | 22.7% | 50.9% | 89.1% |
| gather_scatter | 127 | 14.2% | 44.9% | 81.1% |
| cycle | 55 | 18.2% | 38.2% | 85.5% |
| fan_in | 57 | 5.3% | 29.8% | 84.2% |
| fan_out | 65 | 6.2% | 29.2% | 87.7% |
| bipartite | 44 | 9.1% | 9.1% | 34.1% |
| stack | 84 | 3.6% | 6.0% | 58.3% |
| untyped | 371 | 0.5% | 0.0% | 0.3% |

**What the error analysis adds** (details in the notebook):
- **Untyped laundering is out of reach at any practical budget.**
  - It scores above clean transactions but far below patterned laundering.
  - Reaching 43% of it would take 5,000 alerts a day, with 90% false positives.
  - Its payment mix differs from the documented attempts: 67% ACH, against 99.8% for patterned
    laundering.
- **Rules and model mostly catch different laundering.** What only the rules catch is almost
  all untyped laundering, caught through hubs.
- **False positives differ in kind.** The rules' false positives are busy account-days (median
  18 transactions); the model's are small ones (median 4).

**Drift on test** (ADR-0012, ADR-0014):
- **9 Sep (Friday):** no feature drifts.
- **10 Sep (Saturday):** two graph features drift. The only weekend reference, 3–4 Sep, still
  holds the 1–2 Sep burst in its three-day snapshots.
- **No visible loss:** at a fixed 400 alerts a day, the model detects more on 10 Sep than on 9 Sep
  (52.9% against 42.8%).

## Limitations and caveats

- **Synthetic data.** The simulator's patterns are learnable, and a 1% false-positive share at
  the rules' volume reflects that. These results show that the method works. They are not a
  performance estimate for real data.
- **Labels are known instantly.** A transaction's label is treated as known as soon as it
  happens (ADR-0005), whereas real investigations take weeks. Attempts that cross a split
  boundary are easier than they would be in practice.
- **Short history.** The dataset effectively covers 10 days, so validation and test hold only
  two days each, and there is a single weekend to compare weekends with. Figures move by a
  point or two between splits; read differences of that size as noise.
- **The account-day maximum favours busy accounts.** An account-day takes its highest
  transaction score, so accounts with many transactions climb the ranking. The model learned
  to keep hubs out of the budget, but the metric does not correct for this.
- **Detection counts transactions.** An attempt with many transactions weighs more than a short
  one, which is why attempts are reported too.
- **PR-AUC is reported per transaction; the budgeted metrics, per account-day.** On test, PR-AUC
  drops more than detection does, so the two can move apart.
- **No comparison with published results.** Papers on this dataset use other splits and other
  metrics (usually per-transaction F1), so the numbers are not comparable.
- **Fairness cannot be assessed.** The simulated accounts have no demographic attributes. On real
  data, a monitor like this one would need a fairness review before any use.

## How to reproduce

```bash
make setup      # once; LightGBM needs: sudo apt-get install libgomp1
make pipeline   # about 17 min from an empty data/: download → dbt → rules → features → train → holdout
make notebook   # re-executes the error analysis on the holdout scores
```

Every step is deterministic. `make pipeline` from scratch rebuilds the validation reports
exactly, and running the holdout twice gives bit-identical test scores (ADR-0014).
`config/models.yaml` and `config/rules/*.yaml` are the source of truth. `make tune` would write
a new version of the model config, which would then need a new holdout period that this dataset
does not have.
