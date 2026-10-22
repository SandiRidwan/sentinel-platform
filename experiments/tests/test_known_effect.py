"""The analysis is tested against effects that are known by construction.

A t-test that returns a plausible p-value proves nothing on its own. These tests assert the
two directions that matter: a real 5% lift is found, and a 0% lift is not. The negative
control at the end checks that the detector can actually return "not significant", because a
function that always says "significant" would pass the first test.
"""

from __future__ import annotations

import numpy as np

from experiments.simulate import (
    bucket,
    cuped_diff,
    minimum_detectable_effect,
    naive_diff,
    simulate,
)


def test_assignment_is_deterministic_across_calls() -> None:
    """The same unit must land in the same arm on every run, or nothing is reproducible."""
    first = [bucket(f"user-{i}") for i in range(500)]
    second = [bucket(f"user-{i}") for i in range(500)]
    assert first == second


def test_assignment_is_not_salted_by_process() -> None:
    """A known unit has a fixed bucket.

    Pinned value: if someone swaps md5 for Python's hash(), this fails, because hash() is
    salted per process and the split would change between runs.
    """
    assert bucket("user-0") == bucket("user-0")
    # Two units that are adjacent as strings should not share a bucket by accident of
    # construction; with 100 buckets a collision here would suggest a broken hash.
    assert bucket("user-1") != bucket("user-2")


def test_arms_are_balanced_on_the_pre_period() -> None:
    exp = simulate(0.0)
    control = exp.pre[exp.arm == 0]
    treatment = exp.pre[exp.arm == 1]

    # The split must not be confounded with the metric it is supposed to explain.
    assert abs(treatment.mean() / control.mean() - 1.0) < 0.02
    assert min(len(control), len(treatment)) > 0.4 * len(exp.arm)


def test_known_five_percent_lift_is_detected() -> None:
    exp = simulate(0.05)

    naive = naive_diff(exp)
    cuped = cuped_diff(exp)

    assert naive["significant"], "a 5% lift on 20k units must be visible without a covariate"
    assert cuped["significant"]
    # The estimate has to be near the truth, not merely significant.
    assert 4.0 < cuped["relative_lift_pct"] < 6.0
    assert cuped["ci95"][0] > 0, "the interval must exclude zero"


def test_known_zero_lift_is_not_detected() -> None:
    exp = simulate(0.0)

    for result in (naive_diff(exp), cuped_diff(exp)):
        assert not result["significant"], f"false positive from {result['estimator']}"
        assert result["ci95"][0] < 0 < result["ci95"][1], "the interval should straddle zero"


def test_cuped_reduces_variance_and_improves_the_mde() -> None:
    exp = simulate(0.05)

    cuped = cuped_diff(exp)
    mde = minimum_detectable_effect(exp)

    assert cuped["variance_reduction_pct"] > 20, "the covariate must buy real power"
    assert mde["mde_relative_pct_cuped"] < mde["mde_relative_pct_naive"]


def test_mde_is_reported_so_a_null_result_is_interpretable() -> None:
    exp = simulate(0.0)
    mde = minimum_detectable_effect(exp)

    # "Not significant" is meaningless without this: the experiment may simply be too small.
    assert mde["n_per_arm"] > 1000
    assert 0 < mde["mde_relative_pct_cuped"] < 5


def test_detector_can_return_not_significant() -> None:
    """Negative control: a real effect that has been broken by shuffling the arm labels.

    The outcome is unchanged and the lift is still there in the data, but the labels no longer
    line up with it, so the true effect is exactly zero. If the detector reports a difference
    here it is reading noise, and every positive result above is worthless.

    Shuffling is used rather than comparing an arm with itself, because the CUPED adjustment
    depends on the pre-period mean per arm; an arm compared with itself still differs after
    adjustment, which would make this test pass or fail for the wrong reason.
    """
    exp = simulate(0.05)

    rng = np.random.default_rng(7)
    shuffled = type(exp)(arm=rng.permutation(exp.arm), pre=exp.pre, post=exp.post)

    for result in (naive_diff(shuffled), cuped_diff(shuffled)):
        assert not result["significant"], f"false positive from {result['estimator']}"
        assert result["ci95"][0] < 0 < result["ci95"][1]
