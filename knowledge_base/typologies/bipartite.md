# Bipartite (`bipartite`)

## What it is
A set of input accounts sends money to a set of output accounts, many-to-many. There is no
single hub: each input pays several outputs and each output receives from several inputs.
It spreads and mixes the money in one layer.

## How it shows in the transactions
- The alerted account is one of several that all pay the same group of receivers, or one of
  several receivers paid by the same group of senders.
- Transfers between the two groups happen within a short period, with comparable amounts.
- Little or no money flows inside each group.

## What to check
- `get_graph_neighborhood` with `hops=2`: second-hop accounts reached through several of the
  alerted account's counterparties (a high `via` count) suggest a shared group.
- `get_transactions` on a few counterparties: do they share the same partners?

## Easily confused with
Several branches of one company paying the same suppliers. Look for recurrence, amounts that
fit invoices, and a long history.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424 (money moved from a set of input accounts
to a set of output accounts). Coordinated use of many personal and business accounts is
described in GAFILAT, *Informe de Tipologías Regionales de LA/FT 2025*.
