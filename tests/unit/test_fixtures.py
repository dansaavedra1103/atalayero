"""Guards on the committed fixtures, so a regeneration cannot silently drop what tests rely on."""

from pathlib import Path

from atalayero.ingestion.patterns import TYPOLOGIES, parse_patterns


def test_one_complete_attempt_per_typology(sample_csv: Path, sample_patterns: Path) -> None:
    attempts = parse_patterns(sample_patterns)
    rows = set(sample_csv.read_text().splitlines()[1:])

    assert sorted(a.typology for a in attempts) == sorted(TYPOLOGIES)
    assert all(line in rows for a in attempts for line in a.rows)


def test_covers_untyped_laundering_and_bitcoin(sample_csv: Path, sample_patterns: Path) -> None:
    rows = sample_csv.read_text().splitlines()[1:]
    in_attempts = {line for a in parse_patterns(sample_patterns) for line in a.rows}

    assert any(r.endswith(",1") and r not in in_attempts for r in rows), "no untyped laundering"
    assert any(",Bitcoin," in r for r in rows), "no Bitcoin amounts"
