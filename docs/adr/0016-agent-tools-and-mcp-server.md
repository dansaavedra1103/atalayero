# ADR-0016: The investigator's tools and their MCP server

- Status: accepted; `get_graph_neighborhood` extended by ADR-0019 (each counterparty's reach)
- Date: 2026-10-06

## Context

- The investigator agent works through its own MCP server (design.md §4), with five tools:
  - `get_account_profile`;
  - `get_transactions(account, window)`;
  - `get_graph_neighborhood(account, hops)`;
  - `explain_score` (SHAP);
  - `search_typologies` (FAISS).
- An alert is an account-day, reviewed once its day is over (ADR-0007). The case sets hold
  alerts of validation and test (ADR-0015).
- The tools must respect the project's rules:
  - **never expose a label** (design.md §5);
  - **never let an investigation see data from after the alert's day** (CLAUDE.md rule 3);
  - **keep business logic out of the MCP layer** (rule 4).
- A local LLM has a limited context. The accounts of the case sets hold a median of 3
  transactions a day, but a counterparty the agent wants to look at can be far busier.
- The MCP Python SDK is at version 2: `FastMCP` became `MCPServer`, and `Client(server)` connects
  in process, which the tests use.

## Decision

1. **All the logic lives in `agent/tools.py`**, in a `ToolBox` over the alerts it may be asked
   about. `mcp_server/server.py` only wraps each method as a tool and serves them over stdio
   (`python -m atalayero.mcp_server`). This PR adds four tools; `search_typologies` comes with
   the knowledge base (next ADR).
2. **The cut-off comes from the alert, not from the caller.** Every tool takes an alert ID, and
   the server derives the cut-off from it: the end of the alert's day. The model can look at any
   account (the alerted one by default, or a counterparty), but only before that cut-off. A
   look-back is at most 7 days.
3. **No labels anywhere.**
   - The tools read only `marts.fct_transactions`, the feature files and the models.
   - `models/data.py` gains `load_features`, which reads features by transaction ID without
     joining the labels.
   - A test fails if the source of `agent/` or `mcp_server/` names a label table, the patterns
     table or the answers files, or if any output key mentions a label.
4. **Outputs are capped:** at most 50 transactions, 15 counterparties per hop and 10 cycles. The
   totals always cover everything, so the model knows when it sees only part.
5. **The four tools:**
   - `get_account_profile`: an account's activity over its history and on the alert's day (count,
     USD, distinct counterparties); when it was first and last seen; its self-transfers, payment
     formats and currencies.
   - `get_transactions(days, limit)`: rows newest first, with direction, counterparty, USD and
     native amounts, and payment format.
   - `get_graph_neighborhood(hops, days)`: the counterparties with the money each way and,
     with `hops=2`, the accounts they deal with. It also lists **cycles in time order** that
     bring money back to the account: A→B→A and A→B→C→A.
   - `explain_score`: the account-day's three top-scored transactions, each with the six
     features that moved its score most. It uses **LightGBM's own SHAP values**
     (`pred_contrib=True`), in log-odds, so the `shap` library is not needed.
6. **`explain_score` uses the model behind the alert's score:**
   - on validation, the champion, trained on train alone;
   - on test, the champion's family refit for the test run (ADR-0014). The new
     `Registry.load_holdout_model` finds it by its MLflow run name.

   It refuses a model that is not LightGBM.
7. **Errors reach the model as messages.** A bad request, such as an unknown alert, raises a
   `ToolError` with its reason, rather than a generic failure.
8. **New runtime dependency: `mcp`** (2.3).

## Consequences

- **On real alerts of the dev set**, each tool answers in 0.04–0.16 s and returns under 3 KB of
  JSON. `explain_score` takes 1.5 s on its first call, while it loads the model.
- **Tests enforce the contract:**
  - removing every transaction from the cut-off on changes no output;
  - shifting the cut-off by two days makes those tests fail;
  - explaining a test alert with the champion instead of the refit model fails the
    score-consistency test;
  - the explained scores match the ones the alerts were built from: the holdout scores on test
    and the champion's on validation.
- **A rule's false positive can now be read.** On a dev case where R03 fired, the
  neighbourhood shows the round trip of funds that triggered it. The agent can weigh that
  against the score.
- **The second hop also lists counterparties that deal with each other.** That is how a
  money-moving ring around the account shows up.
- **The MCP server serves the alerts of both case sets, never their answers.**
