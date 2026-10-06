Write the case report now, as JSON with these fields:
- `decision`: "escalate" or "close".
- `typology`: one of fan_out, fan_in, cycle, bipartite, stack, random, scatter_gather,
  gather_scatter or unclassified when you escalate; "none" when you close.
- `evidence`: the transactions your decision rests on, at most 8, each as
  {"transaction_id": <id>, "amount_usd": <amount>}. Cite only transactions a tool showed you,
  with the `amount_usd` exactly as the tool gave it. An escalation cites at least one.
- `confidence`: from 0 to 1, how sure you are of the decision.
- `narrative`: at most 120 words explaining the decision. Refer to transactions as #<id>, and
  only to those you cite as evidence.
