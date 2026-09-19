"""Adapter verifiers: false-alarm and power of the gate under realistic benchmark noise.

The default gate uses an *asymptotic* confidence sequence with a prior on
run-to-run noise and a minimum pair count. That trades an exact guarantee for
speed, so its behaviour must be measured, not assumed. These verifiers simulate
paired benchmark runs with lognormal run noise plus occasional outliers (a GC
pause, a noisy neighbour) and report:

* false-alarm rate (null: identical code) at several noise levels — must be <= alpha + slack;
* detection rate and median pairs-to-verdict at 5%, 10%, 25% regressions;
* the known-bad rule — "fail if the first pair's ratio exceeds the margin" — whose
  false-alarm rate must be detected as unacceptable.

Requires the private core (``boson_cs``); run in Boson's repositories only.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from .gate import MIN_PAIRS, PRIOR_NOISE, PRIOR_WEIGHT, ROBUST_CLIP


@dataclass
class VerifierRecord:
    name: str
    kind: str
    result: str
    score: float | None
    threshold: float | None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _simulate_pairs(rng: np.random.Generator, reps: int, n: int, scale: float, sigma: float, outlier_p: float) -> np.ndarray:
    """Log-ratios of shape (n, reps): candidate/baseline with independent run noise and outliers."""
    def runs():
        noise = rng.normal(0.0, sigma, size=(n, reps))
        out = rng.random((n, reps)) < outlier_p
        noise = noise + out * np.log(rng.uniform(1.5, 3.0, size=(n, reps)))
        return noise
    return math.log(scale) + runs() - runs()


def gate_outcomes(rng: np.random.Generator, reps: int, scale: float, sigma: float, outlier_p: float, alpha: float, margin: float, max_pairs: int,
                  min_pairs: int = MIN_PAIRS, prior_noise: float = PRIOR_NOISE, prior_weight: float = PRIOR_WEIGHT, robust_clip: float = ROBUST_CLIP, pass_min_pairs: int = 0, running_intersection: bool = True) -> dict:
    from boson_cs import BatchAsymptoticCS

    eps = math.log(1.0 + margin)
    lr = np.clip(_simulate_pairs(rng, reps, max_pairs, scale, sigma, outlier_p), -robust_clip, robust_clip)
    cs = BatchAsymptoticCS(alpha=alpha, batch=reps, n_target=max(min_pairs, max_pairs // 2), prior_var=prior_noise ** 2, prior_weight=prior_weight, running_intersection=running_intersection)
    result = np.zeros(reps, dtype=int)   # 0 undecided, 1 pass, 2 fail
    decided_at = np.full(reps, -1)
    for t in range(max_pairs):
        cs.update(lr[t])
        if t + 1 < min_pairs:
            continue
        lo, hi = cs.intervals()
        newly_fail = (result == 0) & (lo > eps)
        newly_pass = (result == 0) & (hi < eps) & (t + 1 >= pass_min_pairs)
        result[newly_fail] = 2; result[newly_pass] = 1
        decided_at[newly_fail | newly_pass] = t + 1
        if (result != 0).all():
            break
    return {
        "scale": scale, "sigma": sigma, "outlier_p": outlier_p,
        "fail_rate": float((result == 2).mean()),
        "pass_rate": float((result == 1).mean()),
        "inconclusive_rate": float((result == 0).mean()),
        "median_pairs_to_verdict": float(np.median(decided_at[decided_at > 0])) if (decided_at > 0).any() else None,
    }


def single_pair_rule_false_alarm(rng: np.random.Generator, reps: int, sigma: float, outlier_p: float, margin: float) -> float:
    lr = _simulate_pairs(rng, reps, 1, 1.0, sigma, outlier_p)[0]
    return float((lr > math.log(1.0 + margin)).mean())


def run_verifiers(fast: bool = False, alpha: float = 0.05, margin: float = 0.05, max_pairs: int = 20, seed: int = 20260919) -> list[VerifierRecord]:
    """Acceptance verifiers for the default gate. Thresholds were set from the measured profile on 2026-09-19."""
    rng = np.random.default_rng(seed)
    reps = 2000 if fast else 20000
    slack = 0.02
    kw = dict(alpha=alpha, margin=margin, max_pairs=max_pairs, running_intersection=False)
    records: list[VerifierRecord] = []
    for sigma in (0.01, 0.05, 0.10):
        r = gate_outcomes(rng, reps, 1.0, sigma, 0.05, **kw)
        records.append(VerifierRecord(f"bench-gate-false-alarm-noise-{sigma:.2f}", "simulator",
                                      "pass" if r["fail_rate"] <= alpha + slack else "fail", r["fail_rate"], alpha + slack, r))
    for scale, floor in ((1.15, 0.60), (1.25, 0.85)):
        r = gate_outcomes(rng, reps, scale, 0.05, 0.05, **kw)
        records.append(VerifierRecord(f"bench-gate-power-regression-{int(round((scale - 1) * 100))}pct", "simulator",
                                      "pass" if r["fail_rate"] >= floor else "fail", r["fail_rate"], floor, r))
        records.append(VerifierRecord(f"bench-gate-miss-regression-{int(round((scale - 1) * 100))}pct", "simulator",
                                      "pass" if r["pass_rate"] <= 0.01 else "fail", r["pass_rate"], 0.01,
                                      {"note": "a real regression must not be passed", **r}))
    r = gate_outcomes(rng, reps, 1.0, 0.01, 0.05, **kw)
    records.append(VerifierRecord("bench-gate-usability-pass-identical-quiet-runner", "simulator",
                                  "pass" if r["pass_rate"] >= 0.50 else "fail", r["pass_rate"], 0.50, r))
    fa = single_pair_rule_false_alarm(rng, reps, 0.05, 0.05, margin)
    records.append(VerifierRecord("bench-gate-known-bad-single-pair-rule", "simulator",
                                  "pass" if fa > alpha + slack else "fail", fa, alpha + slack,
                                  {"note": "the naive 'fail if the first ratio exceeds the margin' rule must be detected as unacceptable; this record passes when it is"}))
    return records
