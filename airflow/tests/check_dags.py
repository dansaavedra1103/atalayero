"""The DAGs load in Airflow's own Python and keep the shape ADR-0021 describes.

Airflow is not a dependency of the project, so this is a script for Airflow's Python, not a pytest
module: `make test-airflow` runs it in the image, and CI too.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DAGS = REPO / "airflow" / "dags"
os.environ.setdefault("ATALAYERO_ROOT", str(REPO))
sys.path.insert(0, str(DAGS))  # as PYTHONPATH does in the container, for atalayero_commands

from airflow.dag_processing.dagbag import DagBag  # noqa: E402 (needs ATALAYERO_ROOT first)
from airflow.sdk.definitions.param import ParamValidationError  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


bag = DagBag(dag_folder=str(DAGS))
check(not bag.import_errors, f"import errors: {bag.import_errors}")
check(
    set(bag.dag_ids) == {"daily_batch", "drift_monitoring", "weekly_retrain", "investigate_alerts"},
    f"unexpected DAGs: {sorted(bag.dag_ids)}",
)
simulation = (datetime(2022, 9, 1), datetime(2022, 9, 10))
for dag in bag.dags.values():
    check(dag.max_active_runs == 1, f"{dag.dag_id} must run one at a time")
    if dag.timetable.can_be_scheduled:
        check(dag.catchup, f"{dag.dag_id} must catch up over the simulation")
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

agent = bag.dags.get("investigate_alerts")
if agent is not None:
    check(not agent.timetable.can_be_scheduled, "the agent runs by hand only")
    # A manual run is dated when it is triggered: past an end date, it would get no task.
    check(agent.end_date is None, "investigate_alerts must have no end date")
    command = agent.get_task("investigate").bash_command
    check("{{" not in command, "parameters must reach the command through its environment")
    settings = yaml.safe_load((REPO / "config" / "settings.yaml").read_text())
    top = agent.params.get_param("top")
    check(
        top.value == settings["batch"]["investigate_top"],
        "the DAG's default top must be batch.investigate_top",
    )
    for name, value in (("day", "2022-09-01"), ("day", "2022-09-10; id"), ("top", 21)):
        try:
            agent.params.get_param(name).resolve(value)
            failures.append(f"investigate_alerts accepts {name}={value!r}")
        except ParamValidationError:
            pass
    check(agent.params.get_param("day").resolve("2022-09-02") == "2022-09-02", "2 Sep has a queue")

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print(f"{len(bag.dags)} DAGs load: {', '.join(sorted(bag.dag_ids))}")
