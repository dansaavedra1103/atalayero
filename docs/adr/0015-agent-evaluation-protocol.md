# ADR-0015: Agent evaluation protocol

- Status: accepted
- Date: 2026-10-06

## Context

- **What phase 3 must show.** Phase 3 builds an investigator agent that receives an alert and
  writes a case report (design.md §4). Its quality is measured offline:
  - on a golden set of 150–200 alerts with known answers, in three groups reported apart;
  - against a baseline that decides from the score alone (design.md §5).
- **The protocol comes first.** As with the models (ADR-0007), it is fixed here, before the
  agent exists, so it cannot be tuned in the agent's favour.
- **The alert unit is the account-day** (ADR-0007). On test, at the rules' volume of 85.5 alerts
  a day, the queue holds almost no laundering outside the documented attempts:
  - LightGBM alerts none of it;
  - the rules alert 23 account-days, but 21 of them are hubs. The 23 hold a median of 1,744
    transactions, which no investigation can read.
- **The queue widens with the model's budget.** With the rules' alerts plus the model's top 2,000
  account-days a day, hubs left out, it holds:

  | Split | Patterned | Untyped | Clean |
  | --- | --- | --- | --- |
  | Validation | 895 | 97 | 3,080 |
  | Test | 752 | 90 | 3,252 |

- **Two models produced the scores.** The test scores come from models refit on train +
  validation (ADR-0014). On validation, only the champion is out of sample, since it was
  trained on train alone.

## Decision

1. **A case is an account-day of the alert queue:** every account-day the rules alerted, plus
   the model's top `agent_evals.queue_budget` (2,000) account-days of each day. Hubs are left out.
   - Each case is an alert some detector actually raised, which an investigator could read.
   - The model's scores are the champion's on validation and the refit model's on test.
2. **Three groups of the same size**, drawn with a seed:
   - **patterned:** the account-day holds laundering of a documented attempt. The answer is
     escalate, with any of its attempts' typologies accepted;
   - **untyped:** it holds laundering of no attempt, and none of one. The answer is escalate;
   - **clean:** it holds no laundering. The answer is close.

   Patterned cases take each typology in turn. Clean cases come half from the rules' alerts and
   half from the model's alone. The mix is a design choice, not the prevalence.
3. **Two case sets, as in the temporal split (ADR-0005):**
   - the **dev set**: 30 cases per group, from validation. It is where every choice is made:
     the language model, the prompts and the baseline's threshold;
   - the **golden set**: 60 per group, from test, read once per reported result.

   `make golden-set` builds both deterministically. They are versioned in `evals/`.
4. **Alerts and answers live in separate files.**
   - `CaseAlert` (`schemas.py`) is what an investigator reads: the account-day, its sources, its
     score, its rank that day, and the transactions that raised it (the rules' triggers and its
     three top-scored ones).
   - `CaseAnswer` holds the group, the decision and the accepted typologies. It lives in the
     `evals` package and is never part of what an investigator reads.
5. **`CaseReport` is the one of design.md, with structured evidence:** `evidence` is a list of
   `Evidence(transaction_id, amount_usd)` instead of a list of strings. That way, every cited
   transaction and amount can be checked exactly. A report closes with typology `none`, and only
   then.
6. **The metrics** (`evals/metrics.py`), per group:
   - decision accuracy in each group;
   - typology accuracy in the patterned group;
   - in the untyped group, the share of reports that claim a specific typology instead of
     `unclassified`;
   - the grounded share and the hallucinated IDs per report;
   - the median steps and latency.

   A case without a valid report counts as wrong. **The primary metric is the mean of the three
   groups' decision accuracies**, so a detector cannot win by deciding the same way on every case.
7. **The score-only baseline** escalates an alert whose rank that day is within a threshold, and
   closes the rest. It never names a typology: it answers `unclassified` when it escalates and
   `none` when it closes.
   - The threshold is fit on the dev set, the smallest one with the best primary metric.
   - It is a rank, not a score. The two models score on different scales, but a rank means the
     same under both: the k-th most suspicious account-day of its day.
8. **Reports** go to `evals/reports/<date>-<set>-<detector>.json` and are versioned. CI
   validates every report against `AgentEvalReport` (CLAUDE.md rule 9).
9. **`make eval`** runs a detector on a set: `SET=dev` by default, `SET=golden` once per
   reported result.

## Consequences

- **The score-only baseline**, with its rank threshold fit on the dev set at 1,133:

  | Set | Patterned | Untyped | Clean | Primary |
  | --- | --- | --- | --- | --- |
  | Dev (validation, where the threshold was fit) | 97% | 70% | 77% | 81.1% |
  | **Golden (test)** | **92%** | **43%** | **72%** | **68.9%** |

  **This is what the agent has to beat: 68.9% on the golden set.**
  - The baseline closes 28 of the 30 clean cases the rules raised, whose model scores are low.
    It gets only 15 of the 30 the model raised alone right.
  - It escalates 26 of the 60 untyped cases.
  - Neither group can be told apart by rank alone; that is where an investigation would have to
    add something.
- **The golden set is harder than the dev set** for the baseline (68.9% against 81.1%). Part of
  the gap is the threshold, fit on dev. The other part is the untyped cases of test, which rank
  lower: from 400 to 2,341, with a median of 1,233.
- **Untyped cases come almost only from the model's wide budget:** 59 of 60 in the golden set,
  ranked well beyond the rules' volume. They test whether the agent recognises laundering the
  operating point would not have alerted.
- **Hubs stay out**, as they do in the primary metric of phase 2. The agent is never measured on
  an account-day with thousands of transactions, which is a limitation the report must state.
- **The rank and the sources are legitimate inputs.** An analyst sees them too, and the baseline
  uses nothing else.
- **The case sets derive from the dataset.** They are published under CDLA-Sharing-1.0, with
  the attribution in `evals/README.md`.
