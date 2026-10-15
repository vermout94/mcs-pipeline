"""Synthetic campaigns with known results.

The manifests follow harness/manifest.schema.json. Durations are given in
seconds per stage and sub-time. Everything else is fixed filler.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

COMMIT = "0" * 39 + "1"
SEED = 12345
STAGES = ("checkout", "build", "test", "push", "sbom", "sign", "provenance", "gate")
SUBTIMES = {"build": ("build.maven", "build.image"), "sign": ("sign.tuf", "sign.sign")}
BASELINE = {"sbom": False, "sign": False, "provenance": False, "policy": False}
SIGN = {"sbom": False, "sign": True, "provenance": False, "policy": False}
CONFIGURATIONS = (("baseline", 0, BASELINE), ("sign", 4, SIGN))
T0 = 100_000_000_000


def _interval(name: str, start_ns: int, seconds: float) -> dict:
    end_ns = start_ns + round(seconds * 1e9)
    return {
        "name": name,
        "start_monotonic_ns": start_ns,
        "end_monotonic_ns": end_ns,
        "start_wall": "2000-01-01T00:00:00.000000000Z",
        "end_wall": "2000-01-01T00:00:01.000000000Z",
        "exit_code": 0,
        "network": name in ("checkout", "push", "sign", "sign.tuf", "sign.sign"),
    }


def _stages(durations: dict) -> tuple[list, int]:
    """Stage entries and the sum of the enabled stages in ns."""
    stages = []
    clock = T0
    total = 0
    for name in STAGES:
        if name not in durations and not any(s in durations for s in SUBTIMES.get(name, ())):
            stages.append({"name": name, "enabled": False, "interval": None, "subtimes": []})
            continue
        subs = []
        start = clock
        if name in SUBTIMES:
            for sub in SUBTIMES[name]:
                iv = _interval(sub, clock, durations[sub])
                subs.append(iv)
                clock = iv["end_monotonic_ns"]
            interval = dict(_interval(name, start, 0), end_monotonic_ns=clock)
        else:
            interval = _interval(name, start, durations[name])
            clock = interval["end_monotonic_ns"]
        total += interval["end_monotonic_ns"] - interval["start_monotonic_ns"]
        clock += 50_000_000
        stages.append({"name": name, "enabled": True, "interval": interval, "subtimes": subs})
    return stages, total


def manifest(campaign: str, order_sha256: str, plan_sha256: str, entry: dict, durations: dict,
             job_s: float, cpu_model: str = "Test CPU A", failure: tuple | None = None,
             divergences: tuple = (), push_layers: tuple = (1, 5, 0)) -> dict:
    """A manifest for one planned run. failure = (class, rule, interval) or None."""
    cid = entry["correlation_id"]
    stages, total = _stages(durations)
    success = failure is None
    null_reasons = {
        "/design/ground_truth_variant": "synthetic",
        "/design/slsa_configuration": "synthetic",
        "/environment/vm": "synthetic",
        "/run/job_log": "synthetic",
        "/versions/caches/grype_db": "synthetic",
        "/outcome/gate/expected": "synthetic",
    }
    for i, stage in enumerate(stages):
        if stage["interval"] is None:
            null_reasons[f"/timing/stages/{i}/interval"] = "stage disabled in this configuration"
    if success:
        for key in ("failed_step", "failed_interval", "failed_stage", "failure_class", "failure_evidence"):
            null_reasons[f"/outcome/{key}"] = "the run succeeded (R01)"
    return {
        "schema_version": 1,
        "campaign": {
            "id": campaign, "kind": "pilot", "plan_sha256": plan_sha256,
            "order_sha256": order_sha256, "seed": SEED, "repo_commit": COMMIT,
        },
        "run": {
            "correlation_id": cid,
            "workflow": ".github/workflows/measure.yml",
            "ref": "main",
            "dispatched_at": "2000-01-01T00:00:00.000000Z",
            "dispatch_response": {
                "workflow_run_id": 1000 + entry["position"],
                "run_url": "https://api.github.com/repos/example/example/actions/runs/1",
                "html_url": "https://github.com/example/example/actions/runs/1",
            },
            "run_id": 1000 + entry["position"],
            "run_attempt": 1,
            "head_sha": COMMIT,
            "display_title": f"measure {cid}",
            "html_url": "https://github.com/example/example/actions/runs/1",
            "matched_by": ["dispatch_response", "run_name", "artifact"],
            "artifact": {
                "name": f"measure-{cid}", "id": 1, "size_in_bytes": 1,
                "digest": "sha256:" + "a" * 64, "sha256": "a" * 64,
                "path": f"artifacts/measure-{cid}.zip", "expires_at": "2000-04-01T00:00:00Z",
            },
            "job_log": None,
        },
        "design": {
            "configuration": entry["configuration"], "cell": entry["cell"], "factors": entry["factors"],
            "slsa_configuration": None, "block": entry["block"],
            "position_in_block": entry["position_in_block"], "position_in_campaign": entry["position"],
            "first_runs": entry["first_runs"], "ground_truth_variant": None,
        },
        "environment": {
            "type": "hosted",
            "runner": {
                "name": "GitHub Actions 1", "version": "2.337.0", "os": "Linux", "arch": "X64",
                "environment": "github-hosted", "group": "GitHub Actions", "labels": ["ubuntu-24.04"],
                "image_os": "ubuntu24", "image_version": "20000101.1.1",
            },
            "cpu": {"model": cpu_model, "logical_cores": 4, "affinity_cores": 4, "flags_sha256": "b" * 64},
            "memory_bytes": 16_000_000_000,
            "kernel": "6.0.0-test",
            "kernel_version": "#1 SMP",
            "os": "Ubuntu 24.04 LTS",
            "docker": {"server_version": "28.0.4", "storage_driver": "overlay2",
                       "cgroup_driver": "systemd", "default_runtime": "runc"},
            "vm": None,
        },
        "clock": {"stage_clock": "CLOCK_MONOTONIC", "stage_clock_resolution_ns": 1,
                  "wall_clock": "CLOCK_REALTIME", "clocksource": "tsc", "api_resolution_s": 1},
        "versions": {
            "versions_json_sha256": "c" * 64,
            "tools": [{"name": "cosign", "pinned_version": "v3.1.3", "sha256": "d" * 64,
                       "reported_version": "v3.1.3"}],
            "actions": [{"name": "actions/checkout", "version": "v7.0.1", "sha": "e" * 40}],
            "base_image": {"name": "ghcr.io/example/base", "digest": "sha256:" + "f" * 64},
            "image": {"name": "ghcr.io/example/app", "tag": cid, "digest": "sha256:" + "1" * 64,
                      "label_correlation_id": cid},
            "caches": {
                "maven_repository": {"cache_name": "m2", "expected_sha256": "2" * 64,
                                     "after_restore_sha256": "2" * 64, "after_run_sha256": "2" * 64,
                                     "files": 1, "bytes": 1},
                "grype_db": None,
                "docker_build_cache": "none",
            },
        },
        "timing": {
            "preparation": [],
            "stages": stages,
            "totals": {"stages_sum_ns": total, "span_ns": total + 1},
            "run_api": {"created_at": "2000-01-01T00:00:00Z", "run_started_at": "2000-01-01T00:00:00Z",
                        "updated_at": "2000-01-01T00:02:00Z"},
            "job_api": {"id": 1, "created_at": "2000-01-01T00:00:00Z", "started_at": "2000-01-01T00:00:02Z",
                        "completed_at": "2000-01-01T00:01:00Z", "queue_s": 2.0, "duration_s": job_s},
            "steps_api": [],
        },
        "checks": {
            "maven": {
                "build_maven": {"download_lines": 0, "build_success": True, "offline_skip_lines": 1},
                "test": {"download_lines": 0, "build_success": True, "offline_skip_lines": 1},
            },
            "maven_repository_unchanged": True,
            "buildkit": {"auth_vertices": [], "from_lines": [], "warnings": []},
            "push": {"pushed_layers": push_layers[0], "existing_layers": push_layers[1],
                     "mounted_layers": push_layers[2]},
            "tests": {"report_files": 16, "classes_run": 19, "tests": 77, "failures": 0,
                      "errors": 0, "skipped": 0},
            "image_label_matches": True,
            "oidc_variables_in_job": {"ACTIONS_ID_TOKEN_REQUEST_URL": True,
                                      "ACTIONS_ID_TOKEN_REQUEST_TOKEN": True},
            "divergences": list(divergences),
        },
        "outcome": {
            "status": "completed",
            "conclusion": "success" if success else "failure",
            "failed_step": None if success else "Stage test",
            "failed_interval": None if success else failure[2],
            "failed_stage": None if success else failure[2].split(".")[0],
            "failure_class": None if success else failure[0],
            "failure_rule": "R01" if success else failure[1],
            "failure_evidence": None if success else "synthetic",
            "gate": {"result": "not_run", "expected": None, "checks": []},
        },
        "orchestrator": {"version": "0.1.0", "classifier_version": 1, "commit": COMMIT, "dirty": False,
                         "python": "3.13.7", "written_at": "2000-01-01T00:02:00.000000Z"},
        "null_reasons": null_reasons,
    }


def order(campaign: str, plan_sha256: str, blocks: int) -> dict:
    """Blocks with baseline first in even blocks and sign first in odd blocks."""
    runs = []
    for block in range(blocks):
        configs = CONFIGURATIONS if block % 2 == 0 else CONFIGURATIONS[::-1]
        for k, (name, cell, factors) in enumerate(configs):
            position = len(runs)
            runs.append({
                "position": position, "block": block, "position_in_block": k,
                "configuration": name, "factors": factors, "cell": cell,
                "correlation_id": f"{campaign}-{position:04d}", "first_runs": block == 0,
            })
    return {
        "schema": "mcs-pipeline order 1", "campaign": campaign, "kind": "pilot",
        "plan_sha256": plan_sha256, "seed": SEED, "algorithm": "synthetic", "python": "3.13.7",
        "created_at": "2000-01-01T00:00:00Z", "repository": "example/example", "workflow": "measure.yml",
        "ref": "main", "repo_commit": COMMIT, "environment": "hosted", "runs": runs,
    }


def write_campaign(data_dir: Path, campaign: str, blocks: int, make) -> Path:
    """Write plan.yaml, order.json and one manifest per planned run.

    make(entry) returns the keyword arguments of manifest() for a run, or None
    for a run without manifest.
    """
    directory = data_dir / campaign
    (directory / "manifests").mkdir(parents=True)
    plan = directory / "plan.yaml"
    plan.write_text(f"campaign: {campaign}\n", encoding="utf-8")
    plan_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
    order_doc = order(campaign, plan_sha256, blocks)
    order_path = directory / "order.json"
    order_path.write_text(json.dumps(order_doc, indent=2) + "\n", encoding="utf-8")
    order_sha256 = hashlib.sha256(order_path.read_bytes()).hexdigest()
    for entry in order_doc["runs"]:
        kwargs = make(entry)
        if kwargs is None:
            continue
        doc = manifest(campaign, order_sha256, plan_sha256, entry, **kwargs)
        path = directory / "manifests" / f"{entry['correlation_id']}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return directory
