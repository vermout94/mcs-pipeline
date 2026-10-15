"""Tables of a campaign: statistics, comparisons, trend, CPU models, failures.

No interactions of the 2^4 design are analyzed: every configuration is
compared with the reference configuration (all factors off) only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as sps

from .load import DIVERGENCE, FAILED, RERUN, TOTALS, Campaign
from .quantiles import equals_maximum, largest_n_at_maximum, quantile
from .stats import Bootstrap, compare, mann_whitney, stage_cost

BOOTSTRAP = Bootstrap(seed=2106950589, resamples=10_000, confidence=0.95)
FENCE_FACTOR = 1.5
QUANTILES = (0.95, 0.99)
ALL = "all"
SUBSETS = (
    (ALL, lambda run: True),
    ("without_first_runs", lambda run: not run.first_runs),
)


@dataclass
class Result:
    summary: list = field(default_factory=list)
    tests: list = field(default_factory=list)
    overhead: list = field(default_factory=list)
    trend: list = field(default_factory=list)
    first_runs: list = field(default_factory=list)
    cpu: list = field(default_factory=list)
    unusual: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    cpu_models: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def describe(values) -> dict:
    a = np.asarray(values, dtype=float)
    n = a.size
    mean = float(a.mean())
    sd = float(a.std(ddof=1)) if n > 1 else None
    median = float(np.median(a))
    row = {
        "n": n,
        "mean_s": mean,
        "sd_s": sd,
        "cv": sd / mean if sd is not None and mean != 0 else None,
        "min_s": float(a.min()),
        "median_s": median,
    }
    for p in QUANTILES:
        row[f"p{round(p * 100)}_s"] = quantile(a, p)
    row["max_s"] = float(a.max())
    row["mad_s"] = float(np.median(np.abs(a - median)))
    for p in QUANTILES:
        row[f"p{round(p * 100)}_is_max"] = equals_maximum(n, p)
    return row


def box_stats(values) -> dict:
    """Quartiles after definition 8 and the fences at 1.5 IQR (Tukey)."""
    a = np.asarray(values, dtype=float)
    q1 = quantile(a, 0.25)
    q3 = quantile(a, 0.75)
    iqr = q3 - q1
    low = q1 - FENCE_FACTOR * iqr
    high = q3 + FENCE_FACTOR * iqr
    inside = a[(a >= low) & (a <= high)]
    return {
        "median": float(np.median(a)),
        "q1": q1,
        "q3": q3,
        "lower_fence": low,
        "upper_fence": high,
        "whisker_low": float(inside.min()),
        "whisker_high": float(inside.max()),
    }


def _values(runs, configuration, metric, keep=lambda run: True):
    return [
        run.metrics[metric]
        for run in runs
        if run.configuration == configuration and metric in run.metrics and keep(run)
    ]


def _metrics_of(runs, configuration, metrics):
    return [m for m in metrics if _values(runs, configuration, m)]


def summarize(campaign: Campaign, result: Result) -> None:
    included = campaign.included()
    models = sorted({r.cpu_model for r in included if r.cpu_model is not None})
    result.cpu_models = models
    groups = [(ALL, lambda run: True)]
    if len(models) > 1:
        groups += [(m, lambda run, m=m: run.cpu_model == m) for m in models]
    for subset, keep in SUBSETS:
        for cpu, in_group in groups:
            for config in campaign.configurations:
                for metric in campaign.metrics:
                    values = _values(included, config.name, metric, lambda r: keep(r) and in_group(r))
                    if values:
                        result.summary.append(
                            {"subset": subset, "cpu_model": cpu, "configuration": config.name, "metric": metric}
                            | describe(values)
                        )


def compare_configurations(campaign: Campaign, result: Result) -> None:
    included = campaign.included()
    ref = campaign.reference
    for config in campaign.configurations:
        if config.is_reference:
            continue
        for subset, keep in SUBSETS:
            ref_metrics = set(_metrics_of(included, ref.name, campaign.metrics))
            for metric in _metrics_of(included, config.name, campaign.metrics):
                x = _values(included, config.name, metric, keep)
                key = {"subset": subset, "configuration": config.name, "reference": ref.name, "metric": metric}
                name = f"{subset}/{config.name}-vs-{ref.name}/{metric}"
                if metric in ref_metrics:
                    y = _values(included, ref.name, metric, keep)
                    if len(x) < 2 or len(y) < 2:
                        result.notes.append(f"{name}: fewer than 2 runs, no comparison")
                        continue
                    test = mann_whitney(x, y)
                    c = compare(BOOTSTRAP, name, x, y)
                    result.tests.append(key | {
                        "n": len(x), "n_reference": len(y),
                        "median_s": c.median_x, "median_reference_s": c.median_y,
                        "u": test.u, "p": test.p, "method": test.method,
                        "a12": c.a12, "a12_ci_low": c.a12_ci[0], "a12_ci_high": c.a12_ci[1],
                    })
                    if metric in TOTALS:
                        result.overhead.append(key | {
                            "kind": "difference_of_medians",
                            "n": len(x), "n_reference": len(y),
                            "median_s": c.median_x, "median_reference_s": c.median_y,
                            "difference_s": c.difference,
                            "difference_ci_low_s": c.difference_ci[0], "difference_ci_high_s": c.difference_ci[1],
                            "relative_to": f"{ref.name}/{metric}",
                            "relative": c.relative,
                            "relative_ci_low": c.relative_ci[0], "relative_ci_high": c.relative_ci[1],
                        })
                else:
                    y = _values(included, ref.name, "stages_sum", keep)
                    if len(x) < 2 or len(y) < 2:
                        result.notes.append(f"{name}: fewer than 2 runs, no stage cost")
                        continue
                    s = stage_cost(BOOTSTRAP, name, x, y)
                    result.overhead.append(key | {
                        "kind": "stage_median",
                        "n": len(x), "n_reference": len(y),
                        "median_s": s.median, "median_reference_s": None,
                        "difference_s": s.median,
                        "difference_ci_low_s": s.median_ci[0], "difference_ci_high_s": s.median_ci[1],
                        "relative_to": f"{ref.name}/stages_sum",
                        "relative": s.share,
                        "relative_ci_low": s.share_ci[0], "relative_ci_high": s.share_ci[1],
                    })


def trend(campaign: Campaign, result: Result) -> None:
    """Second half of the blocks against the first half, per configuration.

    First half: blocks 0 to floor(B/2) - 1. A12 > 0.5 means longer durations
    in the second half.
    """
    included = campaign.included()
    split = campaign.blocks // 2
    for subset, keep in SUBSETS:
        for config in campaign.configurations:
            for metric in _metrics_of(included, config.name, campaign.metrics):
                early = _values(included, config.name, metric, lambda r: keep(r) and r.block < split)
                late = _values(included, config.name, metric, lambda r: keep(r) and r.block >= split)
                name = f"trend/{subset}/{config.name}/{metric}"
                if len(early) < 2 or len(late) < 2:
                    result.notes.append(f"{name}: fewer than 2 runs in a half, no trend test")
                    continue
                test = mann_whitney(late, early)
                c = compare(BOOTSTRAP, name, late, early)
                result.trend.append({
                    "subset": subset, "configuration": config.name, "metric": metric,
                    "first_blocks": f"0-{split - 1}", "second_blocks": f"{split}-{campaign.blocks - 1}",
                    "n_first": len(early), "n_second": len(late),
                    "median_first_s": c.median_y, "median_second_s": c.median_x,
                    "difference_s": c.difference,
                    "difference_ci_low_s": c.difference_ci[0], "difference_ci_high_s": c.difference_ci[1],
                    "u": test.u, "p": test.p, "method": test.method,
                    "a12": c.a12, "a12_ci_low": c.a12_ci[0], "a12_ci_high": c.a12_ci[1],
                })
    for run in included:
        if not run.first_runs:
            continue
        for metric in campaign.metrics:
            if metric not in run.metrics:
                continue
            values = np.asarray(_values(included, run.configuration, metric), dtype=float)
            others = _values(included, run.configuration, metric, lambda r, run=run: r is not run)
            value = run.metrics[metric]
            rank = float(sps.rankdata(values)[list(values).index(value)])
            result.first_runs.append({
                "configuration": run.configuration, "correlation_id": run.correlation_id,
                "position": run.position, "metric": metric, "value_s": value,
                "rank": rank, "n": int(values.size),
                "median_others_s": float(np.median(others)) if others else None,
            })


def cpu_table(campaign: Campaign, result: Result) -> None:
    with_manifest = [r for r in campaign.runs if r.has_manifest]
    models = sorted({r.cpu_model for r in with_manifest if r.cpu_model is not None})
    for config in campaign.configurations:
        for model in models:
            runs = [r for r in with_manifest if r.configuration == config.name and r.cpu_model == model]
            if runs:
                result.cpu.append({
                    "configuration": config.name, "cpu_model": model,
                    "runs": len(runs), "included_runs": sum(r.included for r in runs),
                })


def _context(run, metric: str) -> str:
    parts = []
    family = metric.split(".")[0]
    if metric in TOTALS:
        related = [m for m in run.metrics if m not in TOTALS and "." not in m]
    else:
        related = [m for m in run.metrics if m != metric and m.split(".")[0] == family]
    parts += [f"{m}={run.metrics[m]:.3f}" for m in related]
    if family == "push" or metric in TOTALS:
        parts += [f"{k}={run.context.get(k)}" for k in ("pushed_layers", "existing_layers", "mounted_layers")]
    if family == "build" or metric in TOTALS:
        parts.append(f"auth_vertices={run.context.get('auth_vertices')}")
    return " ".join(parts)


def unusual(campaign: Campaign, result: Result) -> None:
    included = campaign.included()
    for config in campaign.configurations:
        for metric in _metrics_of(included, config.name, campaign.metrics):
            runs = [r for r in included if r.configuration == config.name and metric in r.metrics]
            box = box_stats([r.metrics[metric] for r in runs])
            for run in runs:
                value = run.metrics[metric]
                if box["lower_fence"] <= value <= box["upper_fence"]:
                    continue
                result.unusual.append({
                    "correlation_id": run.correlation_id, "configuration": config.name,
                    "position": run.position, "metric": metric, "value_s": value,
                    "side": "high" if value > box["upper_fence"] else "low",
                    "median_s": box["median"], "q1_s": box["q1"], "q3_s": box["q3"],
                    "lower_fence_s": box["lower_fence"], "upper_fence_s": box["upper_fence"],
                    "cpu_model": run.cpu_model, "runner_image": run.context.get("runner_image"),
                    "queue_s": run.context.get("queue_s"), "first_runs": run.first_runs,
                    "context": _context(run, metric),
                })
    result.unusual.sort(key=lambda row: (row["position"], campaign.metrics.index(row["metric"])))


def failures(campaign: Campaign, result: Result) -> None:
    groups = defaultdict(list)
    for run in campaign.runs:
        if run.exclusion == FAILED:
            groups[(run.configuration, run.failure_class, run.failure_rule)].append(run)
    order = {c.name: i for i, c in enumerate(campaign.configurations)}
    for (config, failure_class, rule), runs in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], kv[0][1] or "", kv[0][2] or "")):
        result.failures.append({
            "configuration": config, "failure_class": failure_class, "failure_rule": rule,
            "count": len(runs),
            "failed_intervals": " | ".join(sorted({r.failed_interval or "" for r in runs})),
            "correlation_ids": " | ".join(r.correlation_id for r in runs),
        })


def run_table(campaign: Campaign, result: Result) -> None:
    for run in campaign.runs:
        row = {
            "correlation_id": run.correlation_id, "configuration": run.configuration,
            "block": run.block, "position": run.position, "first_runs": run.first_runs,
            "has_manifest": run.has_manifest, "included": run.included, "exclusion": run.exclusion,
            "conclusion": run.conclusion, "failure_class": run.failure_class,
            "failure_rule": run.failure_rule, "failed_interval": run.failed_interval,
            "divergences": " | ".join(run.divergences), "run_attempt": run.run_attempt,
            "run_id": run.context.get("run_id"),
            "cpu_model": run.cpu_model, "runner_image": run.context.get("runner_image"),
            "runner_version": run.context.get("runner_version"), "kernel": run.context.get("kernel"),
            "queue_s": run.context.get("queue_s"),
            "pushed_layers": run.context.get("pushed_layers"),
            "existing_layers": run.context.get("existing_layers"),
            "mounted_layers": run.context.get("mounted_layers"),
            "auth_vertices": run.context.get("auth_vertices"),
        }
        for metric in campaign.metrics:
            row[f"{metric}_s"] = run.metrics.get(metric)
        result.runs.append(row)


def count(campaign: Campaign, result: Result) -> None:
    def tally(runs):
        reasons = Counter(r.exclusion for r in runs if r.has_manifest)
        return {
            "planned": len(runs),
            "manifests": sum(r.has_manifest for r in runs),
            "missing": sum(not r.has_manifest for r in runs),
            "included": reasons[None],
            "failed": reasons[FAILED],
            "excluded_divergence": reasons[DIVERGENCE],
            "excluded_rerun": reasons[RERUN],
        }

    result.counts = {"campaign": tally(campaign.runs)} | {
        c.name: tally([r for r in campaign.runs if r.configuration == c.name])
        for c in campaign.configurations
    }


def analyze(campaign: Campaign) -> Result:
    result = Result()
    count(campaign, result)
    run_table(campaign, result)
    summarize(campaign, result)
    compare_configurations(campaign, result)
    trend(campaign, result)
    cpu_table(campaign, result)
    unusual(campaign, result)
    failures(campaign, result)
    for p in QUANTILES:
        result.notes.append(
            f"p{round(p * 100)} equals the sample maximum for n <= {largest_n_at_maximum(p)}"
        )
    return result
