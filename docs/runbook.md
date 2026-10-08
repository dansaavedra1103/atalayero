# Runbook

How to build, run and look after Atalayero on one machine. Every command goes through the
Makefile; [`CLAUDE.md`](../CLAUDE.md) lists them all. The design behind each step is in the
[ADRs](adr/) and in [`architecture.md`](architecture.md).

## Requirements

- **Windows with WSL2 (Ubuntu)**, and Docker Desktop with WSL integration. Run everything from WSL,
  never from PowerShell. About 15 GB of RAM for WSL and Docker together.
- **uv** and Python 3.11; LightGBM needs `sudo apt-get install libgomp1`.
- **An NVIDIA GPU with 16 GB** for the investigator agent (`gpt-oss:20b` on Ollama). Everything
  else runs on the CPU.
- About 5 GB of free disk for `data/` (3.8 GB after a full run, 1.3 GB of it the batch), plus the
  Docker images (Airflow 6.6 GB, the API and dashboard image 2.2 GB) and the models (about 14 GB).

## From scratch

```bash
make setup      # dependencies, pre-commit hooks
make pipeline   # download → dbt → rules → features → evaluate → train → drift → holdout (~17 min)
make up         # Redpanda, Ollama, Airflow, the API behind its gateway, the dashboard
make llm        # pull the pinned models into Ollama and check their digests
make knowledge  # embed the typology notes into the FAISS index
```

`data/` is disposable: deleting it and running these again rebuilds everything. `make up` writes
`.env` the first time (secrets, readable by you only, never committed) and builds the images,
which takes several minutes.

Then fill the batch, either way:

- **Airflow** (http://localhost:8080, user `admin`, password in `data/airflow/passwords.json`):
  DAGs start paused. Unpause `daily_batch` and `drift_monitoring`; they catch up over 1–10 Sep,
  one day at a time. Unpause `weekly_retrain` after that if you want the retrains to run.
- **Outside Airflow:** `make replay` (about 15 min). Do not run both at once.

## Every day

| What | Where |
| --- | --- |
| Alert queue, cases, scores | API: `curl -H "X-API-Key: $KEY" http://localhost:8000/alerts`, with `KEY` from `ATALAYERO_API_KEY` in `.env` |
| KPIs, rules, typologies, drift, the queue | Dashboard: http://localhost:8501 (test days by default) |
| Runs, logs, retries | Airflow: http://localhost:8080 |
| Experiments, registered models | `make mlflow-ui`, then http://localhost:5000 |

**Investigating alerts.** Trigger `investigate_alerts` in Airflow with a `day` (2–10 Sep) and
`top` (1–20), or run `make investigate-day DAY=2022-09-10 TOP=3`. Each investigation shows up in
`/cases/{id}` and in the dashboard's alert view. Count on 45 s to a few minutes an alert,
depending on the GPU's clocks. **Lock the GPU's clocks first** (see below).

**Health checks.**

```bash
curl -s localhost:8080/api/v2/monitor/health    # Airflow
curl -s localhost:8000/health                   # gateway → API; says whether data is published
curl -s localhost:8501/_stcore/health           # dashboard
curl -s localhost:11434/api/ps                  # Ollama: the model loaded, if any
```

## Changes

- **A rule:** edit `config/rules/<rule>.yaml`, bump `version`, add a `history` entry with the
  reason, then `make rules` and `make evaluate`; replay the batch to see it in the KPIs.
- **Model hyperparameters:** `make tune` writes a new version of `config/models.yaml`; `make train`
  promotes a model only if it beats the champion on validation.
- **The agent:** any change to its model, options or prompts bumps `version` in
  `config/agent.yaml`. Measure it with `make eval DETECTOR=agent` on the dev set; the golden set
  is for a reported result only, once per version (ADR-0015).
- **Code:** Airflow runs the repository as it is on disk. Do not switch branches or leave a
  half-made change while a DAG runs.

## When something goes wrong

| Symptom | Cause and fix |
| --- | --- |
| The laptop powers off during an agent run | The GPU overheats under sustained load. From an admin PowerShell: `nvidia-smi -lgc 210,700` (undo with `nvidia-smi -rgc`). The lock is lost on every Windows reboot: apply it again before each long run. Even locked, the GPU can climb from 77 to 84 °C in 30 s: watch `nvidia-smi` and pause the agent's process when it passes about 80 °C (ADR-0024). |
| A `daily_batch` day failed | Later days wait for it (`depends_on_past`). Read the task log, fix, clear the task: running a day again replaces its files. |
| The API answers 503 | No serving database yet: run the batch. The 503 can also come from load shedding (4 queries at once); retry after a second. |
| The API answers 401 or 429 | A missing or wrong `X-API-Key`; or a rate limit (60 a minute per key, 10 a second per client at the gateway). Wait for `Retry-After`. |
| DuckDB says the database is locked | Two writers: `make dbt`, `make pipeline` or `make replay` while Airflow's batch runs. Let one finish. |
| The agent fails on the model's digest | Ollama holds another build of the model: `make llm`. |
| The agent cannot reach Ollama | `make up` (the `ollama` service), then `curl localhost:11434/api/tags`. |
| `make up`: "ports are not available" for 11434 | Another Ollama, installed on Windows, holds the port. Quit it from its tray icon and turn off its start at login, then `make up` again. |
| The dashboard says no data is published | Run the batch; the dashboard reads only `data/serving.duckdb`. |

## Secrets

- `.env` holds the Airflow database password, its Fernet and JWT secrets, and the API key with its
  SHA-256 (the API only ever sees the digest). `make env` adds what is missing and never changes a
  value.
- **Rotating the API key:** delete `ATALAYERO_API_KEY` and `ATALAYERO_API_KEY_SHA256` from `.env`,
  then `make up`: a new key is generated and the API is recreated with its digest.
- Nothing in `.env`, `data/` or `mlruns/` is ever committed.

## Stopping

`make down` stops every service. Data stays in `data/`; Airflow's metadata stays in its Postgres
volume.
