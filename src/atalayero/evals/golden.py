"""Case sets for the investigator agent (ADR-0015).

A case is an account-day of the alert queue: what the rules alerted, plus the model's top
`queue_budget` account-days of each day, hubs left out. Cases fall into three groups, reported
apart (design.md §5):

- patterned: the account-day holds laundering of a documented attempt (answer: escalate, with one
  of its typologies);
- untyped: it holds laundering of no documented attempt, and none of an attempt (escalate);
- clean: it holds no laundering (close).

Each group is sampled to the same size, so the mix is a design choice and not the prevalence.
Alerts and answers go to separate files: an investigator reads the alerts and nothing else.
"""

import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Literal

import duckdb
import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from atalayero.models.evaluate import SplitEvaluator
from atalayero.rules.schema import load_rules
from atalayero.schemas import CaseAlert, Decision
from atalayero.settings import Settings, Split

logger = logging.getLogger(__name__)

CaseSet = Literal["dev", "golden"]
Group = Literal["patterned", "untyped", "clean"]
GROUPS: tuple[Group, ...] = ("patterned", "untyped", "clean")
# The dev set is for choosing models and prompts; the golden set is read once per reported result.
SPLITS: dict[CaseSet, Split] = {"dev": "validation", "golden": "test"}
MODEL = "model"  # the source of an alert within the model's daily budget


class CaseAnswer(BaseModel):
    """What an investigator should conclude on a case. Kept apart from `CaseAlert`, so nothing
    an investigator reads holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str
    group: Group
    decision: Decision
    typologies: tuple[str, ...]  # the accepted ones: those of the account-day's attempts


def alert_id(day: date, account_key: str) -> str:
    return f"{day.isoformat()}:{account_key}"


def _days(column: pd.Series) -> pd.Series:
    return pd.to_datetime(column).dt.date


def label_account_days(detections: pd.DataFrame) -> pd.DataFrame:
    """The group of every account-day that holds laundering, with the typologies of its
    documented attempts (sorted) and the most frequent one (ties: alphabetical), from
    `SplitEvaluator.detections()`. Account-days missing from the result are clean."""
    legs = pd.concat(
        [
            detections.assign(account_key=detections[column])
            for column in ("sender_account_key", "receiver_account_key")
        ]
    ).drop_duplicates(["transaction_id", "account_key"])  # a self-transfer is one leg
    legs["day"] = _days(legs["transacted_at"])
    rows = []
    for (day, account_key), group in legs.groupby(["day", "account_key"], sort=True):
        patterned = group.loc[group["label_group"] == "patterned", "typology"]
        counts = patterned.value_counts()
        rows.append(
            {
                "day": day,
                "account_key": account_key,
                "group": "patterned" if len(patterned) else "untyped",
                "typologies": tuple(sorted(counts.index)),
                "main_typology": min(counts.index[counts == counts.max()]) if len(counts) else None,
            }
        )
    return pd.DataFrame(
        rows, columns=["day", "account_key", "group", "typologies", "main_typology"]
    )


def queue_cases(ranking: pd.DataFrame, rule_hits: pd.DataFrame, budget: int) -> pd.DataFrame:
    """The account-days of the alert queue, hubs left out, from `SplitEvaluator.ranking()` and
    `.rule_hits()`: their sources, score and rank, and the transactions that raised them."""
    ranking = ranking.assign(day=_days(ranking["day"]))
    hits = rule_hits.assign(day=_days(rule_hits["day"]))
    by_rules = hits.groupby(["day", "account_key"]).agg(
        rules=("rule_id", lambda ids: tuple(sorted(set(ids)))),
        rule_transactions=("transaction_id", lambda ids: tuple(int(i) for i in ids)),
    )
    queue = ranking.merge(by_rules, on=["day", "account_key"], how="left")
    in_queue = (queue["rank"] <= budget) | queue["rules"].notna()
    queue = queue[in_queue & ~queue["on_hub"]].copy()
    queue["sources"] = [
        (*(rules if isinstance(rules, tuple) else ()), *((MODEL,) if rank <= budget else ()))
        for rules, rank in zip(queue["rules"], queue["rank"], strict=True)
    ]
    queue["transaction_ids"] = [
        tuple(sorted({int(i) for i in top} | set(triggers if isinstance(triggers, tuple) else ())))
        for top, triggers in zip(queue["top_transactions"], queue["rule_transactions"], strict=True)
    ]
    queue["alert_id"] = [
        alert_id(d, a) for d, a in zip(queue["day"], queue["account_key"], strict=True)
    ]
    return queue[
        ["alert_id", "day", "account_key", "sources", "score", "rank", "transaction_ids"]
    ].reset_index(drop=True)


def sample_cases(candidates: pd.DataFrame, per_group: int, seed: int) -> pd.DataFrame:
    """`per_group` cases of each group, drawn with `seed`, in shuffled order:
    - patterned cases take each main typology in turn, so every typology is represented;
    - clean cases come half from the rules' alerts and half from the model's alone.
    A group with fewer candidates contributes all of them."""
    rng = np.random.default_rng(seed)

    def shuffled(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.sort_values("alert_id")
        return frame.iloc[rng.permutation(len(frame))]

    chosen = []
    patterned = shuffled(candidates[candidates["group"] == "patterned"])
    queues = [g for _, g in patterned.groupby("main_typology", sort=True)]
    picks: list[pd.DataFrame] = []
    while len(picks) < per_group and any(len(q) for q in queues):
        for i, q in enumerate(queues):
            if len(picks) < per_group and len(q):
                picks.append(q.iloc[[0]])
                queues[i] = q.iloc[1:]
    chosen.append(pd.concat(picks) if picks else patterned.iloc[:0])
    chosen.append(shuffled(candidates[candidates["group"] == "untyped"]).iloc[:per_group])
    clean = shuffled(candidates[candidates["group"] == "clean"])
    from_rules = clean["sources"].map(lambda s: s != (MODEL,))
    by_rules, by_model = clean[from_rules], clean[~from_rules]
    half = min(len(by_rules), (per_group + 1) // 2)
    rest = min(len(by_model), per_group - half)
    half = min(len(by_rules), per_group - rest)  # fill from the rules if the model falls short
    chosen.append(pd.concat([by_rules.iloc[:half], by_model.iloc[:rest]]))
    for group, frame in zip(GROUPS, chosen, strict=True):
        if len(frame) < per_group:
            logger.warning("Only %d %s cases for %d wanted", len(frame), group, per_group)
    selected = pd.concat(chosen)
    return selected.iloc[rng.permutation(len(selected))].reset_index(drop=True)


def split_scores(settings: Settings, split: Split) -> tuple[np.ndarray, np.ndarray]:
    """The transaction scores behind the split's model alerts: the champion's on validation
    (trained on train only), and the refit model of the champion's family on test (ADR-0014)."""
    from atalayero.models.registry import Registry  # MLflow is slow to import

    registry = Registry(settings)
    if split == "test":
        path = settings.evaluation.reports_dir / "holdout_scores.parquet"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing: run `make holdout` first")
        frame = duckdb.read_parquet(str(path)).df()
        return frame["transaction_id"].to_numpy(), frame[registry.champion_family()].to_numpy()
    from atalayero.models.data import load_split
    from atalayero.models.families import transaction_scores

    data = load_split(settings, split)
    return data.transaction_ids, transaction_scores(registry.load_champion(), data.features)


