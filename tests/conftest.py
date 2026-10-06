import random
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
import yaml

from atalayero.features.tabular import CATEGORICAL
from atalayero.models.data import BOOLEAN, FEATURES, NUMERIC, Dataset
from atalayero.schemas import Alert, Transaction
from atalayero.settings import Settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent
T0 = datetime(2022, 9, 5, 9, 0)
DAY = 24 * 60  # minutes

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
            "reg_lambda": 0.001,
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
        features["pair_count_24h"] = np.where(labels, 0, features["pair_count_24h"])
        features["hour"] = rng.integers(0, 24, n).astype(float)
        features.loc[rng.random(n) < 0.1, "sender_minutes_since_previous"] = np.nan
        return Dataset(np.arange(n), features[list(FEATURES)], labels)

    return synthetic


@pytest.fixture
def model_settings(
    make_tx: MakeTx,
    fct_transactions_db: WriteDb,
    small_params: dict[str, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Four days of random transactions where large amounts are laundering: 5 Sep is warm-up,
    6-7 Sep train, 8 Sep validation. Features, rule alerts and a small model config are ready."""
    from atalayero.features.graph import build_graph_features
    from atalayero.features.motifs import build_motif_features
    from atalayero.features.tabular import build_tabular_features
    from atalayero.models.families import FAMILIES
    from atalayero.rules.schema import load_rules
    from atalayero.streaming.consumer import AlertSink

    monkeypatch.chdir(REPO_ROOT)
    rng = random.Random(0)
    accounts = [f"00{i % 3}:{i}" for i in range(40)]
    transactions = [
        make_tx(
            i,
            rng.randrange(0, 4 * DAY - 540),
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=rng.choice([500.0, 1500.0, 9500.0]),
        )
        for i in range(800)
    ]
    db = fct_transactions_db(transactions)
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE marts.fct_laundering_labels AS SELECT transaction_id, "
            "amount_paid_usd > 9000 AND transaction_id % 3 = 0 AS is_laundering, "
            "'untyped' AS label_group, NULL::INTEGER AS attempt_id, NULL AS typology "
            "FROM marts.fct_transactions"
        )
    config = tmp_path / "models.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "families": {name: {"params": small_params[name]} for name in FAMILIES},
                "history": [{"version": "1.0", "date": "2026-10-05", "reason": "Tests"}],
            }
        )
    )
    base = Settings()
    settings = base.model_copy(
        update={
            "duckdb_path": db,
            "features_dir": tmp_path / "features",
            "rule_alerts_dir": tmp_path / "alerts",
            "graph_workers": 1,
            "splits": base.splits.model_copy(
                update={
                    "train_end": datetime(2022, 9, 8),
                    "validation_end": datetime(2022, 9, 9),
                    "test_end": datetime(2022, 9, 10),
                }
            ),
            "evaluation": base.evaluation.model_copy(
                update={"hub_accounts": 1, "reports_dir": tmp_path / "reports"}
            ),
            "models": base.models.model_copy(
                update={
                    "config_path": config,
                    "train_start": datetime(2022, 9, 6),
                    "mlflow_tracking_uri": f"sqlite:///{tmp_path}/mlflow/mlflow.db",
                    "mlflow_artifacts_dir": tmp_path / "mlflow" / "artifacts",
                }
            ),
        }
    )
    build_tabular_features(settings)
    build_graph_features(settings)
    build_motif_features(settings)
    sink = AlertSink(settings.rule_alerts_dir)
    sink.extend(
        Alert(
            alert_id=f"R01:{i}",
            rule_id="R01",
            rule_version=load_rules(settings.rules_dir)[0].version,
            account_key=accounts[i],
            triggered_at=datetime(2022, 9, 8, 12) + timedelta(minutes=i),
            transaction_id=i,
            value=10,
            evidence=(i,),
        )
        for i in range(3)
    )
    sink.flush()
    return settings


@pytest.fixture
def holdout_settings(model_settings: Settings) -> Settings:
    """The model fixture with every split a day earlier, so that test holds 8 Sep: train is
    6 Sep, validation 7 Sep, and the rule alerts fall on test."""
    return model_settings.model_copy(
        update={
            "splits": model_settings.splits.model_copy(
                update={
                    "train_end": datetime(2022, 9, 7),
                    "validation_end": datetime(2022, 9, 8),
                    "test_end": datetime(2022, 9, 9),
                }
            )
        }
    )
