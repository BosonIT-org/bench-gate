"""Contract tests (verifier ``bench-format-contract``), gate tests, and an end-to-end rerun-loop test."""
from __future__ import annotations

import json
import os
import subprocess
import textwrap

import pytest

from bench_gate.gate import PairSet, overall
from bench_gate.parsers import ParseError, by_name, detect_format, parse

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


# ------------------------------------------------------------------ parsers
def test_pytest_benchmark_good():
    ms = by_name(parse(os.path.join(FIX, "pytest_bench_good.json")))
    assert set(ms) == {"test_update_batch[200]", "test_interval"}
    assert ms["test_update_batch[200]"].value == pytest.approx(0.0123)
    assert ms["test_update_batch[200]"].n == 25 and ms["test_update_batch[200]"].unit == "s"


@pytest.mark.parametrize("bad", ["pytest_bench_bad_missing_stats.json", "pytest_bench_bad_nan.json", "pytest_bench_bad_truncated.json"])
def test_pytest_benchmark_bad_fails_closed(bad):
    with pytest.raises(ParseError):
        parse(os.path.join(FIX, bad), "pytest-benchmark")


def test_criterion_good_and_bad():
    ms = by_name(parse(os.path.join(FIX, "criterion_good")))
    assert ms["update/batch_200"].value == pytest.approx(1220.5) and ms["update/batch_200"].unit == "ns"
    with pytest.raises(ParseError):
        parse(os.path.join(FIX, "criterion_bad"))


def test_go_bench_good_and_bad():
    ms = by_name(parse(os.path.join(FIX, "go_bench_good.txt"), "go-bench"))
    assert ms["BenchmarkUpdate"].value == 1234.0 and ms["BenchmarkUpdate"].n == 1_000_000
    assert ms["BenchmarkInterval"].value == pytest.approx(61.5)
    with pytest.raises(ParseError):
        parse(os.path.join(FIX, "go_bench_bad.txt"), "go-bench")


def test_detect_format():
    assert detect_format(os.path.join(FIX, "pytest_bench_good.json")) == "pytest-benchmark"
    assert detect_format(os.path.join(FIX, "criterion_good")) == "criterion"
    assert detect_format(os.path.join(FIX, "go_bench_good.txt")) == "go-bench"
    with pytest.raises(ParseError):
        detect_format(os.path.join(FIX, "pytest_bench_bad_truncated.json"))


def test_unknown_format_is_parse_error():
    with pytest.raises(ParseError):
        parse(os.path.join(FIX, "go_bench_good.txt"), "made-up")


# --------------------------------------------------------------------- gate
def test_pairset_rejects_nonpositive():
    ps = PairSet()
    with pytest.raises(ValueError):
        ps.add("b", 0.0, 1.0)


def test_overall_precedence():
    from bench_gate.gate import BenchVerdict as V
    assert overall([V("a", "pass", 0, 0, 3, ""), V("b", "fail", 0, 0, 3, "")])[0] == "fail"
    assert overall([V("a", "pass", 0, 0, 3, ""), V("b", "pass", 0, 0, 3, "")])[0] == "pass"
    assert overall([V("a", "pass", 0, 0, 3, ""), V("b", "continue", 0, 0, 3, "")])[0] == "continue"
    assert overall([V("a", "pass", 0, 0, 3, ""), V("b", "inconclusive", 0, 0, 3, "")])[0] == "inconclusive"
    assert overall([])[0] == "inconclusive"


def test_auto_mode_defaults_to_the_hosted_endpoint(monkeypatch):
    import builtins, bench_gate.gate as gm
    real_import = builtins.__import__
    def no_core(name, *a, **k):
        if name.startswith("boson_cs"):
            raise ImportError("private core absent")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_core)
    g = gm.make_gate("auto", 0.05, 0.05, 20, endpoint=None)
    assert isinstance(g, gm.EndpointGate) and g.url.startswith(gm.DEFAULT_ENDPOINT)
    g2 = gm.make_gate("endpoint", 0.05, 0.05, 20, endpoint="https://verdict.example")
    assert g2.url == "https://verdict.example/v1/benchmark/verdict"


