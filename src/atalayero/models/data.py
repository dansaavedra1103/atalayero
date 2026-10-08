"""Model inputs: the point-in-time features of a split's transactions, with their labels."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from atalayero.features.graph import GRAPH
from atalayero.features.motifs import MOTIFS
from atalayero.features.tabular import CATEGORICAL
from atalayero.features.tabular import FEATURES as TABULAR
from atalayero.ingestion.source import sql_literal
from atalayero.settings import Settings, Split

FEATURES = (*TABULAR, *GRAPH, *MOTIFS)
BOOLEAN = tuple(
    name for name in FEATURES if name.startswith("is_") or name.endswith(("same_bank", "cycle"))
)
NUMERIC = tuple(name for name in FEATURES if name not in (*CATEGORICAL, *BOOLEAN))


@dataclass(frozen=True)
class Dataset:
    transaction_ids: np.ndarray
    features: pd.DataFrame  # columns in FEATURES order
    labels: np.ndarray  # True for laundering
    days: np.ndarray | None = None  # the day of each transaction (datetime64[D])


def load_split(settings: Settings, split: Split) -> Dataset:
    """The features and labels of a split. Train starts at `models.train_start`, after the
    warm-up day."""
    start, end = settings.splits.bounds(split)
    if split == "train":
        start = settings.models.train_start
    return load_period(settings, start, end)


def _feature_files(settings: Settings) -> tuple[Path, Path, Path]:
    paths = tuple(settings.features_dir / f"{n}.parquet" for n in ("tabular", "graph", "motifs"))
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing: run `make features` first")
    return paths


def _feature_query(settings: Settings) -> str:
    """The FROM clause and feature columns of the three feature files, as `t`, `g` and `m`."""
    tabular, graph, motifs = (sql_literal(str(p)) for p in _feature_files(settings))
    return f"""
        {", ".join(f"t.{n}" for n in TABULAR)},
        {", ".join(f"g.{n}" for n in GRAPH)}, {", ".join(f"m.{n}" for n in MOTIFS)}
        FROM read_parquet({tabular}) AS t
        JOIN read_parquet({graph}) AS g USING (transaction_id)
        JOIN read_parquet({motifs}) AS m USING (transaction_id)
    """


def model_inputs(frame: pd.DataFrame) -> pd.DataFrame:
    features = frame[list(FEATURES)].copy()
    # Floats with NaN for what is undefined (nullable integers would carry pd.NA); 0/1 for flags.
    features[list(NUMERIC + BOOLEAN)] = features[list(NUMERIC + BOOLEAN)].astype(float)
    return features


def load_period(settings: Settings, start: datetime, end: datetime) -> Dataset:
    """The features and labels of the transactions in `[start, end)`."""
    columns = _feature_query(settings)
    with duckdb.connect() as con:
        con.execute("SET enable_progress_bar = false")
        con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        frame = con.execute(
            f"""
            SELECT t.transaction_id, t.transacted_at::date AS day, l.is_laundering, {columns}
            JOIN wh.marts.fct_laundering_labels AS l USING (transaction_id)
            WHERE t.transacted_at >= $start AND t.transacted_at < $end
            ORDER BY t.transaction_id
            """,
            {"start": start, "end": end},
        ).df()
    return Dataset(
        transaction_ids=frame["transaction_id"].to_numpy(),
        features=model_inputs(frame),
        labels=frame["is_laundering"].to_numpy(dtype=bool),
        days=frame["day"].to_numpy(dtype="datetime64[D]"),
    )


def load_features(settings: Settings, transaction_ids: Sequence[int]) -> pd.DataFrame:
    """The model inputs of some transactions, indexed by transaction ID, and nothing else: no
    label is read, so an investigator's tools can use it (ADR-0016)."""
    columns = _feature_query(settings)
    with duckdb.connect() as con:
        con.execute("SET enable_progress_bar = false")
        frame = con.execute(
            f"""
            SELECT t.transaction_id, {columns}
            WHERE list_contains($ids, t.transaction_id)
            ORDER BY t.transaction_id
            """,
            {"ids": [int(i) for i in transaction_ids]},
        ).df()
    return model_inputs(frame).set_index(frame["transaction_id"].rename(None))
