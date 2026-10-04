"""Derive the simulator's exchange rates from the data and write them as a dbt seed.

The source has no FX table, but its cross-currency transactions imply fixed rates: for every
currency, the rate implied by transactions against the US Dollar agrees in both directions to
about 1e-8 (Bitcoin: 6e-4, as its amounts are quoted to 6 decimals). The median absorbs the
rounding of tiny amounts to cents. The seed is committed because the test fixture holds too few
cross-currency transactions to derive every rate (ADR-0003).
"""

import csv
import logging
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

BASE_CURRENCY = "US Dollar"


def derive_fx_rates(duckdb_path: Path) -> list[tuple[str, float, int]]:
    """Return (currency, usd_per_unit, observations) for every currency in `raw.transactions`."""
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        rates = con.execute(
            """
            WITH implied AS (
                SELECT payment_currency AS currency, amount_received / amount_paid AS usd_per_unit
                FROM raw.transactions
                WHERE receiving_currency = $base AND payment_currency <> $base AND amount_paid > 0
                UNION ALL
                SELECT receiving_currency, amount_paid / amount_received
                FROM raw.transactions
                WHERE payment_currency = $base AND receiving_currency <> $base
                  AND amount_received > 0
            )
            SELECT currency, median(usd_per_unit), count(*) FROM implied GROUP BY 1
            UNION ALL
            SELECT $base, 1.0, 0
            ORDER BY 1
            """,
            {"base": BASE_CURRENCY},
        ).fetchall()
        currencies = {
            c
            for (c,) in con.execute(
                "SELECT payment_currency FROM raw.transactions "
                "UNION SELECT receiving_currency FROM raw.transactions"
            ).fetchall()
        }
    missing = currencies - {currency for currency, _, _ in rates}
    if missing:
        raise ValueError(f"no transactions against {BASE_CURRENCY} for: {sorted(missing)}")
    return rates


def write_fx_seed(duckdb_path: Path, out_csv: Path) -> int:
    """Write the derived rates to `out_csv`; return the number of currencies."""
    rates = derive_fx_rates(duckdb_path)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["currency", "usd_per_unit", "observations"])
        writer.writerows((c, f"{rate:.12g}", n) for c, rate, n in rates)
    logger.info("Wrote %d exchange rates to %s", len(rates), out_csv)
    return len(rates)
