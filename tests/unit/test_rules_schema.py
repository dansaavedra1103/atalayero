from datetime import timedelta
from pathlib import Path

import pydantic
import pytest
import yaml

from atalayero.rules.schema import Rule, load_rules, parse_duration

REPO_ROOT = Path(__file__).parents[2]

VALID = {
    "id": "R99",
    "name": "test_rule",
    "owner": "tests",
    "version": "1.1",
    "description": "A rule for tests",
    "metric": "outgoing_transactions",
    "window": "1h",
    "threshold": {"min_count": 10},
    "cooldown": "24h",
    "history": [
        {"version": "1.0", "date": "2026-10-01", "reason": "Initial version"},
        {"version": "1.1", "date": "2026-10-04", "reason": "Raise the threshold"},
    ],
}


def test_repository_rules_are_valid() -> None:
    rules = load_rules(REPO_ROOT / "config" / "rules")

    assert [(r.id, r.metric) for r in rules] == [
        ("R02", "distinct_counterparties"),
        ("R04", "outgoing_transactions"),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("90m", timedelta(minutes=90)), ("24h", timedelta(hours=24)), ("2d", timedelta(days=2))],
)
def test_parses_durations(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["24 hours", "0h", "h", "24", "1.5h", 24])
def test_rejects_bad_durations(text: object) -> None:
    with pytest.raises(ValueError, match="expected a positive duration"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"version": 1.1}, "valid string"),  # unquoted in YAML: loads as a float
        ({"version": "1.2"}, "add an entry with the reason"),
        ({"thresholds": {"min_count": 3}}, "Extra inputs are not permitted"),
        ({"typology": "smurfing"}, "unknown typology"),
        ({"metric": "amount"}, "distinct_counterparties"),
        ({"threshold": {"min_count": 0}}, "greater than or equal to 1"),
    ],
)
def test_rejects_invalid_rules(change: dict, error: str) -> None:
    with pytest.raises(pydantic.ValidationError, match=error):
        Rule.model_validate(VALID | change)


def _write(directory: Path, filename: str, rule: dict) -> None:
    directory.mkdir(exist_ok=True)
    (directory / filename).write_text(yaml.safe_dump(rule))


def test_file_name_must_match_the_rule(tmp_path: Path) -> None:
    _write(tmp_path, "R98_other_name.yaml", VALID)

    with pytest.raises(ValueError, match="file name must be R99_test_rule.yaml"):
        load_rules(tmp_path)


def test_rule_ids_are_unique(tmp_path: Path) -> None:
    _write(tmp_path, "R99_test_rule.yaml", VALID)
    _write(tmp_path / "other", "R99_test_rule.yaml", VALID)  # not loaded: other directory
    _write(tmp_path, "R99_second.yaml", VALID | {"name": "second"})

    with pytest.raises(ValueError, match="duplicate rule IDs"):
        load_rules(tmp_path)
