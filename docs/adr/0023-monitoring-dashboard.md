# ADR-0023: The monitoring dashboard

- Status: accepted
- Date: 2026-10-08

## Context

- **design.md §6 asks for a local dashboard** over the KPI tables in DuckDB: alerts a day, false
  positives, detection, performance per rule, and the mix of typologies.
- **The KPIs exist** in the serving database the batch publishes (ADR-0020), with readers in
  `atalayero.monitoring.kpis` and `atalayero.batch.queries`.
- **Interfaces stay thin** (CLAUDE.md, rule 4): the dashboard may only call the package.
- **Every outcome is hindsight.** False positives and detection come from the dataset's labels,
  and train days were scored by a model that saw them.
- **Streamlit sends usage statistics** to its makers unless told not to, and has no login of its
  own.

## Decision

1. **Streamlit 1.64** (`dashboard/app.py`), new runtime dependency. It shows, for the phases
   chosen (test by default):
   - four headline numbers: days, alerts a day, the share of alerts with no laundering, and the
     share of laundering detected;
   - the queue a day by source (model only, rules only, both), and detection and false
     positives a day, on one axis of shares;
   - each rule's alerts, the share of its account-days that hold laundering, and the share that
     reach the queue (the rest fall on hubs);
   - detection by typology;
   - drift against train on validation and test days;
   - the alert queue of a day, and for a chosen alert, every transaction of its account-day and
     the agent's investigation if one ran.
2. **Thin.** Every number comes from the package. Rates over several days are pooled (counts
   summed, then divided) by `kpis.summary`, `queue_sources`, `rule_totals` and
   `typology_totals`, never averaged in the page. Data is cached a minute, keyed by the serving
   database's path, as the batch may publish again.
3. **Honest framing.** A caption under the title says the data is synthetic, that the outcomes
   are hindsight, and that train days are in-sample. The phase selector starts on test.
4. **Colour by role.** Series take the first three slots of the reference categorical palette,
   in fixed order, with their dark-mode steps when the viewer's theme is dark. Text keeps the
   theme's ink. A single series takes no legend.
5. **Nothing leaves the machine.** `.streamlit/config.toml` turns usage statistics off. It keeps
   XSRF protection on, takes no uploads, watches no files, shows viewers no developer menu, and
   shows no error details in the browser.
6. **The container** reuses the API's image (the same environment, from the build cache). It
   listens on `127.0.0.1:8501` and mounts the repository read-only, with a read-only root and a
   tmpfs for `/tmp` and Streamlit's home. It drops every capability, forbids new privileges, and
   runs under memory, CPU and process limits, as the API does (ADR-0022). `make dashboard` runs
   it outside Docker.
7. **Tests** run the page with Streamlit's `AppTest` against a published fixture. They check the
   headline numbers, the three charts, the phase selector, and the message shown before any
   data is published.

## Consequences

- **No login.** Anyone who can reach the laptop's localhost sees the dashboard. Beyond it, the
  gateway would have to front it with authentication.
- **The palette was not re-validated here**: the validator needs Node, which this machine lacks.
  The three slots are the ones the reference palette documents as validated together, in both
  modes.
- **The page reads only the serving database**, so it never blocks the batch, and it shows
  nothing until the batch has published.
