"""Manifest of one run (harness/manifest.schema.json).

build() maps the artifact (timing.json, environment.json) and the REST API data
into the manifest. Every null value gets a reason in null_reasons: the reason
that collect.py recorded in the artifact, or the reason why the orchestrator
could not fill the field. validate() checks the schema and that every null
value has a reason.
"""

import json
import platform
import re
from datetime import datetime

import jsonschema

from . import MANIFEST_SCHEMA, VERSION
from .classify import CLASSIFIER_VERSION
from .plan import FACTORS

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class ManifestError(Exception):
    pass


# JSON pointers


def escape(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def pick(doc, pointer):
    """(found, value) at a JSON pointer."""
    node = doc
    for raw in pointer.strip("/").split("/") if pointer.strip("/") else []:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


class Reasons(dict):
    def add(self, pointer, reason):
        self.setdefault(pointer, reason)


class Source:
    """A JSON file of the artifact and the null_reasons it records."""

    def __init__(self, name, doc, missing_reason):
        self.name = name
        self.doc = doc
        self.missing_reason = missing_reason
        self.reasons = (doc or {}).get("null_reasons") or {}

    def reason(self, pointer):
        parts = pointer.strip("/").split("/")
        for i in range(len(parts), 0, -1):
            candidate = "/" + "/".join(parts[:i])
            if candidate in self.reasons:
                return f"{self.name}: {self.reasons[candidate]}"
        return None

    def get(self, pointer, target, reasons):
        if self.doc is None:
            reasons.add(target, self.missing_reason)
            return None
        found, value = pick(self.doc, pointer)
        if not found:
            reasons.add(target, self.reason(pointer) or f"{self.name} has no {pointer}")
            return None
        if value is None:
            reasons.add(target, self.reason(pointer) or f"{self.name} records null at {pointer}")
        return value

    def obj(self, pointer, target, reasons, keys):
        """Object with keys target name -> source path below pointer, null if the source object is null."""
        if self.get(pointer, target, reasons) is None:
            return None
        return {t: self.get(f"{pointer}/{s}", f"{target}/{escape(t)}", reasons) for t, s in keys.items()}


# Helpers


def seconds_between(start, end):
    if not start or not end:
        return None
    return (parse_time(end) - parse_time(start)).total_seconds()


def parse_time(value):
    match = RFC3339.match(value)
    if not match:
        raise ValueError(f"not an RFC 3339 timestamp: {value!r}")
    text = value.replace("Z", "+00:00")
    fraction = match.group(1)
    if fraction and len(fraction) > 7:
        text = text.replace(fraction, fraction[:7], 1)
    return datetime.fromisoformat(text)


def interval_reasons(item, pointer, reasons):
    if item.get("start_monotonic_ns") is None:
        reasons.add(pointer, "no start mark in events.jsonl")
    elif item.get("end_monotonic_ns") is None:
        reasons.add(pointer, "no end mark in events.jsonl, the step stopped before the end mark")


INTERVAL_KEYS = ("name", "start_monotonic_ns", "end_monotonic_ns", "start_wall", "end_wall", "exit_code", "network")


def interval(item):
    return {key: item.get(key) for key in INTERVAL_KEYS[:-1]} | {"network": bool(item.get("network"))}


def version_matches(pinned, reported):
    core = pinned.lstrip("v").split("+")[0]
    return re.search(rf"(?<![0-9.]){re.escape(core)}(?![0-9])", reported) is not None


# Build


def build(*, order, run_plan, dispatch, run, job, artifact_meta, artifact, job_log_meta,
          job_log_reason, matched_by, classification, orchestrator, now):
    """Manifest of one run.

    order, run_plan  order.json and its entry for this run
    dispatch         {"dispatched_at", "response"} of the orchestrator, response may be None
    run, job         workflow run and its job (REST API), job may be None
    artifact_meta    {"name", "id", "size_in_bytes", "digest", "sha256", "path", "expires_at"} or None
    artifact         artifact.Artifact or None
    job_log_meta     {"path", "sha256", "bytes"} or None, then job_log_reason says why
    """
    reasons = Reasons()
    no_artifact = "the run uploaded no artifact" if artifact is None else None
    env = Source("environment.json", artifact.json("environment.json") if artifact else None,
                 no_artifact or "environment.json missing in the artifact")
    tim = Source("timing.json", artifact.json("timing.json") if artifact else None,
                 no_artifact or "timing.json missing in the artifact")
    factors = run_plan["factors"]

    manifest = {
        "schema_version": 1,
        "campaign": {
            "id": order["campaign"],
            "kind": order["kind"],
            "plan_sha256": order["plan_sha256"],
            "order_sha256": order["order_sha256"],
            "seed": order["seed"],
            "repo_commit": order["repo_commit"],
        },
        "run": {
            "correlation_id": run_plan["correlation_id"],
            "workflow": f".github/workflows/{order['workflow']}",
            "ref": order["ref"],
            "dispatched_at": dispatch.get("dispatched_at"),
            "dispatch_response": dispatch.get("response"),
            "run_id": run["id"],
            "run_attempt": run.get("run_attempt"),
            "head_sha": run.get("head_sha"),
            "display_title": run.get("display_title"),
            "html_url": run.get("html_url"),
            "matched_by": matched_by,
            "artifact": artifact_meta,
            "job_log": job_log_meta,
        },
        "design": {
            "configuration": run_plan["configuration"],
            "cell": run_plan["cell"],
            "factors": {name: factors[name] for name in FACTORS},
            "slsa_configuration": None,
            "block": run_plan["block"],
            "position_in_block": run_plan["position_in_block"],
            "position_in_campaign": run_plan["position"],
            "first_runs": run_plan["first_runs"],
            "ground_truth_variant": None,
        },
    }
    if dispatch.get("dispatched_at") is None:
        reasons.add("/run/dispatched_at", dispatch.get("reason") or "run found by its run name, dispatch time unknown")
    if dispatch.get("response") is None:
        reasons.add("/run/dispatch_response", dispatch.get("reason") or "no dispatch response recorded")
    if artifact_meta is None:
        reasons.add("/run/artifact", "the run uploaded no artifact measure-<correlation ID>")
    if job_log_meta is None:
        reasons.add("/run/job_log", job_log_reason)
    reasons.add("/design/slsa_configuration", "mapping of the configurations to SLSA levels not fixed yet (RQ4)")
    reasons.add("/design/ground_truth_variant", "unchanged application, no seeded violation (RQ3)")

    # Environment
    manifest["environment"] = {
        "type": order["environment"],
        "runner": {
            "name": env.get("/runner/name", "/environment/runner/name", reasons),
            "version": env.get("/runner/version", "/environment/runner/version", reasons),
            "os": env.get("/runner/os", "/environment/runner/os", reasons),
            "arch": env.get("/runner/arch", "/environment/runner/arch", reasons),
            "environment": env.get("/runner/environment", "/environment/runner/environment", reasons),
            "group": (job or {}).get("runner_group_name"),
            "labels": (job or {}).get("labels"),
            "image_os": env.get("/runner/image_os", "/environment/runner/image_os", reasons),
            "image_version": env.get("/runner/image_version", "/environment/runner/image_version", reasons),
        },
        "cpu": env.obj("/cpu", "/environment/cpu", reasons,
                       {k: k for k in ("model", "logical_cores", "affinity_cores", "flags_sha256")}),
        "memory_bytes": env.get("/memory_bytes", "/environment/memory_bytes", reasons),
        "kernel": env.get("/kernel", "/environment/kernel", reasons),
        "kernel_version": env.get("/kernel_version", "/environment/kernel_version", reasons),
        "os": env.get("/os", "/environment/os", reasons),
        "docker": env.obj("/docker", "/environment/docker", reasons,
                          {k: k for k in ("server_version", "storage_driver", "cgroup_driver", "default_runtime")}),
        "vm": None,
    }
    for key in ("group", "labels"):
        if manifest["environment"]["runner"][key] is None:
            reasons.add(f"/environment/runner/{key}", "the Jobs API names no runner for the job")
    reasons.add("/environment/vm", "hosted runner, no job VM" if order["environment"] == "hosted"
                else "life cycle of the job VM not recorded yet")

    manifest["clock"] = {
        "stage_clock": "CLOCK_MONOTONIC",
        "stage_clock_resolution_ns": tim.get("/clock/stage_clock_resolution_ns", "/clock/stage_clock_resolution_ns", reasons),
        "wall_clock": "CLOCK_REALTIME",
        "clocksource": env.get("/clock/clocksource", "/clock/clocksource", reasons),
        "api_resolution_s": 1,
    }

    # Versions
    tools = env.get("/tools", "/versions/tools", reasons)
    tool_list = None
    if tools is not None:
        tool_list = []
        for index, (name, spec) in enumerate(tools.items()):
            target = f"/versions/tools/{index}"
            entry = {
                "name": name,
                "pinned_version": spec.get("pinned_version"),
                "sha256": spec.get("sha256"),
                "reported_version": spec.get("reported_version"),
            }
            if entry["pinned_version"] is None and entry["sha256"] is None:
                reasons.add(target, "tool of the runner image, not pinned in tools/versions.json")
            elif entry["sha256"] is None:
                reasons.add(f"{target}/sha256", "no SHA-256 pinned for this tool")
            if entry["reported_version"] is None:
                reasons.add(f"{target}/reported_version",
                            env.reason(f"/tools/{name}/reported_version") or "no version reported")
            tool_list.append(entry)
    actions = env.get("/actions", "/versions/actions", reasons)
    base = env.get("/base_image", "/versions/base_image", reasons)
    base_image = None
    if base is not None:
        name, _, digest = base.partition("@")
        base_image = {"name": name, "digest": digest}
    image = env.get("/image", "/versions/image", reasons)
    manifest["versions"] = {
        "versions_json_sha256": env.get("/versions_json_sha256", "/versions/versions_json_sha256", reasons),
        "tools": tool_list,
        "actions": None if actions is None else [
            {"name": k, "version": v["version"], "sha": v["sha"]} for k, v in actions.items()
        ],
        "base_image": base_image,
        "image": None if image is None else {
            "name": image["name"],
            "tag": image["tag"],
            "digest": image["digest"],
            "label_correlation_id": image.get("label_correlation_id"),
        },
        "caches": {
            "maven_repository": env.obj("/maven_repository", "/versions/caches/maven_repository", reasons, {
                "cache_name": "cache_name",
                "expected_sha256": "expected_sha256",
                "after_restore_sha256": "after_restore/content_sha256",
                "after_run_sha256": "after_run/content_sha256",
                "files": "after_restore/files",
                "bytes": "after_restore/bytes",
            }),
            "grype_db": None,
            "docker_build_cache": "none",
        },
    }
    if image is not None and image.get("label_correlation_id") is None:
        reasons.add("/versions/image/label_correlation_id", "docker image inspect returned no label value")
    reasons.add("/versions/caches/grype_db", "factor A not implemented yet, no Grype database")

    # Timing
    preparation = tim.get("/preparation", "/timing/preparation", reasons)
    stages = tim.get("/stages", "/timing/stages", reasons)
    manifest["timing"] = {
        "preparation": None if preparation is None else [interval(i) for i in preparation],
        "stages": None if stages is None else [
            {
                "name": s["name"],
                "enabled": s["enabled"],
                "interval": interval(s["interval"]) if s.get("interval") else None,
                "subtimes": [interval(i) for i in s.get("subtimes") or []],
            }
            for s in stages
        ],
        "totals": tim.obj("/totals", "/timing/totals", reasons, {"stages_sum_ns": "stages_sum_ns", "span_ns": "span_ns"}),
        "run_api": {
            "created_at": run.get("created_at"),
            "run_started_at": run.get("run_started_at"),
            "updated_at": run.get("updated_at"),
        },
        "job_api": None if job is None else {
            "id": job["id"],
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "queue_s": seconds_between(job.get("created_at"), job.get("started_at")),
            "duration_s": seconds_between(job.get("started_at"), job.get("completed_at")),
        },
        "steps_api": [
            {k: step.get(k) for k in ("number", "name", "status", "conclusion", "started_at", "completed_at")}
            for step in (job or {}).get("steps") or []
        ],
    }
    if job is None:
        reasons.add("/timing/job_api", "the Jobs API lists no job for the run")
    elif None in (job.get("started_at"), job.get("completed_at")):
        reasons.add("/timing/job_api", "the job has no start or no end time")
    for key, value in manifest["timing"]["run_api"].items():
        if value is None:
            reasons.add(f"/timing/run_api/{key}", "the REST API returned null")
    for index, step in enumerate(manifest["timing"]["steps_api"]):
        if None in step.values():
            reasons.add(f"/timing/steps_api/{index}", "step did not run or did not finish")
    for index, item in enumerate(manifest["timing"]["preparation"] or []):
        interval_reasons(item, f"/timing/preparation/{index}", reasons)
    for index, stage in enumerate(manifest["timing"]["stages"] or []):
        pointer = f"/timing/stages/{index}"
        if stage["interval"] is None:
            reasons.add(f"{pointer}/interval", "stage disabled in this configuration" if not stage["enabled"]
                        else "stage enabled, but the run stopped before it")
        else:
            interval_reasons(stage["interval"], f"{pointer}/interval", reasons)
        for sub_index, sub in enumerate(stage["subtimes"]):
            interval_reasons(sub, f"{pointer}/subtimes/{sub_index}", reasons)

    # Checks
    manifest["checks"] = {
        "maven": env.obj("/maven", "/checks/maven", reasons, {"build_maven": "build-maven", "test": "test"}),
        "maven_repository_unchanged": env.get("/maven_repository/unchanged_by_run", "/checks/maven_repository_unchanged", reasons),
        "buildkit": env.obj("/buildkit", "/checks/buildkit", reasons,
                            {k: k for k in ("auth_vertices", "from_lines", "warnings")}),
        "push": env.get("/push", "/checks/push", reasons),
        "tests": env.obj("/tests", "/checks/tests", reasons,
                         {k: k for k in ("report_files", "classes_run", "tests", "failures", "errors", "skipped")}),
        "image_label_matches": env.get("/image/label_matches", "/checks/image_label_matches", reasons),
        "oidc_variables_in_job": env.get("/oidc_variables_in_job", "/checks/oidc_variables_in_job", reasons),
        "divergences": divergences(order, run_plan, run, job, env.doc, tim.doc),
    }

    # Outcome
    manifest["outcome"] = {
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "failed_step": classification.failed_step,
        "failed_interval": classification.failed_interval,
        "failed_stage": classification.failed_stage,
        "failure_class": classification.failure_class,
        "failure_rule": classification.rule,
        "failure_evidence": classification.evidence,
        "gate": {"result": "not_run", "expected": None, "checks": []},
    }
    for key in ("failed_step", "failed_interval", "failed_stage", "failure_class", "failure_evidence"):
        if manifest["outcome"][key] is None:
            reasons.add(f"/outcome/{key}", "the run succeeded (R01)" if classification.rule == "R01"
                        else f"not determined by rule {classification.rule}")
    if factors["policy"]:
        raise ManifestError("factor D (policy) is not implemented in the manifest yet")
    reasons.add("/outcome/gate/expected", "factor D off, no gate decision expected")

    manifest["orchestrator"] = {
        "version": VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "commit": orchestrator.get("commit"),
        "dirty": orchestrator.get("dirty"),
        "python": platform.python_version(),
        "written_at": now,
    }
    if orchestrator.get("commit") is None or orchestrator.get("dirty") is None:
        reasons.add("/orchestrator", orchestrator.get("reason") or "git information not available")

    manifest["null_reasons"] = dict(sorted(reasons.items()))
    return manifest


def divergences(order, run_plan, run, job, env, timing):
    found = []
    if run.get("head_sha") != order["repo_commit"]:
        found.append(f"run head_sha {run.get('head_sha')} differs from the campaign commit {order['repo_commit']}")
    if (run.get("run_attempt") or 1) > 1:
        found.append(f"run attempt {run.get('run_attempt')}, the run was re-run")
    if env is None:
        return found
    recorded = env.get("run") or {}
    if recorded.get("sha") and recorded.get("sha") != run.get("head_sha"):
        found.append(f"environment.json records commit {recorded.get('sha')}, the run API {run.get('head_sha')}")
    if recorded.get("environment") != order["environment"]:
        found.append(f"environment.json records runner={recorded.get('environment')}, planned {order['environment']}")
    runner = (env.get("runner") or {}).get("name")
    if job and job.get("runner_name") and runner and runner != job.get("runner_name"):
        found.append(f"environment.json records runner {runner!r}, the Jobs API {job.get('runner_name')!r}")
    for name, spec in (env.get("tools") or {}).items():
        pinned, reported = spec.get("pinned_version"), spec.get("reported_version")
        if pinned is None:
            continue
        if reported is None:
            found.append(f"tool {name}: no reported version, pinned {pinned}")
        elif not version_matches(pinned, reported):
            found.append(f"tool {name}: reported {reported!r}, pinned {pinned}")
    maven_repository = env.get("maven_repository") or {}
    after_restore = (maven_repository.get("after_restore") or {}).get("content_sha256")
    if maven_repository and after_restore != maven_repository.get("expected_sha256"):
        found.append(f"Maven repository after the restore {after_restore}, pinned {maven_repository.get('expected_sha256')}")
    if maven_repository and maven_repository.get("unchanged_by_run") is False:
        found.append("the run changed the Maven repository")
    for name, check in (env.get("maven") or {}).items():
        if check and check.get("download_lines"):
            found.append(f"{name}.log: {check['download_lines']} download lines in an offline build")
    image = env.get("image") or {}
    if image and image.get("label_matches") is False:
        found.append(f"image label {image.get('label_correlation_id')!r} differs from the correlation ID")
    planned = run_plan["factors"]
    recorded_factors = (timing or {}).get("factors")
    if recorded_factors is not None and recorded_factors != {k: planned[k] for k in FACTORS}:
        found.append(f"timing.json records factors {recorded_factors}, planned {planned}")
    return found


# Validation


def _date_time(value):
    if not isinstance(value, str):
        return True
    parse_time(value)
    return True


def validator():
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    checker = jsonschema.FormatChecker(formats=())
    checker.checks("date-time", raises=ValueError)(_date_time)
    return jsonschema.Draft202012Validator(schema, format_checker=checker)


def null_pointers(node, pointer=""):
    if node is None:
        yield pointer
    elif isinstance(node, dict):
        for key, value in node.items():
            if pointer == "" and key == "null_reasons":
                continue
            yield from null_pointers(value, f"{pointer}/{escape(key)}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from null_pointers(value, f"{pointer}/{index}")


def covered(pointer, reasons):
    parts = pointer.split("/")
    return any("/".join(parts[:i]) in reasons for i in range(2, len(parts) + 1))


def validate(manifest, schema_validator=None):
    """List of problems, empty if the manifest is valid."""
    problems = []
    for error in (schema_validator or validator()).iter_errors(manifest):
        problems.append(f"schema: {error.message} at {error.json_path}")
    reasons = manifest.get("null_reasons") or {}
    nulls = list(null_pointers(manifest))
    for pointer in nulls:
        if not covered(pointer, reasons):
            problems.append(f"null without reason at {pointer}")
    for pointer in reasons:
        found, value = pick(manifest, pointer)
        if not found or not any(p == pointer or p.startswith(pointer + "/") for p in nulls):
            problems.append(f"null_reasons names {pointer}, which holds no null value")
    return problems
