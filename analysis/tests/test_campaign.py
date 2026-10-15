"""End-to-end runs on synthetic campaigns whose results are known.

Campaign A (shift): 30 blocks of baseline and sign. Among the included runs
both configurations have the same values in every common stage, the sign
stage adds exactly 5 s, so every sign run takes longer than every baseline
run. One baseline run fails (R08), one sign run has a divergence, the sign
run with the longest durations gets a push outlier of 35.7 s.

Campaign B (drift): the test stage grows with the position in the campaign,
the CPU model alternates by block.
"""

import csv
import hashlib
import json
import re
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
import synthetic

import mcs_analysis.__main__ as cli
from mcs_analysis.__main__ import main

SHIFT = "pilot-hosted-2000-01-01"
DRIFT = "pilot-hosted-2000-01-02"
FAILED_POSITION = 4     # baseline, block 2
DIVERGENT_POSITION = 6  # sign, block 3
ANALYSIS_DIR = Path(__file__).resolve().parents[1]
# A fixed state of analysis/, so that the tests do not depend on the checkout.
CLEAN = {"commit": "a" * 40, "dirty": False, "reason": None}
HEADER = re.compile(r"analysis commit: ([0-9a-f]{40}|unknown) \(dirty: (true|false|unknown)\)")


def run_with_state(state: dict, argv: list) -> int:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "git_state", lambda: dict(state))
        return main(argv)


def _base(k: int) -> dict:
    return {
        "checkout": 0.9 + 0.01 * k, "build.maven": 5.0 + 0.02 * k, "build.image": 2.0 + 0.01 * k,
        "test": 20.0 + 0.03 * k, "push": 7.0 + 0.005 * k,
    }


def shift_campaign(data_dir: Path) -> tuple[Path, str]:
    counters = {"baseline": 0, "sign": 0}
    outlier = {}

    def make(entry):
        if entry["position"] in (FAILED_POSITION, DIVERGENT_POSITION):
            kwargs = {"durations": _base(0) | ({"sign.tuf": 0.5, "sign.sign": 4.5} if entry["factors"]["sign"] else {}),
                      "job_s": 50.0}
            if entry["position"] == FAILED_POSITION:
                kwargs["failure"] = ("test", "R08", "test")
            else:
                kwargs["divergences"] = ("content hash of the Maven repository differs",)
            return kwargs
        name = entry["configuration"]
        i = counters[name]
        counters[name] += 1
        k = i if name == "baseline" else 28 - i  # same values, other order
        d = _base(k)
        if name == "sign":
            d |= {"sign.tuf": 0.5, "sign.sign": 4.5}
            if k == 28:
                d["push"] = 35.7
                outlier["id"] = entry["correlation_id"]
        total = sum(d.values())
        return {"durations": d, "job_s": float(round(total + 20))}

    directory = synthetic.write_campaign(data_dir, SHIFT, 30, make)
    return directory, outlier["id"]


def drift_campaign(data_dir: Path) -> Path:
    def make(entry):
        d = {"checkout": 1.0, "build.maven": 5.0, "build.image": 2.0,
             "test": 20.0 + 0.01 * entry["position"], "push": 7.0}
        if entry["factors"]["sign"]:
            d |= {"sign.tuf": 0.5, "sign.sign": 4.5}
        cpu = "Test CPU A" if entry["block"] % 2 == 0 else "Test CPU B"
        return {"durations": d, "job_s": 60.0, "cpu_model": cpu}

    return synthetic.write_campaign(data_dir, DRIFT, 30, make)


def read_csv(path: Path) -> list:
    lines = [line for line in path.read_text().splitlines() if not line.startswith("#")]
    return list(csv.DictReader(lines))


def pick(rows, **match):
    found = [r for r in rows if all(r[k] == v for k, v in match.items())]
    assert len(found) == 1, (match, found)
    return found[0]


