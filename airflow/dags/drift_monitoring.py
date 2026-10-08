"""Drift monitoring (ADR-0012, ADR-0021): after each day of the batch, retrain if it drifted.

The batch already compares validation and test days with train (ADR-0020). This DAG waits for the
day's batch run, reads its verdict, and triggers `weekly_retrain` when the day drifted; on other
days the trigger is skipped."""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.standard.sensors.external_task import ExternalTaskSensor
from airflow.sdk import DAG
from atalayero_commands import ENV, ROOT, package

NO_DRIFT = 3  # exit code of `atalayero.batch drifted` for a day that did not drift

with DAG(
    dag_id="drift_monitoring",
    description="Retrain when a day of the batch drifts",
    schedule="@daily",
    start_date=datetime(2022, 9, 1),
    end_date=datetime(2022, 9, 10),
    catchup=True,
    max_active_runs=1,
    default_args={"cwd": ROOT, "env": ENV, "append_env": True},
    tags=["atalayero", "monitoring"],
) as dag:
    batch_done = ExternalTaskSensor(
        task_id="wait_for_batch",
        external_dag_id="daily_batch",
        external_task_id="publish",
        mode="reschedule",
        poke_interval=60,
        timeout=int(timedelta(hours=12).total_seconds()),
    )
    drifted = BashOperator(
        task_id="check_drift",
        bash_command=package("atalayero.batch", "drifted", "{{ ds }}"),
        skip_on_exit_code=NO_DRIFT,
    )
    retrain = TriggerDagRunOperator(
        task_id="trigger_retrain",
        trigger_dag_id="weekly_retrain",
        conf={"reason": "drift on {{ ds }}"},
    )

    batch_done >> drifted >> retrain
