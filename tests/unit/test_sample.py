from pathlib import Path

from atalayero.ingestion.sample import write_sample

HEADER = (
    "Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,"
    "Amount Paid,Payment Currency,Payment Format,Is Laundering"
)
Account = tuple[str, str]


def _account(bank: str, name: str) -> Account:
    return bank, name


A, B, C, D, E, F, G, H, X = (_account("001", name) for name in "ABCDEFGHX")
K, L, M, N = (_account("0020", name) for name in "KLMN")
P = [_account("003", f"P{i}") for i in range(4)]


def _tx(
    when: str,
    sender: Account,
    receiver: Account,
    laundering: bool = False,
    fmt: str = "ACH",
    amount: str = "100.00",
) -> str:
    currency = "Bitcoin" if fmt == "Bitcoin" else "US Dollar"
    return (
        f"2022/09/{when},{sender[0]},{sender[1]},{receiver[0]},{receiver[1]},"
        f"{amount},{currency},{amount},{currency},{fmt},{int(laundering)}"
    )


def _block(kind: str, rows: list[str]) -> list[str]:
    return [f"BEGIN LAUNDERING ATTEMPT - {kind}", *rows, f"END LAUNDERING ATTEMPT - {kind}"]


def _write_sources(tmp_path: Path, rows: list[str], blocks: list[list[str]]) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    csv_path, patterns_path = tmp_path / "trans.csv", tmp_path / "patterns.txt"
    csv_path.write_text("\n".join([HEADER, *rows]) + "\n")
    patterns_path.write_text("".join("\n".join(b) + "\n\n" for b in blocks))
    return csv_path, patterns_path


def _sample(tmp_path: Path, rows: list[str], blocks: list[list[str]], **kwargs: int) -> list[str]:
    csv_path, patterns_path = _write_sources(tmp_path, rows, blocks)
    out_csv, out_patterns = tmp_path / "out.csv", tmp_path / "out.txt"
    written = write_sample(csv_path, patterns_path, out_csv, out_patterns, **kwargs)
    header, *body = out_csv.read_text().splitlines()
    assert header == HEADER
    assert written == len(body)
    return body


# Clean window by default: 2022-09-05 00:00 to 2022-09-06 23:59.
FAN_OUT = [_tx("05 10:00", A, B, True), _tx("05 11:00", A, C, True)]
CYCLE = [_tx("05 09:00", D, E, True), _tx("05 09:30", E, D, True)]
BEFORE_FAN_OUT = _tx("04 12:00", A, X)  # 22 h before the fan-out: part of its story
OUTSIDE = _tx("02 00:00", A, X)  # 58 h before: in no story
UNTYPED = _tx("06 08:00", F, G, True)  # laundering outside every documented attempt
AFTER_UNTYPED = _tx("06 20:00", G, H)
BUSY = [_tx(f"05 15:0{i}", p, K) for i, p in enumerate(P)]  # K: busiest clean account
BITCOIN = _tx("05 13:00", L, M, fmt="Bitcoin", amount="0.012345")
LATE = _tx("20 00:00", N, M)  # after the clean window
ROWS = [
    OUTSIDE,
    BEFORE_FAN_OUT,
    *CYCLE,
    *FAN_OUT,
    BITCOIN,
    *BUSY,
    UNTYPED,
    AFTER_UNTYPED,
    LATE,
]
BLOCKS = [_block("FAN-OUT", FAN_OUT), _block("CYCLE", CYCLE)]


def test_sample_holds_complete_stories_in_source_order(tmp_path: Path) -> None:
    body = _sample(tmp_path, ROWS, BLOCKS, n_rows=0, n_untyped=1)

    assert body == [row for row in ROWS if row not in (OUTSIDE, LATE)]
    assert (tmp_path / "out.txt").read_text() == (tmp_path / "patterns.txt").read_text()


def test_attempts_sharing_accounts_are_skipped(tmp_path: Path) -> None:
    fan_out = [_tx("05 10:00", A, B, True), _tx("05 11:00", A, C, True)]
    fan_in = [_tx("05 12:00", B, D, True), _tx("05 12:30", E, D, True)]  # shares B
    cycle = [_tx("05 09:00", F, G, True), _tx("05 09:30", G, F, True)]
    blocks = [_block("FAN-OUT", fan_out), _block("FAN-IN", fan_in), _block("CYCLE", cycle)]

    body = _sample(tmp_path, [*fan_out, *fan_in, *cycle], blocks, n_rows=0)

    assert body == cycle
    assert (tmp_path / "out.txt").read_text() == "\n".join(_block("CYCLE", cycle)) + "\n\n"


def test_sample_is_deterministic(tmp_path: Path) -> None:
    first = _sample(tmp_path / "a", ROWS, BLOCKS, n_rows=100)
    second = _sample(tmp_path / "b", ROWS, BLOCKS, n_rows=100)

    assert first == second
