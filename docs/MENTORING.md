# Mentoring notes

How I would bring someone into this codebase, and what I would expect them to learn. Written
down because "I can mentor" is a claim, and a document that could onboard a real person is
evidence of it.

## Week 1: read, then change one thing

**Day 1-2 — understand the shape.** Do not read the code yet. Read, in this order:

1. `README.md` — what the platform claims.
2. `governance/sources.yml` — what it is allowed to touch, and what was refused.
3. `docs/adr/` — the decisions, including the ones that went wrong first.
4. `docs/rfc/0001-...` — an open problem, deliberately not solved.

**Day 3 — run it.** `make doctor`, `make up`, `make seed`, `make scrape`, `make quality`.
If any step fails, the runbook is the first place to look and the second is the log. Fixing a
setup problem is a legitimate first task.

**Day 4-5 — one small change.** Add a spider for one more sandbox source. That touches the
register, the item schema, the contract test and the warehouse model — the whole vertical, at
a size where a mistake is cheap.

## What I would teach, and in what order

### 1. Failure should be loud

The first thing to internalise is that a pipeline that lies is worse than one that stops. Every
bug in this repository's history that mattered was a quiet one:

- A masking rule that stopped matching, leaving PII in the output. Looks identical to a clean
  dataset.
- A staging cache that skipped existing files, so a re-scrape was ignored and the warehouse
  served yesterday's rows. No error anywhere.
- A spider reading the category from the wrong page, so every row had `NULL`. The join still
  "worked" and returned one group.

The habit: when something succeeds, ask what it would look like if it had silently done
nothing. If the answer is "the same", the check is missing.

### 2. Prove the layer, do not trust the boundary

Four times in this build, the layer above was correct and the layer below was not:

- `utls` negotiated HTTP/2 while Go's transport spoke HTTP/1.1. The symptom was a binary
  frame reported as a malformed HTTP response.
- A relative path baked into a dbt view resolved only while the working directory happened to
  be the dbt project.
- Two writers produced different columns in the same directory, and the reader took the schema
  from whichever file it opened first.
- Metabase needed its own database, which compose created and Kubernetes did not.

The habit: when two components meet, test the seam, not each side.

### 3. Say what you did not do

Every ADR in this repository has a "Consequences" section that names the cost. Every README
says what is not implemented. `docs/rfc/0001` is explicitly **not built**.

This is not modesty. A reviewer who finds an unstated limitation stops trusting the stated
ones.

### 4. Measure before optimising, and measure the right thing

The Go benchmark reports ~30,500 requests/second against a local server. That number says
nothing about any real target, and the README says so. A benchmark quoted without its
conditions is marketing.

The same applies to the quality suite: `PASS=41` means something only because the tests assert
row counts, column types and value ranges — not because 41 is a large number.

## How I would review their first PR

- Read the description before the diff. If the description is vague, that is the first comment.
- Find the failure path. What happens when the thing this depends on is down, empty, or slow?
- Ask for one test that would fail if the change were reverted. If they cannot name one, the
  change is not verified.
- Approve when it is good. A review that never approves teaches nothing about the standard.

## What I would not do

- **Not rewrite their PR.** A corrected PR teaches the correction once; a comment that
  explains the reasoning teaches it for the next ten.
- **Not hand over the hard part.** The interesting bug is the lesson. Pair on it, do not take
  the keyboard.
- **Not pretend to know.** If I do not know why something behaves a certain way, saying so is
  faster and more useful than a confident guess. Half the entries in this repository's ADRs
  started as "I do not know why that failed".
