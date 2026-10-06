# Agent evals

The case sets and reports of the investigator agent's offline evaluation (ADR-0015).

| File | Content |
| --- | --- |
| `dev_alerts.jsonl` | 90 alerts from validation (30 patterned, 30 untyped, 30 clean): for choosing models and prompts |
| `golden_alerts.jsonl` | 180 alerts from test (60 per group): read once per reported result |
| `*_answers.jsonl` | The expected decision and accepted typologies of each alert, in the same order |
| `reports/` | One report per detector and set: `<date>-<set>-<detector>.json`, validated by CI |

An investigator reads the `*_alerts.jsonl` files and nothing else. The answers are only for
scoring.

- `make golden-set` rebuilds both sets deterministically (it needs `make train` and
  `make holdout`).
- `make eval SET=dev` (or `SET=golden`) evaluates a detector and writes a report.

## Attribution and license

The case sets are derived from *IBM Transactions for Anti Money Laundering (AML)*, published on
Kaggle by Erik Altman:
<https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml>.
They are synthetic data, with no real people or institutions.

They are published under the **Community Data License Agreement – Sharing – Version 1.0**, not
under the repository's MIT license. The full text is in
[`tests/fixtures/LICENSE-CDLA-Sharing-1.0.txt`](../tests/fixtures/LICENSE-CDLA-Sharing-1.0.txt).
