# Data card: IBM AML, HI-Small

Atalayero uses one dataset: *IBM Transactions for Anti Money Laundering (AML)*, HI-Small variant.
It is **synthetic**: a multi-agent simulation of banks, people and companies generated the
transactions. It describes no real person or institution. No other data enters the project
(CLAUDE.md, rule 1).

- Source: [Kaggle](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml),
  published by Erik Altman, dataset version 8.
- Paper: E. Altman, J. Blanuša, L. von Niederhäusern, B. Egressy, A. Anghel, K. Atasu.
  *Realistic Synthetic Financial Transactions for Anti-Money Laundering Models.*
  [arXiv:2306.16424](https://arxiv.org/abs/2306.16424).
- License: Community Data License Agreement – Sharing 1.0 (text in
  [`tests/fixtures/`](../tests/fixtures/README.md)). The repository's code is MIT; the data and the
  fixtures sampled from it keep the dataset's license.
- How it is obtained: `make ingest` downloads both files anonymously and checks their SHA-256
  (details in [`data/README.md`](../data/README.md)). Nothing from the dataset is committed but
  the small test fixtures.

## Contents

| File | Content |
| --- | --- |
| `HI-Small_Trans.csv` | 5,078,345 transactions, 5,177 of them labelled as laundering (0.10%) |
| `HI-Small_Patterns.txt` | 370 documented laundering attempts in 8 typologies, 3,209 transactions in all |

Each transaction has a timestamp (to the minute), the sending and receiving bank and account, the
amount paid and received with their currencies, a payment format, and the label. "HI" means the
higher of the two illicit ratios IBM published; "Small" is the smallest of the three sizes.

The 8 typologies of the attempts are fan-out, fan-in, cycle, bipartite, stack, scatter-gather,
gather-scatter and random.

## What the project found in it

- **The simulation ends on 10 Sep 2022.** From 11 to 18 Sep there are only 1,108 transactions,
  59% of them laundering: the tails of attempts that started earlier. Atalayero uses 1–10 Sep
  only; on later days the date alone would predict the label (ADR-0005).
- **Volume is uneven.** A burst on 1–2 Sep (1.1M and 754k transactions), then a weekly cycle of
  about 482k transactions on weekdays and 207k on weekends. The laundering rate grows from 0.078%
  in the first six days to 0.11% in the last four.
- **Most laundering is documented; much is not.** Only 3,209 of the 5,177 laundering transactions
  belong to a documented attempt. 98% of the other 1,968 touch no account of any attempt, and
  their payment formats differ: documented attempts are ACH but one. No model here detects the
  undocumented laundering at a practical alert budget (`docs/model_card.md`).
- **Attempts overlap.** 180 of the 370 attempts share accounts with another attempt.
- **Accounts need their bank.** Account IDs are unique only together with the bank: 8 IDs appear
  under more than one bank. Leading zeros in bank codes are significant.
- **No exchange rates.** Amounts come in 15 currencies. The 72,170 cross-currency transactions
  imply fixed rates, which `make fx-rates` derives into a committed seed (ADR-0003).
- **A few senders dominate.** The 15 busiest senders of the train days (the "hubs") touch 12.8% of
  the laundering in validation. A detector that alerts them catches laundering without telling it
  apart, so evaluations report detection without hubs (ADR-0007).
- **The source file is not sorted by time**; transaction IDs are row numbers in it (ADR-0001).

## How Atalayero splits it

| Days | Role |
| --- | --- |
| 1 Sep | Warm-up: history for the first features |
| 2–6 Sep | Train |
| 7–8 Sep | Validation |
| 9–10 Sep | Test, run once (ADR-0014) |

Splits are by time, never random. A feature for a transaction at time t uses only data from
before t (ADR-0008). Labels and the patterns file live apart from everything the investigator
agent can read (ADR-0016).

## What it can and cannot show

- **It can show** design, rigour and operation: point-in-time features, temporal evaluation
  against a rules baseline, versioned rules and models, an agent measured against score-only,
  and a pipeline that rebuilds from scratch.
- **It cannot show** performance on real transactions. The simulator's laundering follows its own
  generative rules, documented attempts are mostly ACH, and real labels are noisier and later.
  Every number in this repository describes the simulation.
- **It holds no personal data**, so no privacy review applies; a real deployment would need one.
