"""The rerun loop: alternate baseline and candidate runs until the gate decides.

Order alternates each pair (baseline-first, then candidate-first) so runner
drift and warm-up effects do not systematically favour one side. The baseline
is checked out into a git worktree once; the candidate is the working tree.

Every loop iteration produces one pair per benchmark name and re-evaluates the
whole pair set (the confidence sequence is valid at every look, so stopping at
the first decisive verdict is legitimate). A parse failure or a missing
benchmark on either side ends the loop with *inconclusive*.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field

from .gate import PairSet, overall
from .parsers import ParseError, by_name, parse


@dataclass
class RunConfig:
    bench_command: str
    results_path: str          # relative to the tree the command runs in
    fmt: str = "auto"
    baseline_ref: str = "origin/main"
    margin: float = 0.05
    alpha: float = 0.05
    max_pairs: int = 20
    mode: str = "auto"
    endpoint: str | None = None
    token: str | None = None
    work_dir: str = ".bench-gate"
    report_path: str = ".bench-gate/report.json"
    fail_on_inconclusive: bool = False


@dataclass
class RunReport:
    overall: str
    reason: str
    pairs: int
    benchmarks: list[dict]
    config: dict
    started_at_utc: str
    cycle_time_s: float
    events: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__


def _run(cmd: str, cwd: str, events: list[str]) -> None:
    events.append(f"run: {cmd} (cwd={cwd})")
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"benchmark command failed in {cwd} (exit {proc.returncode}): {tail}")


def _ensure_worktree(repo: str, ref: str, path: str, events: list[str]) -> str:
    if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git")):
        return path
    subprocess.run(["git", "-C", repo, "fetch", "--quiet", "origin"], check=False, capture_output=True)
    res = subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", path, ref], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"cannot create baseline worktree for {ref}: {res.stderr.strip()}")
    events.append(f"worktree: {ref} -> {path}")
    return path


def run(config: RunConfig, repo: str = ".") -> RunReport:
    from .gate import make_gate

    started = dt.datetime.now(dt.timezone.utc)
    t0 = time.monotonic()
    events: list[str] = []
    os.makedirs(os.path.join(repo, config.work_dir), exist_ok=True)
    baseline_dir = os.path.join(repo, config.work_dir, "baseline")
    pairs = PairSet()
    verdicts = []
    result, reason = "inconclusive", "no pairs collected"
    try:
        gate = make_gate(config.mode, config.alpha, config.margin, config.max_pairs, config.endpoint, config.token)
        _ensure_worktree(repo, config.baseline_ref, baseline_dir, events)
        for i in range(config.max_pairs):
            order = [("baseline", baseline_dir), ("candidate", repo)] if i % 2 == 0 else [("candidate", repo), ("baseline", baseline_dir)]
            measured: dict[str, dict[str, float]] = {}
            for side, tree in order:
                _run(config.bench_command, tree, events)
                results = os.path.join(tree, config.results_path)
                measured[side] = {m.name: m.value for m in by_name(parse(results, config.fmt)).values()}
            common = sorted(set(measured["baseline"]) & set(measured["candidate"]))
            if not common:
                raise ParseError("no benchmark names in common between baseline and candidate")
            for name in common:
                pairs.add(name, measured["baseline"][name], measured["candidate"][name])
            verdicts = gate.evaluate(pairs)
            result, reason = overall(verdicts)
            events.append(f"pair {i + 1}: {result} — {reason}")
            if result in ("pass", "fail", "inconclusive"):
                break
        if result == "continue":
            result, reason = "inconclusive", f"budget of {config.max_pairs} pairs spent without a decisive verdict"
    except (ParseError, RuntimeError, ValueError) as exc:
        result, reason = "inconclusive", f"{type(exc).__name__}: {exc}"
        events.append(reason)
    report = RunReport(
        overall=result, reason=reason, pairs=pairs.n(),
        benchmarks=[v.as_dict() for v in verdicts],
        config={k: v for k, v in config.__dict__.items() if k != "token"},
        started_at_utc=started.isoformat(), cycle_time_s=round(time.monotonic() - t0, 3), events=events,
    )
    out = os.path.join(repo, config.report_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(report.as_dict(), fh, indent=2)
    return report


def summary_markdown(report: RunReport) -> str:
    badge = {"pass": "PASS — no regression beyond margin", "fail": "FAIL — regression detected", "inconclusive": "INCONCLUSIVE"}.get(report.overall, report.overall)
    lines = [f"## bench-gate: {badge}", "", f"{report.reason}. Pairs used: {report.pairs}.", "",
             "| Benchmark | Verdict | Ratio interval (candidate / baseline) | n | Reason |", "|---|---|---|---|---|"]
    for b in report.benchmarks:
        iv = b.get("interval_ratio")
        ivs = "—" if not iv else f"[{iv[0]:.3f}, {iv[1]:.3f}]"
        lines.append(f"| {b['name']} | {b['result']} | {ivs} | {b['n']} | {b['reason']} |")
    lines += ["", f"_alpha={report.config['alpha']}, margin={report.config['margin']:.1%}, max pairs={report.config['max_pairs']}; intervals are valid at every look, so stopping early is legitimate._"]
    return "\n".join(lines)


def shell_quote(s: str) -> str:
    return shlex.quote(s)
