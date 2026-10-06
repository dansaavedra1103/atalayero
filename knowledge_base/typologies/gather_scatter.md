# Gather-scatter (`gather_scatter`)

## What it is
Several accounts send money to one account (gather), which then spreads it over several
other accounts (scatter). The central account works as a hub that mixes the incoming funds
before dispersing them.

## How it shows in the transactions
- A fan-in to the central account followed, soon after, by a fan-out from it.
- What leaves is close to what came in, over hours or a few days.
- The senders and the receivers are different sets of accounts.

## What to check
- `get_transactions` over the last days on the alerted account: many distinct senders, then
  many distinct receivers, with totals that match.
- `get_account_profile`: is the account otherwise quiet, so that it only relays?
- `get_graph_neighborhood`: the counterparties on each side, and whether they relay too.

## Easily confused with
A treasury or payroll account that is funded by a few transfers and pays many employees, or
a merchant that collects from customers and pays suppliers. Those have a steady, recurring
history.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424. Concentration and redistribution of
funds through accounts of third parties appear throughout GAFILAT, *Informe de Tipologías
Regionales de LA/FT 2025* (use of third parties; structuring).
