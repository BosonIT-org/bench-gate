# bench-gate

Rerun a noisy CI benchmark **only until an anytime-valid test decides** — pass, fail, or honestly *inconclusive* — instead of "run it three times and squint."

`bench-gate` alternates baseline and candidate runs on the same runner, pairs them, and maintains a confidence sequence on the mean log-ratio `ln(candidate / baseline)`. Because the interval is valid at every look, stopping at the first decisive verdict is legitimate: a 25% regression is usually caught in five pairs; identical code on a quiet runner passes a 5% margin in about nine; and when the runner is too noisy to tell, it says so rather than guessing.

## Use it

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- uses: kacj77/bench-gate@v0
  with:
    bench-command: "pytest bench --benchmark-json out.json -q"
    results: out.json            # pytest-benchmark JSON, Criterion.rs directory, or `go test -bench` text
    margin: "0.05"               # tolerated slowdown (5%). 2% needs quiet, self-hosted runners.
    max-pairs: "20"
    # No endpoint needed: verdicts come from BosonIT's hosted service (free tier, anonymous, rate limited).
    # Optional: env BENCH_GATE_TOKEN for a tenant key; input `endpoint` to point at another service.
```

The step writes a verdict table to the job summary and sets outputs `verdict` (`pass` / `fail` / `inconclusive`), `pairs`, and `report`. It exits non-zero on `fail`; set `fail-on-inconclusive: "true"` to also block when the budget is spent without a decision.

Formats: **pytest-benchmark** JSON (`--benchmark-json`), **Criterion.rs** (`target/criterion/`), **Go** (`go test -bench`). Anything malformed yields *inconclusive*, never *pass*.

## What the verdicts mean

The default method is a variance-adaptive asymptotic confidence sequence on the winsorized log-ratio with a 10% prior on run-to-run noise, a minimum of five pairs before any verdict, and no early-interval carry-over. Its behaviour was **measured**, not assumed (20,000 simulated replications per cell; lognormal run noise plus 5% outliers of 1.5–3×; margin 5%; 20-pair budget; α = 0.05):

| Runner noise | Identical code: pass / fail / inconclusive | +10% regression caught | +15% | +25% | Real regression passed |
|---|---|---|---|---|---|
| 1% | 59% / 0.0% / 41% | — | — | — | 0.0% |
| 5% | 34% / 0.0% / 66% | 35% | 72% | 94% | 0.0% |
| 10% | 21% / 0.6% / 79% | — | — | 88% | 0.0% |

Read it as: *fail* is trustworthy (false alarms under 1%), *pass* is trustworthy (a real regression is never passed in the suite), and *inconclusive* is the honest default on noisy runners — quieter runners or a larger budget turn it into passes. A 2% margin keeps the same false-alarm and miss properties but rarely reaches *pass* on GitHub-hosted runners.

The naive rule "fail if the first run is more than the margin slower" has a **27% false-alarm rate** under 5% noise in the same simulation; that is the problem this tool exists to remove.

With several benchmarks, each is tested at `alpha / k` (Bonferroni), so "nothing regressed" is a family-wise statement.

## Modes

- **endpoint** (public users): pairs are posted to the hosted verdict service (`https://boson-verdict.crawlyield.workers.dev` by default), which holds the mathematics and stores every verdict as an evidence packet you can fetch back. An unreachable or malformed service yields *inconclusive*, never *pass*. The client identifies itself (`User-Agent: bench-gate/<version>`); Cloudflare rejects anonymous default agents.
- **local** (Boson's own repositories): the private core package is installed by the workflow and runs in-process.
- **auto**: local if the core is importable, else endpoint.

An `--method exact` option runs the nonasymptotic betting sequence instead: exact at any number of pairs, but its width scales with the clip range, so it needs far more pairs to resolve small margins.

## Command line

```sh
pip install .
bench-gate run --bench-command "pytest bench --benchmark-json out.json -q" --results out.json --baseline-ref origin/main
bench-gate evaluate pairs.json --margin 0.05
bench-gate parse target/criterion
```

## Verifiers

`python -m pytest` runs the format contract tests (known-good and known-bad fixtures for every format), gate tests, and an end-to-end rerun loop against a synthetic repository. In Boson repositories, `bench_gate.verify.run_verifiers()` re-measures the profile table above and writes a receipt-shaped report (`reports/verifiers_full.json`). Change a default only with a new report.

## License

Apache-2.0 for this adapter. The confidence-sequence core it calls is proprietary to BosonIT, LLC and is not part of this repository.
