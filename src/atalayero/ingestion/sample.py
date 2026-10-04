"""Build the test fixtures: complete "stories" sampled from the source files, kept verbatim.

A story is a set of focus accounts and a time window. It holds every transaction that touches a
focus account inside the window, so per-account windows (rules, daily activity) are complete for
focus accounts; their counterparties appear only through those transactions. The stories are:

- attempts: one documented laundering attempt per typology, with all its accounts, from
  `STORY_MARGIN` before its first transaction to `STORY_MARGIN` after its last;
- untyped: `n_untyped` laundering transactions outside every documented attempt, with their
  sender and receiver, `STORY_MARGIN` around the transaction;
- clean: accounts never involved in laundering, over `clean_window`: the busiest one (a realistic
  false positive for velocity rules), one that pays in Bitcoin, then seeded picks until the
  sample reaches about `n_rows` rows.

Stories must be self-contained: 180 of the 370 attempts share accounts with another attempt, and
their rows would look untyped in the fixture. Among the rest, attempts and untyped cases are the
ones whose story size is closest to the median of their group, which keeps the fixture
representative and small.
"""

import csv
import hashlib
import logging
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import duckdb

from atalayero.ingestion.patterns import TYPOLOGIES, LaunderingAttempt, parse_patterns
from atalayero.ingestion.source import SOURCE_COLUMNS, SOURCE_TIMESTAMP_FORMAT, sql_literal

logger = logging.getLogger(__name__)

STORY_MARGIN = timedelta(hours=24)
CLEAN_WINDOW = (datetime(2022, 9, 5), datetime(2022, 9, 6, 23, 59))  # a Monday and a Tuesday


class _Story(NamedTuple):
    story: str
    group: str
    key: int
    size: int
    pays_bitcoin: bool


def _closest_to_median(stories: list[_Story], k: int) -> list[_Story]:
    median = statistics.median(s.size for s in stories)
    return sorted(stories, key=lambda s: (abs(s.size - median), s.key))[:k]


def _seeded_order(stories: list[_Story], seed: int) -> list[_Story]:
    return sorted(stories, key=lambda s: hashlib.sha256(f"{seed}:{s.story}".encode()).hexdigest())


def _build_stories(
    con: duckdb.DuckDBPyConnection,
    attempts: list[LaunderingAttempt],
    clean_window: tuple[datetime, datetime],
) -> None:
    """Create the `story_rows` table: (story, group, key, row_id, bitcoin) for every candidate."""
    pattern_lines = [(a.attempt_id, line) for a in attempts for line in a.rows]
    names = list(SOURCE_COLUMNS)
    ts = f"strptime(timestamp, {sql_literal(SOURCE_TIMESTAMP_FORMAT)})"
    con.execute(
        f"""
        CREATE TABLE legs AS
        SELECT rowid AS row_id, {ts} AS ts, from_bank AS bank, from_account AS account,
               is_laundering = '1' AS laundering, payment_format FROM source
        UNION ALL
        SELECT rowid, {ts}, to_bank, to_account, is_laundering = '1', payment_format FROM source
        """
    )
    split = ", ".join(f"parts[{i}] AS {name}" for i, name in enumerate(names, start=1))
    con.execute(
        f"""
        CREATE TABLE attempt_rows AS
        SELECT p.attempt_id, s.rowid AS row_id
        FROM (
            SELECT attempt_id, {split}
            FROM (SELECT unnest($ids) AS attempt_id, string_split(unnest($lines), ',') AS parts)
        ) p
        JOIN source s ON {" AND ".join(f"s.{name} = p.{name}" for name in names)}
        """,
        {"ids": [i for i, _ in pattern_lines], "lines": [line for _, line in pattern_lines]},
    )
    typology_of = {a.attempt_id: a.typology for a in attempts}
    con.execute(
        "CREATE TABLE attempt_typology AS SELECT unnest($ids) AS attempt_id, "
        "unnest($typologies) AS typology",
        {"ids": list(typology_of), "typologies": list(typology_of.values())},
    )
    margin = f"INTERVAL {int(STORY_MARGIN.total_seconds())} SECOND"
    con.execute(
        f"""
        CREATE TABLE candidates AS
        -- attempts: every account of the attempt, around the attempt's time span
        SELECT 'attempt:' || a.attempt_id AS story, t.typology AS grp, a.attempt_id AS key,
               l.bank, l.account,
               min(l.ts) OVER w - {margin} AS lo, max(l.ts) OVER w + {margin} AS hi
        FROM attempt_rows a JOIN attempt_typology t USING (attempt_id)
        JOIN legs l USING (row_id)
        WINDOW w AS (PARTITION BY a.attempt_id)
        UNION ALL
        -- untyped: laundering transactions outside every documented attempt
        SELECT 'untyped:' || row_id, 'untyped', row_id, bank, account, ts - {margin}, ts + {margin}
        FROM legs WHERE laundering AND row_id NOT IN (SELECT row_id FROM attempt_rows)
        UNION ALL
        -- clean: accounts never involved in laundering, active in the clean window
        SELECT DISTINCT 'clean:' || bank || ':' || account, 'clean', 0, bank, account, $lo, $hi
        FROM legs
        WHERE ts BETWEEN $lo AND $hi AND (bank, account) NOT IN (
            SELECT DISTINCT bank, account FROM legs WHERE laundering
        )
        """,
        {"lo": clean_window[0], "hi": clean_window[1]},
    )
    con.execute(
        """
        CREATE TABLE story_rows AS
        SELECT DISTINCT c.story, c.grp, c.key, l.row_id, l.payment_format = 'Bitcoin' AS bitcoin,
               a.attempt_id
        FROM (SELECT DISTINCT * FROM candidates) c
        JOIN legs l ON l.bank = c.bank AND l.account = c.account AND l.ts BETWEEN c.lo AND c.hi
        LEFT JOIN attempt_rows a ON a.row_id = l.row_id
        """
    )


