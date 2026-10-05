# ADR-0008: Point-in-time features

- Status: accepted
- Date: 2026-10-05

## Context

- CLAUDE.md rule 3: the features of a transaction at time t use only data with a timestamp before
  t, graph features included. Building features from the full graph would leak the future and
  inflate every metric.
- Transactions have minute resolution and many share a minute. Inside a minute the real order is
  unknown: `transaction_id` is the row number in a source file that is not sorted by time
  (ADR-0001). The online rules break those ties by ID because a stream needs a total order
  (ADR-0004). A model has no such need.
- ADR-0005 left the warm-up period to phase 2, depending on the longest window.
- 5,077,237 transactions on 1–10 Sep live in DuckDB. The 1 Sep burst alone holds 1.1M of them.

## Decision

1. **Contract.** The features of a transaction at time t use its own fields and the transactions
   strictly before t. Transactions in the same minute never count, the transaction itself
   included. History may cross split boundaries backward, never forward, and stops at
   `splits.test_end`: 11–18 Sep is never read.
2. **Python modules with DuckDB SQL, not dbt.** The features live in `src/atalayero/features/`, so
   a pytest can check the contract on any of them, tabular or graph. The output goes to
   `data/features/` as Parquet, without labels. Labels are joined only for training and
   evaluation.
3. **29 tabular features** (`features/tabular.py`, `make features`). A window `[t - w, t)` is a
   DuckDB `RANGE` frame ending `1 MICROSECOND PRECEDING`. An empty window gives 0; a value that is
   undefined gives NULL (no previous transaction, no mean).
   - Own fields: payment format, payment and receiving currency, amount in US Dollar,
     cross-currency, self-transfer, same bank, hour.
   - Per account, for both the sender and the receiver:
     - sent and received transactions in 1 h;
     - sent and received transactions, US Dollar amounts and distinct counterparties in 24 h;
     - minutes since the account's previous transaction.
   - Per pair: earlier transactions from the sender to the receiver, and the other way round.
   - The amount relative to the sender's mean amount sent in the last 24 h.
4. **Leakage guard.** A test builds random transactions where many share a minute. It removes
   every other transaction at or after t and checks that the features of the transaction at t do
   not change. Leaks were injected on purpose to check the test:
   - a window that includes the current minute;
   - a window that looks ahead;
   - pair counts that see the same minute;
   - a non-strict match on the reverse pair.

   The test catches all four. A feature that counts its own transaction does not change when the
   rest are removed, so that kind of leak slips through this test. Hand-written tests pin exact
   values to cover it, and a fifth injected leak of that kind was caught by them.
5. **Warm-up.** Training rows start on 2 Sep. 1 Sep serves only as history: the longest tabular
   window is 24 h.
6. **14 graph features from daily snapshots** (`features/graph.py`). For day d, the graph holds
   the transactions in `[d - 3 days, d)`, without self-transfers. It is built before any
   transaction of d, so it meets the contract by construction. Three days cover the typical
   attempt (ADR-0005). The features per account, for both the sender and the receiver:
   - distinct senders and receivers (in- and out-degree);
   - US Dollar received and sent;
   - PageRank weighted by transaction count, times the number of accounts in the snapshot, so it
     averages 1 whatever the snapshot size;
   - size of the account's Louvain community, on the undirected and unweighted graph, with a
     fixed seed;
   - whether the account sits on a directed cycle of two or three accounts.

   An account missing from a snapshot gets zeros. The removal test of point 4 applies here too,
   and it caught two leaks injected on purpose: a snapshot that includes its own day, and a join
   to the next day's snapshot. `networkx` is a new runtime dependency. Snapshots are built in
   parallel in spawned processes (`graph_workers`, 4 by default).
7. **Memory cap.** The build sets a DuckDB `memory_limit` of 4 GB and spills to `data/tmp/`.

## Consequences

- For the 5,077,237 transactions, the tabular features take about 26 s with a peak of about
  5.4 GB. Without the cap the peak is 8.7 GB and the run takes 21 s.
- The graph features take about 5 min with 4 workers: the main process peaks at about 3.5 GB, and
  each worker takes about 1.5 GB. Louvain dominates the time, at about 65 s per snapshot.
  Snapshots hold 244,000 to 395,000 accounts. They are larger while the 1 Sep burst is inside the
  window, and the snapshot of 1 Sep is empty.
- The features carry signal on train. Laundering against clean transactions:

  | Feature | Laundering | Clean |
  | --- | --- | --- |
  | Median amount | 4,600 USD | 970 USD |
  | Same bank | 2% | 19% |
  | Earlier transactions on the same pair (mean) | 0.86 | 3.92 |
  | Minutes since the sender's previous transaction (mean) | 1,277 | 311 |
  | Sender sent nothing in the snapshot (2–6 Sep) | 42% | 7% |
  | Receiver on a cycle of two or three accounts (2–6 Sep) | 4.3% | 0.1% |
  | Receiver's community size (median, 2–6 Sep) | 4,202 | 7,306 |

  PageRank does not tell the two apart: the median is 0.8 for both.

- Pair counts and minutes since the previous transaction look back to 1 Sep with no bound, so they
  grow over the ten days. The drift checks (PSI) will see that. It is a property of the data
  available at each time, not a leak.
- Rules count the same minute and features do not. Rule alerts and model scores can therefore
  differ, but only for transactions inside the same minute.
- The graph snapshots of 2 and 3 Sep have less than three days of history.
