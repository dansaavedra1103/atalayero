# ADR-0013: Bounded look-backs and fewer graph features

- Status: accepted
- Date: 2026-10-05

## Context

- Drift monitoring (ADR-0012) flagged both validation days on four features with a PSI above 0.25:
  - `pair_count_before`, at 2.2–3.0;
  - the sender's PageRank, up to 2.9;
  - the Louvain community sizes, at 1.4–1.5.
- `pair_count_before` carried 21.6% of the champion's gain. It counted every earlier transaction
  of the pair back to 1 Sep, so it grows with the history available. `minutes_since_previous`
  had no bound either (PSI 0.15).
- The graph snapshots of train hold the 1–2 Sep burst, and those of validation do not. Dividing
  community sizes by the snapshot size did not help (PSI 0.7–1.6). PageRank never told laundering
  apart (ADR-0008), and Louvain took about 65 s per snapshot, most of `make features`.
- Validation against the train weekdays, by look-back of the pair count:

  | Pair count over | PSI |
  | --- | --- |
  | All history | 2.2–3.0 |
  | 96 h | 1.0–2.5 |
  | 24 h | 0.05–0.06 |

  The 96 h window still drifts, because it is not yet full on the first train days.

## Decision

1. **Pair counts look back 24 h:** `pair_count_24h` and `reverse_pair_count_24h` replace the
   counts over all history. The reverse count is the earlier transactions the other way round
   before t, minus those before t − 24 h.
2. **`minutes_since_previous` looks back 96 h** and is NULL beyond (PSI 0.04 instead of 0.15).
3. **PageRank and community sizes are dropped**, and Louvain with them. The graph keeps degrees,
   amounts and short cycles: 10 features instead of 14.
4. **Every look-back is now bounded**, at most 96 h, so no feature grows with the history
   available. The models read 44 features.

## Consequences

- **Drift is gone.** No feature reaches a PSI of 0.25 on either validation day; 5 and 6 features
  are moderate, and the highest is 0.18 (`sender_graph_in_degree`). The rule alerts stay within
  their band.
- **`make features` takes about 6.5 minutes** instead of about 10: without Louvain the graph takes
  under a minute.
- **Re-tuning** (`config/models.yaml` v1.3) adopts new parameters for logistic regression and
  Isolation Forest. It vetoes LightGBM, whose best PR-AUC trial detects 18.4% without hubs against
  18.9% with the parameters it had (v1.2).
- **Validation at the rules' volume** (75.5 alerts a day):

  | Detector | Without hubs | False positives | Attempts | PR-AUC |
  | --- | --- | --- | --- | --- |
  | Rules R01–R04 | 8.3% | 60% | 44/157 | — |
  | **LightGBM** | **19.0%** | 0% | 63/157 | 0.553 |
  | Logistic regression | 12.0% | 34% | 50/157 | 0.286 |
  | Isolation Forest | 7.7% | 66% | 49/157 | 0.013 |

  LightGBM is the champion (v6), and it starts a new line on the new feature list (ADR-0011).
  Logistic regression beats the rules. Isolation Forest comes close to them for the first time
  (7.7% against 8.3%).
- **LightGBM's detection without hubs, by alerts a day**, across the three feature sets:

  | Alerts a day | 25 | 50 | 75 | 100 | 150 | 200 | 400 | 1,000 |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | 43 features (ADR-0009) | 9.0% | 13.8% | 18.4% | 23.3% | 28.8% | 35.9% | 47.1% | 59.4% |
  | 48, drifting (ADR-0011) | 6.8% | 13.1% | 17.3% | 22.2% | 31.1% | 37.2% | 51.5% | 62.8% |
  | 44, bounded (this ADR) | 8.3% | 13.4% | 18.7% | 23.0% | 30.1% | 36.2% | 49.0% | 60.5% |

  On validation, the bounded features give up some PR-AUC (0.553 against 0.595) and some detection
  at large budgets, and recover the small budgets. That trade is deliberate. Validation sits right
  after train, where history-dependent features still look alike; test (9–10 Sep) lies further
  away, and that is where stable features should pay off. The single test run will tell.
- **At the rules' volume**, the champion catches 19 of the 66 cycle transactions, 14 random and 63
  scatter-gather. Untyped laundering is still not detected at that volume.