@pytest.fixture(scope="module")
def shift(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    directory, outlier = shift_campaign(data)
    assert run_with_state(CLEAN, ["--data", str(data), "run", SHIFT]) == 0
    return directory / "analysis", outlier


@pytest.fixture(scope="module")
def drift(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    directory = drift_campaign(data)
    assert run_with_state(CLEAN, ["--data", str(data), "run", DRIFT]) == 0
    return directory / "analysis"


def test_counts_and_failures(shift):
    out, _ = shift
    record = json.loads((out / "analysis.json").read_text())
    assert record["counts"]["campaign"] == {
        "planned": 60, "manifests": 60, "missing": 0, "included": 58,
        "failed": 1, "excluded_divergence": 1, "excluded_rerun": 0,
    }
    failures = read_csv(out / "failures.csv")
    assert failures == [{
        "configuration": "baseline", "failure_class": "test", "failure_rule": "R08", "count": "1",
        "failed_intervals": "test", "correlation_ids": f"{SHIFT}-{FAILED_POSITION:04d}",
    }]
    runs = {r["correlation_id"]: r for r in read_csv(out / "runs.csv")}
    assert runs[f"{SHIFT}-{DIVERGENT_POSITION:04d}"]["exclusion"] == "divergence"
    assert runs[f"{SHIFT}-{DIVERGENT_POSITION:04d}"]["included"] == "false"


def test_summary(shift):
    out, _ = shift
    rows = read_csv(out / "summary.csv")
    assert {r["cpu_model"] for r in rows} == {"all"}
    row = pick(rows, subset="all", cpu_model="all", configuration="baseline", metric="test")
    assert row["n"] == "29"
    assert float(row["median_s"]) == pytest.approx(20.0 + 0.03 * 14)
    assert float(row["p99_s"]) == pytest.approx(20.0 + 0.03 * 28)
    assert (row["p95_is_max"], row["p99_is_max"]) == ("false", "true")
    sign = pick(rows, subset="all", cpu_model="all", configuration="sign", metric="sign")
    assert float(sign["median_s"]) == pytest.approx(5.0) and float(sign["sd_s"]) == pytest.approx(0.0, abs=1e-9)
    without = pick(rows, subset="without_first_runs", cpu_model="all", configuration="baseline", metric="test")
    assert without["n"] == "28"


def test_tests(shift):
    out, _ = shift
    rows = read_csv(out / "tests.csv")
    for metric in ("checkout", "build", "build.maven", "build.image", "test"):
        row = pick(rows, subset="all", metric=metric)
        assert float(row["a12"]) == 0.5 and float(row["p"]) == 1.0
        assert row["method"] == "asymptotic"  # identical values in both samples are ties
    total = pick(rows, subset="all", metric="stages_sum")
    assert total["method"] == "exact"
    assert float(total["a12"]) == 1.0
    assert float(total["p"]) == pytest.approx(2 / math.comb(58, 29), rel=1e-5)
    assert (float(total["a12_ci_low"]), float(total["a12_ci_high"])) == (1.0, 1.0)
    assert pick(rows, subset="all", metric="job")["method"] == "asymptotic"


def test_overhead(shift):
    out, _ = shift
    rows = read_csv(out / "overhead.csv")
    total = pick(rows, subset="all", metric="stages_sum")
    assert total["kind"] == "difference_of_medians"
    assert float(total["difference_s"]) == pytest.approx(5.0, abs=1e-6)
    assert float(total["difference_ci_low_s"]) < 5.0 < float(total["difference_ci_high_s"])
    base_median = sum(_base(14).values())
    assert float(total["relative"]) == pytest.approx(5.0 / base_median, abs=1e-6)
    stage = pick(rows, subset="all", metric="sign")
    assert stage["kind"] == "stage_median" and stage["relative_to"] == "baseline/stages_sum"
    assert float(stage["difference_s"]) == pytest.approx(5.0)
    assert float(stage["difference_ci_low_s"]) == pytest.approx(5.0)
    assert float(stage["difference_ci_high_s"]) == pytest.approx(5.0)


def test_unusual(shift):
    out, outlier = shift
    rows = read_csv(out / "unusual.csv")
    assert {(r["correlation_id"], r["metric"]) for r in rows} == {
        (outlier, "push"), (outlier, "stages_sum"), (outlier, "job"),
    }
    push = pick(rows, metric="push")
    assert float(push["value_s"]) == pytest.approx(35.7) and push["side"] == "high"
    assert "pushed_layers=1 existing_layers=5 mounted_layers=0" in push["context"]


def test_tables_and_header(shift):
    out, _ = shift
    for name in ("summary.csv", "tests.csv", "overhead.csv", "summary.tex", "overhead.tex", "failures.tex"):
        text = (out / name).read_text()
        assert f"campaign: {SHIFT}" in text
        assert f"campaign commit: {synthetic.COMMIT}" in text
        assert f"analysis commit: {'a' * 40} (dirty: false)" in text
    summary = (out / "summary.tex").read_text()
    assert "\\toprule" in summary and "\\midrule" in summary and "\\bottomrule" in summary
    assert "a: equals the sample maximum" in summary and "n \\le 66" in summary
    assert "No run failed." not in (out / "failures.tex").read_text()
    record = json.loads((out / "analysis.json").read_text())
    assert record["settings"]["bootstrap"]["seed"] == 2106950589
    assert record["settings"]["bootstrap"]["resamples"] == 10_000
    assert record["analysis"]["lock"]["exclude_newer"] == "2026-10-01T00:00:00Z"
    assert record["analysis"]["libraries"]["numpy"] == "2.5.3"
    assert (record["analysis"]["commit"], record["analysis"]["dirty"]) == ("a" * 40, False)
    assert record["analysis"]["git_unknown_reason"] is None
    for name, digest in record["outputs"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert (out / "boxplot.pdf").read_bytes()[:4] == b"%PDF"
    assert b"CreationDate" not in (out / "trend.pdf").read_bytes()


def test_trend_and_first_runs(drift):
    rows = read_csv(drift / "trend.csv")
    for config in ("baseline", "sign"):
        row = pick(rows, subset="all", configuration=config, metric="test")
        assert (row["first_blocks"], row["second_blocks"]) == ("0-14", "15-29")
        assert (row["n_first"], row["n_second"]) == ("15", "15")
        assert row["method"] == "exact"
        assert float(row["a12"]) == 1.0
        assert float(row["p"]) == pytest.approx(2 / math.comb(30, 15), rel=1e-6)
        without = pick(rows, subset="without_first_runs", configuration=config, metric="test")
        assert (without["n_first"], without["n_second"]) == ("14", "15")
    first = read_csv(drift / "first_runs.csv")
    for row in (r for r in first if r["metric"] == "test"):
        assert float(row["rank"]) == 1.0 and row["n"] == "30"


def test_cpu_models(drift):
    assert read_csv(drift / "cpu.csv") == [
        {"configuration": c, "cpu_model": m, "runs": "15", "included_runs": "15"}
        for c in ("baseline", "sign") for m in ("Test CPU A", "Test CPU B")
    ]
    rows = read_csv(drift / "summary.csv")
    assert {r["cpu_model"] for r in rows} == {"all", "Test CPU A", "Test CPU B"}
    assert pick(rows, subset="all", cpu_model="Test CPU B", configuration="sign", metric="test")["n"] == "15"
    assert "No run failed." in (drift / "failures.tex").read_text()


def test_independent_of_python_hash_seed(tmp_path):
    """Two processes with different PYTHONHASHSEED give the same tables."""
    data = tmp_path / "data"
    shift_campaign(data)
    outputs = []
    for hash_seed in ("0", "4242"):
        out = tmp_path / f"out-{hash_seed}"
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        subprocess.run(
            [sys.executable, "-m", "mcs_analysis", "--data", str(data), "run", SHIFT,
             "--out", str(out), "--allow-dirty"],
            cwd=ANALYSIS_DIR, env=env, check=True, capture_output=True,
        )
        outputs.append(out)
    names = sorted(p.name for p in outputs[0].iterdir())
    assert names == sorted(p.name for p in outputs[1].iterdir())
    assert "overhead.csv" in names and "tests.csv" in names
    for name in names:
        assert (outputs[0] / name).read_bytes() == (outputs[1] / name).read_bytes(), name


def small_campaign(tmp_path) -> Path:
    data = tmp_path / "data"
    synthetic.write_campaign(data, SHIFT, 3, lambda e: {
        "durations": _base(e["position"]) | ({"sign.tuf": 0.5, "sign.sign": 4.5} if e["factors"]["sign"] else {}),
        "job_s": 60.0 + e["position"],
    })
    return data


@pytest.mark.parametrize("state", [
    {"commit": "a" * 40, "dirty": True, "reason": None},
    {"commit": None, "dirty": None, "reason": "not a Git repository"},
])
def test_run_stops_without_allow_dirty(tmp_path, capsys, state):
    data = small_campaign(tmp_path)
    assert run_with_state(state, ["--data", str(data), "run", SHIFT]) == 2
    assert not (data / SHIFT / "analysis").exists()
    assert "--allow-dirty" in capsys.readouterr().err


def test_unknown_state_with_allow_dirty(tmp_path):
    data = small_campaign(tmp_path)
    state = {"commit": None, "dirty": None, "reason": "not a Git repository"}
    assert run_with_state(state, ["--data", str(data), "run", SHIFT, "--allow-dirty"]) == 0
    out = data / SHIFT / "analysis"
    for name in ("summary.csv", "overhead.tex"):
        text = (out / name).read_text()
        assert "analysis commit: unknown (dirty: unknown)" in text and "None" not in text
    record = json.loads((out / "analysis.json").read_text())
    assert record["analysis"]["commit"] is None and record["analysis"]["dirty"] is None
    assert record["analysis"]["git_unknown_reason"] == "not a Git repository"
    assert record["analysis"]["allow_dirty"] is True


def test_header_format_of_the_checkout(tmp_path):
    """Whatever the state of this checkout, the header has the fixed format."""
    data = small_campaign(tmp_path)
    assert main(["--data", str(data), "run", SHIFT, "--allow-dirty"]) == 0
    text = (data / SHIFT / "analysis" / "summary.csv").read_text()
    assert HEADER.search(text)