def test_endpoint_gate_sends_identifying_user_agent_and_json():
    from bench_gate.gate import EndpointGate
    g = EndpointGate("https://verdict.example", alpha=0.05, margin=0.05, max_pairs=20, token="abc")
    ps = PairSet(); ps.add("b", 1.0, 1.1)
    req = g.build_request(ps)
    assert req.full_url == "https://verdict.example/v1/benchmark/verdict" and req.get_method() == "POST"
    assert req.get_header("User-agent", "").startswith("bench-gate/")   # Cloudflare 1010 otherwise
    assert req.get_header("Content-type") == "application/json" and req.get_header("Authorization") == "Bearer abc"
    assert json.loads(req.data)["benchmarks"]["b"]["pairs"] == [[1.0, 1.1]]


def test_endpoint_gate_fails_closed_when_unreachable():
    from bench_gate.gate import EndpointGate
    g = EndpointGate("http://127.0.0.1:9", alpha=0.05, margin=0.02, max_pairs=5, timeout=0.5)
    ps = PairSet(); ps.add("b", 1.0, 1.0)
    vs = g.evaluate(ps)
    assert vs[0].result == "inconclusive" and "unavailable" in vs[0].reason


boson_cs = pytest.importorskip("boson_cs", reason="local gate tests need the private core")


def test_local_gate_detects_regression_and_passes_identical():
    from bench_gate.gate import LocalGate
    g = LocalGate(alpha=0.05, margin=0.05, max_pairs=50)
    reg = PairSet(); same = PairSet()
    import random
    rng = random.Random(1)
    for _ in range(50):
        b = 1.0 * (1 + rng.gauss(0, 0.01))
        reg.add("slow", b, b * 1.25 * (1 + rng.gauss(0, 0.01)))   # 25% slower
        same.add("same", b, b * (1 + rng.gauss(0, 0.01)))          # identical
    r = g.evaluate(reg)[0]; s = g.evaluate(same)[0]
    assert r.result == "fail" and r.lower > 0.0488
    assert s.result == "pass" and s.upper < 0.0488


def test_local_gate_bonferroni_uses_alpha_over_k():
    from bench_gate.gate import LocalGate
    g = LocalGate(alpha=0.05, margin=0.05, max_pairs=10)
    ps = PairSet()
    for _ in range(3):
        ps.add("a", 1.0, 1.0); ps.add("b", 1.0, 1.0)
    vs = g.evaluate(ps)
    assert {v.name for v in vs} == {"a", "b"} and all(v.n == 3 for v in vs)


# --------------------------------------------------------- end-to-end runner
def _make_repo(tmp_path, base_scale: float, cand_scale: float, noise: float):
    """A git repo whose benchmark command writes pytest-benchmark JSON scaled by a per-commit constant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    script = textwrap.dedent(f"""
        import json, random, sys
        scale = float(open("SCALE").read())
        rng = random.Random()
        out = {{"benchmarks": [{{"name": "bench_a", "stats": {{"mean": scale * (1 + rng.gauss(0, {noise})), "rounds": 5}}}}]}}
        json.dump(out, open("out.json", "w"))
    """)
    (repo / "bench.py").write_text(script)
    (repo / "SCALE").write_text(str(base_scale))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    (repo / "SCALE").write_text(str(cand_scale))
    return repo


@pytest.mark.parametrize("cand_scale,expected", [(1.30, "fail"), (1.00, "pass")])
def test_runner_end_to_end(tmp_path, cand_scale, expected):
    from bench_gate.runner import RunConfig, run
    repo = _make_repo(tmp_path, 1.0, cand_scale, noise=0.01)
    cfg = RunConfig(bench_command="python3 bench.py", results_path="out.json", baseline_ref="main",
                    margin=0.05, alpha=0.05, max_pairs=40, mode="local")
    report = run(cfg, repo=str(repo))
    assert report.overall == expected, report.events[-3:]
    assert 1 <= report.pairs <= 40
    assert os.path.exists(repo / ".bench-gate" / "report.json")


def test_runner_inconclusive_on_parse_failure(tmp_path):
    from bench_gate.runner import RunConfig, run
    repo = _make_repo(tmp_path, 1.0, 1.0, noise=0.0)
    cfg = RunConfig(bench_command="python3 -c \"open('out.json','w').write('{')\"", results_path="out.json",
                    baseline_ref="main", max_pairs=3, mode="local")
    report = run(cfg, repo=str(repo))
    assert report.overall == "inconclusive" and "ParseError" in report.reason
