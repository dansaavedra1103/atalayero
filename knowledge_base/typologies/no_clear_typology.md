# Laundering without a clear typology (`unclassified`)

## What it is
Not all laundering has a recognisable shape. Some of it looks like ordinary economic
activity: payments disguised as payroll, purchases from suppliers or services, made to give
cleaned money a legitimate use (integration). Mule accounts, recruited to receive and pass
on money for a fee, also leave only small traces.

## How it shows in the transactions
- Individually unremarkable transfers, but out of line with the account's own history:
  sudden new counterparties, a payment format or currency the account rarely uses, or
  amounts unlike its usual ones.
- An account with little history that suddenly receives and pays out comparable sums.
- Money that arrives and leaves within a short time, without a clear economic reason.

## What to check
- `get_account_profile`: compare the alerted day with the whole history (counts, amounts,
  counterparties, payment formats, currencies).
- `explain_score`: which features drove the model's score, and do they point at something
  concrete in the transactions?
- `get_transactions`: is the activity consistent with a business or a household?

## How to report it
Escalate with typology `unclassified` when the evidence supports laundering but matches none
of the patterns. Do not force a specific typology onto it.

## Sources
Altman et al., arXiv:2306.16424, note that laundering is not limited to specific patterns
and include integration payments disguised as legitimate activity. Mule accounts and
third-party fronts are described in GAFILAT, *Informe de Tipologías Regionales de LA/FT
2025* (use of third parties).
