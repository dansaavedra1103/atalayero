# Test fixtures

## `hi_small_sample.csv`

1,000 rows of `HI-Small_Trans.csv` from the *IBM Transactions for Anti Money Laundering (AML)*
dataset (Kaggle, version 8), in the source format: same header, rows byte-for-byte as in the source.

**Changed from the source:** this file is a seeded, stratified sample of the source rows
(50 laundering + 950 non-laundering; laundering is about 0.1% of the full file). Rows keep their
source order and no row was edited. It is synthetic data: no real people or institutions.

Regenerate it with `make fixture` (needs `make ingest` first). The output is deterministic.

### Attribution and license

- Dataset: *IBM Transactions for Anti Money Laundering (AML)*, published on Kaggle by Erik Altman:
  <https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml>
- Paper: E. Altman, J. Blanuša, L. von Niederhäusern, B. Egressy, A. Anghel, K. Atasu.
  *Realistic Synthetic Financial Transactions for Anti-Money Laundering Models.*
  [arXiv:2306.16424](https://arxiv.org/abs/2306.16424).
- License: **Community Data License Agreement – Sharing – Version 1.0**, full text in
  [`LICENSE-CDLA-Sharing-1.0.txt`](LICENSE-CDLA-Sharing-1.0.txt). `hi_small_sample.csv` is
  published under that agreement, not under the repository's MIT license.
