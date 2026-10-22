"""Experiment analysis with a known answer.

The point of this module is not that it can compute a t-test. It is that the pipeline is
tested against an effect that is known by construction, so a bug in the analysis cannot hide
behind a plausible-looking p-value. Two cases are run on every execution:

    lift = 0.05   must be detected as significant
    lift = 0.00   must NOT be detected as significant

If the first fails, the analysis is too noisy or the estimator is wrong. If the second fails,
the analysis reports an effect that is not there, which is worse than missing one.

Assignment is a hash of the unit key, so the split is identical on every run and on every
machine. CUPED uses the pre-period metric as a covariate, which is the standard way to buy
power without collecting more data.

    python -m experiments.simulate
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats

REPORT = Path("quality/reports/experiments.json")

ALPHA = 0.05
POWER = 0.80
UNITS = 20_000
SEED = 20261022


def bucket(unit: str, buckets: int = 100) -> int:
    """Stable bucket for a unit key.

    md5 rather than Python's hash(), because hash() is salted per process and would give a
    different split on every run. That single choice is the difference between a
    reproducible experiment and a coincidence.
    """
    digest = hashlib.md5(unit.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % buckets


@dataclass
class Experiment:
    arm: np.ndarray          # 0 = control, 1 = treatment
    pre: np.ndarray          # pre-period metric, the covariate
    post: np.ndarray         # post-period metric, the outcome


def simulate(lift: float, units: int = UNITS, seed: int = SEED) -> Experiment:
    """A synthetic experiment with a known relative lift on the outcome.

    The pre-period metric carries the user-level noise that makes experiments expensive:
    without a covariate, that variance sits in the outcome and swamps a small effect.
    """
    rng = np.random.default_rng(seed)

    # Per-unit baseline quality, so pre and post are correlated the way real data is.
    baseline = rng.normal(50.0, 12.0, units)
    pre = baseline + rng.normal(0.0, 3.0, units)

    arms = np.array([bucket(f"user-{i}") < 50 for i in range(units)], dtype=np.int8)
    # A 0.05 lift applies only to the treatment arm; the null case multiplies by 1.0.
    multiplier = np.where(arms == 1, 1.0 + lift, 1.0)
    post = baseline * multiplier + rng.normal(0.0, 3.0, units)

    return Experiment(arm=arms, pre=pre, post=post)


def naive_diff(exp: Experiment) -> dict:
    control = exp.post[exp.arm == 0]
    treatment = exp.post[exp.arm == 1]
    return _welch(control, treatment, label="naive")


def cuped_diff(exp: Experiment) -> dict:
    """Difference in means after removing the part of the outcome the covariate explains.

    theta is estimated on the pooled pre-period data, not per arm, because the covariate
    relationship is a property of the population, not of the treatment.
    """
    cov = np.cov(exp.pre, exp.post)
    theta = cov[0, 1] / cov[0, 0]

    adjusted = exp.post - theta * (exp.pre - exp.pre.mean())

    control = adjusted[exp.arm == 0]
    treatment = adjusted[exp.arm == 1]
    result = _welch(control, treatment, label="cuped")
    result["theta"] = round(float(theta), 4)
    result["variance_reduction_pct"] = round(
        100 * (1 - adjusted.var() / exp.post.var()), 2
    )
    return result


def _welch(control: np.ndarray, treatment: np.ndarray, label: str) -> dict:
    diff = float(treatment.mean() - control.mean())
    t_stat, p_value = stats.ttest_ind(treatment, control, equal_var=False)
    se = float(np.sqrt(treatment.var(ddof=1) / len(treatment) + control.var(ddof=1) / len(control)))
    # Welch-Satterthwaite degrees of freedom, so the interval is not quietly assuming
    # equal variances.
    dof = (treatment.var(ddof=1) / len(treatment) + control.var(ddof=1) / len(control)) ** 2 / (
        (treatment.var(ddof=1) / len(treatment)) ** 2 / (len(treatment) - 1)
        + (control.var(ddof=1) / len(control)) ** 2 / (len(control) - 1)
    )
    critical = stats.t.ppf(1 - ALPHA / 2, dof)

    return {
        "estimator": label,
        "control_mean": round(float(control.mean()), 4),
        "treatment_mean": round(float(treatment.mean()), 4),
        "absolute_diff": round(diff, 4),
        "relative_lift_pct": round(100 * diff / float(control.mean()), 3),
        "ci95": [round(diff - critical * se, 4), round(diff + critical * se, 4)],
        "t_statistic": round(float(t_stat), 3),
        # A large enough t underflows scipy's p to exactly 0.0. Recording 0.0 without a note
        # reads as certainty; the honest statement is that p is below what a float can hold.
        "p_value": float(p_value),
        "p_value_note": "underflow (< 1e-300)" if p_value == 0.0 else "",
        "significant": bool(p_value < ALPHA),
        "n_control": int(len(control)),
        "n_treatment": int(len(treatment)),
    }


def minimum_detectable_effect(exp: Experiment) -> dict:
    """The smallest effect this sample could detect, per arm and with the covariate.

    Reported because "not significant" is uninterpretable without it: it may mean no effect,
    or it may mean the experiment was never able to see one.
    """
    z_alpha = stats.norm.ppf(1 - ALPHA / 2)
    z_beta = stats.norm.ppf(POWER)

    def mde(variance: float, n_per_arm: int) -> float:
        return float((z_alpha + z_beta) * np.sqrt(2 * variance / n_per_arm))

    cov = np.cov(exp.pre, exp.post)
    theta = cov[0, 1] / cov[0, 0]
    adjusted = exp.post - theta * (exp.pre - exp.pre.mean())

    n = min(int((exp.arm == 0).sum()), int((exp.arm == 1).sum()))
    baseline = float(exp.post[exp.arm == 0].mean())

    return {
        "n_per_arm": n,
        "mde_absolute_naive": round(mde(exp.post.var(ddof=1), n), 4),
        "mde_absolute_cuped": round(mde(adjusted.var(ddof=1), n), 4),
        "mde_relative_pct_naive": round(100 * mde(exp.post.var(ddof=1), n) / baseline, 3),
        "mde_relative_pct_cuped": round(100 * mde(adjusted.var(ddof=1), n) / baseline, 3),
        "power_target": POWER,
        "alpha": ALPHA,
    }


def balance(exp: Experiment) -> dict:
    """Arms must look alike before the outcome is read."""
    control = exp.pre[exp.arm == 0]
    treatment = exp.pre[exp.arm == 1]
    _, p_value = stats.ttest_ind(treatment, control, equal_var=False)
    ratio = float(treatment.mean() / control.mean())

    return {
        "pre_mean_control": round(float(control.mean()), 4),
        "pre_mean_treatment": round(float(treatment.mean()), 4),
        "ratio": round(ratio, 4),
        "p_value": float(p_value),
        "balanced": bool(p_value >= ALPHA and 0.98 <= ratio <= 1.02),
    }


def run_case(lift: float, label: str) -> dict:
    exp = simulate(lift)
    return {
        "case": label,
        "true_lift_pct": round(lift * 100, 2),
        "balance": balance(exp),
        "naive": naive_diff(exp),
        "cuped": cuped_diff(exp),
        "mde": minimum_detectable_effect(exp),
    }


def main() -> int:
    started = datetime.now(timezone.utc)

    effect = run_case(0.05, "known_5pct_lift")
    null = run_case(0.00, "known_0pct_lift")

    checks = {
        "effect_detected": bool(effect["cuped"]["significant"] and effect["naive"]["significant"]),
        "null_not_detected": bool(
            not null["cuped"]["significant"] and not null["naive"]["significant"]
        ),
        "arms_balanced": bool(effect["balance"]["balanced"] and null["balance"]["balanced"]),
        # The covariate has to actually buy power, otherwise CUPED is decoration.
        "cuped_reduces_variance": bool(effect["cuped"]["variance_reduction_pct"] > 20),
    }

    report = {
        "ran_at": started.isoformat(),
        "units": UNITS,
        "seed": SEED,
        "alpha": ALPHA,
        "power_target": POWER,
        "cases": {"effect": effect, "null": null},
        "checks": checks,
        "result": "PASS" if all(checks.values()) else "FAIL",
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    for case in (effect, null):
        c = case["cuped"]
        print(
            f"{case['case']:20s} true={case['true_lift_pct']:>5}%  "
            f"cuped_lift={c['relative_lift_pct']:>6}%  p={c['p_value']:.3g}  "
            f"significant={c['significant']}"
        )
    print()
    print(f"variance reduction from CUPED : {effect['cuped']['variance_reduction_pct']}%")
    print(f"MDE (cuped, relative)         : {effect['mde']['mde_relative_pct_cuped']}%")
    print(f"MDE (naive, relative)         : {effect['mde']['mde_relative_pct_naive']}%")
    for name, ok in checks.items():
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"RESULT: {report['result']}")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
