"""The investigator agent on the top of one day's alert queue (ADR-0024), by hand only.

On a local model the agent takes minutes an alert, so it stays off the schedule (ADR-0021):
trigger it with a day and how many alerts to investigate, from the top of the queue. Each
investigation is kept beside its day, where `/cases` and the dashboard read it. It needs Ollama up,
with the models of `config/settings.yaml` pulled (`make llm`) and the typology index built
(`make knowledge`)."""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param
from atalayero_commands import ENV, ROOT, package

with DAG(
    dag_id="investigate_alerts",
    description="The investigator agent on the top alerts of one day (manual)",
    schedule=None,
    # No end date: a manual run is dated when it is triggered, and Airflow gives a run dated after
    # a DAG's end date no task.
    start_date=datetime(2022, 9, 1),
    catchup=False,
    max_active_runs=1,
    params={
        "day": Param(
            "2022-09-10",
            type="string",
            pattern=r"^2022-09-(0[2-9]|10)$",
            description="A day of the simulation with an alert queue: 2 to 10 Sep 2022",
        ),
        "top": Param(
            5,  # as batch.investigate_top in config/settings.yaml
            type="integer",
            minimum=1,
            maximum=20,
            description="How many alerts to investigate, from the top of the day's queue",
        ),
    },
    default_args={
        "cwd": ROOT,
        # The parameters reach the command through its environment, never pasted into it.
        "env": {**ENV, "DAY": "{{ params.day }}", "TOP": "{{ params.top }}"},
        "append_env": True,
        "execution_timeout": timedelta(hours=2),
    },
    tags=["atalayero", "agent"],
) as dag:
    BashOperator(
        task_id="investigate",
        bash_command=package("atalayero.agent", "investigate", '--day "$DAY"', '--top "$TOP"'),
    )
