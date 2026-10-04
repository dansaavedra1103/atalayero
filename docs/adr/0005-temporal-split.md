# ADR-0005: Temporal split for model and agent evaluation

- Status: accepted
- Date: 2026-10-04

## Context

- CLAUDE.md rule 3: splits are temporal, never random, and features use only data before each
  transaction.
- The simulation effectively ends on 10 Sep 2022. From 11 to 18 Sep there are only 1,108
  transactions, 59% of them laundering: the tails of attempts that started earlier. Evaluating
  on those days would inflate every metric, and the date alone would predict the label.
- Days 1–10 run Thursday to Saturday. Volume follows a weekly cycle (about 482k transactions on
  weekdays, 207k on weekends) after a burst on 1 and 2 Sep (1.1M and 754k). The laundering rate
  grows over time, from 0.078% in the first six days to 0.11% in the last four.
- Documented attempts last about 3 days (up to 8), so about 100 attempts cross any boundary
  between days.
- The R02/R04 thresholds (ADR-0004) were calibrated on alert volumes over all of 1–10 Sep.

## Decision

1. **Split by `transacted_at`:**

   | Split | Days (2022) | Transactions | Laundering | Rate | Patterned / untyped |
   | --- | --- | --- | --- | --- | --- |
   | Train | 1–6 Sep (Thu–Tue) | 3,248,921 | 2,530 | 0.078% | 1,312 / 1,218 |
   | Validation | 7–8 Sep (Wed–Thu) | 965,524 | 1,036 | 0.107% | 657 / 379 |
   | Test | 9–10 Sep (Fri–Sat) | 862,792 | 956 | 0.111% | 585 / 371 |
   | Excluded | 11–18 Sep | 1,108 | 655 | 59% | 655 / 0 |

   Boundaries are half-open: train is `< 2022-09-07`, validation `< 2022-09-09`, test
   `< 2022-09-11`. Phase 2 puts these dates in `config/settings.yaml`.
2. **Model selection and thresholds use validation only.** The test split is read once per
   reported result. Final models may be refit on train + validation before the test run.
3. **Features may look back across boundaries.** A test transaction's features can use earlier
   transactions from train and validation (that data exists at time t), never later ones.
   Transactions after 10 Sep are never used, not even as feature history.
4. **The agent's golden set (phase 3) comes from the test split**, so the agent is not measured on
   cases the models were tuned on.
5. **Rules-only baseline:** before comparing models with R02/R04, phase 2 rechecks their
   thresholds on train + validation only.

## Consequences

- Validation and test each hold about 1,000 laundering transactions with similar rates, so
  thresholds tuned on validation transfer to test. Train has a lower rate; class weights or
  calibration must account for it.
- Labels are treated as known as soon as a transaction happens. Real investigations take weeks,
  so results are optimistic for attempts that cross a boundary: their earlier transactions are
  already labeled when later ones are scored. Reports must say so.
- The first hours of 1 Sep have no history for windowed features. Phase 2 decides whether a
  warm-up period is dropped from training rows, depending on its longest window.
- The test split mixes a busy Friday with a quiet Saturday, and drift checks (PSI) must not
  mistake the weekly cycle for drift.
