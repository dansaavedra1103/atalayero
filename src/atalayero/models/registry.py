"""MLflow tracking and model registry, local and under `data/mlflow/` (ADR-0009)."""

import logging
from importlib.metadata import version
from pathlib import Path

import mlflow
import mlflow.sklearn
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException
from sklearn.pipeline import Pipeline

from atalayero.settings import Settings

logger = logging.getLogger(__name__)

CHAMPION = "champion"
PRIMARY = "validation_detection_without_hubs"  # model version tag that champions compete on
# What a logged model needs to load; listed, because inferring it spawns a slow subprocess.
_REQUIREMENTS = ("scikit-learn", "lightgbm", "pandas", "numpy", "cloudpickle")


class Registry:
    def __init__(self, settings: Settings) -> None:
        models = settings.models
        uri = models.mlflow_tracking_uri
        if uri.startswith("sqlite:///"):
            Path(uri.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        mlflow.set_tracking_uri(uri)
        self.client = MlflowClient(uri)
        self.model_name = models.registered_model
        experiment = self.client.get_experiment_by_name(models.experiment)
        self.experiment_id = (
            experiment.experiment_id
            if experiment is not None
            else self.client.create_experiment(
                models.experiment, artifact_location=models.mlflow_artifacts_dir.resolve().as_uri()
            )
        )

    def log_run(
        self,
        run_name: str,
        params: dict[str, object],
        metrics: dict[str, float],
        curves: dict[str, list[tuple[int, float]]] | None = None,
        artifacts: dict[str, str] | None = None,
        model: Pipeline | None = None,
    ) -> tuple[str, str | None]:
        """One run: parameters, metrics, curves (metric per step), text artifacts and the model.
        Returns the run ID and the model URI."""
        with mlflow.start_run(experiment_id=self.experiment_id, run_name=run_name) as run:
            mlflow.log_params(params)
            mlflow.log_metrics(metrics)
            for key, points in (curves or {}).items():
                for step, value in points:
                    mlflow.log_metric(key, value, step=step)
            for name, text in (artifacts or {}).items():
                mlflow.log_text(text, name)
            model_uri = None
            if model is not None:
                info = mlflow.sklearn.log_model(
                    model,
                    name="model",
                    serialization_format="cloudpickle",
                    pip_requirements=[f"{p}=={version(p)}" for p in _REQUIREMENTS],
                )
                model_uri = info.model_uri
        return run.info.run_id, model_uri

    def champion_value(self) -> float | None:
        try:
            champion = self.client.get_model_version_by_alias(self.model_name, CHAMPION)
        except MlflowException:
            return None
        return float(champion.tags[PRIMARY])

    def promote(self, model_uri: str, value: float) -> bool:
        """Register the model; it becomes the champion if it beats the current champion's primary
        validation metric (or there is none)."""
        current = self.champion_value()
        registered = mlflow.register_model(model_uri, self.model_name, tags={PRIMARY: str(value)})
        if current is not None and value <= current:
            logger.info(
                "%s v%s (%.4f) does not beat the champion (%.4f)",
                self.model_name,
                registered.version,
                value,
                current,
            )
            return False
        self.client.set_registered_model_alias(self.model_name, CHAMPION, registered.version)
        logger.info("%s v%s is the champion (%.4f)", self.model_name, registered.version, value)
        return True

    def load_champion(self) -> Pipeline:
        return mlflow.sklearn.load_model(f"models:/{self.model_name}@{CHAMPION}")
