# ADR-0020: The daily batch and the serving database

- Status: accepted
- Date: 2026-10-08

## Context

- **Phase 4 schedules the system** (design.md §6): a daily batch from ingestion to alerts and
  KPIs, weekly retraining and drift monitoring, served by an API and a dashboard.
- **The dataset does not grow.** The simulation covers 1–10 Sep 2022 (ADR-0005). A daily batch
  can only replay it, one simulated day per run.
- **Phases 1–3 compute everything in one pass** over the whole period: `make features`,
  `make rules`, the scores of a split.
- **A day's features look back at most 96 hours** (ADR-0013), and its graph snapshot covers the
  three days before it (ADR-0008). Three things depend on more than that:
  - **relay depths** build on the depths of earlier relays (ADR-0011), chain after chain;
  - **cooldowns**: whether a rule fires depends on its earlier alerts (ADR-0006);
  - **ties between chains.** When several shortest chains close a cycle, the search returns
    one of them depending on the order its state was built in. That order goes back further
    than any window. It decides `cycle_hours` and the evidence of a short-cycle alert (R03),
    not whether a cycle exists or its length.

  Rebuilding a day from a window of history gave a different `cycle_hours` for 3% of the
  transactions in a test fixture. Breaking ties another way would change the features the
  champion was trained on, and the exact reproduction `make pipeline` promises (ADR-0014).
- **DuckDB allows one writer or many readers of a file, not both at once.** The batch and dbt
  write the warehouse; an API and a dashboard reading it would block them, or be blocked.

## Decision

1. **A day at a time.** `atalayero.batch` runs one day of the simulation and writes it to its
   own directory, `data/batch/days/<day>/`:
   - `features.parquet`: the 44 features of the day's transactions, with no labels;
   - `rule_alerts.parquet`: the rules' alerts triggered that day;
   - `scores.parquet`: the champion's score of each transaction;
   - `alerts.parquet`: the day's alert queue;
   - `drift.json`: the day against train;
   - `state/`: the motif pass and the rules as the day left them;
   - `manifest.json`: the day's phase, counts, rule versions and champion version.
2. **Each day carries on from the state the day before left.**
   - The tabular and graph features read four days of history from the warehouse.
   - The motif pass and the rule engine load the previous day's `state/`, run the day's
     transactions, and save their own. The state is pickled and gzipped, as the batch wrote it.
   - The first day of the simulation starts empty.
   - A day runs only once the day before it is complete. A day whose predecessor ran with other
     rule versions refuses to run: the days must be replayed from the first one.
3. **A day is complete when its manifest exists.** A run deletes the manifest first, replaces
   every file atomically (written beside it, then moved into place), and writes the manifest
   last. Running a day again replaces it.
4. **What runs depends on the day's phase:**

   | Phase | Days | Features, rules | Scores, queue | Drift |
   | --- | --- | --- | --- | --- |
   | warm-up | 1 Sep | yes | no | no |
   | train | 2–6 Sep | yes | yes | no: it is the reference |
   | validation | 7–8 Sep | yes | yes | yes |
   | test | 9–10 Sep | yes | yes | yes |

   - **Scores come from the registered champion**, whatever day it is, and the manifest records
     its version. Train days are scored too, but the model has seen them: their numbers are
     in-sample, and every table carries the phase so that nobody reads them as a result.
   - **The queue is the one the agent's case sets use** (ADR-0015): the account-days the rules
     alerted, plus the model's top `batch.alert_budget` account-days of the day, hubs left out.
     The budget is 100, close to the rules' volume (85.5 a day on test, ADR-0014) and one of
     the evaluation's budgets (ADR-0007).
   - **Drift compares the day with the train days' own files**, by kind of day, as `make drift`
     does (ADR-0012).
5. **The serving database.** After each run, the batch builds `data/serving.duckdb` from the
   complete days and moves it into place in one step. It holds the days, the alert queue, the
   rule alerts, every transaction of each alerted account-day with its score, and drift.
   - The API and the dashboard only ever open it, read-only. A reader holding the old file does
     not block the next publication, and never sees half of one.
   - **It holds no labels**, only what the monitoring system knows at alert time.
6. **Commands:** `make daily DAY=<day>` runs one day, `make replay` every day in order; both
   publish. Airflow will call the same functions (ADR-0021).

## Consequences

- **A day at a time gives what a single pass gives.**
  - A test replays six days of a dense fixture and compares them with `make features` and
    `make rules` over the same period: every feature is bit-identical, and every alert too.
  - The queue of a validation day matches the queue that `SplitEvaluator` builds from the same
    scores, and its drift matches `make drift`.
  - On the full data, `make replay` runs the ten days in about 15 minutes. Against `make rules`,
    all 959 alerts are identical. Against `make features`, every feature is bit-identical except
    nine sums of amounts (over 24 h, or in a graph snapshot), whose last bits differ: at most
    2 × 10⁻¹⁵ relative, because the sums add up in another order. The champion's 3,962,316
    scores are identical, so the queues are too.
- **The state takes room.** At the end of a day, the motif pass holds about 1.6 million legs
  (220 MB pickled) and the rules about 66 MB; gzipped, a day's state takes about 110 MB. The ten
  days take 1.3 GB under `data/batch/`, which stays disposable.
- **A day depends on all the days before it**, through their state. Re-running a day in the
  middle means re-running the days after it, and a rule change means replaying from the first
  day. With ten days that takes about 15 minutes.
- **The pickled state is trusted because the batch wrote it.** Anyone who can write `data/` can
  already change the warehouse and the models.
- **A champion promoted mid-replay scores the days after it.** Each manifest says which version
  scored the day; nothing rescores the days before.
- **The serving database is rebuilt in full** at every publication. It is small: the queue and
  its transactions, not every score.
