"""Model inputs: the point-in-time features of a split's transactions, with their labels."""

from dataclasses import dataclass

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
    tabular = settings.features_dir / "tabular.parquet"
    graph = settings.features_dir / "graph.parquet"
    motifs = settings.features_dir / "motifs.parquet"
    for path in (tabular, graph, motifs):
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing: run `make features` first")
    with duckdb.connect() as con:
        con.execute("SET enable_progress_bar = false")
        con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        frame = con.execute(
            f"""
            SELECT t.transaction_id, t.transacted_at::date AS day, l.is_laundering,
                {", ".join(f"t.{n}" for n in TABULAR)},
                {", ".join(f"g.{n}" for n in GRAPH)}, {", ".join(f"m.{n}" for n in MOTIFS)}
            FROM read_parquet({sql_literal(str(tabular))}) AS t
            JOIN read_parquet({sql_literal(str(graph))}) AS g USING (transaction_id)
            JOIN read_parquet({sql_literal(str(motifs))}) AS m USING (transaction_id)
            JOIN wh.marts.fct_laundering_labels AS l USING (transaction_id)
            WHERE t.transacted_at >= $start AND t.transacted_at < $end
            ORDER BY t.transaction_id
            """,
            {"start": start, "end": end},
        ).df()
    features = frame[list(FEATURES)].copy()
    # Floats with NaN for what is undefined (nullable integers would carry pd.NA); 0/1 for flags.
    features[list(NUMERIC + BOOLEAN)] = features[list(NUMERIC + BOOLEAN)].astype(float)
    return Dataset(
        transaction_ids=frame["transaction_id"].to_numpy(),
        features=features,
        labels=frame["is_laundering"].to_numpy(dtype=bool),
        days=frame["day"].to_numpy(dtype="datetime64[D]"),
    )
