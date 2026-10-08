# Agent evaluation: Atalayero investigator (phase 3)

An LLM agent that investigates the alerts of the transaction monitor and decides whether to
escalate or close each one, with a typology, cited evidence and a narrative. It runs on a local
model and is measured offline against the score-only baseline, on a **synthetic** dataset (IBM
AML, HI-Small).

**On the golden set, the agent does not beat the baseline: it scores 68.3%, against 68.9% for
the score alone.** It decides as the baseline in 177 of 180 alerts. Its three changes to the
model's call escalated one laundering alert and two clean ones, so it loses by one alert. All 180
of its reports passed verification, with no invented transaction. The typology it names is right
in 4 of 60 patterned alerts.

None of this measures performance on real transactions or real investigators.

- Date: 2026-10-08 · Phase 3 of Atalayero, a public portfolio project · License: MIT
- Decisions behind every number below: [ADRs 0015–0019](adr/)
- Reports: [`evals/reports/`](../evals/reports/), one JSON per run, with every case's report

## What is evaluated

| | |
| --- | --- |
| Agent | A LangGraph graph: triage → investigate (tool loop) → draft → ground (ADR-0018) |
| Language model | `gpt-oss:20b` on Ollama, pinned by digest; reasoning effort low, temperature 0, seed 0, 16,384-token context |
| Tools | Five, served over MCP (ADR-0016): account profile, transactions, graph neighbourhood, score explanation (LightGBM contributions), typology search over 50 notes in FAISS (ADR-0017) |
| Starting point | The model's call: it escalates the account-days ranked within 1,133 of their day (ADR-0019) |
| Verification | Deterministic code: every cited transaction must exist, precede the cut-off, have been shown by a tool, and match its amount within 1% |
| Configuration | `config/agent.yaml`, versioned; every report records the version and a checksum of the prompts |
| Hardware | One laptop GPU (RTX 3080 Laptop, 16 GB); the model takes 12.7 GB of it |

**The baseline** decides from the score alone: it escalates an alert ranked within 1,133 of its
day and closes the rest. The threshold was fit on the dev set (ADR-0015).

## How it was measured

- **A case is an alert some detector raised:** an account-day the rules alerted, or one of the
  model's top 2,000 of its day. Hub accounts are left out.
- **Three groups of the same size**, reported apart:
  - **patterned:** laundering of a documented attempt. The answer is escalate, with one of its
    typologies;
  - **untyped:** laundering of no documented attempt. The answer is escalate;
  - **clean:** no laundering. The answer is close.

  The mix is a design choice, not the prevalence. Clean cases come half from the rules' alerts
  and half from the model's alone.
- **Two case sets**, as in the temporal split (ADR-0005):
  - the **dev set**, 30 cases per group from validation (7–8 Sep), where every choice was made:
    the prompts, the baseline's threshold, the agent's starting point;
  - the **golden set**, 60 per group from test (9–10 Sep), run once for the reported result.
- **The primary metric is the mean of the three groups' decision accuracies.** A detector cannot
  win by deciding the same way on every case. A case without a valid report counts as wrong.
- **Also measured:** typology accuracy (patterned), the share of untyped reports that claim a
  specific typology, the grounded share, hallucinated transaction IDs per report, tool calls and
  seconds per case.
- **The agent never sees an answer.** Its tools expose neither the labels nor the patterns file,
  and see nothing after the end of the alert's day.

## Results

### On the golden set

The single run of agent v1.4, the version chosen on the dev set, on 180 alerts from the test
days (`evals/reports/2026-10-08-golden-agent-v1.4.json`):

| Detector | Patterned | Untyped | Clean | Primary |
| --- | --- | --- | --- | --- |
| Score-only baseline | 92% | 43% | 72% | **68.9%** |
| Agent v1.4 | 92% | 45% | 68% | 68.3% |

| Agent v1.4 | |
| --- | --- |
| Changes to the model's call | 3 of 180, all escalations: 1 untyped (right), 2 clean (wrong) |
| Typology accuracy, patterned | 7% (4 of 60). It named a specific typology 25 times, 4 of them right |
| Specific typology claimed, untyped | 10% (6 of 60) |
| Reports that passed verification | 100% (180 of 180) |
| Invented transaction IDs per report | 0 |
| Tool calls per case (median) | 5 |
| Seconds per case (median) | 44 |

- **Both detectors lose about 12 points from dev to golden.** The agent follows the model's call,
  so it inherits the drop, which comes mostly from the untyped group (70% → 43% for the
  baseline). The threshold of 1,133 was fit on the dev set.
- **The baseline never names a typology**, so the agent's 4 right typologies are all it adds on
  that metric.

### On the dev set, version by version

