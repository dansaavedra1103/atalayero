# ADR-0024: The investigator agent on the batch's alert queue

- Status: accepted
- Date: 2026-10-08

## Context

- **The agent runs on the case sets** (ADR-0015, ADR-0018): alerts sampled from the validation
  and test queues of the evaluation, explained with the evaluation's models: on validation, the
  champion trained on train; on test, the champion's family refit on train and validation
  (ADR-0014). Its tool server served those alerts only.
- **The daily batch has a queue of its own** (ADR-0020), every day from the first train day,
  scored by the registered champion. `/cases/{alert_id}` (ADR-0022) and the dashboard (ADR-0023)
  show an alert with the agent's investigation if one ran, read so far from where
  `make investigate` keeps the case sets' investigations.
- **The same account-day can be in both**, with the same ID and a different model behind its
  score.
- **The agent stays off the schedule** (ADR-0021): on the laptop's GPU it takes about 45 s an
  alert, and long runs need the GPU's clocks locked (phase 3). It also lost to score-only on the
  golden set by one alert (`docs/agent_eval.md`): its reports help a reviewer read an alert; they
  decide nothing.

## Decision

1. **A batch alert is explained by the model that scored it.** With `ToolBox(..., batch=True)`,
   `explain_score` loads the champion version the day's manifest records, and reads the day's own
   `features.parquet`, which holds no labels. The other tools use only the alert's day and
   account, and do not change. Nothing changes on the case sets, so the evaluation protocol and
   its reports stand.
2. **The tool server takes a day.** `python -m atalayero.mcp_server --day <day>` serves that day's
   queue; without `--day`, the case sets, as before.
3. **`investigate_day(settings, day, top)`** takes the first `top` alerts of the day's queue by
   rank and hands them to the agent as `CaseAlert`s: what it sees on the case sets, without the
   day's phase. It keeps each investigation in `data/batch/days/<day>/investigations/`, written
   whole and moved into place. `make investigate-day DAY=<day> [TOP=<n>]` runs it;
   `batch.investigate_top` (5) is the default.
4. **`/cases` and the dashboard read only those.** The case sets' investigations stay in
   `data/agent/investigations/`: they explain scores the batch did not serve.
5. **A manual DAG, `investigate_alerts`.** No schedule, one run at a time, two hours at most.
   - Parameters: `day`, which must match the days with a queue (2 to 10 Sep 2022), and `top`, from
     1 to 20 (5 by default, as `batch.investigate_top`).
   - They reach the command through its environment, never pasted into it, so a parameter cannot
     inject shell.
   - No end date: a manual run is dated when it is triggered, and a run dated after the end date
     gets no task (the bug of ADR-0021's first retrain trigger).
   - `airflow/tests/check_dags.py` checks all of it, including that the DAG refuses a day without
     a queue, a day with shell in it, and too many alerts.

## Consequences

- **Verified end to end in Docker.** `investigate_alerts` with day 10 Sep and top 3 ran in the
  Airflow container against Ollama on the GPU and succeeded in 8 minutes:
  - every tool answered inside the container, `search_typologies` included, and `explain_score`
    gave each alert the score the batch had given it (0.997182 for the first);
  - all three reports passed verification, with no invented transaction; each investigation took
    144–189 s, pauses included;
  - `/cases` showed the three investigations and none on the fourth alert.
  - In hindsight, the three account-days hold scatter-gather laundering. The agent escalated
    all three, as the score alone would, and named the typology wrong each time (fan-in twice,
    cycle once), as on the golden set (`docs/agent_eval.md`).
- **The GPU sets the pace.** With its clocks locked at 210–700 MHz, the laptop's GPU went from 77
  to 84 °C in 30 s of generation. The run went through only because a watcher outside the repository
  paused the agent's process at 80 °C and resumed it at 70 °C, six times. Docker Desktop runs
  containers in their own VM, so the watcher signals the process through `docker exec`.
- **Ranks mean the same as on the case sets**: the batch ranks a day's account-days as the
  evaluation does, so the agent's `model_call_rank` rule (ADR-0019) holds.
- **Train days can be investigated**, which the case sets never hold; their scores are in-sample,
  as the dashboard says of their KPIs.
- **A day run again keeps its investigations.** An alert still in its queue keeps an
  investigation that may explain an earlier champion's score. That needs a retrain to promote a
  new champion first, and on this dataset none does (ADR-0021).
- **Ollama must be up**, with the pinned models pulled and the typology index built (`make llm`,
  `make knowledge`), and the GPU's clocks locked before a long run.
