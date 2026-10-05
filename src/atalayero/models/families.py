"""The model families compared with the rules (ADR-0009): a GLM, gradient boosting and an
unsupervised reference. Each turns a transaction's features into a score: higher means more
suspicious.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from lightgbm import LGBMClassifier
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from atalayero.features.tabular import CATEGORICAL
from atalayero.models.data import BOOLEAN, NUMERIC, Dataset
from atalayero.rules.schema import HistoryEntry

FamilyName = Literal["logistic_regression", "lightgbm", "isolation_forest"]
Params = dict[str, float | int | str | None]

# Counts, amounts, minutes and graph sizes are heavy-tailed and never negative.
_LOGGED = tuple(name for name in NUMERIC if name != "hour")


def _numeric() -> Pipeline:
    """Median imputation (with indicators of what was missing), then log1p."""
    return make_pipeline(
        SimpleImputer(strategy="median", add_indicator=True), FunctionTransformer(np.log1p)
    )


def as_categories(features: pd.DataFrame) -> pd.DataFrame:
    """LightGBM splits on pandas categories natively."""
    return features.astype({name: "category" for name in CATEGORICAL})


def logistic_regression(params: Params, seed: int) -> Pipeline:
    prepare = ColumnTransformer(
        [
            ("numeric", make_pipeline(_numeric(), StandardScaler()), list(_LOGGED)),
            ("hour", StandardScaler(), ["hour"]),
            ("flags", "passthrough", list(BOOLEAN)),
            ("categories", OneHotEncoder(handle_unknown="ignore"), list(CATEGORICAL)),
        ]
    )
    model = LogisticRegression(
        C=params["C"], class_weight=params["class_weight"], max_iter=1000, random_state=seed
    )
    return Pipeline([("prepare", prepare), ("model", model)])


def lightgbm(params: Params, seed: int) -> Pipeline:
    model = LGBMClassifier(
        n_estimators=params["n_estimators"],
        learning_rate=params["learning_rate"],
        num_leaves=params["num_leaves"],
        min_child_samples=params["min_child_samples"],
        subsample=params["subsample"],
        subsample_freq=1,
        colsample_bytree=params["colsample_bytree"],
        reg_lambda=params["reg_lambda"],
        random_state=seed,
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
    )
    return Pipeline([("categories", FunctionTransformer(as_categories)), ("model", model)])


def isolation_forest(params: Params, seed: int) -> Pipeline:
    """Numeric features and flags only: categories have no distance to isolate on."""
    prepare = ColumnTransformer(
        [
            ("numeric", _numeric(), list(_LOGGED)),
            ("rest", "passthrough", ["hour", *BOOLEAN]),
        ]
    )
    model = IsolationForest(
        n_estimators=params["n_estimators"],
        max_samples=params["max_samples"],
        max_features=params["max_features"],
        random_state=seed,
        n_jobs=4,  # as fast as all cores on 2M rows; each extra thread holds a copy of the data
    )
    return Pipeline([("prepare", prepare), ("model", model)])


@dataclass(frozen=True)
class Family:
    build: Callable[[Params, int], Pipeline]
    params: frozenset[str]  # the hyperparameters it takes, all required
    supervised: bool  # unsupervised families never see the labels


FAMILIES: dict[FamilyName, Family] = {
    "logistic_regression": Family(
        logistic_regression, frozenset({"C", "class_weight", "negative_rate"}), True
    ),
    "lightgbm": Family(
        lightgbm,
        frozenset(
            {
                "n_estimators",
                "learning_rate",
                "num_leaves",
                "min_child_samples",
                "subsample",
                "colsample_bytree",
                "reg_lambda",
                "negative_rate",
            }
        ),
        True,
    ),
    "isolation_forest": Family(
        isolation_forest, frozenset({"n_estimators", "max_samples", "max_features"}), False
    ),
}


def training_rows(labels: np.ndarray, negative_rate: float, seed: int) -> np.ndarray:
    """Every laundering row and a random share of the clean ones: with 0.1% positives, most clean
    rows add time, not information."""
    rng = np.random.default_rng(seed)
    return labels | (rng.random(len(labels)) < negative_rate)


def fit(name: FamilyName, params: Params, data: Dataset, seed: int) -> Pipeline:
    family = FAMILIES[name]
    model = family.build(params, seed)
    if not family.supervised:
        return model.fit(data.features)
    keep = training_rows(data.labels, float(params["negative_rate"]), seed)
    return model.fit(data.features[keep], data.labels[keep])


def transaction_scores(model: Pipeline, features: pd.DataFrame) -> np.ndarray:
    """Higher is more suspicious: the probability of laundering, or the anomaly score."""
    if hasattr(model[-1], "predict_proba"):
        return model.predict_proba(features)[:, 1]
    return -model.score_samples(features)


class FamilyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    params: Params


class ModelsConfig(BaseModel):
    """`config/models.yaml`: the hyperparameters of each family, versioned like a rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=r"^\d+\.\d+$")
    families: dict[FamilyName, FamilyConfig]
    history: tuple[HistoryEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> "ModelsConfig":
        for name, config in self.families.items():
            expected = FAMILIES[name].params
            if set(config.params) != expected:
                raise ValueError(
                    f"{name} takes exactly {sorted(expected)}, got {sorted(config.params)}"
                )
        if self.history[-1].version != self.version:
            raise ValueError(
                f"the last history entry is version {self.history[-1].version}, but the config "
                f"is version {self.version}: add an entry with the reason"
            )
        return self


def load_models_config(path: Path) -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text()))
