"""bench-gate command line.

    bench-gate run --bench-command "pytest bench --benchmark-json out.json" --results out.json \
                   --baseline-ref origin/main --margin 0.02 --alpha 0.05 --max-pairs 12 [--mode auto|local|endpoint] [--endpoint URL]
    bench-gate evaluate pairs.json [--alpha ..] [--margin ..] [--max-pairs ..]
    bench-gate parse <results> [--format auto|pytest-benchmark|criterion|go-bench]

Exit status: 0 on pass, 1 on fail, 0 on inconclusive unless --fail-on-inconclusive.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .gate import PairSet, make_gate, overall
from .parsers import ParseError, parse
from .runner import RunConfig, run, summary_markdown


def _write_github_outputs(report) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"verdict={report.overall}\n")
            fh.write(f"pairs={report.pairs}\n")
            fh.write(f"report={report.config.get('report_path')}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(summary_markdown(report) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench-gate")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--bench-command", required=True)
    r.add_argument("--results", required=True, help="results path relative to the tree the command runs in")
    r.add_argument("--format", default="auto")
    r.add_argument("--baseline-ref", default="origin/main")
    r.add_argument("--margin", type=float, default=0.05)
    r.add_argument("--alpha", type=float, default=0.05)
    r.add_argument("--max-pairs", type=int, default=20)
    r.add_argument("--mode", default="auto", choices=["auto", "local", "endpoint"])
    r.add_argument("--endpoint", default=os.environ.get("BENCH_GATE_ENDPOINT"))
    r.add_argument("--token", default=os.environ.get("BENCH_GATE_TOKEN"))
    r.add_argument("--report", default=".bench-gate/report.json")
    r.add_argument("--fail-on-inconclusive", action="store_true")
    r.add_argument("--repo", default=".")

    e = sub.add_parser("evaluate")
    e.add_argument("pairs_json")
    e.add_argument("--margin", type=float, default=0.05)
    e.add_argument("--alpha", type=float, default=0.05)
    e.add_argument("--max-pairs", type=int, default=20)
    e.add_argument("--mode", default="auto", choices=["auto", "local", "endpoint"])
    e.add_argument("--endpoint", default=os.environ.get("BENCH_GATE_ENDPOINT"))

    q = sub.add_parser("parse")
    q.add_argument("results")
    q.add_argument("--format", default="auto")

    a = p.parse_args(argv)

    if a.cmd == "parse":
        try:
            ms = parse(a.results, a.format)
        except ParseError as exc:
            print(f"inconclusive: {exc}", file=sys.stderr)
            return 2
        print(json.dumps([m.as_dict() for m in ms], indent=2))
        return 0

    if a.cmd == "evaluate":
        with open(a.pairs_json) as fh:
            ps = PairSet.from_dict(json.load(fh))
        gate = make_gate(a.mode, a.alpha, a.margin, a.max_pairs, a.endpoint)
        vs = gate.evaluate(ps)
        res, reason = overall(vs)
        print(json.dumps({"overall": res, "reason": reason, "benchmarks": [v.as_dict() for v in vs]}, indent=2))
        return 1 if res == "fail" else 0

    cfg = RunConfig(
        bench_command=a.bench_command, results_path=a.results, fmt=a.format, baseline_ref=a.baseline_ref,
        margin=a.margin, alpha=a.alpha, max_pairs=a.max_pairs, mode=a.mode, endpoint=a.endpoint, token=a.token,
        report_path=a.report, fail_on_inconclusive=a.fail_on_inconclusive,
    )
    report = run(cfg, repo=a.repo)
    print(summary_markdown(report))
    _write_github_outputs(report)
    if report.overall == "fail":
        return 1
    if report.overall == "inconclusive" and a.fail_on_inconclusive:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
