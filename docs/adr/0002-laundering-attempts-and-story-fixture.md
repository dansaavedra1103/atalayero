# ADR-0002: Laundering attempts table and story-based test fixture

- Status: accepted
- Date: 2026-10-04

## Context

- The patterns file documents 370 laundering attempts in 8 typologies (bipartite, cycle, fan-in,
  fan-out, gather-scatter, random, scatter-gather, stack). Each attempt is a block of transaction
  lines in the transactions CSV format, with no transaction ID. All 3,209 lines appear verbatim in
  the transactions file. It is the only ground truth for typologies.
- Only 3,209 of the 5,177 laundering transactions belong to a documented attempt. 98% of the other
  1,968 touch no account of any attempt, and they use varied payment formats, while documented
  attempts are all ACH but one.
- 180 of the 370 attempts share accounts with another attempt.
- Account IDs are unique only together with their bank: 8 account IDs appear under more than one
  bank.
- The first test fixture (ADR-0001) was a random sample. Each account appeared about once, so no
  attempt, account history or time window could be tested with it.

## Decision

1. **`raw.laundering_attempts`**, one row per (attempt, transaction): `attempt_id` (1-based
   position in the file), `typology` (snake_case English identifier), `description` (source text)
   and `transaction_id`. Each line is matched to `raw.transactions` on all 11 source columns. The
   load fails, leaving any previous table in place, unless every line matches exactly one
   transaction. The parser is strict about the block structure and the list of typologies.
   `make ingest` loads it after `raw.transactions`.
2. **Accounts are identified by `(bank, account)`** wherever logic works per account.
3. **Story-based fixture**, replacing the random sample. A story is a set of focus accounts and a
   time window, and holds every transaction that touches a focus account inside the window:
   - one documented attempt per typology: every account of the attempt, attempt span ± 24 h;
   - two laundering transactions outside every attempt ("untyped"): sender and receiver, ± 24 h;
   - clean accounts (never in laundering) over 5–6 Sep 2022: the busiest one, one that pays in
     Bitcoin, then more in seeded order until about 1,000 rows.

   Only self-contained stories qualify: no row of an attempt other than the story's own, so every
   laundering row in the fixture is either part of a complete sampled attempt or truly untyped.
   Among those, attempts and untyped cases are the ones closest to the median story size of their
   group. The seeded order is a SHA-256 of seed and account, stable across library versions. The
   matching blocks of the patterns file are written to `hi_small_patterns_sample.txt`.
4. **Untyped laundering is a category of its own.** The fixture covers it, and the phase 3
   golden set will use it for edge cases: the decision is scored, the typology is not.

## Consequences

- The fixture has 1,212 rows, above the ~1,000 target: self-contained stories are larger than
  average, which leaves no room for extra clean accounts. The test suite still runs in about 2 s.
- Tests can rely on complete histories for focus accounts inside their windows, one complete
  attempt per typology, untyped laundering, Bitcoin amounts and a busy clean account.
- Not covered by the fixture: accounts shared between attempts (mule reuse). That needs the full
  dataset or a hand-written case.
- Rule threshold tests (R02, R04) use hand-written rows; fixture stories serve as integration cases.
