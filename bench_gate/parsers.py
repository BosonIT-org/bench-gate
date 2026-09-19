"""Benchmark output parsers → canonical measurements.

Contract (verifier ``bench-format-contract``): every supported format parses to
``Measurement(name, value, unit, n)``; anything malformed raises
:class:`ParseError`, and the gate turns a ParseError into an *inconclusive*
verdict — never into a pass.

Supported formats
-----------------
* ``pytest-benchmark`` JSON (``--benchmark-json out.json``): ``benchmarks[].name`` and
  ``benchmarks[].stats.mean`` (seconds), ``stats.rounds``.
* ``criterion`` (Criterion.rs): a directory containing ``*/new/estimates.json`` and
  ``*/new/benchmark.json``, or a single ``estimates.json``; ``mean.point_estimate`` in ns.
* ``go-bench`` text (``go test -bench``): ``BenchmarkName-8  <n>  <ns> ns/op``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from glob import glob
from typing import Iterable


class ParseError(ValueError):
    """Malformed or unrecognised benchmark output."""


@dataclass(frozen=True)
class Measurement:
    name: str
    value: float          # central estimate, lower is better
    unit: str             # "s", "ns", ...
    n: int                # rounds / iterations behind the estimate

    def as_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "unit": self.unit, "n": self.n}


_GO_LINE = re.compile(r"^(Benchmark\S+?)(?:-\d+)?\s+(\d+)\s+([0-9.]+)\s+ns/op\b")


def parse_pytest_benchmark(path: str) -> list[Measurement]:
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(f"pytest-benchmark: cannot read JSON from {path}: {exc}") from exc
    benches = doc.get("benchmarks") if isinstance(doc, dict) else None
    if not isinstance(benches, list) or not benches:
        raise ParseError("pytest-benchmark: no 'benchmarks' array")
    out: list[Measurement] = []
    for b in benches:
        try:
            stats = b["stats"]
            mean = float(stats["mean"])
            rounds = int(stats.get("rounds", 1))
            name = str(b["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(f"pytest-benchmark: malformed benchmark entry: {exc}") from exc
        if not (mean > 0) or mean != mean:  # NaN guard
            raise ParseError(f"pytest-benchmark: non-positive or NaN mean for {name}")
        out.append(Measurement(name, mean, "s", rounds))
    return out


def parse_criterion(path: str) -> list[Measurement]:
    if os.path.isdir(path):
        files = sorted(glob(os.path.join(path, "**", "new", "estimates.json"), recursive=True))
        if not files:
            raise ParseError(f"criterion: no */new/estimates.json under {path}")
    else:
        files = [path]
    out: list[Measurement] = []
    for est_path in files:
        try:
            with open(est_path) as fh:
                est = json.load(fh)
            point = float(est["mean"]["point_estimate"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ParseError(f"criterion: malformed estimates at {est_path}: {exc}") from exc
        if not (point > 0):
            raise ParseError(f"criterion: non-positive mean in {est_path}")
        name = _criterion_name(est_path)
        n = 1
        bench_json = os.path.join(os.path.dirname(est_path), "benchmark.json")
        if os.path.exists(bench_json):
            try:
                with open(bench_json) as fh:
                    meta = json.load(fh)
                name = str(meta.get("full_id") or meta.get("function_id") or name)
            except (OSError, json.JSONDecodeError):
                pass
        out.append(Measurement(name, point, "ns", n))
    return out


def _criterion_name(est_path: str) -> str:
    parts = os.path.normpath(est_path).split(os.sep)
    # .../<bench>/new/estimates.json
    return parts[-3] if len(parts) >= 3 else os.path.basename(os.path.dirname(est_path))


def parse_go_bench(path: str) -> list[Measurement]:
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise ParseError(f"go-bench: cannot read {path}: {exc}") from exc
    out: list[Measurement] = []
    for line in lines:
        m = _GO_LINE.match(line.strip())
        if m:
            name, iters, ns = m.group(1), int(m.group(2)), float(m.group(3))
            if not (ns > 0):
                raise ParseError(f"go-bench: non-positive ns/op for {name}")
            out.append(Measurement(name, ns, "ns", iters))
    if not out:
        raise ParseError("go-bench: no 'Benchmark... ns/op' lines found")
    return out


_PARSERS = {
    "pytest-benchmark": parse_pytest_benchmark,
    "criterion": parse_criterion,
    "go-bench": parse_go_bench,
}


def detect_format(path: str) -> str:
    if os.path.isdir(path):
        return "criterion"
    lower = path.lower()
    if lower.endswith(".json"):
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ParseError(f"cannot detect format: unreadable JSON {path}: {exc}") from exc
        if isinstance(doc, dict) and "benchmarks" in doc:
            return "pytest-benchmark"
        if isinstance(doc, dict) and "mean" in doc and "point_estimate" in doc.get("mean", {}):
            return "criterion"
        raise ParseError(f"cannot detect format of JSON {path}")
    return "go-bench"


def parse(path: str, fmt: str = "auto") -> list[Measurement]:
    """Parse ``path`` in ``fmt`` (or auto-detect). Raises ParseError on anything malformed."""
    if fmt == "auto":
        fmt = detect_format(path)
    try:
        parser = _PARSERS[fmt]
    except KeyError as exc:
        raise ParseError(f"unknown format {fmt!r}; expected one of {sorted(_PARSERS)}") from exc
    return parser(path)


def by_name(measurements: Iterable[Measurement]) -> dict[str, Measurement]:
    out: dict[str, Measurement] = {}
    for m in measurements:
        if m.name in out:
            raise ParseError(f"duplicate benchmark name {m.name!r} in one run")
        out[m.name] = m
    return out
