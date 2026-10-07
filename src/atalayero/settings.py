"""Runtime settings: `config/settings.yaml`, overridable with ATALAYERO_* environment variables."""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class DatasetFile(BaseModel):
    name: str
    sha256: str


class DatasetSettings(BaseModel):
    base_url: str
    version: int
    transactions: DatasetFile
    patterns: DatasetFile

    def url(self, file: DatasetFile) -> str:
        return f"{self.base_url}/{file.name}?datasetVersionNumber={self.version}"


class StreamingSettings(BaseModel):
    bootstrap_servers: str
    topic: str
    speedup: float = Field(ge=0)  # simulated seconds per real second; 0 = as fast as possible
    idle_timeout_seconds: float = Field(gt=0)


Split = Literal["train", "validation", "test"]


class SplitSettings(BaseModel):
    """Half-open boundaries of the temporal split (ADR-0005)."""

    train_end: datetime
    validation_end: datetime
    test_end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "SplitSettings":
        if not self.train_end < self.validation_end < self.test_end:
            raise ValueError("splits must satisfy train_end < validation_end < test_end")
        return self

    def bounds(self, split: Split) -> tuple[datetime, datetime]:
        """`[start, end)` of a split; train starts with the data."""
        return {
            "train": (datetime.min, self.train_end),
            "validation": (self.train_end, self.validation_end),
            "test": (self.validation_end, self.test_end),
        }[split]


class EvaluationSettings(BaseModel):
    budgets: tuple[int, ...] = Field(min_length=1)  # alerts per day
    hub_accounts: int = Field(ge=0)
    reports_dir: Path


class ModelSettings(BaseModel):
    config_path: Path
    train_start: datetime  # the first day of training rows, after the warm-up (ADR-0008)
    seed: int
    mlflow_tracking_uri: str
    mlflow_artifacts_dir: Path
    experiment: str
    registered_model: str
    tuning_trials: dict[str, int]  # family -> Optuna trials


class DriftSettings(BaseModel):
    bins: int = Field(ge=2)
    moderate_psi: float = Field(gt=0)
    significant_psi: float = Field(gt=0)
    alert_volume_band: tuple[float, float]


class AgentEvalSettings(BaseModel):
    queue_budget: int = Field(ge=1)
    dev_cases: int = Field(ge=1)
    golden_cases: int = Field(ge=1)
    top_transactions: int = Field(ge=1)
    seed: int
    dir: Path
    runs_dir: Path


class OllamaSettings(BaseModel):
    url: str
    models: dict[str, str | None]  # model tag -> pinned digest (None: not pinned yet)


class KnowledgeSettings(BaseModel):
    notes_dir: Path
    index_dir: Path
    embedding_model: str
    top_k: int = Field(ge=1)


class AgentSettings(BaseModel):
    config_path: Path
    investigations_dir: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATALAYERO_",
        env_nested_delimiter="__",
        yaml_file="config/settings.yaml",
    )

    data_dir: Path
    duckdb_path: Path
    rules_dir: Path
    alerts_dir: Path
    rule_alerts_dir: Path
    features_dir: Path
    graph_workers: int = Field(ge=1)
    splits: SplitSettings
    evaluation: EvaluationSettings
    models: ModelSettings
    drift: DriftSettings
    agent_evals: AgentEvalSettings
    ollama: OllamaSettings
    knowledge: KnowledgeSettings
    agent: AgentSettings
    dataset: DatasetSettings
    streaming: StreamingSettings

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit arguments > environment variables > settings.yaml.
        return init_settings, env_settings, YamlConfigSettingsSource(settings_cls)
