# ADR 0017: The experiment analysis is tested against a known effect

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** data-platform

## Context

An A/B analysis that returns a plausible p-value is indistinguishable from a correct one.
The failure modes are quiet: a wrong variance, an arm mix-up, a covariate that is applied to
the wrong period, or a detector that reports significance for everything. None of them
produce an error; all of them produce a number someone will act on.

This is the same problem as evaluating an agent: a test suite only means something when the
right answer is known independently of the system under test.

## Decision

The analysis is run against synthetic experiments whose effect is known by construction, and
the two directions are both asserted:

| Case | Required outcome |
|---|---|
| true lift 5% | significant, estimate within 4–6%, CI excludes zero |
| true lift 0% | **not** significant, CI straddles zero |

Plus two supporting checks:

- **Balance.** The arms must not differ on the pre-period metric (ratio within 2%, Welch
  p ≥ 0.05). A difference there means the split, not the treatment, explains the result.
- **A negative control that can fail.** The 5% data is re-used with the arm labels shuffled,
  so the true effect is exactly zero. If the detector reports a difference there, it is
  reading noise and every positive result is worthless.

Design choices that are load-bearing:

1. **Assignment is `md5(unit)`, not `random()` and not Python's `hash()`.** `hash()` is
   salted per process, so the split would differ between runs and two people comparing
   results would be looking at different experiments. A test pins a known unit's bucket so
   swapping the hash function fails loudly.
2. **CUPED's theta is estimated on pooled pre-period data**, not per arm: the
   covariate–outcome relationship is a property of the population, not of the treatment.
3. **MDE is reported next to every result.** "Not significant" is uninterpretable without
   it: it may mean no effect, or it may mean the experiment could never have seen one.
4. **`p = 0.0` is annotated as underflow.** A t-statistic of 42 underflows a float. Reporting
   `0.0` without the note reads as certainty; the honest statement is that p is below what a
   float can hold.

## Consequences

- The analysis is a module (`experiments/simulate.py`) with a CLI, not a notebook, so the
  same code path is tested and run.
- `experiments/assignment.sql` documents the SQL form of the same assignment for analysts
  working in the warehouse. It is not executed by the tests — the Python path is — and the
  file says so.
- The simulation is not a substitute for a real experiment: it has no interference, no
  novelty effect and no ratio metrics. It tests the estimator, not the world.

## Evidence

`quality/reports/experiments.json`:

| case | true lift | CUPED lift | p | significant |
|---|---|---|---|---|
| known 5% | 5.00% | 5.043% | < 1e-300 | yes |
| known 0% | 0.00% | 0.040% | 0.737 | no |

Variance reduction from the covariate: 87.9%. MDE (relative, 80% power): 0.35% with CUPED
against 1.01% without. 20,000 units, seed 20261022.
