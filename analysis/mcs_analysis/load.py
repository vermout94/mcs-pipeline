"""Read a campaign: order.json, plan.yaml and the run manifests.

The analysis reads only the versioned files of a campaign. Every manifest is
validated against harness/manifest.schema.json and checked against order.json.
Any inconsistency stops the analysis (LoadError). A planned run without
manifest is reported as missing.

Exclusion rule, version 1 (fixed before the pilot of 2026-10-15 finished):
a run enters the timing analysis if its manifest is valid, the run succeeded
(failure rule R01), checks.divergences is empty and run_attempt is 1.
Runs that did not succeed are counted by failure class and rule (RQ2).
Outliers stay in the data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

EXCLUSION_RULE_VERSION = 1
STAGES = ("checkout", "build", "test", "push", "sbom", "sign", "provenance", "gate")
TOTALS = ("stages_sum", "job")

FAILED = "failed"
DIVERGENCE = "divergence"
RERUN = "re-run"


class LoadError(Exception):
    """The campaign data is inconsistent."""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Configuration:
    name: str
    cell: int
    factors: dict

    @property
    def is_reference(self) -> bool:
        return not any(self.factors.values())


@dataclass
class Run:
    correlation_id: str
    configuration: str
    block: int
    position: int
    first_runs: bool
    has_manifest: bool
    conclusion: str | None = None
    failure_class: str | None = None
    failure_rule: str | None = None
    failed_interval: str | None = None
    divergences: list = field(default_factory=list)
    run_attempt: int | None = None
    exclusion: str | None = None
    metrics: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)

    @property
    def included(self) -> bool:
        return self.has_manifest and self.exclusion is None

    @property
    def cpu_model(self) -> str | None:
        return self.context.get("cpu_model")


@dataclass
class Campaign:
    id: str
    kind: str
    environment: str
    repo_commit: str
    order_sha256: str
    plan_sha256: str
    blocks: int
    configurations: list
    runs: list
    metrics: list
    inputs: dict

    @property
    def reference(self) -> Configuration:
        refs = [c for c in self.configurations if c.is_reference]
        if len(refs) != 1:
            raise LoadError("the campaign needs exactly one configuration with all factors off")
        return refs[0]

    def included(self) -> list:
        return [r for r in self.runs if r.included]

    def missing(self) -> list:
        return [r for r in self.runs if not r.has_manifest]


def schema_validator(schema_path: Path):
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker(formats=())
    )


def _duration_s(interval) -> float | None:
    if interval is None:
        return None
    start = interval["start_monotonic_ns"]
    end = interval["end_monotonic_ns"]
    if start is None or end is None:
        return None
    return (end - start) / 1e9


def metrics_of(manifest: dict) -> tuple[dict, dict]:
    """Durations in seconds and their sort keys.

    Stages and sub-times from CLOCK_MONOTONIC (ns), the sum of the enabled
    stages from timing.totals and the job duration from the Jobs API (1 s).
    """
    values: dict = {}
    keys: dict = {}
    for stage in manifest["timing"]["stages"]:
        if not stage["enabled"]:
            continue
        index = STAGES.index(stage["name"])
        duration = _duration_s(stage["interval"])
        if duration is not None:
            values[stage["name"]] = duration
            keys[stage["name"]] = (index, 0)
        for k, sub in enumerate(stage["subtimes"], start=1):
            duration = _duration_s(sub)
            if duration is not None:
                values[sub["name"]] = duration
                keys[sub["name"]] = (index, k)
    totals = manifest["timing"]["totals"]
    if totals is not None and totals.get("stages_sum_ns") is not None:
        values["stages_sum"] = totals["stages_sum_ns"] / 1e9
        keys["stages_sum"] = (len(STAGES), 0)
    job = manifest["timing"]["job_api"]
    if job is not None and job.get("duration_s") is not None:
        values["job"] = float(job["duration_s"])
        keys["job"] = (len(STAGES), 1)
    return values, keys


def context_of(manifest: dict) -> dict:
    """Facts of the manifest that help to explain an unusual value."""
    env = manifest["environment"]
    checks = manifest["checks"]
    job = manifest["timing"]["job_api"] or {}
    push = checks.get("push") or {}
    buildkit = checks.get("buildkit") or {}
    return {
        "cpu_model": (env.get("cpu") or {}).get("model"),
        "runner_image": (env.get("runner") or {}).get("image_version"),
        "runner_version": (env.get("runner") or {}).get("version"),
        "kernel": env.get("kernel"),
        "queue_s": job.get("queue_s"),
        "pushed_layers": push.get("pushed_layers"),
        "existing_layers": push.get("existing_layers"),
        "mounted_layers": push.get("mounted_layers"),
        "auth_vertices": len(buildkit.get("auth_vertices") or []),
        "run_id": manifest["run"]["run_id"],
    }


def exclusion_of(manifest: dict) -> str | None:
    """Exclusion rule version 1."""
    outcome = manifest["outcome"]
    if outcome["failure_rule"] != "R01":
        return FAILED
    if outcome["conclusion"] != "success" or outcome["failure_class"] is not None:
        raise LoadError(
            f"{manifest['run']['correlation_id']}: failure rule R01 without a successful run"
        )
    if manifest["checks"]["divergences"]:
        return DIVERGENCE
    if manifest["run"]["run_attempt"] != 1:
        return RERUN
    return None


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise LoadError(message)


def load_campaign(campaign_dir: Path, schema_path: Path) -> Campaign:
    campaign_id = campaign_dir.name
    order_path = campaign_dir / "order.json"
    plan_path = campaign_dir / "plan.yaml"
    _check(order_path.is_file(), f"{campaign_id}: order.json missing")
    _check(plan_path.is_file(), f"{campaign_id}: plan.yaml missing")
    order = json.loads(order_path.read_text(encoding="utf-8"))
    order_sha256 = sha256_of(order_path)
    plan_sha256 = sha256_of(plan_path)
    _check(order["campaign"] == campaign_id, f"{campaign_id}: order.json names {order['campaign']}")
    _check(order["plan_sha256"] == plan_sha256, f"{campaign_id}: SHA-256 of plan.yaml differs from order.json")

    validator = schema_validator(schema_path)
    inputs = {
        "harness/manifest.schema.json": sha256_of(schema_path),
        "order.json": order_sha256,
        "plan.yaml": plan_sha256,
    }

    configurations: dict = {}
    runs = []
    metric_keys: dict = {}
    manifest_dir = campaign_dir / "manifests"
    planned = {entry["correlation_id"] for entry in order["runs"]}
    if manifest_dir.is_dir():
        extra = sorted(p.name for p in manifest_dir.glob("*.json") if p.stem not in planned)
        _check(not extra, f"{campaign_id}: manifests without a planned run: {', '.join(extra)}")

    for entry in sorted(order["runs"], key=lambda e: e["position"]):
        name = entry["configuration"]
        config = Configuration(name, entry["cell"], dict(entry["factors"]))
        _check(configurations.setdefault(name, config) == config, f"{campaign_id}: configuration {name} is not consistent in order.json")
        cid = entry["correlation_id"]
        run = Run(
            correlation_id=cid,
            configuration=name,
            block=entry["block"],
            position=entry["position"],
            first_runs=entry["first_runs"],
            has_manifest=False,
        )
        runs.append(run)
        path = manifest_dir / f"{cid}.json"
        if not path.is_file():
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path))
        _check(not errors, f"{cid}: manifest does not validate: {errors[0].message if errors else ''}")
        camp = manifest["campaign"]
        design = manifest["design"]
        _check(camp["id"] == campaign_id, f"{cid}: campaign {camp['id']}")
        _check(camp["order_sha256"] == order_sha256, f"{cid}: SHA-256 of order.json differs")
        _check(camp["plan_sha256"] == plan_sha256, f"{cid}: SHA-256 of plan.yaml differs")
        _check(camp["repo_commit"] == order["repo_commit"], f"{cid}: campaign commit differs")
        _check(camp["seed"] == order["seed"], f"{cid}: seed differs")
        _check(manifest["run"]["correlation_id"] == cid, f"{cid}: correlation ID differs")
        for key, expected in (
            ("configuration", name),
            ("cell", entry["cell"]),
            ("factors", entry["factors"]),
            ("block", entry["block"]),
            ("position_in_block", entry["position_in_block"]),
            ("position_in_campaign", entry["position"]),
            ("first_runs", entry["first_runs"]),
        ):
            _check(design[key] == expected, f"{cid}: design.{key} differs from order.json")

        outcome = manifest["outcome"]
        run.has_manifest = True
        run.conclusion = outcome["conclusion"]
        run.failure_class = outcome["failure_class"]
        run.failure_rule = outcome["failure_rule"]
        run.failed_interval = outcome["failed_interval"]
        run.divergences = list(manifest["checks"]["divergences"])
        run.run_attempt = manifest["run"]["run_attempt"]
        run.exclusion = exclusion_of(manifest)
        run.context = context_of(manifest)
        if run.exclusion is None:
            run.metrics, keys = metrics_of(manifest)
            for metric, key in keys.items():
                _check(metric_keys.setdefault(metric, key) == key, f"{cid}: order of {metric} differs")
        inputs[f"manifests/{cid}.json"] = sha256_of(path)

    return Campaign(
        id=campaign_id,
        kind=order["kind"],
        environment=order["environment"],
        repo_commit=order["repo_commit"],
        order_sha256=order_sha256,
        plan_sha256=plan_sha256,
        blocks=1 + max(e["block"] for e in order["runs"]),
        configurations=sorted(configurations.values(), key=lambda c: c.cell),
        runs=runs,
        metrics=sorted(metric_keys, key=lambda m: metric_keys[m]),
        inputs=dict(sorted(inputs.items())),
    )
