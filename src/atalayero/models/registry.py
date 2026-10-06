"""MLflow tracking and model registry, local and under `data/mlflow/` (ADR-0009)."""

import hashlib
import logging
from collections.abc import Sequence
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
FEATURES = "features"  # model version tag: a signature of the feature list the model takes
HOLDOUT_RUN = "{family} on test"  # MLflow run of a family refit for the test run (ADR-0014)
# What a logged model needs to load; listed, because inferring it spawns a slow subprocess.
_REQUIREMENTS = ("scikit-learn", "lightgbm", "pandas", "numpy", "cloudpickle")


def feature_signature(features: Sequence[str]) -> str:
    """A short hash of the ordered feature names a model takes."""
    return hashlib.sha256(",".join(features).encode()).hexdigest()[:12]


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

    def champion(self) -> tuple[float, str | None] | None:
        """The current champion's primary validation metric and feature signature."""
        try:
            champion = self.client.get_model_version_by_alias(self.model_name, CHAMPION)
        except MlflowException:
            return None
        return float(champion.tags[PRIMARY]), champion.tags.get(FEATURES)

    def champion_family(self) -> str:
        """The model family of the current champion (the `family` parameter of its run)."""
        champion = self.client.get_model_version_by_alias(self.model_name, CHAMPION)
        return self.client.get_run(champion.run_id).data.params["family"]

    def champion_value(self) -> float | None:
        current = self.champion()
        return None if current is None else current[0]

    def promote(self, model_uri: str, value: float, features: Sequence[str]) -> bool:
        """Register the model; it becomes the champion if it beats the current champion's primary
        validation metric, or if there is none. A champion that takes another feature list
        cannot score this model's inputs: the new model starts a new line of champions."""
        signature = feature_signature(features)
        champion = self.champion()
        registered = mlflow.register_model(
            model_uri, self.model_name, tags={PRIMARY: str(value), FEATURES: signature}
        )
        current = None if champion is None else champion[0]
        if champion is not None and champion[1] != signature:
            logger.warning(
                "The champion takes other features (%s, now %s): %s v%s replaces it at %.4f, "
                "against its %.4f",
                champion[1],
                signature,
                self.model_name,
                registered.version,
                value,
                champion[0],
            )
            current = None
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

    def load_holdout_model(self, family: str) -> Pipeline:
        """The latest model of `family` refit on train and validation by `make holdout`: the one
        behind the test scores. It is logged, never registered (ADR-0014)."""
        name = HOLDOUT_RUN.format(family=family)
        runs = self.client.search_runs(
            [self.experiment_id],
            filter_string=f"attributes.run_name = '{name}'",
            order_by=["attributes.start_time DESC"],
            max_results=1,
        )
        if not runs:
            raise LookupError(f"no MLflow run '{name}': run `make holdout` first")
        return mlflow.sklearn.load_model(f"runs:/{runs[0].info.run_id}/model")
