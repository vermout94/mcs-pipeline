# Analysis

The analysis reads a campaign under `data/<campaign>/` and writes tables and figures to `data/<campaign>/analysis/`. It reads only the versioned files of a campaign: `plan.yaml`, `order.json` and the manifests. It never reads the artifacts, so the analysis can be repeated from this repository after the artifacts have expired.

## Setup

Python 3.13.7 ([`.python-version`](.python-version)) with [uv](https://docs.astral.sh/uv/). [`uv.lock`](uv.lock) pins every dependency with its SHA-256: jsonschema 4.26.0, matplotlib 3.11.2, numpy 2.5.3 and scipy 1.18.1, and pytest 9.1.1 for the tests. The lock was written on 2026-10-15 with uv 0.13.1. [`pyproject.toml`](pyproject.toml) sets `exclude-newer = "2026-10-01T00:00:00Z"`, so uv resolves only releases that were uploaded to PyPI before that date. `analysis.json` records this date, the version of the uv that ran the analysis and the library versions.

```bash
cd analysis
uv sync --locked
uv run --locked pytest
uv run --locked python -m mcs_analysis check <campaign>
uv run --locked python -m mcs_analysis run <campaign>
```

`check` validates the manifests and counts planned, missing, failed, excluded and included runs. `run` writes the outputs. It stops if `analysis/` has uncommitted changes or if git cannot tell, for example in a copy without `.git` or in a repository without commit. With `--allow-dirty` it runs anyway. The header of every output names the commit of the analysis (or `unknown`) and the state of `analysis/` (`dirty: true`, `false` or `unknown`), and `analysis.json` gives the reason for an unknown state. `--out <directory>` writes to another directory.

The outputs of a campaign are committed after the code that produced them, because their header names the commit of the analysis.

## Inputs and checks

Every manifest is validated against [`harness/manifest.schema.json`](../harness/manifest.schema.json). The analysis stops if a manifest does not validate, if its campaign ID, seed, campaign commit or the SHA-256 of `plan.yaml` or `order.json` differ from `order.json`, if its design (configuration, cell, factors, block, positions, first runs) differs from the planned run, or if a manifest belongs to no planned run. A planned run without manifest counts as missing.

## Exclusion rule

Version 1, fixed on 2026-10-15 before the pilot `pilot-hosted-2026-10-15` had finished. A run enters the timing analysis if its manifest is valid, its failure rule is R01 (the run succeeded), `checks.divergences` is empty and `run_attempt` is 1. Runs with another failure rule are counted by configuration, failure class and rule in `failures.csv` (RQ2). Successful runs with a divergence and re-runs are excluded and counted in `analysis.json`. Outliers stay in the data.

## Metrics

Per run, in seconds: every enabled stage and its sub-times from `CLOCK_MONOTONIC`, the sum of the enabled stages (`timing.totals.stages_sum_ns`) and the duration of the job from the Jobs API (`timing.job_api.duration_s`, resolution 1 s).

## Methods

- Quantiles follow definition 8 of Hyndman and Fan (1996) with the plotting position p_k = (k - 1/3)/(n + 1/3), numpy `quantile` with `method="median_unbiased"`. The p-quantile equals the sample maximum when n <= (1 + p)/(3 (1 - p)), for p99 up to n = 66 and for p95 up to n = 13. The columns `p95_is_max` and `p99_is_max` mark these values, and `summary.tex` adds a note.
- The statistics per configuration and metric are n, mean, standard deviation with n - 1, coefficient of variation (standard deviation over mean), minimum, median, p95, p99, maximum and the unscaled median absolute deviation.
- The tests follow Arcuri and Briand (2014). Every metric that a configuration shares with the reference configuration (all factors off) gets a two-sided Mann-Whitney U test. The method is set explicitly: exact if both samples have fewer than 50 values and the pooled sample has no ties, otherwise the normal approximation with tie correction and continuity correction. If all values are equal, the p-value is undefined (`nan`). Every p-value is reported as computed, without correction for multiple tests. The effect size is A12 = (R1/m - (m + 1)/2)/n with midranks, equation (1) of Arcuri and Briand. A12 above 0.5 means longer durations in the configuration than in the reference.
- The overhead is the difference of the medians of a configuration and the reference for the sum of the stages and for the job, in seconds and relative to the median of the reference. For a stage that the reference does not run, the median of the stage and its share of the median sum of stages of the reference.
- The bootstrap intervals are percentile intervals at 95 %, the 250th and the 9,750th of 10,000 ordered replicates (Efron and Tibshirani, Sec. 13.3). Both samples are resampled independently with replacement. The seed is 2106950589, drawn once with `secrets.randbelow(2**31)`. Each comparison draws from its own stream, numpy PCG64 seeded with `SeedSequence(int(SHA-256("<seed>:<name of the comparison>")))`. The results therefore depend neither on the order of the computation nor on `PYTHONHASHSEED`. A test runs the analysis in two processes with different `PYTHONHASHSEED` and compares every output file byte by byte.
- The trend compares, per configuration and metric, the second half of the blocks with the first half (blocks 0 to floor(B/2) - 1 against the rest), with the same test, A12 and intervals. A12 above 0.5 means longer durations in the second half. `first_runs.csv` gives the value and the rank of every run with `first_runs` among the included runs of its configuration.
- Every table is computed for all included runs and without the runs with `first_runs`.
- `cpu.csv` counts the runs per configuration and CPU model. If more than one model occurs, `summary.csv` also has the statistics per model.
- Unusual values lie below Q1 - 1.5 IQR or above Q3 + 1.5 IQR per configuration and metric, with the quartiles of definition 8. These are the points outside the whiskers of the box plot. `unusual.csv` lists them with facts from the manifest that may explain them: CPU model, runner image, queue time, the layers of the push and the sub-times of the stage.
- Interactions of the factors are not analyzed. Every configuration is compared with the reference only.

## Outputs

| File | Content |
|---|---|
| `analysis.json` | campaign, commits, versions, settings, counts, notes, SHA-256 of every input and output |
| `runs.csv` | one row per planned run: inclusion, exclusion reason, failure class and rule, CPU model, durations |
| `summary.csv`, `summary.tex` | statistics per subset, CPU model, configuration and metric |
| `tests.csv`, `tests.tex` | U test and A12 against the reference |
| `overhead.csv`, `overhead.tex` | overhead with intervals, the LaTeX table with p and A12 of the totals |
| `trend.csv`, `first_runs.csv` | second against first half of the blocks, values of the first runs |
| `cpu.csv` | runs per configuration and CPU model |
| `unusual.csv` | values outside the fences |
| `failures.csv`, `failures.tex` | runs that did not succeed by configuration, failure class and rule |
| `boxplot.pdf`, `trend.pdf` | box plots per metric and durations over the position in the campaign |

Every CSV and LaTeX file starts with comment lines (`#` and `%`) that name the campaign, the campaign commit and the commit of the analysis. The LaTeX files hold a `tabular` with booktabs and no caption. The PDF files carry the same facts in their metadata and have no creation date, so the same inputs give byte identical files.

## Modules

| Module | Purpose |
|---|---|
| [`load.py`](mcs_analysis/load.py) | reads and checks a campaign, durations per run, exclusion rule |
| [`quantiles.py`](mcs_analysis/quantiles.py) | quantiles after definition 8 |
| [`stats.py`](mcs_analysis/stats.py) | U test, A12, bootstrap |
| [`analyze.py`](mcs_analysis/analyze.py) | the tables |
| [`output.py`](mcs_analysis/output.py) | CSV and LaTeX |
| [`plots.py`](mcs_analysis/plots.py) | PDF figures |
| [`provenance.py`](mcs_analysis/provenance.py) | commit, uncommitted changes, uv and library versions |

The tests in [`tests/`](tests/) use synthetic campaigns with known results ([`tests/synthetic.py`](tests/synthetic.py)): quantiles against equation (1) of Hyndman and Fan, the A12 example of Arcuri and Briand (p. 235), exact p-values of separated samples, a known shift of 5 s, a failed run, a run with a divergence, an outlier in `push`, a linear drift and two CPU models.
