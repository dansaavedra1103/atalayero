# ADR-0021: Airflow runs the schedules

- Status: accepted
- Date: 2026-10-08

## Context

- **Phase 4 schedules the system** (design.md §6): a daily batch, a weekly retraining that only
  promotes a better model, and drift monitoring that can trigger it.
- **The pieces exist as commands:** a day of the batch, the KPIs in dbt and the publication
  (ADR-0020); the features and the training (ADR-0008, ADR-0009); each day's drift verdict.
- **Airflow runs only inside Docker on WSL** (CLAUDE.md). The latest release is 3.3.2. Airflow 3
  splits the scheduler, a DAG processor, a triggerer and an API server, and its default auth
  manager is the simple one.
- **Airflow pins hundreds of packages**, through its constraint files. The project pins its own
  in `uv.lock`. One environment for both would bend one set of pins or the other.
- **MLflow recorded absolute paths.** The registry stores each model's artifacts under the
  repository's absolute path on the host.
- **The machine is one laptop**, with about 15 GB of RAM for WSL and Docker together.

## Decision

1. **One Airflow container, `airflow standalone`, with Postgres for the metadata.**
   - Standalone runs the four components in one container, with the LocalExecutor. Postgres 16
     holds the metadata, in a named volume.
   - At most two tasks run at once (`AIRFLOW__CORE__PARALLELISM`): a batch day takes a few GB.
   - The UI and the REST API listen on `127.0.0.1:8080` only.
   - **One admin user**, from the simple auth manager. Airflow generates its password into
     `data/airflow/passwords.json`.
   - **Secrets** (the Postgres password, the API's JWT secret, the Fernet key) are generated once
     by `make up` into `.env`, readable by its owner only, and never committed. `.env.example`
     lists them.
2. **Two Pythons in the image.** The image extends `apache/airflow:3.3.2-python3.11`, and builds
   the project's own environment beside Airflow's, at `/opt/atalayero/venv`, from `uv.lock`
   (`uv sync --locked --no-dev`). It also installs the OpenMP runtime LightGBM needs.
   - **Tasks only run the package's commands** with that environment: BashOperator tasks call
     `python -m atalayero.…` or `dbt`. The DAG files import nothing from `atalayero`, and hold no
     logic (CLAUDE.md, rule 4).
3. **The repository is mounted at the same path as on the host** (`ATALAYERO_ROOT`), so the paths
   MLflow recorded resolve inside the container.
   - It is mounted read-only, except `data/`. dbt writes its target and logs to `/tmp`.
   - The container runs as the host user (`AIRFLOW_UID`), so whatever it writes under `data/`
     stays the user's.
   - The code is not in the image, only the dependencies: a change to `src/` needs no rebuild; a
     change to `uv.lock` does.
4. **Three DAGs replay the simulation** (`airflow/dags/`):
   - **`daily_batch`**: daily from 1 to 10 Sep 2022, catching up, one run at a time. Its tasks
     are `run_day` → `build_kpis` → `publish`. `run_day` waits for the day before it to succeed.
   - **`drift_monitoring`**: on the same days. It waits for the day's `publish`, then asks
     `python -m atalayero.batch drifted <day>`. That command exits 0 if the day drifted and 3
     if not, and on 3 the trigger of `weekly_retrain` is skipped.
   - **`weekly_retrain`**: weekly, which is one run on 4 Sep, plus every drift trigger. It
     rebuilds the features (with two graph workers instead of four) and runs the training.
     Training promotes a model only if it beats the champion's validation metric (ADR-0009).
   - Airflow 3 schedules a cron with `CronTriggerTimetable`, whose logical date is the moment of
     the trigger. `{{ ds }}` is therefore the simulated day itself, 1 to 10 Sep.
5. **The DAGs are checked in Airflow's own Python.** `airflow/tests/check_dags.py` loads them
   with `DagBag`. It checks that they import without errors, cover the simulation one run at a
   time, run only the package's commands, and keep the order and the waits above. A second check
   imports the package with the image's project environment.
   - CI builds the image and runs both (job `airflow`); `make test-airflow` runs them locally.
   - Airflow is not a dependency of the project, so these checks are a script, not tests in
     `make check`.
6. **The agent stays off the schedule.** A manual `investigate_alerts` DAG comes in its own
   change: the agent first has to take the batch's alerts and explain them with the model that
   scored them.

## Consequences

- **`make up` builds the image the first time**, which takes several minutes. It then starts
  Airflow and Postgres beside Redpanda and Ollama.
- **The schedules show the mechanism, not new data.** Nothing new arrives, and the splits are
  fixed (ADR-0005). A retrain finds the same champion and promotes nothing. The drift on 10 Sep
  triggers a retrain that changes nothing.
- **Standalone suits one machine.** Spreading Airflow over several would take the separate
  services of Airflow's reference Compose file, and a shared executor.
- **The containers are tied to the host's layout** through the same-path mount. `data/` stays
  disposable.
- **Only Airflow is limited to localhost.** Redpanda and Ollama still publish their ports on
  every interface, as before this change.
