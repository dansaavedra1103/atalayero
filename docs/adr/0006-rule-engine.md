# ADR-0006: Rule engine — four rules, batch evaluation and calibration

- Status: accepted
- Date: 2026-10-05

## Context

- Phase 1 evaluated R02 and R04 online with a minimal schema (ADR-0004). Phase 2 needs the four
  rules of the design, evaluated over the stored data for the rules-only baseline, with thresholds
  set on train + validation only (ADR-0005 §5).
- The design's R01 was structuring, which is not one of the source's eight typologies. On 1–10 Sep
  there is no clustering of amounts below 10,000 USD: in 500 USD bands from 7,000 to 12,500 USD,
  each band holds 52–76 laundering transactions, with no jump under 10,000.
- Documented attempts are slow. The median span is 85 h for fan-in and 72 h for cycles (at most
  96 h). Cycles have 2–12 transactions (median 4); 52 of the 54 return to the account that started
  them, and consecutive legs follow each other in time.
- Hubs send a lot but receive little: on 1–10 Sep the 15 hub accounts send 449,859 transactions and
  receive 2,892.

## Decision

1. **Rule YAML schema, extended.** The R02 and R04 files stay valid unchanged.
   - `direction`: `out` (default) or `in`, only for `distinct_counterparties`. With `in`, the
     window is kept per receiver, its counterparties are the senders and the amount that counts is
     `amount_received_usd`.
   - Metric `short_cycle`, which takes `threshold.max_hops` (at least 2). Count metrics take
     `threshold.min_count`. Mixing them is rejected.
2. **Cycle semantics.** On each transaction C→A, the rule looks for an earlier chain A→…→C of at
   most `max_hops - 1` transactions inside `[t - window, t]`, each leg at or after the previous one
   (the same minute is allowed). Every transaction in the cycle needs `min_amount_usd` or more, and
   self-transfers never count. The money has come back to A, so A gets the alert. `value` is the
   length of the shortest cycle in transactions, and the evidence is that cycle, most recent
   first. The cooldown applies per A, and no search runs while A cools down.
3. **Cycle search.** It runs from both ends (earliest arrival forward from A, latest departure
   backward from C) and always expands the cheaper side, so hubs only cost much when a chain really
   goes through them. A unit test checks it against a brute-force search on 30 random graphs.
4. **R01 is `rapid_concentration` (fan-in) instead of structuring.** It mirrors R02 (fan-out). This
   departs from the design.
5. **Batch evaluation (`make rules`).** It uses the same `OnlineEvaluator` and the same Parquet
   alert format as the replay. It reads only transactions before `splits.test_end` and writes to
   `data/rules/alerts/`. The split boundaries of ADR-0005 now live in `config/settings.yaml`.
6. **Calibration on 1–8 Sep (train + validation) only.** The criteria are alerts per day, whether
   the alerts land on hubs, and the share of alerts whose evidence holds at least one laundering
   transaction. Labels from train and validation may be used; test is never read. Candidate
   thresholds, on 1–8 Sep:

   R01 candidates (direction `in`; "attempts" counts the 31 fan-in attempts active by 8 Sep):

   | Window | Senders | Min amount | Alerts/day | With laundering | Fan-in attempts |
   | --- | --- | --- | --- | --- | --- |
   | 24 h | ≥ 5 | 1,000 USD | 436.8 | 5% | 12 |
   | 24 h | ≥ 5 | 5,000 USD | 51.0 | 11% | 8 |
   | 24 h | ≥ 10 | 5,000 USD | 20.3 | 7% | 0 |
   | 48 h | ≥ 8 | 5,000 USD | 29.6 | 11% | 5 |
   | 96 h | ≥ 8 | 5,000 USD | 34.4 | 21% | 9 |
   | 96 h | ≥ 10 | 2,500 USD | 130.5 | 7% | 9 |
   | **96 h** | **≥ 10** | **5,000 USD** | **23.9** | **19%** | **7** |
   | 96 h | ≥ 12 | 5,000 USD | 15.6 | 13% | 4 |

   R03 candidates (96 h window; "attempts" counts the 45 cycle attempts active by 8 Sep):

   | Max transactions | Min amount | Alerts/day | With laundering | Cycle attempts |
   | --- | --- | --- | --- | --- |
   | 4 | 1,000 USD | 178.8 | 9% | 16 |
   | 6 | 2,500 USD | 90.6 | 13% | 18 |
   | 4 | 5,000 USD | 8.8 | 83% | 14 |
   | 6 | 5,000 USD | 9.1 | 84% | 17 |
   | 8 | 5,000 USD | 9.3 | 84% | 18 |
   | **12** | **5,000 USD** | **9.6** | **84%** | **21** |

   R03 takes 12 transactions, the longest documented cycle: on this data it costs the same time as
   4 and finds more cycles.
7. **R02 and R04 are rechecked, not retuned.** On 1–8 Sep they give 16.0 and 45.3 alerts/day,
   against 15.8 and 44.7 on 1–10 Sep (ADR-0004). Thresholds and versions stay as they are: the
   recheck guards against test data leaking into the baseline. Making the rules better is not the
   goal of this phase; beating them is the models' job.

## Consequences

- The four rules on 1–8 Sep:

  | Rule | Alerts/day | Accounts | On hubs | With laundering |
  | --- | --- | --- | --- | --- |
  | R01 rapid_concentration | 23.9 | 176 | 0% | 19% |
  | R02 rapid_dispersion | 16.0 | 23 | 94% | 12% |
  | R03 short_cycle | 9.6 | 41 | 0% | 84% |
  | R04 high_velocity | 45.3 | 242 | 33% | 4% |

  Train and validation are stable: R01 gives 24.0 and 23.5 alerts/day, R03 8.5 and 13.0. "With
  laundering" helps calibrate but it is not the evaluation metric: on hubs it says little
  (ADR-0004), and the alert-budget protocol comes in the next ADR.
- R01 and R03 never fire on hubs. R03 is the most precise rule, but it touches only 21 of 45
  cycle attempts. R01 also fires on gather-scatter and scatter-gather attempts, which contain
  fan-in stages.
- `make rules` evaluates the 5,077,237 transactions of 1–10 Sep in about 2 min, with a peak of
  about 2.2 GB. It writes 959 alerts: R01 252, R02 158, R03 102, R04 447. The R02 and R04 alerts are
  identical, one by one, to the 605 of the phase 1 replay.
- `make stream` now evaluates all four rules. The cycle rule keeps 96 h of legs of 5,000 USD or
  more in memory.
- The design's file tree lists `R01_structuring.yaml`; it now shows `R01_rapid_concentration.yaml`.
