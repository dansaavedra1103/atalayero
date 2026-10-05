from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from atalayero.features.tabular import CATEGORICAL
from atalayero.models.data import BOOLEAN, FEATURES, NUMERIC, Dataset
from atalayero.schemas import Transaction

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent
T0 = datetime(2022, 9, 5, 9, 0)

_DUCKDB_TYPES = {
    int: "BIGINT",
    datetime: "TIMESTAMP",
    Decimal: "DECIMAL(20, 6)",
    float: "DOUBLE",
    str: "VARCHAR",
}


@pytest.fixture
def sample_csv() -> Path:
    """Story sample of HI-Small_Trans.csv in the source format (see tests/fixtures/README.md)."""
    return FIXTURES_DIR / "hi_small_sample.csv"


@pytest.fixture
def sample_patterns() -> Path:
    """The laundering attempts of `sample_csv`, verbatim from HI-Small_Patterns.txt."""
    return FIXTURES_DIR / "hi_small_patterns_sample.txt"


MakeTx = Callable[..., Transaction]


@pytest.fixture
def make_tx() -> MakeTx:
    """Build a US Dollar transaction `minutes` after T0."""

    def make(
        transaction_id: int,
        minutes: float,
        sender: str = "001:A",
        receiver: str = "001:B",
        usd: float = 6000.0,
    ) -> Transaction:
        amount = Decimal(str(usd))
        return Transaction(
            transaction_id=transaction_id,
            transacted_at=T0 + timedelta(minutes=minutes),
            sender_account_key=sender,
            receiver_account_key=receiver,
            amount_paid=amount,
            payment_currency="US Dollar",
            amount_paid_usd=usd,
            amount_received=amount,
            receiving_currency="US Dollar",
            amount_received_usd=usd,
            payment_format="ach",
        )

    return make


WriteDb = Callable[[list[Transaction]], Path]


@pytest.fixture
def fct_transactions_db(tmp_path: Path) -> WriteDb:
    """Write transactions to `marts.fct_transactions` in a fresh DuckDB file, with one extra
    column as in the real mart."""

    def write(transactions: list[Transaction]) -> Path:
        db = tmp_path / "atalayero.duckdb"
        fields = Transaction.model_fields
        columns = ", ".join(f"{name} {_DUCKDB_TYPES[f.annotation]}" for name, f in fields.items())
        with duckdb.connect(str(db)) as con:
            con.execute("CREATE SCHEMA marts")
            con.execute(
                f"CREATE TABLE marts.fct_transactions ({columns}, is_self_transfer BOOLEAN)"
            )
            con.executemany(
                f"INSERT INTO marts.fct_transactions VALUES ({', '.join('?' * (len(fields) + 1))})",
                [[*tx.model_dump().values(), False] for tx in transactions],
            )
        return db

    return write


LoadTx = Callable[[list[Transaction]], duckdb.DuckDBPyConnection]


@pytest.fixture
def load_tx() -> LoadTx:
    """An in-memory DuckDB with the transactions in table `tx`, in the columns of
    `marts.fct_transactions` that features read."""

    def load(transactions: list[Transaction]) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        fields = Transaction.model_fields
        columns = ", ".join(f"{name} {_DUCKDB_TYPES[f.annotation]}" for name, f in fields.items())
        con.execute(f"CREATE TABLE tx ({columns})")
        con.executemany(
            f"INSERT INTO tx VALUES ({', '.join('?' * len(fields))})",
            [list(tx.model_dump().values()) for tx in transactions],
        )
        return con

    return load


@pytest.fixture
def small_params() -> dict[str, dict]:
    """Hyperparameters of each model family that train in a blink."""
    return {
        "logistic_regression": {"C": 1.0, "class_weight": "balanced", "negative_rate": 0.5},
        "lightgbm": {
            "n_estimators": 30,
            "learning_rate": 0.1,
            "num_leaves": 7,
            "min_child_samples": 5,
            "subsample": 1.0,
            "colsample_bytree": 1.0,
            "reg_lambda": 0.0,
            "negative_rate": 0.5,
        },
        "isolation_forest": {"n_estimators": 20, "max_samples": 64, "max_features": 1.0},
    }


@pytest.fixture
def synthetic() -> Callable[..., Dataset]:
    """A factory of model inputs with a planted signal."""

    def synthetic(n: int = 600, seed: int = 0) -> Dataset:
        """Random features; laundering has larger amounts and fewer earlier pair transactions."""
        rng = np.random.default_rng(seed)
        labels = rng.random(n) < 0.05
        features = pd.DataFrame(
            {name: rng.lognormal(2, 1, n) for name in NUMERIC}
            | {name: (rng.random(n) < 0.2).astype(float) for name in BOOLEAN}
            | {name: rng.choice(["ach", "wire", "cheque"], n) for name in CATEGORICAL}
        )
        features["amount_usd"] *= np.where(labels, 20, 1)
        features["pair_count_before"] = np.where(labels, 0, features["pair_count_before"])
        features["hour"] = rng.integers(0, 24, n).astype(float)
        features.loc[rng.random(n) < 0.1, "sender_minutes_since_previous"] = np.nan
        return Dataset(np.arange(n), features[list(FEATURES)], labels)

    return synthetic
