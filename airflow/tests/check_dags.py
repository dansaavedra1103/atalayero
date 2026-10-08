"""The DAGs load in Airflow's own Python and keep the shape ADR-0021 describes.

Airflow is not a dependency of the project, so this is a script for Airflow's Python, not a pytest
module: `make test-airflow` runs it in the image, and CI too.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DAGS = REPO / "airflow" / "dags"
os.environ.setdefault("ATALAYERO_ROOT", str(REPO))
sys.path.insert(0, str(DAGS))  # as PYTHONPATH does in the container, for atalayero_commands

from airflow.dag_processing.dagbag import DagBag  # noqa: E402 (needs ATALAYERO_ROOT first)

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


bag = DagBag(dag_folder=str(DAGS))
check(not bag.import_errors, f"import errors: {bag.import_errors}")
check(
    set(bag.dag_ids) == {"daily_batch", "drift_monitoring", "weekly_retrain"},
    f"unexpected DAGs: {sorted(bag.dag_ids)}",
)
simulation = (datetime(2022, 9, 1), datetime(2022, 9, 10))
for dag in bag.dags.values():
    check(dag.catchup, f"{dag.dag_id} must catch up over the simulation")
    check(dag.max_active_runs == 1, f"{dag.dag_id} must run one day at a time")
    dates = (dag.start_date.replace(tzinfo=None), dag.end_date.replace(tzinfo=None))
    check(dates == simulation, f"{dag.dag_id} must cover 1–10 Sep 2022, not {dates}")
    for task in dag.tasks:
        command = getattr(task, "bash_command", None)
        if command is not None:  # only the package's commands, with the project's environment
            check(
                re.match(r"/opt/atalayero/venv/bin/(python -m atalayero\.|dbt )", command)
                is not None,
                f"{dag.dag_id}.{task.task_id} runs something else: {command}",
            )
            check(task.cwd == str(REPO), f"{dag.dag_id}.{task.task_id} must run in the repo")

batch = bag.dags.get("daily_batch")
if batch is not None:
    order = [t.task_id for t in batch.topological_sort()]
    check(order == ["run_day", "build_kpis", "publish"], f"daily_batch order: {order}")
    check(batch.get_task("run_day").depends_on_past, "a day must wait for the day before it")
    check("tag:batch" in batch.get_task("build_kpis").bash_command, "the KPIs are tag:batch")

drift = bag.dags.get("drift_monitoring")
if drift is not None:
    sensor = drift.get_task("wait_for_batch")
    check(
        (sensor.external_dag_id, sensor.external_task_id) == ("daily_batch", "publish"),
        "drift waits for the day's publication",
    )
    cli = (REPO / "src" / "atalayero" / "batch" / "__main__.py").read_text()
    no_drift = int(re.search(r"^NO_DRIFT = (\d+)", cli, re.M).group(1))
    check(
        drift.get_task("check_drift").skip_on_exit_code == [no_drift],
        "check_drift must skip on the batch's NO_DRIFT exit code",
    )
    trigger = drift.get_task("trigger_retrain")
    check(trigger.trigger_dag_id == "weekly_retrain", "drift triggers weekly_retrain")
    # A run dated after weekly_retrain's end date would get no tasks: date it with the drifted day.
    check(
        trigger.logical_date == "{{ logical_date }}" and trigger.reset_dag_run,
        "the retrain run must take the drifted day as its logical date, and rerun on a retrigger",
    )

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print(f"{len(bag.dags)} DAGs load: {', '.join(sorted(bag.dag_ids))}")
