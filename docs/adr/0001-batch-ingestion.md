# ADR-0001: Batch ingestion — source, raw schema and transaction IDs

- Status: accepted; decision 5 (test fixture) superseded by ADR-0002
- Date: 2026-10-04

## Context

Phase 1 needs the IBM AML HI-Small dataset locally, rebuildable from scratch with one command,
with no paid or cloud services and no credentials.

- The only official distribution is Kaggle. The one anonymous mirror found (Hugging Face) has the
  transactions file but not the patterns file, and relabels the license as Apache-2.0.
- Kaggle's public API endpoint serves single files of this dataset anonymously, and accepts a
  dataset version number.
- The source CSV has traps:
  - the header repeats `Account` (sender, then receiver);
  - bank IDs have significant leading zeros (`010`, `03208`);
  - amounts have up to 6 decimals (Bitcoin, ~148k rows) and up to 13 integer digits;
  - timestamps have minute resolution and the file is not sorted by time;
  - there is no transaction ID, and 398 rows are exact duplicates of another row.
- Later phases need a stable transaction ID: the agent cites transactions as evidence, the
  grounding check verifies them, and the patterns ground truth must map onto them.

## Decision

1. **Source.** Download each file from Kaggle's public endpoint, anonymously, pinned to dataset
   version 8, and verify its SHA-256. URL, version and checksums live in `config/settings.yaml`.
   The download uses the standard library (`urllib`, `hashlib`), streams to `<file>.part` and
   renames only after the checksum matches.
2. **Typed raw layer.** CSV → `data/raw/transactions.parquet` → DuckDB table `raw.transactions`,
   recreated on every load (`CREATE OR REPLACE`). Columns are named explicitly (snake_case) and
   typed at load: bank and account IDs `VARCHAR`, amounts `DECIMAL(20, 6)`, `TIMESTAMP`,
   `BOOLEAN`. A malformed file fails the load instead of reaching dbt. `DECIMAL(18, 2)` was tried
   first and silently rounded the Bitcoin amounts, some of them to 0.00.
3. **`transaction_id` = 1-based row number in the source file**, header excluded. It is stable
   because the file is pinned by checksum. A content hash would give the 398 duplicate rows the
   same ID. It is computed as the `rowid` of a freshly created table with insertion order
   preserved: same result as `row_number() OVER ()`, which runs single-threaded and took 71 s
   against 13 s. Checked against all 5,078,345 source rows.
4. **Runtime dependencies:** `duckdb` (warehouse and CSV/Parquet engine, so no pandas or pyarrow),
   `pydantic-settings` (the project's configuration convention) and `pyyaml` (its YAML source).
5. **Test fixture.** `tests/fixtures/hi_small_sample.csv`: 1,000 source rows, byte-for-byte,
   seeded and stratified (5% laundering). Published under CDLA-Sharing-1.0 with the license text,
   attribution and a notice that it is a sample (sections 3.1 and 3.3 of the agreement).

## Consequences

- `make ingest` rebuilds `data/` from scratch; the load takes about 15 s once the files are
  downloaded. Anyone can reproduce it without a Kaggle account.
- If Kaggle removes version 8 or the files change, the download fails loudly. Moving to a new
  version means reviewing it, updating version and checksums, and a new ADR if the schema changes.
- Any change to the source file changes the IDs; the checksum pin is what keeps them stable.
- `DECIMAL(20, 6)` needs 128-bit storage and is slower to parse than 64-bit decimals; acceptable
  for a 5M-row batch.
- Same-minute timestamps and the unsorted file mean that ordering by time needs a tiebreak
  (`transaction_id`). The streaming replay and the "only data with timestamp < t" rule must
  account for this; the temporal split ADR will settle it.
- The fixture is not under the repository's MIT license.
- The patterns file is downloaded and verified but not parsed yet.
