"""Laundering attempts from the patterns file: parse them and map them onto `transaction_id`.

The patterns file lists each documented laundering attempt as a block:

    BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out
    <transaction lines, same format as the transactions CSV>
    END LAUNDERING ATTEMPT - FAN-OUT
    <blank line>
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

from atalayero.ingestion.source import SOURCE_COLUMNS, typed_columns_sql

logger = logging.getLogger(__name__)

TYPOLOGIES = (
    "fan_out",
    "fan_in",
    "cycle",
    "bipartite",
    "stack",
    "random",
    "scatter_gather",
    "gather_scatter",
)

_BEGIN = re.compile(r"BEGIN LAUNDERING ATTEMPT - (?P<kind>[A-Z-]+)(?::\s*(?P<description>.*))?")
_END = re.compile(r"END LAUNDERING ATTEMPT - (?P<kind>[A-Z-]+)")


class PatternsFormatError(ValueError):
    """The patterns file does not have the expected block structure."""


@dataclass(frozen=True)
class LaunderingAttempt:
    attempt_id: int  # 1-based position in the patterns file
    typology: str  # one of TYPOLOGIES
    description: str  # e.g. "Max 16-degree Fan-Out"; empty when the source gives none
    rows: tuple[str, ...]  # transaction lines, verbatim
    block: tuple[str, ...]  # every line from BEGIN to END, verbatim


def _typology(kind: str, line_no: int) -> str:
    typology = kind.lower().replace("-", "_")
    if typology not in TYPOLOGIES:
        raise PatternsFormatError(f"line {line_no}: unknown typology {kind!r}")
    return typology


def parse_patterns(path: Path) -> list[LaunderingAttempt]:
    attempts: list[LaunderingAttempt] = []
    block: list[str] = []
    kind = description = ""
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if begin := _BEGIN.fullmatch(line):
            if block:
                raise PatternsFormatError(f"line {line_no}: BEGIN inside an open attempt")
            kind, description = begin["kind"], begin["description"] or ""
            block = [line]
        elif end := _END.fullmatch(line):
            if not block or end["kind"] != kind:
                raise PatternsFormatError(f"line {line_no}: END without a matching BEGIN")
            block.append(line)
            attempts.append(
                LaunderingAttempt(
                    attempt_id=len(attempts) + 1,
                    typology=_typology(kind, line_no),
                    description=description.strip(),
                    rows=tuple(block[1:-1]),
                    block=tuple(block),
                )
            )
            block = []
        elif not line.strip():
            if block:
                raise PatternsFormatError(f"line {line_no}: blank line inside an attempt")
        elif block:
            block.append(line)
        else:
            raise PatternsFormatError(f"line {line_no}: transaction outside an attempt")
    if block:
        raise PatternsFormatError(f"unterminated attempt: {block[0]}")
    return attempts


def load_laundering_attempts(patterns_path: Path, duckdb_path: Path) -> int:
    """(Re)create `raw.laundering_attempts`: one row per (attempt, transaction); return the count.

    Each pattern line is matched to `raw.transactions` on every source column. The load fails,
    leaving any previous table untouched, unless every line matches exactly one transaction.
    """
    attempts = parse_patterns(patterns_path)
    pattern_rows = [(a, line) for a in attempts for line in a.rows]
    join = " AND ".join(f"t.{name} = p.{name}" for name in SOURCE_COLUMNS)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute(
            f"""
            CREATE TEMP TABLE pattern_rows AS
            SELECT pattern_row, attempt_id, typology, description,
                   {typed_columns_sql("string_split(line, ',')")}
            FROM (
                SELECT unnest(range(1, len($lines) + 1)) AS pattern_row,
                       unnest($attempt_ids) AS attempt_id, unnest($typologies) AS typology,
                       unnest($descriptions) AS description, unnest($lines) AS line
            )
            """,
            {
                "attempt_ids": [a.attempt_id for a, _ in pattern_rows],
                "typologies": [a.typology for a, _ in pattern_rows],
                "descriptions": [a.description for a, _ in pattern_rows],
                "lines": [line for _, line in pattern_rows],
            },
        )
        con.execute(
            f"""
            CREATE TEMP TABLE matches AS
            SELECT p.pattern_row, p.attempt_id, p.typology, p.description, t.transaction_id
            FROM pattern_rows p LEFT JOIN raw.transactions t ON {join}
            """
        )
        (unmatched,) = con.execute(
            "SELECT count(*) FROM (SELECT pattern_row FROM matches GROUP BY 1 "
            "HAVING count(transaction_id) <> 1)"
        ).fetchone()
        if unmatched:
            raise ValueError(
                f"{patterns_path}: {unmatched} pattern lines do not match exactly one transaction"
            )
        con.execute(
            """
            CREATE OR REPLACE TABLE raw.laundering_attempts AS
            SELECT attempt_id, typology, description, transaction_id
            FROM matches ORDER BY attempt_id, transaction_id
            """
        )
    logger.info(
        "Loaded %d attempts (%d transactions) into raw.laundering_attempts",
        len(attempts),
        len(pattern_rows),
    )
    return len(pattern_rows)
