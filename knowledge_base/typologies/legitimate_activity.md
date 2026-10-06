# Legitimate activity that looks suspicious (`none`)

## What it is
Many alerts are raised by activity that only resembles laundering. An investigation should
close them, with typology `none`, when the evidence shows an ordinary explanation.

## Common cases
- **Busy accounts:** companies, merchants and payment processors have many counterparties
  and many transactions every day. Rules that count counterparties or velocity fire on them.
- **Two-way trade:** a supplier that is also a customer, or a loan and its repayment, create
  round trips that a cycle rule flags.
- **Regular disbursements:** payroll and supplier runs look like a fan-out, but repeat with
  the same receivers and similar amounts every period.
- **One-off large payments:** a house, a car or a tax payment can make an account's day
  unusual without any layering around it.

## What to check
- `get_account_profile`: a long, steady history with the same payment formats and
  currencies argues for legitimate activity.
- `get_transactions`: recurring counterparties and amounts that fit a business.
- `get_graph_neighborhood`: money that comes back in a cycle at very different amounts, or
  after a long delay, is less likely to be a laundering round trip.
- `explain_score`: a low score, driven by features unrelated to the alerted pattern, also
  argues for closing.

## How to report it
Close with typology `none` and cite the transactions that show the ordinary explanation.

## Sources
Threshold rules on counts and velocity fire on high-volume accounts by construction. GAFILAT,
*Informe de Tipologías Regionales de LA/FT 2025*, treats movements that are inconsistent with
a client's economic capacity, or that lack an economic justification, as the red flags: the
reverse of what an ordinary explanation shows.
