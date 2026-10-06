# Fan-in (`fan_in`)

## What it is
Many accounts send money to one account in a short time. It gathers funds that were placed
in pieces, through several people or accounts, into a single point that controls them.

## How it shows in the transactions
- A burst of incoming transfers to one account from two or more distinct senders, often
  within hours or a few days.
- Senders that are new to the account, or that send to it only once.
- Each incoming amount is modest; the total is large for the account.
- The collected money often leaves again soon after, in one or a few larger transfers.

## What to check
- `get_transactions` over the last days: count distinct senders and the total received.
- `get_account_profile`: does the alerted day's incoming activity stand out from the history?
- `get_graph_neighborhood`: do the senders share a common upstream source (`hops=2`)?

## Easily confused with
A merchant or a company collecting payments from customers: many senders, but a steady
history, amounts that match goods or services, and little money leaving straight away.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424. Fragmented deposits by several
coordinated people, later concentrated in accounts or front companies the organisation
controls, are described in GAFILAT, *Informe de Tipologías Regionales de LA/FT 2025*
(structuring, "pitufeo").
