# Test fixtures

## `hi_small_sample.csv` and `hi_small_patterns_sample.txt`

A story-based sample of the *IBM Transactions for Anti Money Laundering (AML)* dataset (Kaggle,
version 8), in the source formats: `hi_small_sample.csv` has the header and rows of
`HI-Small_Trans.csv`, and `hi_small_patterns_sample.txt` has blocks of `HI-Small_Patterns.txt`.
Rows and blocks are byte-for-byte as in the source and in source order.

**Changed from the source:** both files are samples; no row was edited. They are synthetic data:
no real people or institutions. Regenerate them with `make fixture` (needs `make ingest` first);
the output is deterministic.

A story is a set of focus accounts and a time window, and holds every transaction that touches a
focus account inside the window (ADR-0002):

| Stories | Focus accounts | Window |
| --- | --- | --- |
| 8 laundering attempts, one per typology | every account of the attempt | attempt span ± 24 h |
| 2 laundering transactions outside every attempt ("untyped") | sender and receiver | ± 24 h |
| 2 clean accounts (never in laundering) | the busiest one (receives 80 transfers in one hour) and one that pays in Bitcoin | 5–6 Sep 2022 |

Tests can rely on:

- for focus accounts, every transaction inside the window is present; their counterparties appear
  only through those transactions;
- every laundering row belongs either to a complete attempt in `hi_small_patterns_sample.txt` or
  to no attempt at all, because only self-contained stories are sampled.

Not covered: accounts shared between attempts (180 of the 370 attempts in the full data). Use the
full dataset or a hand-written case for that.

### Attribution and license

- Dataset: *IBM Transactions for Anti Money Laundering (AML)*, published on Kaggle by Erik Altman:
  <https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml>
- Paper: E. Altman, J. Blanuša, L. von Niederhäusern, B. Egressy, A. Anghel, K. Atasu.
  *Realistic Synthetic Financial Transactions for Anti-Money Laundering Models.*
  [arXiv:2306.16424](https://arxiv.org/abs/2306.16424).
- License: **Community Data License Agreement – Sharing – Version 1.0**, full text in
  [`LICENSE-CDLA-Sharing-1.0.txt`](LICENSE-CDLA-Sharing-1.0.txt). `hi_small_sample.csv` and
  `hi_small_patterns_sample.txt` are published under that agreement, not under the repository's
  MIT license.
