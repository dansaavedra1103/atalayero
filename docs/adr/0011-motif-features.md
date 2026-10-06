# ADR-0011: Motif features

- Status: accepted
- Date: 2026-10-05

## Context

- The LightGBM champion (ADR-0009, ADR-0010) detects 19.4% of the validation laundering without
  hubs at the rules' volume. An oracle that picked the best account-days at the same volume would
  reach 40.2%, so the model captures about half of what the budget allows.
- The model misses whole typologies. At the rules' volume it detects, out of each typology's
  transactions in validation:

  | Typology | Detected |
  | --- | --- |
  | Fan-in | 36/60 |
  | Gather-scatter | 65/96 |
  | Scatter-gather | 62/134 |
  | Cycles | 2/66 |
  | Stack | 7/150 |
  | Random | 3/38 |
  | Bipartite | 5/49 |
  | Untyped (no documented attempt) | 0/379 |

  The R03 rule alone finds 11 of the 66 cycle transactions.
- The graph features of ADR-0008 describe accounts in daily snapshots. They cannot tell whether a
  given transaction closes a cycle or passes money on.
- In the published results on this dataset, adding per-transaction subgraph features (IBM's Graph
  Feature Preprocessor) takes LightGBM from a minority-class F1 of 21% to 63%. Graph neural
  networks land in the same range. The splits differ from ours, so the figures are not comparable
  one to one.
- The documented attempts finished by 6 Sep (train only) show money passing through accounts
  almost intact. In cycles, stack, random and scatter-gather, the amount an account sends over
  what it received has a median of 1.0, with 10th–90th percentiles between 0.85 and 1.2. The
  median wait between receiving and sending is 9–58 hours, and the 90th percentile reaches
  about 70 hours.

## Decision

1. **Five motif features per transaction** (`features/motifs.py`), all within a 96 h window:

   | Feature | Meaning |
   | --- | --- |
   | `cycle_length` | Transactions in the shortest cycle the transaction closes: a chain from the receiver back to the sender, in time order, of at most 6 transactions in all. 0 if none. |
   | `cycle_hours` | Hours from that cycle's first leg to this transaction. NULL if none. |
   | `relay_in_96h` | Transactions the sender received whose amount is close to this one: it sends 0.85 to 1.2 times what it got. |
   | `relay_depth` | How many such relays precede this one in a chain (layering). |
   | `fan_paths` | Other accounts that got money from one of the sender's senders and sent money to the receiver: the parallel paths of a scatter-gather. |

2. **Same contract as ADR-0008, enforced a minute at a time.** One pass in event-time order: every
   transaction of a minute is scored against the state of earlier minutes, then the whole minute
   joins the state. The removal test covers these features too, and it caught a leak injected on
   purpose (scoring each transaction after its own minute's earlier ones).
3. **The cycle search is R03's.** It moves out of the rule into `RecentLegs` in
   `rules/online.py`, shared by the rule and the features, so the brute-force test covers both.
   The features count every amount; R03 only counts transactions of 5,000 USD or more.
4. **Cycles need amounts close to the closing one.** Every leg must lie within 1.5 times the
   closing transaction's amount, either way. Money comes back almost whole in documented cycles:
   in train, the largest amount of an attempt is a median 1.10 times its smallest, and 1.35 times
   at the 90th percentile. The band also cuts the search, which without it took 244 s for the
   first 2M transactions instead of 67 s.
5. `make features` builds them into `data/features/motifs.parquet`, and the models read 48
   features.
6. **A champion is tied to its feature list.** Every registered model version carries a
   signature of the feature names it takes. A model on another feature list cannot score the
   current inputs, so when the list changes, the best new model becomes the champion even if its
   primary metric is lower, and a warning says so. This amends the promotion rule of ADR-0009.

## Consequences

- **Cost.** Motif features take about 4 minutes for the 5,077,237 transactions, with a peak of
  about 3.1 GB, so `make features` now takes about 10 minutes. On 1–10 Sep, 902 transactions close
  a cycle, 487,476 have a relay and 33,176 have a fan path.
- **R03's alerts do not change** with the shared search: 959 alerts, identical one by one.
- **Re-tuning on the 48 features** (`config/models.yaml` v1.2) adopts the search's best
  parameters for all three families, with no veto. Validation PR-AUC: LightGBM 0.595 (0.506
  before), logistic regression 0.350 (0.246) and Isolation Forest 0.011 (0.004).
- **Validation at the rules' volume** (75.5 alerts a day):

  | Detector | Without hubs | False positives | Attempts | PR-AUC |
  | --- | --- | --- | --- | --- |
  | Rules R01–R04 | 8.3% | 60% | 44/157 | — |
  | **LightGBM** | **18.0%** (19.4% before) | 0% | 65/157 (54) | 0.595 |
  | Logistic regression | 13.2% (7.8%) | 24% | 47/157 | 0.350 |
  | Isolation Forest | 4.4% (1.1%) | 85% | 27/157 | 0.011 |

  Logistic regression now beats the rules. Isolation Forest still does not.
- **LightGBM against the previous champion, detection without hubs by alerts a day:**

  | Alerts a day | 25 | 50 | 75 | 100 | 150 | 200 | 400 | 1,000 |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | Previous (43 features) | 9.0% | 13.8% | 18.4% | 23.3% | 28.8% | 35.9% | 47.1% | 59.4% |
  | Motifs (48 features) | 6.8% | 13.1% | 17.3% | 22.2% | 31.1% | 37.2% | 51.5% | 62.8% |

  At 100 alerts a day or fewer the previous model is about one point better, which is 10 to 15
  transactions over two days and within noise. From 150 alerts a day the motif model is better,
  and at 400 its false positives fall from 26% to 15%.
- **By typology at the rules' volume**, the budget moves toward what the motifs target:

  | Typology | Before | With motifs |
  | --- | --- | --- |
  | Cycles | 2 | 22 |
  | Random | 3 | 16 |
  | Fan-out | 21 | 8 |
  | Scatter-gather | 62 | 44 |

  In all, 186 patterned transactions are detected instead of 201, but they come from more
  attempts (65 instead of 54). Untyped laundering is still not detected at that volume.
- **The new champion line was chosen by hand.** At the protocol's exact point the motif model
  detects 1.4 points less (18.0% against 19.4%). It was adopted for its PR-AUC, its attempts, its
  cycles and the larger budgets, and that loss is reported as it is (CLAUDE.md rule 7).
