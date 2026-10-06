# Random (`random`)

## What it is
Money hops through a chain of accounts controlled by the same group, in no regular shape and
without coming back to the start: a random walk. Each hop puts more distance between the
money and its origin.

## How it shows in the transactions
- An account receives a sum and passes almost all of it on soon after, to an account that
  does the same.
- No branching into many receivers and no return to the origin, unlike fan-out or cycle.
- Similar amounts along the chain, sometimes with changes of payment format or currency.

## What to check
- `get_transactions`: is money passed on soon after it arrives, at a similar amount?
- `get_graph_neighborhood` with `hops=2`: follow where the money went and where it came from.
- Repeat `get_transactions` on the next account in the chain to see if it relays too.

## Easily confused with
An account that receives a salary and pays a few bills: it passes money on, but to
different, recurring payees and in amounts unrelated to what came in.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424, as a cycle whose funds do not return to
the original account. Successive transfers between third-party accounts to make funds hard
to trace are described in GAFILAT, *Informe de Tipologías Regionales de LA/FT 2025*.
