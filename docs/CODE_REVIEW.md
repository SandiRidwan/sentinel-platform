# Code review checklist

What I look for, in the order I look for it. Written down so the review is the same whether
it is a good day or a Friday afternoon.

## 1. Does it do what it says?

- The change description matches the diff. If the description is vague, that is the first
  thing to fix — a vague description usually hides a vague change.
- If it claims a number, where did the number come from? A measured value has a command
  behind it. An estimated value says so.

## 2. Is failure visible?

This is the check I care most about, because the failures that hurt are the quiet ones.

- An `except` that changes the meaning of a result must surface that in the output, not only
  in a log.
- A pipeline stage that can produce zero rows must fail, not succeed with zero.
- A test suite that can verify nothing must fail. (`L4 expectations` reported `PASS` over an
  entirely skipped set; that is now a failure.)
- A gate that can be bypassed is decoration. If the CI job says `continue-on-error`, ask what
  it is for.

## 3. Is the type honest?

- Does a column named `games_played` come back as `INTEGER` from one path and `BIGINT` from
  another? Schema drift between writers is how a lake becomes unreadable.
- Does an all-null column become a string in one flush and an int in the next? Declare the
  schema at write time; do not infer it per batch.
- Does a numeric parse failure become `None`, or does it become `0`? `None` is honest. `0` is
  a fabricated measurement.

## 4. Are the boundaries respected?

- Does this module import something that belongs to another layer? `tools/` should not import
  `ingestion/`; a spider should not import the dbt project.
- Does a subprocess call use `sys.executable`? Using bare `python` silently leaves the
  virtualenv.
- Does a path assume a working directory? A relative path baked into a view works only while
  the process happens to be somewhere specific.

## 5. Is it tested at the right level?

- Logic with branches → unit test.
- A page selector → contract test.
- A model contract → dbt test.
- A distribution that should not collapse → expectation.
- A test that asserts "the request returned" is not a test. It is a test that the network
  exists. Assert the status code, or assert the error is empty, or assert the row count.

## 6. Is the security posture unchanged?

- Any new secret in the diff? Check the shape of the name, not just the obvious ones:
  `*cookiejar*`, `*_jar.txt`, `*.har`, `_PRIVATE_*`, `.env`.
- Any new external call? Which host, and is it in `governance/sources.yml`?
- Any new dependency? Why that one, and what does it bring in?

## 7. Would the next person understand it?

- Does the comment explain **why**, not what? `# increment i` is noise. `# the third
  breadcrumb item has no link on the catalogue page` is the reason the code is shaped that way.
- Is there a note where a decision was surprising? A short ADR beats a paragraph in a PR that
  nobody will read again.

## What I do not block on

- Formatting. `ruff format` decides, not a preference.
- Naming, unless it is misleading. `countrys` was worth blocking; `cnt` is not.
- Test style, as long as the assertion is real.
- Perfect coverage. Coverage is a proxy; a real assertion over the thing that breaks is worth
  more than 100% of nothing.

## How to give the review

- Say what is wrong, where, and what would make it right.
- Separate "this is broken" from "I would have done it differently". Only the first blocks.
- If I cannot explain why something is wrong, I have not finished reviewing it.
