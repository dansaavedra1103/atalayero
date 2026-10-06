# Stack (`stack`)

## What it is
A bipartite pattern with more layers: money moves from a first group of accounts to a
second group, and from the second to a third. Each layer adds distance between the origin
and the destination of the funds.

## How it shows in the transactions
- The alerted account both receives from a group of accounts and pays another group, within
  a short time, passing most of the money on.
- Its counterparties on each side do the same with further groups.
- Amounts that shrink or split a little at each layer.

## What to check
- `get_graph_neighborhood` with `hops=2`: several counterparties that lead to the same
  second-hop accounts, on both the incoming and the outgoing side.
- `get_transactions`: does the account pass on what it receives, soon after?
- Repeat on a counterparty to see whether it is a layer too.

## Easily confused with
Supply chains and intermediaries (distributors, payment agents) that buy and sell between
the same groups of companies, with a steady history.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424, as a bipartite pattern with an
additional layer. Successive transfers between many third-party accounts are described in
GAFILAT, *Informe de Tipologías Regionales de LA/FT 2025*.
