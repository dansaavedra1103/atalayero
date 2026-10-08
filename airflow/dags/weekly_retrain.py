"""Weekly retraining (ADR-0009, ADR-0021), also triggered by drift.

It rebuilds the features and fits every family of `config/models.yaml` on train, compares them
with the rules on validation, and promotes a model only if it beats the champion. The splits are
fixed (ADR-0005), so on this dataset a retrain finds the same champion and promotes nothing."""

from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from atalayero_commands import ENV, ROOT, package

with DAG(
    dag_id="weekly_retrain",
    description="Refit the models; promote one only if it beats the champion",
    schedule="@weekly",
    start_date=datetime(2022, 9, 1),
    end_date=datetime(2022, 9, 10),
    catchup=True,
    max_active_runs=1,
    default_args={
        "cwd": ROOT,
        # Fewer graph workers than on the host: each takes about 1.5 GB (config/settings.yaml).
        "env": {**ENV, "ATALAYERO_GRAPH_WORKERS": "2"},
        "append_env": True,
    },
    tags=["atalayero", "models"],
) as dag:
    features = BashOperator(
        task_id="build_features", bash_command=package("atalayero.features", "build")
    )
    train = BashOperator(task_id="train", bash_command=package("atalayero.models", "train"))

    features >> train
