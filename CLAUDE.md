# Atalayero

Transaction-monitoring system for AML/fraud: versioned rules + ML models + an LLM
investigator agent whose quality is measured with offline evals. Public portfolio
project. Full design: `docs/design.md` — read it before planning work on a new phase.

## Language
- Talk to me in Spanish.
- Write code, comments, docstrings, commit messages and docs in English.
  Identifiers are English too (YAML keys, enum values, field names).
  Only exception: `docs/design.md`, the original design, stays in Spanish.

## Current status
- Current phase: **1 — Data** (batch ingestion, streaming replay, dbt models).
- Phases: 1 Data · 2 Rules & models · 3 Agent & evals · 4 Product & ops.
- Do not start work from a later phase unless I ask. I update this section when a phase closes.

## Stack
Python 3.11 · uv · DuckDB · dbt · Redpanda (Kafka API)
· scikit-learn · LightGBM · networkx · MLflow (local) · LangGraph · MCP Python SDK · FAISS
· local LLMs: Ollama (default) or Hugging Face models · FastAPI · Streamlit (dashboard)
· Airflow (local, Docker) · Docker Compose.
Everything runs locally: no cloud services.
Dev environment: WSL2 (Ubuntu) on Windows. Run everything from WSL, never from PowerShell.

## Commands
All commands go through the Makefile. When you add a new command, add it to the Makefile AND to this list.
- `make setup`    — install dependencies with uv, install pre-commit hooks
- `make check`    — ruff format --check + ruff lint + pytest
- `make ingest`   — download the dataset (SHA-256 verified) and load DuckDB `raw.transactions` (via Parquet) and `raw.laundering_attempts`
- `make fixture`  — regenerate the story-based test fixtures from the downloaded dataset
- `make fx-rates` — regenerate the exchange-rate seed `dbt/seeds/fx_rates_usd.csv` from the full dataset
- `make dbt`      — dbt build on the full dataset (needs `make ingest`)
- `make dbt-sample` — load the test fixtures into `data/sample/` and dbt build on them (what CI runs)
- `make pipeline` — end-to-end batch run on the dev target
- `make up`       — docker compose up and wait until healthy (phase 1: redpanda; later api, mlflow, airflow, ollama)
- `make down`     — docker compose down
- `make stream`   — replay the transactions through Redpanda and evaluate the YAML rules online; alerts to `data/stream/alerts/` (needs `make up` and `make dbt`)
- `make test-integration` — tests that need running services (Redpanda); not run in CI
- `make eval`     — run agent evals offline and write a dated report to evals/reports/

## Repo map
- `src/atalayero/` — ALL business logic: ingestion, streaming, features, rules, models, monitoring, agent, knowledge, api
- `src/atalayero/mcp_server/` — MCP tools for the agent (thin wrappers over the package)
- `dbt/` — staging → intermediate → marts; custom tests in `dbt/tests/`
- `config/rules/*.yaml` — rule definitions (source of truth for thresholds)
- `airflow/dags/` — orchestration only, no business logic
- `evals/` — golden set, eval runner, versioned reports
- `docs/adr/` — architecture decision records

## Non-negotiable rules
1. **No real data, ever.** Only the public synthetic IBM AML dataset. Never add data from employers or any real person.
2. **Zero cost, local only.** No paid or cloud services. DuckDB is the only warehouse; LLMs run locally (Ollama by default, or Hugging Face models). GitHub Actions CI is the only hosted service.
3. **No temporal leakage.** A feature for a transaction at time t uses only data with timestamp < t — graph features included. Train/test splits are temporal, never random.
4. **Thin interfaces.** FastAPI routers, MCP tools, Airflow DAGs and the Streamlit dashboard only call functions from `src/atalayero/`. Never duplicate logic in them.
5. **Rules are config.** Thresholds live in `config/rules/*.yaml`, never hardcoded in Python. Changing a rule = bump `version` + add a `history` entry with the reason.
6. **Shared schemas.** Pydantic models (`Transaction`, `CaseReport`, …) are defined once and reused by API, agent and tests.
7. **Always compare against a baseline.** Models vs rules-only; agent vs score-only. If something loses to its baseline, report it — don't hide it.
8. **Never commit** `data/`, `mlruns/`, `.env`, credentials (e.g. Kaggle token) or model artifacts.
9. **LLM evals run offline** with `make eval`. CI only validates the schema of the latest report.

## Workflow
- One feature = one branch = one PR. Branch names: `feat/…`, `fix/…`, `docs/…`, `chore/…`.
- For any non-trivial task: propose a short plan first and wait for my OK before editing files.
- Write or update tests together with the code. Run `make check` (and `make dbt` if dbt changed) before telling me a task is done.
- Commits follow Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`).
- New runtime dependency, schema change or change to the evaluation protocol → new ADR in `docs/adr/NNNN-short-title.md` (Context, Decision, Consequences).
  Dev-only tools (linters, test runners, pre-commit hooks) don't need an ADR.
- Keep diffs small and focused. Do not refactor unrelated code.
- If something in this file is wrong, outdated or missing, tell me instead of working around it.

## Code style
- Type hints everywhere; `ruff format` + `ruff check`.
- `logging`, not `print`.
- Configuration via `config/settings.yaml` + environment variables (pydantic-settings).
- Tests use small fixtures in `tests/fixtures/` (≈1,000 rows), never the full dataset.

## Gotchas
- The pipeline must always be rebuildable from scratch with one command; `data/` is disposable.
- The dataset is synthetic: docs must not claim real-world performance.
- Airflow runs only inside Docker on WSL.
