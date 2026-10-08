"""The daily batch (ADR-0020): one run per day of the simulation, 1–10 Sep 2022, in order.

Each run computes the day (features, rule alerts, scores, queue, drift), builds the KPIs in dbt
and publishes a new serving database. A day carries on from the state the day before left, so
runs go one at a time and a day waits for the one before it to succeed."""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from atalayero_commands import ENV, ROOT, dbt, package

with DAG(
    dag_id="daily_batch",
    description="One day of the simulation: features, alerts, scores, queue, drift, KPIs",
    schedule="@daily",
    start_date=datetime(2022, 9, 1),
    end_date=datetime(2022, 9, 10),
    catchup=True,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
        "cwd": ROOT,
        "env": ENV,
        "append_env": True,
    },
    tags=["atalayero", "batch"],
) as dag:
    run_day = BashOperator(
        task_id="run_day",
        bash_command=package("atalayero.batch", "day", "{{ ds }}"),
        depends_on_past=True,
    )
    build_kpis = BashOperator(task_id="build_kpis", bash_command=dbt("build", "--select tag:batch"))
    publish = BashOperator(task_id="publish", bash_command=package("atalayero.batch", "publish"))

    run_day >> build_kpis >> publish
