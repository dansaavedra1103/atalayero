# ADR-0012: Drift monitoring with PSI

- Status: accepted
- Date: 2026-10-05

## Context

- The design asks for the PSI of the features between windows, plus the alert volume, as the
  signal that would trigger a retrain.
- Volume follows a weekly cycle: about 482k transactions on weekdays and 207k on weekends
  (ADR-0005). Comparing a Saturday with a Wednesday would report the calendar as drift.
- Some features depend on how much history exists at each time, by design (ADR-0008):
  - earlier transactions on the same pair and minutes since the previous transaction look back
    to 1 Sep with no bound;
  - the early graph snapshots hold the 1 Sep burst.
- Retraining on a schedule belongs to phase 4 (Airflow).

## Decision

1. **PSI per feature and per day**, against train, with no labels. Numeric features use the
   reference's quantile bins (10 by default), closed on the right, so that a point mass such as
   the zeros of a count keeps a bin of its own. Missing values have their own bin. Categories and
   flags are compared by share, and empty bins are floored at a share of 1e-4.
2. **Day kinds:** weekdays are compared with the train weekdays (2, 5 and 6 Sep) and weekends
   with the train weekend (3 and 4 Sep). If train has no day of a kind, every train day is used.
3. **Rule-alert volume:** the account-days the rules alert each day, against the train mean for
   that kind of day.
4. **Thresholds** live in `settings.yaml` under `drift`: a PSI of 0.1 is moderate and 0.25 is
   significant. The alert volume must stay within 0.5–2 times the reference.
5. **A day drifts** when a feature reaches the significant PSI or the alert volume leaves its
   band. `make drift` writes `data/reports/drift_<split>.json` with `drift_detected` and the
   reasons, on validation by default; test only when asked (ADR-0005). Nothing retrains
   automatically yet.

## Consequences

- **Both validation days drift**, each on 4 features with a significant PSI:

  | Feature | 7 Sep | 8 Sep |
  | --- | --- | --- |
  | `pair_count_before` | 2.23 | 3.01 |
  | `sender_graph_pagerank` | 2.85 | — |
  | Community sizes | 1.4–1.5 | 1.4–1.5 |

  11 and 12 features reach the moderate level. The rule alerts stay within their band (71 and 80
  account-days, against 93).
- **The drift comes from how these features are built**, not from a change in behaviour:
  - Pair counts grow with the history available.
  - The train snapshots of 2–4 Sep hold the 1 Sep burst and are larger than later ones, which
    shifts community sizes and the scaled PageRank.
- **The risk is real.** `pair_count_before` is the champion's second feature, with 21.6% of the
  gain. The graph sizes add about 2%. A version of these features with a bounded window would
  not drift by construction.
- Weekdays are compared with weekdays, so the weekly cycle is not reported as drift.
