"""The gate: paired log-ratios → confidence sequence → verdict.

Per benchmark name, each rerun contributes one pair ``(baseline, candidate)``.
We test the mean of the *clipped log ratio*

    r_i = clip( ln(candidate_i / baseline_i), -L, L )

against a margin ``eps = ln(1 + margin)``: a regression is "the candidate is
credibly more than ``margin`` slower". Clipping to ``[-L, L]`` makes ``r_i``
bounded so the exact, nonasymptotic betting confidence sequence applies at any
number of reruns (three pairs or three hundred); a regression larger than
``e^L`` (4x by default) is clipped but still detected as large.

With ``k`` benchmark names the per-benchmark level is ``alpha / k`` (Bonferroni),
so "no benchmark regressed" is a family-wise statement at level ``alpha``.

Two execution modes, chosen by :func:`make_gate`:

* **local** — the proprietary core ``boson_cs`` is importable (Boson's own
  repositories install it from the private cores repository). Never true in
  the public distribution of this adapter.
* **endpoint** — pairs are posted to the hosted verdict service
  (``POST {endpoint}/v1/benchmark/verdict``); the service holds the core.
  Unreachable or malformed responses yield *inconclusive*, never *pass*.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_CLIP = math.log(4.0)


@dataclass
class BenchVerdict:
    name: str
    result: str            # pass | fail | continue | inconclusive
    lower: float | None    # log-ratio units
    upper: float | None
    n: int
    reason: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "result": self.result,
            "interval_log_ratio": None if self.lower is None else [self.lower, self.upper],
            "interval_ratio": None if self.lower is None else [math.exp(self.lower), math.exp(self.upper)],
            "n": self.n,
            "reason": self.reason,
        }


@dataclass
class PairSet:
    """Pairs per benchmark name, in rerun order."""

    pairs: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def add(self, name: str, baseline: float, candidate: float) -> None:
        if not (baseline > 0 and candidate > 0):
            raise ValueError(f"{name}: measurements must be positive")
        self.pairs.setdefault(name, []).append((float(baseline), float(candidate)))

    def n(self) -> int:
        return max((len(v) for v in self.pairs.values()), default=0)

    def as_dict(self) -> dict:
        return {name: {"pairs": [[b, c] for b, c in ps]} for name, ps in self.pairs.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "PairSet":
        ps = cls()
        for name, body in d.items():
            for b, c in body["pairs"]:
                ps.add(name, b, c)
        return ps


def log_ratios(pairs: list[tuple[float, float]], clip: float) -> list[float]:
    return [max(-clip, min(clip, math.log(c / b))) for b, c in pairs]


def overall(verdicts: list[BenchVerdict]) -> tuple[str, str]:
    results = {v.result for v in verdicts}
    if not verdicts:
        return "inconclusive", "no benchmarks parsed"
    if "fail" in results:
        failed = [v.name for v in verdicts if v.result == "fail"]
        return "fail", f"regression in: {', '.join(failed)}"
    if results == {"pass"}:
        return "pass", "no benchmark regressed beyond the margin"
    if "continue" in results:
        return "continue", "at least one benchmark is not yet decisive"
    return "inconclusive", "budget spent with at least one benchmark undecided"


# ------------------------------------------------------------------ local mode
# Robustness defaults. These are *measured* by bench_gate.verify against realistic runner
# noise (lognormal run noise plus 5% outliers of 1.5-3x); change them only with a new report.
MIN_PAIRS = 5            # no verdict before this many pairs
PRIOR_NOISE = 0.10       # prior run-to-run noise of a log-ratio, as a std-dev (10%: GitHub-hosted runners)
PRIOR_WEIGHT = 2.0       # pseudo-pairs of weight behind the prior
ROBUST_CLIP = 0.35       # winsorize each log-ratio to +/- 0.35 (~ +/-42%) before the sequence
DEFAULT_MARGIN = 0.05    # tolerated slowdown; 2% needs quiet runners (see README profile table)
DEFAULT_MAX_PAIRS = 20
EXTREME_CLIP = ROBUST_CLIP


class LocalGate:
    """Runs the core in-process. Requires ``boson_cs`` (private).

    ``method="asymptotic"`` (default): variance-adaptive asymptotic confidence
    sequence on the winsorized log-ratio, with a 10% prior on run-to-run noise, a
    minimum of ``MIN_PAIRS`` pairs before any verdict, and no running intersection.
    Measured profile (``bench_gate.verify``, 20,000 replications, 5% outliers of
    1.5-3x, margin 5%, 20-pair budget): false alarms 0.0% / 0.0% / 0.6% at 1% / 5% /
    10% run noise; a 25% regression is caught 94% of the time (median 5 pairs), a
    15% regression 72%, a 10% regression 35%; a real regression is never passed
    (0.0%); identical code passes 59% of the time on 1%-noise runners and is
    otherwise honestly inconclusive. Re-measure before changing any default.

    ``method="exact"``: the nonasymptotic betting sequence on the clipped log-ratio.
    Exact at any n, but its width scales with the clip range, so it needs far more
    pairs to resolve small margins. Offered for users who want the guarantee and
    can afford the reruns.
    """

    def __init__(self, alpha: float, margin: float, max_pairs: int, clip: float = DEFAULT_CLIP, method: str = "asymptotic",
                 min_pairs: int = MIN_PAIRS, prior_noise: float = PRIOR_NOISE, prior_weight: float = PRIOR_WEIGHT, robust_clip: float = ROBUST_CLIP) -> None:
        from boson_cs import AsymptoticCS, BettingCS, Interval, benchmark_verdict  # noqa: F401  (private core)

        self._AsymptoticCS, self._BettingCS = AsymptoticCS, BettingCS
        self._Interval, self._benchmark_verdict = Interval, benchmark_verdict
        if method not in ("asymptotic", "exact"):
            raise ValueError("method must be 'asymptotic' or 'exact'")
        self.alpha, self.margin, self.max_pairs, self.clip, self.method = alpha, margin, max_pairs, clip, method
        self.min_pairs, self.prior_noise, self.prior_weight, self.robust_clip = min_pairs, prior_noise, prior_weight, robust_clip

    def _interval(self, ps: list[tuple[float, float]], alpha: float):
        if self.method == "exact":
            cs = self._BettingCS(alpha=alpha, grid_size=2001)
            for r in log_ratios(ps, self.clip):
                cs.update((r + self.clip) / (2.0 * self.clip))
            iv = cs.interval()
            return 2.0 * self.clip * iv.lower - self.clip, 2.0 * self.clip * iv.upper - self.clip, iv.n
        # No running intersection for the asymptotic method: with few pairs an early interval can be
        # wrong, and intersecting would make that error permanent (measured: it caused ~4% passes on
        # real 25% regressions; without intersection the miss rate is 0.0% in the verifier suite).
        cs = self._AsymptoticCS(alpha=alpha, n_target=max(self.min_pairs, self.max_pairs // 2),
                                prior_var=self.prior_noise ** 2, prior_weight=self.prior_weight,
                                running_intersection=False)
        for r in log_ratios(ps, self.robust_clip):
            cs.update(r)
        iv = cs.interval()
        return iv.lower, iv.upper, iv.n

    def evaluate(self, pairs: PairSet) -> list[BenchVerdict]:
        k = max(1, len(pairs.pairs))
        eps = math.log(1.0 + self.margin)
        out: list[BenchVerdict] = []
        for name, ps in pairs.pairs.items():
            lo, hi, n = self._interval(ps, self.alpha / k)
            if n < self.min_pairs:
                out.append(BenchVerdict(name, "continue", lo, hi, n, f"fewer than {self.min_pairs} pairs; variance not yet estimable"))
                continue
            v = self._benchmark_verdict(self._Interval(lo, hi, n), margin=eps, n_max=self.max_pairs)
            out.append(BenchVerdict(name, v.result.value, lo, hi, n, v.reason))
        return out


# --------------------------------------------------------------- endpoint mode
class EndpointGate:
    """Posts the whole pair set to the hosted verdict service (stateless, idempotent)."""

    def __init__(self, endpoint: str, alpha: float, margin: float, max_pairs: int, clip: float = DEFAULT_CLIP, timeout: float = 20.0, token: str | None = None) -> None:
        self.url = endpoint.rstrip("/") + "/v1/benchmark/verdict"
        self.alpha, self.margin, self.max_pairs, self.clip, self.timeout, self.token = alpha, margin, max_pairs, clip, timeout, token

    def evaluate(self, pairs: PairSet) -> list[BenchVerdict]:
        body = json.dumps({
            "alpha": self.alpha, "margin": self.margin, "clip": self.clip, "max_pairs": self.max_pairs,
            "benchmarks": pairs.as_dict(),
        }).encode()
        req = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json", **({"authorization": f"Bearer {self.token}"} if self.token else {})}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode())
            return [
                BenchVerdict(name, str(v["result"]), v["interval_log_ratio"][0] if v.get("interval_log_ratio") else None,
                             v["interval_log_ratio"][1] if v.get("interval_log_ratio") else None, int(v["n"]), str(v.get("reason", "")))
                for name, v in doc["benchmarks"].items()
            ]
        except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Fail closed: the gate never passes on a service error.
            return [BenchVerdict(name, "inconclusive", None, None, len(ps), f"verdict service unavailable or malformed response: {exc}") for name, ps in pairs.pairs.items()]


def make_gate(mode: str, alpha: float, margin: float, max_pairs: int, endpoint: str | None = None, token: str | None = None, clip: float = DEFAULT_CLIP, method: str = "asymptotic"):
    if mode == "local":
        return LocalGate(alpha, margin, max_pairs, clip, method)
    if mode == "endpoint":
        if not endpoint:
            raise ValueError("endpoint mode requires an endpoint URL")
        return EndpointGate(endpoint, alpha, margin, max_pairs, clip, token=token)
    if mode == "auto":
        try:
            return LocalGate(alpha, margin, max_pairs, clip, method)
        except ImportError:
            if endpoint:
                return EndpointGate(endpoint, alpha, margin, max_pairs, clip, token=token)
            raise RuntimeError("no local core and no endpoint configured; set 'endpoint'")
    raise ValueError(f"unknown mode {mode!r}")