def _select_stories(
    con: duckdb.DuckDBPyConnection, n_rows: int, n_untyped: int, seed: int
) -> tuple[set[int], list[str]]:
    """Pick the stories; return the selected row IDs and story names."""
    # Only self-contained stories: no row of another attempt, so every laundering row in the
    # fixture belongs to a complete sampled attempt or to no attempt at all.
    stories = [
        _Story(*row)
        for row in con.execute(
            "SELECT story, grp, key, count(DISTINCT row_id), bool_or(bitcoin) FROM story_rows "
            "GROUP BY ALL HAVING bool_and(attempt_id IS NULL OR story = 'attempt:' || attempt_id)"
        ).fetchall()
    ]
    by_group: dict[str, list[_Story]] = {}
    for story in stories:
        by_group.setdefault(story.group, []).append(story)

    picked = [s for t in TYPOLOGIES if t in by_group for s in _closest_to_median(by_group[t], 1)]
    if by_group.get("untyped"):
        picked += _closest_to_median(by_group["untyped"], n_untyped)
    clean = _seeded_order(by_group.get("clean", []), seed)
    if clean:
        picked.append(max(clean, key=lambda s: s.size))  # stable: max keeps the first maximum
        picked += [s for s in clean if s.pays_bitcoin and s not in picked][:1]

    def rows_of(names: list[str]) -> set[int]:
        query = "SELECT row_id FROM story_rows WHERE list_contains($names, story)"
        return {r for (r,) in con.execute(query, {"names": names}).fetchall()}

    rows = rows_of([s.story for s in picked])
    for story in (s for s in clean if s not in picked):
        if len(rows) >= n_rows:
            break
        rows |= rows_of([story.story])
        picked.append(story)
    return rows, [s.story for s in picked]


def write_sample(
    csv_path: Path,
    patterns_path: Path,
    out_csv: Path,
    out_patterns: Path,
    n_rows: int = 1000,
    n_untyped: int = 2,
    clean_window: tuple[datetime, datetime] = CLEAN_WINDOW,
    seed: int = 42,
) -> int:
    """Write the story sample to `out_csv` and the selected attempts to `out_patterns`, both
    byte-for-byte as in the source files and in source order; return the number of rows."""
    attempts = parse_patterns(patterns_path)
    names = ", ".join(sql_literal(name) for name in SOURCE_COLUMNS)
    with duckdb.connect() as con:
        # rowid is the position in the file, as in load_batch.csv_to_parquet; all_varchar keeps
        # every value exactly as written (leading zeros, decimals).
        con.execute("SET preserve_insertion_order = true")
        con.execute(
            f"""
            CREATE TABLE source AS
            SELECT * FROM read_csv({sql_literal(csv_path)}, header = true, all_varchar = true,
                                   names = [{names}])
            """
        )
        _build_stories(con, attempts, clean_window)
        rows, stories = _select_stories(con, n_rows, n_untyped, seed)
        values = con.execute(
            "SELECT * FROM source WHERE list_contains($rows, rowid) ORDER BY rowid",
            {"rows": sorted(rows)},
        ).fetchall()

    with csv_path.open(newline="") as f:
        header = f.readline()
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as out:
        out.write(header)
        csv.writer(out, lineterminator="\n").writerows(values)

    selected = {int(s.split(":")[1]) for s in stories if s.startswith("attempt:")}
    blocks = [a.block for a in attempts if a.attempt_id in selected]
    out_patterns.write_text("".join("\n".join(block) + "\n\n" for block in blocks))

    logger.info("Wrote %d rows from %d stories to %s", len(values), len(stories), out_csv)
    logger.info("Stories: %s", ", ".join(stories))
    return len(values)
