# ADR-0007: Alert-budget evaluation protocol

- Status: accepted
- Date: 2026-10-05

## Context

- The design's central metric is the alert budget: with N alerts a day that can be reviewed, what
  share of the laundering is detected, and how many alerts are false positives. PR-AUC is reported
  too, because the classes are so imbalanced.
- Labels are per transaction. The rules alert accounts, at most once a day per rule (ADR-0004,
  ADR-0006). In phase 3, the agent investigates one alert at a time.
- Validation (7–8 Sep) holds 450,244 account-days (accounts with transactions on a day, sent or
  received). Of these, 1,629 contain laundering, and the split has 1,036 laundering transactions
  from 157 documented attempts.
- The 15 hub accounts (ADR-0004) make up 30 of those account-days. 27 of the 30 contain laundering
  and together they touch 133 laundering transactions (12.8%). A detector that alerts the 15 hubs
  every day would therefore "detect" 12.8% of the laundering with 10% false positives, while
  handing the analyst up to 18,661 transactions per alert.
- The protocol has to be set before any model is trained, so it cannot be tuned in a model's favour.

## Decision

1. **The alert is an account-day:** everything one account sent and received on one calendar day,
   reviewed once the day is over.
2. **Scored detectors.** A model scores transactions. An account-day takes the highest score among
   its transactions, and the top N account-days of each day become alerts. Ties are broken by
   account key.
3. **Rules.** Each rule alert becomes the account-day of its account, on the day of the
   transaction that triggered it. The rules together give the union of those account-days: one
   operating point, at their own volume. The alerts must come from the rule versions in
   `config/rules`, or the evaluation fails.
4. **Detection and false positives.** A laundering transaction is detected when the account-day of
   its sender or of its receiver is alerted on that day. A false positive is an alerted account-day
   with no laundering transaction of that account on that day.
5. **Hubs are reported apart.** Hubs are the 15 top senders of the train split by transaction
   count (`evaluation.hub_accounts`). The report gives the share of alerts that land on hubs, and
   "detection without hubs": laundering detected through an alert on an account that is not a hub.
6. **Primary comparison: detection without hubs at the rules' volume.** Models get, day by day, as
   many alerts as the rules raised that day. The following are always reported alongside it:
   - detection with hubs;
   - false-positive share;
   - patterned and untyped laundering;
   - documented attempts with at least one transaction detected;
   - detection per typology;
   - share of alerts on hubs;
   - fixed budgets of 50, 100, 200 and 400 alerts a day (`evaluation.budgets`) and curves of
     detection against alerts per day;
   - PR-AUC over the transactions, for scored detectors.
7. **Splits (ADR-0005).** Model selection uses validation only. Test is read once per reported
   result, and the CLI warns when it is.
8. **Runtime dependency `scikit-learn`.** It provides average precision now, and the models in the
   next PRs.
9. **`make evaluate`** evaluates the rules-only baseline on validation: all rules together, then
   each rule alone. It writes `data/reports/rules_only_validation.json` and logs a table.

## Consequences

- Rules-only baseline on validation (7–8 Sep, 1,036 laundering transactions, 157 attempts):

  | Detector | Alerts/day | False positives | Detection | Without hubs | Attempts | On hubs |
  | --- | --- | --- | --- | --- | --- | --- |
  | R01–R04 | 75.5 | 60% | 21.1% | 8.3% | 44 | 20% |
  | R01 rapid_concentration | 23.5 | 77% | 3.5% | 3.5% | 10 | 0% |
  | R02 rapid_dispersion | 15.0 | 10% | 12.8% | 0.0% | 0 | 100% |
  | R03 short_cycle | 13.0 | 12% | 4.8% | 4.8% | 34 | 0% |
  | R04 high_velocity | 39.0 | 65% | 12.8% | 0.0% | 0 | 38% |

  This is what the models have to beat: **8.3% of laundering detected without hubs at 75.5
  alerts a day.** All of R02's and R04's detection comes through hubs. R02's low false-positive
  share is the hub effect from the Context, not precision.
- The max of an account-day's scores rises with its number of transactions, so busy accounts climb
  the ranking even when each of their transactions scores low. Alerts spent on hubs count against
  the primary metric. Models have to learn to keep busy accounts out of the budget, or pay for
  them.
- Detection counts transactions, so an attempt with many transactions weighs more than a short
  one. Attempts are reported on their own for that reason.
- An alert covers its whole day, including transactions after the one that triggered a rule. Rules
  and models are treated the same way.
- Labels are known as soon as a transaction happens (ADR-0005), which is optimistic for attempts
  that cross a split boundary.
- Reports live in `data/reports/` and can be rebuilt. The figures that get published go into the
  model card at the end of phase 2.
