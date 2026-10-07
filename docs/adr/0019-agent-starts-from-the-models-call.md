# ADR-0019: The agent starts from the model's call

- Status: accepted
- Date: 2026-10-07

## Context

- **Two versions of the agent lost to the score-only baseline on the dev set** (ADR-0015, primary
  metric = mean decision accuracy over the three groups):

  | Detector | Patterned | Untyped | Clean | Primary |
  | --- | --- | --- | --- | --- |
  | Score-only baseline | 97% | 70% | 77% | 81.1% |
  | Agent v1.0 | 7% | 0% | 97% | 34.4% |
  | Agent v1.1 | 13% | 0% | 97% | 36.7% |

  - **v1.0** called the graph tool in 10 of 90 cases, and closed citing low scores.
  - **v1.1** had to look at the graph and the score explanation before concluding, and was told
    that a score is no evidence either way. It called the five tools in every case and still
    closed 85 of 90.
  - **Both** wrote reports that passed verification every time, with no invented transaction.
- **From the alerted account, a pattern rarely shows.** We checked the 30 patterned dev alerts
  against their labels. This was an analysis only; the agent never sees a label.
  - In 12 of them, the account is a **leaf** of its attempt: it takes part in one of its
    transactions. The pattern shows at the counterparty, the hub.
  - **Fan-out attempts spread over days.** By the alert's cut-off, 1–2 of their 7–14
    transactions exist. No investigator can name that typology at alert time.
- **The rank alone separates the dev set:**

  | Group | Rank ≤ 1,133 | Rank > 1,133 |
  | --- | --- | --- |
  | Patterned | 29 | 1 |
  | Untyped | 21 | 9 |
  | Clean | 7 | 23 |

  The model scores with features of the whole graph (ADR-0008, ADR-0011), more than the agent
  can rebuild in a dozen tool calls. v1.1 told the agent to set that signal aside.
- **v1.2 was told the model's call, and overrode it to close.** Its prompt let it close an alert
  the model would escalate when it could state an ordinary explanation.
  - It closed all 11 dev cases it reached before the run stopped (the laptop powered off).
  - The model called 7 of them to escalate, and all 7 were laundering. The agent closed each one
    on "no cycle, fan or relay, consistent with normal activity", once with "a short history".
  - It got 2 of the 11 right; the model's call alone gets 9. We stopped there rather than spend
    the other 79 cases on it.
  - The language model takes "I found no pattern" for "I found an ordinary explanation". From an
    account's own tools, no pattern is what most laundering looks like (above).
- **ADR-0018 ruled that no prompt states the baseline's threshold**, so that the agent would
  decide on its own. The dev results show that this leaves the agent with less information than
  the baseline it is compared with.

## Decision

1. **The agent starts from the model's call** (`config/agent.yaml`, version 1.2).
   - `model_call_rank` is a new config field. The system prompt says the model escalates the
     account-days ranked within that many of their day and closes the rest. The triage gives the
     model's call on the alert at hand.
2. **The agent can only overturn the model's call upwards** (version 1.3, after v1.2's run).
   - **When the model escalates, the escalation stands.** The draft's JSON schema allows no other
     decision, and the agent checks it again: a report that closes goes back to investigation. The
     agent names the typology and the evidence, and writes the narrative.
   - **When the model closes, the agent escalates on a red flag:** in and out on the same day,
     split among or gathered from several counterparties, a counterparty that pays or is paid by
     many accounts, money that comes back, accounts that only relay. Without one, the close
     stands.
3. **`model_call_rank` is the score-only baseline's threshold, fit on the dev set (1,133).**
   - A test checks that the two agree. A refit (a new champion model, a new dev set) cannot leave
     the agent on a stale threshold, and changing it bumps the config version.
   - This replaces ADR-0018's rule that no prompt states the baseline's threshold. Its rule
     against other statistics of the dataset in the prompts stands.
4. **`get_graph_neighborhood` shows each counterparty's reach.** This updates ADR-0016.
   - Every listed counterparty comes with how many accounts it pays and how many pay it in the
     tool's window (`accounts_it_pays`, `accounts_paying_it`), from transactions before the
     cut-off.
   - A leaf can then see the hub behind its one payment. On the full warehouse the call takes
     about 0.14 s.

## Consequences

- **The agent is measured as the baseline plus an investigation.**
  - An agent that never changes the model's call scores exactly the baseline. Every point above
    or below it comes from the investigation.
  - On the dev set, the room is in 10 cases: 9 untyped alerts and 1 patterned alert beyond the
    threshold, to escalate. The ceiling is a primary metric of 92.2% (every escalation right,
    the 7 clean alerts within the threshold still escalated). Every clean alert beyond the
    threshold that the agent escalates costs a point.
- **The agent cannot clear the model's false positives.** Closing alerts is what an investigator
  saves a team in practice; this agent does not, until a version shows on the dev set that it
  closes clean alerts without closing laundering. Its value lies in the escalations it adds, and
  in naming and documenting the escalations it keeps.
- **The comparison stays fair.**
  - The agent gets what the baseline uses: the alert's rank and a threshold fit on the dev set,
    where ADR-0015 makes every choice.
  - The golden set remains untouched until the single reported run.
- **Some typologies cannot be named at alert time.** Fan-out's transactions mostly come after
  the cut-off, which caps typology accuracy. `docs/agent_eval.md` will say so.
- **The counterparty reach is often a weak signal here.** In the dev cases checked, the
  counterparties of patterned leaves paid 0–8 accounts on the alert's day, because attempts
  spread over days.
- **A new champion model means a new threshold.** Refitting the baseline on the dev set changes
  `model_call_rank`, and the test above fails until the config follows.
