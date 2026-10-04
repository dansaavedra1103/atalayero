from pathlib import Path

from atalayero.ingestion.sample import write_sample


def test_sample_rows_are_verbatim_stratified_and_ordered(sample_csv: Path, tmp_path: Path) -> None:
    out = tmp_path / "sample.csv"

    rows = write_sample(sample_csv, out, n_rows=100, laundering_share=0.2, seed=7)

    source_lines = sample_csv.read_text().splitlines()
    header, *body = out.read_text().splitlines()
    assert rows == len(body) == 100
    assert header == source_lines[0]  # duplicate "Account" column kept as in the source
    assert sum(line.endswith(",1") for line in body) == 20
    positions = [source_lines.index(line) for line in body]  # raises if a line was altered
    assert positions == sorted(positions)


def test_sample_is_deterministic(sample_csv: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"

    write_sample(sample_csv, first, n_rows=50, seed=3)
    write_sample(sample_csv, second, n_rows=50, seed=3)

    assert first.read_bytes() == second.read_bytes()
