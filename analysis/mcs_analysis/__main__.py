"""Command line: check and analyze a campaign.

    uv run --locked python -m mcs_analysis check <campaign>
    uv run --locked python -m mcs_analysis run <campaign> [--allow-dirty]

`run` writes to data/<campaign>/analysis/. It stops if analysis/ has
uncommitted changes or if git cannot tell (no Git checkout, no commit),
unless --allow-dirty is given. The header of every output names the commit
and the state of analysis/ (true, false or unknown), analysis.json the
reason for an unknown state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analyze import BOOTSTRAP, FENCE_FACTOR, analyze
from .load import EXCLUSION_RULE_VERSION, LoadError, load_campaign, sha256_of
from .output import write_tables
from .plots import write_plots
from .provenance import REPO_ROOT, environment, git_state, lock_settings
from .quantiles import DEFINITION

SCHEMA = REPO_ROOT / "harness" / "manifest.schema.json"


def shown(path: Path) -> str:
    """A path relative to the repository or the working directory, never absolute."""
    for base in (REPO_ROOT, Path.cwd()):
        try:
            return str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            continue
    return path.name


def settings() -> dict:
    return {
        "quantiles": DEFINITION,
        "variation": "coefficient of variation = sd / mean, sd with n - 1",
        "mad": "median absolute deviation from the median, unscaled",
        "mann_whitney": (
            "scipy.stats.mannwhitneyu, two-sided, method set explicitly: 'exact' if both "
            "samples have fewer than 50 values and the pooled sample has no ties, otherwise "
            "'asymptotic' with tie correction and continuity correction"
        ),
        "a12": "Vargha and Delaney A12 = (R1/m - (m + 1)/2)/n with midranks (Arcuri and Briand, equation (1))",
        "p_values": "reported as computed, no correction for multiple tests",
        "bootstrap": {
            "interval": "percentile, B alpha-th and B (1 - alpha)-th ordered replicate",
            "seed": BOOTSTRAP.seed,
            "resamples": BOOTSTRAP.resamples,
            "confidence": BOOTSTRAP.confidence,
            "resampling": "both samples independently with replacement, x first",
            "stream": "numpy PCG64 from SeedSequence(int(SHA-256('<seed>:<name of the comparison>')))",
        },
        "trend": "second half of the blocks against the first half (blocks below floor(B/2))",
        "unusual": f"outside Q1 - {FENCE_FACTOR} IQR or Q3 + {FENCE_FACTOR} IQR, quartiles after definition 8",
        "exclusion_rule": {
            "version": EXCLUSION_RULE_VERSION,
            "rule": (
                "included if the manifest is valid, failure_rule is R01, checks.divergences "
                "is empty and run_attempt is 1"
            ),
        },
        "subsets": ["all", "without_first_runs"],
        "interactions": "not analyzed",
    }


def cmd_check(args) -> int:
    campaign = load_campaign(args.data / args.campaign, SCHEMA)
    counts = {
        "planned": len(campaign.runs),
        "manifests": sum(r.has_manifest for r in campaign.runs),
        "missing": [r.correlation_id for r in campaign.missing()],
        "failed": [
            f"{r.correlation_id} {r.failure_class} {r.failure_rule}"
            for r in campaign.runs if r.exclusion == "failed"
        ],
        "excluded": [
            f"{r.correlation_id} {r.exclusion}"
            for r in campaign.runs if r.exclusion not in (None, "failed")
        ],
        "included": len(campaign.included()),
    }
    print(json.dumps({"campaign": campaign.id, "all_manifests_valid": True} | counts, indent=2))
    return 0


def cmd_run(args) -> int:
    git = git_state()
    if not args.allow_dirty:
        if git["dirty"] is None:
            print(f"cannot check analysis/ for uncommitted changes ({git['reason']}). "
                  "Pass --allow-dirty to run anyway.", file=sys.stderr)
            return 2
        if git["dirty"]:
            print("analysis/ has uncommitted changes. Commit first or pass --allow-dirty.", file=sys.stderr)
            return 2
    campaign_dir = args.data / args.campaign
    campaign = load_campaign(campaign_dir, SCHEMA)
    out_dir = args.out if args.out is not None else campaign_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = analyze(campaign)
    written = write_tables(out_dir, campaign, result, git)
    written += write_plots(out_dir, campaign, git)
    record = {
        "schema": "mcs-pipeline analysis 1",
        "campaign": campaign.id,
        "kind": campaign.kind,
        "environment": campaign.environment,
        "campaign_commit": campaign.repo_commit,
        "analysis": {
            "version": __version__,
            "commit": git["commit"],
            "dirty": git["dirty"],
            "git_unknown_reason": git["reason"],
            "allow_dirty": args.allow_dirty,
            "lock": lock_settings(),
        } | environment(),
        "settings": settings(),
        "counts": result.counts,
        "cpu_models": result.cpu_models,
        "notes": result.notes,
        "inputs": campaign.inputs,
        "outputs": {name: sha256_of(out_dir / name) for name in sorted(written)},
    }
    (out_dir / "analysis.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    total = result.counts["campaign"]
    print(f"{campaign.id}: {total['planned']} planned, {total['manifests']} manifests, "
          f"{total['included']} included, {total['failed']} failed, {total['missing']} missing")
    for row in result.overhead:
        if row["subset"] == "all":
            print(f"  {row['configuration']} {row['metric']} ({row['kind']}): {row['difference_s']:.3f} s "
                  f"[{row['difference_ci_low_s']:.3f}, {row['difference_ci_high_s']:.3f}], "
                  f"{100 * row['relative']:.1f} % [{100 * row['relative_ci_low']:.1f}, "
                  f"{100 * row['relative_ci_high']:.1f}]")
    print(f"wrote {len(written) + 1} files to {shown(out_dir)}/")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mcs_analysis")
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "data", help="directory of the campaigns")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate the manifests and count the runs")
    check.add_argument("campaign")
    run = sub.add_parser("run", help="analyze a campaign")
    run.add_argument("campaign")
    run.add_argument("--out", type=Path, default=None, help="output directory (default data/<campaign>/analysis)")
    run.add_argument("--allow-dirty", action="store_true", help="run with uncommitted changes in analysis/")
    args = parser.parse_args(argv)
    try:
        return cmd_check(args) if args.command == "check" else cmd_run(args)
    except LoadError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