| Detector | Patterned | Untyped | Clean | Primary | Notes |
| --- | --- | --- | --- | --- | --- |
| Score-only baseline | 97% | 70% | 77% | **81.1%** | The bar |
| Agent v1.0 | 7% | 0% | 97% | 34.4% | Closed 87 of 90 |
| Agent v1.1 | 13% | 0% | 97% | 36.7% | Closed 85 of 90 |
| Agent v1.2 | — | — | — | — | Stopped at 11 cases: 2 right, against 9 for the baseline |
| Agent v1.3 | — | — | — | — | Stopped at 37 cases: 30 right, against 32 for the baseline |
| Agent v1.4 | 97% | 70% | 77% | 81.1% | Never overrode the model: decides as the baseline |

Up to v1.3, every report passed verification. v1.4's passed in 88 of 90 cases: the other two
mention in their narrative a transaction they do not cite. No version invented a transaction.

## How the agent changed

Each change was made on the dev set and is recorded in `config/agent.yaml`'s history.

1. **v1.0 anchored on the score.** It called the graph tool in 10 of 90 cases and closed citing
   low scores.
2. **v1.1 had to follow the money** (graph and score explanation before concluding, a score is
   no evidence either way). It called all five tools every time and still closed 85 of 90.
   - Checked against the labels, 12 of the 30 patterned alerts are **leaves**: the account takes
     part in one transaction of the attempt, and the pattern shows only at its counterparty.
   - The rank alone separated the dev set better than anything the agent could rebuild.
3. **v1.2 started from the model's call** and could override it on concrete evidence. It
   overrode every escalation it saw (7 of 7, all laundering) with "no pattern, normal activity".
4. **v1.3 made the override one-way.** The model's escalations stand; the agent can only
   escalate an alert the model would close. It did so twice, both clean, each on a single red
   flag.
5. **v1.4 asks for two red flags** to escalate a model close. Neither the pattern of the rule
   that raised the alert nor the account's everyday activity counts. Among the model's closes on
   dev, each red flag shows on more clean alerts than laundering ones. Because this rule was
   shaped on the dev labels, the dev set overstates it.

## Typical errors

- **"I found no pattern" read as "I found an ordinary explanation."** From an account's own
  transactions, most laundering looks ordinary. v1.2 closed 7 of 7 escalations it saw, all of
  them laundering, on "no cycle, fan or relay".
- **Anchoring on the score.** v1.0's narratives cited the low score as the reason to close.
- **The pattern that raised the alert, taken as the evidence.** v1.4's prompt says it does not
  count, yet all three of its golden overrides rest on it: the 23 inflows of an R01 alert (the
  laundering one), and the bursts of payments of two R04 alerts (both clean), which one narrative
  calls "fan-out and high-velocity, two red flags".
- **A common red flag taken as proof.** On dev, v1.3 called a counterparty that pays 7 accounts a
  hub.
- **Wrong typologies.** On the golden set, 21 of the 25 specific typologies named for patterned
  alerts are wrong: `stack` for random and cycle attempts (3 each), `fan_out` for gather-scatter
  (3). It named fan-out right in 3 of 8 cases and fan-in in 1 of 8, and never named cycle,
  bipartite, gather-scatter, scatter-gather, stack or random right. 30 of its 55 escalations of
  patterned alerts say `unclassified`.
- **A narrative that mentions what it does not cite.** Two dev reports failed verification this
  way after their retries, and one golden narrative refers to a hub "not shown in evidence".

## Limits

- **The data is synthetic.** None of these numbers carries over to a real bank.
- **Some typologies are hard to name at alert time.** Attempts spread over days: on the dev set,
  a fan-out attempt had 1–2 of its 7–14 transactions by the alert's cut-off. Many alerted
  accounts are leaves, whose pattern shows only at a counterparty.
- **The sets are small.** With 30 cases per group, one case moves a group's accuracy by 3.3
  points on dev; on the golden set, by 1.7.
- **The agent cannot clear the model's false positives.** Since v1.3 it never closes an alert the
  model escalates, so its decisions differ from the baseline only where it adds escalations.
- **One language model, on one laptop.** A 20-billion-parameter model fits in 16 GB of video
  memory; the larger ones considered (17–24 GB) do not. A second model was planned for the dev
  set and not run: with the model's escalations fixed, it could only have changed the escalations
  the agent adds and the typologies it names. Each case takes about 30–60 seconds.
- **The dev set chose v1.4 knowing its labels.** Its two-flag rule was shaped by counting red
  flags on the dev set's labels. The golden set, run once, is the result to quote.

## Reproduce

```bash
make up            # Redpanda and Ollama (GPU)
make llm           # pull the pinned models
make knowledge     # embed the typology notes
make golden-set    # dev and golden sets into evals/ (needs make train and make holdout)
make eval DETECTOR=baseline SET=dev
make eval DETECTOR=agent SET=dev     # resumable: cases are cached in data/evals/runs/
make eval DETECTOR=agent SET=golden  # once per reported result
```

An agent run takes about 75 minutes on the dev set and 2 hours 20 minutes on the golden set,
on the laptop above. Long runs heat the GPU: on this laptop, a core clock capped at 700 MHz
(`nvidia-smi -lgc 210,700`, run as administrator on Windows) kept it at 70 W instead of 90 W.
