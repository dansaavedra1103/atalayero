# ADR-0004: Streaming replay and online rules

- Status: accepted
- Date: 2026-10-04

## Context

- Phase 1 replays the transactions through Redpanda (Kafka API) and evaluates two rules online,
  R02 and R04, defined in YAML with a minimal schema. The full rule engine comes in phase 2.
- The transactions have minute resolution, so many share a timestamp.
- DuckDB allows a single writer process, and the producer keeps the database open while it reads.
- Calibration on 1–10 Sep (one alert per account and rule per 24 h):

  | Candidate | Alerts/day | Accounts | Alerts on the 15 hub accounts |
  | --- | --- | --- | --- |
  | ≥ 10 counterparties in 24 h, transactions ≥ 5,000 USD | 15.8 | 23 | 95% |
  | ≥ 20 outgoing transactions in 1 h | 15.0 | 15 | 100% |
  | ≥ 10 outgoing transactions in 1 h | 44.7 | 287 | 34% |

  Fifteen accounts of bank 070 (one sends 168,672 transactions in 10 days) dominate any simple
  dispersion or velocity rule. With that volume, any of their 24 h windows also touches some
  laundering transaction, so "the window contains laundering" says little about such alerts.

## Decision

1. **Runtime dependency `confluent-kafka`** (librdkafka, binary wheels). **Redpanda v26.1.18**,
   one node in Docker Compose (`make up`, `make down`). CI does not use Docker.
2. **Shared schemas** in `atalayero.schemas`: `Transaction` (no label; extra fields are rejected,
   so ground truth cannot reach the stream) and `Alert`. Messages are JSON.
3. **Order.** The topic has one partition, which gives a total order. The producer reads
   `marts.fct_transactions` ordered by `(transacted_at, transaction_id)` and keys messages by
   sender account, ready for partitioning by account later. The evaluator rejects input out of
   that order.
4. **Rule semantics.** Per sender account, over outgoing transactions in the trailing window
   `[t - window, t]` (event time, both ends included). Self-transfers and transactions under
   `min_amount_usd` never count. A `cooldown` allows one alert per account and rule within that
   span. `alert_id` is `<rule>:<transaction>`, so a replayed alert keeps its ID. The evidence keeps
   the 100 most recent transaction IDs, and `value` holds the full measure. Windows and cooldowns
   that have run out are dropped, so state follows recent activity.
5. **Rule YAML schema**: `id`, `name`, `typology` (optional, one of the 8), `owner`, `version` (a
   quoted string), `description`, `metric` (`distinct_counterparties` or
   `outgoing_transactions`), `window`, `threshold` (`min_count`, `min_amount_usd`), `cooldown` and
   `history`, whose last entry must be the current version. It replaces the design sketch: the
   metric is explicit, the amount says its currency, and owner and cooldown are new.
6. **Thresholds (rules-only baseline):** R02 at 10 counterparties in 24 h with transactions of
   5,000 USD or more, R04 at 10 outgoing transactions in 1 h, both with a 24 h cooldown. Hub
   accounts dominate them; this is the baseline that phase 2 models must beat (CLAUDE.md rule 7).
7. **Replay.** Producer and consumer run side by side in one process and talk only through
   Redpanda. A replay starts at the topic's end offset, with no topic deletion, and the consumer
   stops once it has read every message produced. If messages stop arriving after the producer
   is done, the replay fails; if the consumer fails, the producer stops. Offsets are not
   committed: the state lives in memory and every replay is a full run. A `speedup` setting paces
   event time; 0 means as fast as possible.
8. **Alerts go to Parquet** (`data/stream/alerts/part-*.parquet`), not to DuckDB, because of the
   single writer. A new run replaces the previous parts.
9. **Tests.** Unit tests in CI cover the thresholds, window boundary, cooldown, order, schema
   and sink. An integration test (`make test-integration`, needs Redpanda) checks that the alerts
   of a replay through Redpanda equal those of evaluating the same transactions directly.

## Consequences

- The full replay through Redpanda takes about 2 min 45 s for 5,078,345 transactions (about
  30,000 per second end to end) and writes 605 alerts, identical one by one to a direct
  evaluation of the same transactions.
- Those alerts match the SQL calibration exactly: R02 15.8 alerts/day on 23 accounts and R04 44.7
  on 287 (1–10 Sep), two independent implementations agreeing.
- Peak memory is about 1.65 GB: about 1.4 GB on the producer side (DuckDB sorts 5M rows) and about
  200 MB of evaluator state.
- Hubs produce 95% of the R02 alerts and a third of the R04 ones: an input for phase 2 (account
  segmentation, models).
- No durable consumer state: a replay interrupted halfway has to start again.
- Alerts are not yet a dbt source; they will be when phase 2 prioritizes alerts.