def case_paths(settings: Settings, case_set: CaseSet) -> tuple[Path, Path]:
    directory = settings.agent_evals.dir
    return directory / f"{case_set}_alerts.jsonl", directory / f"{case_set}_answers.jsonl"


def build_case_set(settings: Settings, case_set: CaseSet) -> tuple[Path, Path]:
    """Sample a case set from its split's alert queue and write its alerts and answers."""
    split = SPLITS[case_set]
    if split == "test":
        logger.warning("Reading the test split: once per reported result (ADR-0005)")
    config = settings.agent_evals
    per_group = config.dev_cases if case_set == "dev" else config.golden_cases
    evaluator = SplitEvaluator(settings, split)
    transaction_ids, scores = split_scores(settings, split)
    evaluator.evaluate_rules(settings.rule_alerts_dir, load_rules(settings.rules_dir))
    rule_hits = evaluator.rule_hits()
    labels = label_account_days(evaluator.detections())
    evaluator.set_scores(transaction_ids, scores)
    queue = queue_cases(
        evaluator.ranking(top=config.top_transactions), rule_hits, config.queue_budget
    )
    candidates = queue.merge(labels, on=["day", "account_key"], how="left")
    candidates["group"] = candidates["group"].fillna("clean")
    candidates["typologies"] = candidates["typologies"].map(
        lambda t: t if isinstance(t, tuple) else ()
    )
    logger.info("%s queue on %s: %s", case_set, split, candidates["group"].value_counts().to_dict())
    selected = sample_cases(candidates, per_group, config.seed)

    alerts_path, answers_path = case_paths(settings, case_set)
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    alerts, answers = [], []
    for case in selected.itertuples():
        alerts.append(
            CaseAlert(
                alert_id=case.alert_id,
                account_key=case.account_key,
                day=case.day,
                sources=case.sources,
                score=float(case.score),
                rank=int(case.rank),
                transaction_ids=case.transaction_ids,
            )
        )
        answers.append(
            CaseAnswer(
                alert_id=case.alert_id,
                group=case.group,
                decision="close" if case.group == "clean" else "escalate",
                typologies=case.typologies,
            )
        )
    alerts_path.write_text("".join(a.model_dump_json() + "\n" for a in alerts))
    answers_path.write_text("".join(a.model_dump_json() + "\n" for a in answers))
    logger.info(
        "%s set: %d cases (%s), written to %s and %s",
        case_set,
        len(alerts),
        selected["group"].value_counts().to_dict(),
        alerts_path,
        answers_path,
    )
    return alerts_path, answers_path


def load_alerts(settings: Settings, case_set: CaseSet) -> list[CaseAlert]:
    alerts_path, _ = case_paths(settings, case_set)
    if not alerts_path.exists():
        raise FileNotFoundError(f"{alerts_path} is missing: run `make golden-set` first")
    return [CaseAlert.model_validate_json(line) for line in alerts_path.read_text().splitlines()]


def load_answers(settings: Settings, case_set: CaseSet) -> dict[str, CaseAnswer]:
    """The answers of a case set: for evaluation only, never for an investigator."""
    _, answers_path = case_paths(settings, case_set)
    answers = [
        CaseAnswer.model_validate_json(line) for line in answers_path.read_text().splitlines()
    ]
    return {answer.alert_id: answer for answer in answers}


def cases_sha256(settings: Settings, case_set: CaseSet) -> str:
    """A checksum of a case set's alerts and answers, recorded in every report on it."""
    digest = hashlib.sha256()
    for path in case_paths(settings, case_set):
        digest.update(path.read_bytes())
    return digest.hexdigest()
