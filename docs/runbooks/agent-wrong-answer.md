# Runbook: the agent gave a wrong answer

## First: is it a wrong answer, or a refusal in the wrong place?

```bash
python -m agents.ask --json "your question"
```

Read three fields in this order:

1. `plan.tool` — `refuse` means the agent declined. That is a correct outcome when the data
   is not there, and a bug when it is.
2. `sql` — the exact query that ran. If the SQL does not express the question, the bug is in
   the planner. If it does, the bug is in the data or in `verify`.
3. `rows` — what came back. Zero rows with a non-count tool becomes a refusal by design.

## Cause 1 — the planner matched the wrong word

The plan is a deterministic mapping from words to a table, a column and a metric, and it
fails in ways that are visible in `plan.rationale`.

The first version of this agent answered "which country has the largest population" with
`250 rows in gold.dim_country` because `"count" in "countries"` is true. Substring matching
for intent is the bug; `_has()` in `agents/graph.py` matches whole words.

If `plan.tool` is `count` for a question that is not asking for a count, look for a hint
collision. Hints are matched longest-first, which is why `"population density"` must exist
as its own key: with only `"population"` and `"density"`, the longer one wins and the
question sorts by total population instead of by density.

## Cause 2 — the table or column is not in the registry

`plan.tool == "refuse"` with a reason naming the table means the registry does not know it.
That is the gate working. Add the table to `governance/agent_registry.yml` with its columns
and types, and restart nothing — the registry is read per request.

If the table is in the database but should stay unreachable, add it to `denied:` with the
reason. An undocumented exclusion is a future bug.

## Cause 3 — the answer is right but the summary is not

`summarise()` reads column names from the result rather than composing a sentence. If the
summary is misleading, check the column names the tool returned — a lookup that returns
`country_name` and a lookup that returns `group_key` take different branches.

## Cause 4 — the data changed and the eval still passes

The eval compares against expectations computed by SQL in `agents/eval.py`, so it follows
the data. If the warehouse is empty or half-loaded, valid questions start returning zero
rows and the eval fails loudly, which is correct.

## After any fix

```bash
python -m agents.eval     # 20 answered, 10 refused; must be 30/30
pytest agents/tests -q    # the registry gate's own tests
```

Then add the question that failed to the eval set. A wrong answer that is not in the eval
set will come back.

## What not to do

Do not relax `verify` to return a row of zeros when the result is empty, and do not add the
question to `OUT_OF_SCOPE_TERMS` to make it refuse. Both turn a bug into a policy, and the
next person cannot tell the difference.
