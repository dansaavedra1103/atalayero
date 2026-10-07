# ADR-0018: The investigator agent

- Status: accepted; the rule that no prompt states the baseline's threshold (decision 6) superseded by ADR-0019
- Date: 2026-10-06

## Context

- **What design.md §4 asks for:** an agent that receives an alert and writes a structured case
  report, in four steps (triage, investigation through MCP tools, drafting, and a grounding
  check that sends it back when a cited transaction does not exist or an amount is wrong). It
  runs on local models only.
- **What earlier ADRs fixed:**
  - the protocol and the case sets (ADR-0015);
  - the five tools behind an MCP server (ADR-0016, ADR-0017);
  - Ollama on the GPU (ADR-0017).
- **A local model is less reliable than a hosted one.** It can call a tool that does not exist,
  pass the wrong alert, loop on tools, write JSON that does not parse, or cite transactions it
  never saw. The agent has to contain each of those, and the evaluation has to count them.
- **Everything else in the project is versioned**: the rules, the model hyperparameters, the
  case sets. An evaluation is only meaningful for a known model and known prompts.

## Decision

1. **A LangGraph graph of four nodes** (`agent/graph.py`):
   - **triage:** reads the alert and writes a plan, without tools;
   - **investigate:** calls tools until the model stops calling them or spends `max_steps`
     tool calls. Calls past the budget are answered "not run";
   - **draft:** writes the report as JSON, with Ollama's structured output constrained to the
     `CaseReport` schema minus the alert ID, which the agent adds itself;
   - **ground:** verifies the report. A report that fails goes back to investigation with the
     list of problems, at most `max_grounding_retries` times. After that, it is kept and
     marked as not grounded.
2. **The grounding check is deterministic code** (`agent/grounding.py`), with no language model.
   A report is grounded when:
   - every cited transaction exists, happened before the alert's cut-off, and appeared in a tool
     output (or in the alert itself);
   - every cited amount is within 1% of the real one;
   - the narrative mentions no transaction (`#<id>`) it does not cite;
   - an escalation cites at least one transaction.

   A cited transaction that does not exist, falls after the cut-off or was never shown counts as
   **hallucinated**. Verification reads the warehouse directly, through `ToolBox.lookup`, which
   is not a tool.
3. **The alert ID never goes through the model.** Tools are shown to the model without their
   `alert_id` parameter, and the agent adds it to every call, overriding any the model passes.
   An investigation cannot drift to another alert, nor past its cut-off.
4. **Tools go through MCP.** `make investigate` starts the MCP server as a subprocess over stdio
   (`python -m atalayero.mcp_server`). The tests connect the same server in process.
5. **`config/agent.yaml` is versioned like the rules.** Any change to the model, its options,
   the limits or the prompts bumps `version` and adds a `history` entry. The model must be
   pinned by digest in `config/settings.yaml`, or the agent refuses to run. Every investigation
   records the config version and a checksum of the prompts. Version 1.0:
   - **model:** `gpt-oss:20b`, the model chosen for this first version;
   - **reasoning effort:** low;
   - **options:** temperature 0, seed 0, a context of 16,384 tokens;
   - **limits:** 12 tool calls and 2 grounding retries.
6. **Prompts** (`agent/prompts/`), in English:
   - `investigate` (the system prompt) explains the alert, the tools, how to investigate and
     what can be concluded. The rules' descriptions are read from `config/rules`, so the prompt
     cannot drift from them;
   - `triage` and `draft` frame those two steps.

   **No prompt states a statistic of the dataset, or the baseline's threshold.**
7. **A failure is a result, not an exception.** An investigation returns its report (or none),
   its grounding, its steps, model calls, retries, tokens and time, and its transcript. A crash
   (Ollama down, a broken reply) sets `error` and keeps the transcript so far.
8. **New runtime dependency: `langgraph`** (1.2).

## Consequences

- **The mechanics work with the real model.** A smoke test ran `make investigate` on one dev
  case of each group, with `gpt-oss:20b` pinned (`17052f91…`). This is not an evaluation; that
  is the next step, on the whole dev set.

  | Case | Expected | Agent | Tool calls | Model calls | Retries | Grounded | Time |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | Patterned (fan-out), rank 362 | escalate | close | 3 | 6 | 0 | yes | 126 s, cold start |
  | Clean, raised by R03 | close | close | 4 | 7 | 0 | yes | 35 s |
  | Untyped, rank 1,990 | escalate | close | 3 | 8 | 1 | yes | 34 s |

  - The agent plans, calls the tools it needs (passing a counterparty when it wants one),
    searches the typology notes and writes a report that passes verification.
  - None of the three reports cites a transaction it was not shown.
  - In the untyped case, the first draft came back empty and failed to parse. The retry
    recovered it.
- **The first version closes everything.** In the fan-out case it found the right note
  ("one account sends money to many…") and still closed, relying on what it called a low score.
  A detector that decides the same way on every case scores at most 33% on the primary metric
  (ADR-0015).
  - That is what the dev-set iterations are for: the prompts and, if needed, the model.
  - Every change bumps `config/agent.yaml`.
- **The model fits on the GPU.** `gpt-oss:20b` takes 12.7 GB of VRAM with a 16k context, and
  the embedding model stays on the CPU (ADR-0017).
- **Cost.** An investigation takes about 35 s warm and 15–20k tokens. That puts the 90 dev cases
  at about an hour and the 180 golden cases at about two.
- **Pulling the model took 45 minutes** (14 GB). It happens once, into the Ollama volume.
