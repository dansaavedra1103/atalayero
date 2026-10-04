"""Format of the source files: columns of the transactions CSV, shared by every ingestion step."""

# The source header repeats "Account" (sender, then receiver), so columns are named explicitly.
# Bank IDs have leading zeros ("010" is not "10"), so they stay VARCHAR. Account IDs are unique only
# together with their bank: 8 account IDs appear under more than one bank.
# Amounts have up to 6 decimals (Bitcoin) and 13 integer digits: DECIMAL(20, 6) is exact.
SOURCE_COLUMNS: dict[str, str] = {
    "timestamp": "TIMESTAMP",
    "from_bank": "VARCHAR",
    "from_account": "VARCHAR",
    "to_bank": "VARCHAR",
    "to_account": "VARCHAR",
    "amount_received": "DECIMAL(20, 6)",
    "receiving_currency": "VARCHAR",
    "amount_paid": "DECIMAL(20, 6)",
    "payment_currency": "VARCHAR",
    "payment_format": "VARCHAR",
    "is_laundering": "BOOLEAN",
}
SOURCE_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M"


def sql_literal(value: object) -> str:
    """Quote a value as a SQL string literal (COPY and DDL statements take no parameters)."""
    return "'" + str(value).replace("'", "''") + "'"


def typed_columns_sql(parts: str) -> str:
    """SQL select list that types a VARCHAR[] of source-format fields (a CSV line split on ',').

    Source fields are never quoted and never contain commas, so splitting on ',' is exact.
    """
    exprs = []
    for i, (name, type_) in enumerate(SOURCE_COLUMNS.items(), start=1):
        field = f"{parts}[{i}]"
        if type_ == "TIMESTAMP":
            expr = f"strptime({field}, {sql_literal(SOURCE_TIMESTAMP_FORMAT)})"
        else:
            expr = f"CAST({field} AS {type_})"
        exprs.append(f"{expr} AS {name}")
    return ", ".join(exprs)
