# ADR-0003: dbt models on DuckDB — layers, exchange rates and labels

- Status: accepted
- Date: 2026-10-04

## Context

- The raw schema (ADR-0001, ADR-0002) holds typed transactions and documented laundering
  attempts. Phase 1 needs dbt models (staging → intermediate → marts) built on DuckDB, in CI on
  the test fixture and locally on the full dataset.
- Amounts come in 15 currencies and the source has no exchange-rate table. Summing amounts across
  currencies is meaningless, and rules such as R02 (`min_amount`) need one currency.
- The 72,170 cross-currency transactions imply fixed rates: per currency, the median rate implied
  by transactions against US Dollar agrees in both directions to about 1e-8 (Bitcoin: 6e-4, its
  amounts are quoted to 6 decimals). Tiny amounts are rounded to cents, so single ratios are noisy.
- The test fixture has direct US Dollar pairs for only 7 of the 15 currencies, so rates cannot be
  derived inside dbt when it runs on the fixture.
- The phase 3 agent must never see the labels (design §5).
- dbt sends anonymous usage statistics to dbt Labs by default.

## Decision

1. **Runtime dependency `dbt-duckdb`** (with `dbt-core`). No dbt packages: generic tests are the
   built-in ones plus singular tests in `dbt/tests/`, so `dbt build` needs no network.
2. **Layout.** Project in `dbt/`, run from the repository root through the Makefile. A committed
   `dbt/profiles.yml` (local file, no secrets) reads the same `ATALAYERO_DUCKDB_PATH` as the Python
   settings; there is one target, `dev`. Schemas: `staging` (views), `intermediate` and `marts`
   (tables), `reference` (seeds). Usage statistics are disabled in `dbt_project.yml`.
3. **Exchange rates as a derived, committed seed.** `make fx-rates` computes the median implied
   rate against US Dollar over both directions from `raw.transactions` and writes
   `dbt/seeds/fx_rates_usd.csv`. A singular test checks that the seed still matches the data
   (within 0.1% for currencies with at least 30 observations), so it bites on the full dataset.
   Paid and received amounts must agree in US Dollar within 1% + 0.05 USD (observed: at most
   0.75% for amounts of 1 USD or more).
4. **Labels apart.** `marts.fct_transactions` has no label. `marts.fct_laundering_labels` holds
   `is_laundering`, `label_group` (`patterned`, `untyped`, `clean`), `attempt_id` and `typology`.
5. **Accounts keyed by `bank:account`** in every model.
6. **Whole-period aggregates are descriptive.** `int_account_daily_activity`, `int_account_edges`
   and `dim_accounts` cover the whole period. Phase 2 features must be computed only from data
   before each transaction (no temporal leakage).
7. **Source freshness is deferred to phase 4**, when Airflow loads data on a schedule. With a
   static dataset it measures nothing.
8. **CI** runs `make dbt-sample`: it loads the fixtures into `data/sample/` with the ingestion code
   and runs `dbt build` on them.

## Consequences

- `make dbt` builds and tests everything on the full dataset in about 15 s; `make dbt-sample`
  does the same on the fixture in about 5 s.
- The design sketch had `dbt/profiles.yml.example`; a real `profiles.yml` works out of the box
  because it only points at a local file.
- If the source data changed, the seed test would fail until `make fx-rates` is run again.
- US Dollar amounts are DOUBLE, not DECIMAL: they are derived values for analysis, and the
  reconciliation tests allow for floating-point error.
- Features, alerts and KPI marts come later, with the streaming rules and phase 2.
