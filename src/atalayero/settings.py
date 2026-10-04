"""Runtime settings: `config/settings.yaml`, overridable with ATALAYERO_* environment variables."""

from pathlib import Path

from pydantic import BaseModel, Field
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
