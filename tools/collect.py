#!/usr/bin/env python3
"""Collects the timing, the environment and the checks of one measurement run.

Runs as the last step of the measurement job (if: always()). Missing data never
fails the step: a value that cannot be read is null and its reason is listed in
null_reasons. Reads $MCS_OUT/events.jsonl (tools/timing.py) and writes to
$MCS_OUT:

  timing.json       preparation intervals, the eight stages and their sub-times
  environment.json  runner, CPU, kernel, clock, tool versions, image, offline
                    checks of build and test, BuildKit and push details

A summary table goes to $GITHUB_STEP_SUMMARY. Uses only the standard library.
"""

import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import m2hash  # noqa: E402

# Stage order and the input that enables a factor stage (None: baseline).
STAGES = [
    ("checkout", None),
    ("build", None),
    ("test", None),
    ("push", None),
    ("sbom", "MCS_SBOM"),
    ("sign", "MCS_SIGN"),
    ("provenance", "MCS_PROVENANCE"),
    ("gate", "MCS_POLICY"),
]

OUT = os.environ.get("MCS_OUT", "")
null_reasons = {}


def note(pointer, reason):
    null_reasons[pointer] = reason
    return None


def iso(wall_ns):
    if wall_ns is None:
        return None
    seconds, nanos = divmod(wall_ns, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{nanos:09d}Z"


def run(args, timeout=60, cwd=None):
    """Output (stdout and stderr) of a command, None if it fails."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout + done.stderr).strip()


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def load_json(path):
    text = read(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# Timing


def load_events():
    events = []
    text = read(os.path.join(OUT, "events.jsonl")) or ""
    for line in text.splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def interval(name, start, end):
    return {
        "name": name,
        "start_monotonic_ns": start["monotonic_ns"] if start else None,
        "end_monotonic_ns": end["monotonic_ns"] if end else None,
        "start_wall": iso(start["wall_ns"]) if start else None,
        "end_wall": iso(end["wall_ns"]) if end else None,
        "exit_code": end["exit_code"] if end else None,
        "network": bool(start and start.get("network")),
    }


def find(events, kind, name, event):
    hits = [e for e in events if e["kind"] == kind and e["name"] == name and e["event"] == event]
    if not hits:
        return None
    return hits[0] if event == "start" else hits[-1]


def duration(item):
    if item and item["start_monotonic_ns"] is not None and item["end_monotonic_ns"] is not None:
        return item["end_monotonic_ns"] - item["start_monotonic_ns"]
    return None


def build_timing(events):
    preparation = []
    seen = []
    for e in events:
        if e["kind"] == "prep" and e["name"] not in seen:
            seen.append(e["name"])
    for name in seen:
        preparation.append(
            interval(name, find(events, "prep", name, "start"), find(events, "prep", name, "end"))
        )

    stages = []
    for name, flag in STAGES:
        enabled = flag is None or os.environ.get(flag) == "true"
        start = find(events, "stage", name, "start")
        end = find(events, "stage", name, "end")
        subs = []
        sub_names = []
        for e in events:
            if e["kind"] == "sub" and e.get("stage") == name and e["name"] not in sub_names:
                sub_names.append(e["name"])
        for sub in sub_names:
            subs.append(interval(sub, find(events, "sub", sub, "start"), find(events, "sub", sub, "end")))
        stages.append(
            {
                "name": name,
                "enabled": enabled,
                "interval": interval(name, start, end) if (start or end) else None,
                "subtimes": subs,
            }
        )

    enabled = [s for s in stages if s["enabled"]]
    durations = [duration(s["interval"]) for s in enabled]
    stages_sum = sum(durations) if durations and None not in durations else None
    if stages_sum is None:
        note("/totals/stages_sum_ns", "an enabled stage has no complete interval")
    first = enabled[0]["interval"] if enabled else None
    last = enabled[-1]["interval"] if enabled else None
    span = None
    if first and last and first["start_monotonic_ns"] is not None and last["end_monotonic_ns"] is not None:
        span = last["end_monotonic_ns"] - first["start_monotonic_ns"]
    else:
        note("/totals/span_ns", "first or last enabled stage has no complete interval")

    return {
        "schema": "mcs-pipeline timing 1",
        "clock": {
            "stage_clock": "CLOCK_MONOTONIC",
            "stage_clock_resolution_ns": clock_resolution(),
            "wall_clock": "CLOCK_REALTIME",
        },
        "factors": {
            "sbom": os.environ.get("MCS_SBOM") == "true",
            "sign": os.environ.get("MCS_SIGN") == "true",
            "provenance": os.environ.get("MCS_PROVENANCE") == "true",
            "policy": os.environ.get("MCS_POLICY") == "true",
        },
        "preparation": preparation,
        "stages": stages,
        "totals": {"stages_sum_ns": stages_sum, "span_ns": span},
    }


def clock_resolution():
    try:
        return int(round(time.clock_getres(time.CLOCK_MONOTONIC) * 1e9))
    except (AttributeError, OSError):
        return None


# Environment


def runner_version():
    """Version of the Actions runner, from the Runner.Worker process above this one."""
    pid = os.getpid()
    for _ in range(12):
        stat = read(f"/proc/{pid}/stat")
        if not stat:
            break
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        if ppid <= 1:
            break
        try:
            exe = os.readlink(f"/proc/{ppid}/exe")
        except OSError:
            exe = ""
        if os.path.basename(exe) == "Runner.Worker":
            listener = os.path.join(os.path.dirname(exe), "Runner.Listener")
            version = run([listener, "--version"], timeout=30)
            if version:
                return version.splitlines()[-1].strip()
            return note("/runner/version", f"{listener} --version failed")
        pid = ppid
    return note("/runner/version", "no Runner.Worker process found above the collect step")


def cpu_info():
    text = read("/proc/cpuinfo")
    if text is None:
        note("/cpu/model", "/proc/cpuinfo not readable")
        return {"model": None, "logical_cores": os.cpu_count(), "flags_sha256": None}
    model = None
    flags = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "model name" and model is None:
            model = value.strip()
        if key == "flags" and flags is None:
            flags = " ".join(sorted(set(value.split())))
    return {
        "model": model,
        "logical_cores": os.cpu_count(),
        "affinity_cores": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "flags_sha256": hashlib.sha256(flags.encode()).hexdigest() if flags else None,
    }


def memory_bytes():
    text = read("/proc/meminfo") or ""
    match = re.search(r"^MemTotal:\s+(\d+) kB", text, re.M)
    if not match:
        return note("/memory_bytes", "MemTotal not found in /proc/meminfo")
    return int(match.group(1)) * 1024


def os_release():
    text = read("/etc/os-release") or ""
    match = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', text, re.M)
    return match.group(1) if match else None


def first_line(text):
    return text.splitlines()[0].strip() if text else None


def tool_versions(pins):
    tools = {}
    bin_dir = os.environ.get("MCS_BIN", "")

    def entry(name, reported, pinned=None, sha=None):
        path = os.path.join(bin_dir, name) if bin_dir else name
        tools[name] = {
            "pinned_version": pinned,
            "sha256": sha,
            "path": path if os.path.exists(path) else None,
            "reported_version": reported,
        }
        if reported is None:
            note(f"/tools/{name}/reported_version", "version command failed or tool missing")

    for name, spec in (pins.get("tools") or {}).items():
        path = os.path.join(bin_dir, name)
        if name == "cosign":
            out = run([path, "version", "--json"])
            reported = (json.loads(out).get("gitVersion") if out and out.startswith("{") else first_line(out))
        elif name in ("syft", "grype"):
            out = run([path, "version", "-o", "json"])
            reported = json.loads(out).get("version") if out and out.startswith("{") else first_line(out)
        elif name == "opa":
            reported = first_line(run([path, "version"]))
        elif name == "conftest":
            reported = first_line(run([path, "--version"]))
        else:
            reported = first_line(run([path, "--version"]))
        entry(name, reported, spec.get("version"), spec.get("sha256"))

    java = os.path.join(os.environ.get("JAVA_HOME", ""), "bin", "java")
    entry("java", first_line(run([java, "-version"])), pins.get("jdk", {}).get("version"), pins.get("jdk", {}).get("sha256"))
    entry("maven", first_line(run(["./mvnw", "--version"], cwd=os.path.join(REPO, "app"))),
          pins.get("maven", {}).get("version"), pins.get("maven", {}).get("sha256"))
    entry("python", sys.version.split()[0])
    entry("git", first_line(run(["git", "--version"])))
    entry("docker", run(["docker", "version", "--format", "{{.Client.Version}} {{.Server.Version}}"]))
    entry("buildx", first_line(run(["docker", "buildx", "version"])))
    return tools


def docker_info():
    out = run(["docker", "info", "--format", "{{json .}}"])
    if not out:
        return note("/docker", "docker info failed")
    info = json.loads(out)
    return {
        "server_version": info.get("ServerVersion"),
        "storage_driver": info.get("Driver"),
        "driver_status": info.get("DriverStatus"),
        "cgroup_driver": info.get("CgroupDriver"),
        "default_runtime": info.get("DefaultRuntime"),
    }


# Checks of the run


def maven_logs():
    result = {}
    for name in ("build-maven", "test"):
        text = read(os.path.join(OUT, f"{name}.log"))
        if text is None:
            result[name] = note(f"/maven/{name}", f"{name}.log missing")
            continue
        downloads = re.findall(r"^\[INFO\] Download(?:ing|ed) from ", text, re.M)
        result[name] = {
            "download_lines": len(downloads),
            "build_success": "BUILD SUCCESS" in text,
            "offline_skip_lines": len(re.findall(r"requires online mode for execution but Maven is currently offline", text)),
        }
    return result


def test_results():
    files = sorted(glob.glob(os.path.join(REPO, "app", "target", "surefire-reports", "TEST-*.xml")))
    if not files:
        return note("/tests", "no surefire reports")
    totals = {"classes": len(files), "tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for key in ("tests", "failures", "errors", "skipped"):
            totals[key] += int(root.get(key, "0"))
    return totals


def maven_repository(pins):
    before = load_json(os.path.join(OUT, "maven-repository.json"))
    repo = os.path.expanduser("~/.m2/repository")
    after = m2hash.repository_hash(repo) if os.path.isdir(repo) else None
    if after is None:
        note("/maven_repository/after_run", "~/.m2/repository missing")
    expected = (pins.get("maven_repository") or {}).get("content_sha256")
    return {
        "cache_name": (pins.get("maven_repository") or {}).get("cache_name"),
        "expected_sha256": expected,
        "after_restore": before,
        "after_run": after,
        "unchanged_by_run": bool(before and after and before["content_sha256"] == after["content_sha256"]),
        "settings_xml_present": os.path.exists(os.path.expanduser("~/.m2/settings.xml")),
    }


def buildkit_log():
    """Lines of the BuildKit plain log that show how FROM was resolved."""
    text = read(os.path.join(OUT, "build-image.log"))
    if text is None:
        return note("/buildkit", "build-image.log missing")
    names = {}
    lines = []
    for line in text.splitlines():
        match = re.match(r"^#(\d+) (.*)$", line)
        if not match:
            continue
        vertex, rest = match.groups()
        if vertex not in names:
            names[vertex] = rest
        name = names[vertex]
        if "load metadata for" in name or name.startswith("[auth]") or "FROM" in name:
            lines.append(line)
    auth = sorted({names[v] for v in names if names[v].startswith("[auth]")})
    return {"auth_vertices": auth, "from_lines": lines}


def push_log():
    text = read(os.path.join(OUT, "push.log"))
    if text is None:
        return note("/push", "push.log missing")
    return {
        "pushed_layers": len(re.findall(r": Pushed$", text, re.M)),
        "existing_layers": len(re.findall(r": Layer already exists$", text, re.M)),
        "mounted_layers": len(re.findall(r": Mounted from ", text, re.M)),
    }


def image_details():
    image = load_json(os.path.join(OUT, "image.json"))
    if image is None:
        return note("/image", "image.json missing, the run stopped before push")
    tag_ref = f"{image['name']}:{image['tag']}"
    label = run(["docker", "image", "inspect", "--format",
                 '{{index .Config.Labels "io.github.vermout94.mcs-pipeline.correlation-id"}}', tag_ref])
    image["label_correlation_id"] = label
    image["label_matches"] = label == os.environ.get("CORRELATION_ID")
    return image


def oidc_variables():
    # Presence only, never the values.
    return {
        name: name in os.environ
        for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    }


def safe(pointer, function, *args):
    """Value of function(*args), null with a reason if it raises."""
    try:
        return function(*args)
    except Exception as error:  # noqa: BLE001, collect must not fail the job
        return note(pointer, f"{type(error).__name__}: {error}")


def base_image(pins):
    mirror = pins["images"]["base"]["mirror"]
    return f"{mirror['name']}@{mirror['digest']}"


def build_environment(pins):
    env = os.environ
    versions_path = os.path.join(REPO, "tools", "versions.json")
    with open(versions_path, "rb") as f:
        versions_sha256 = hashlib.sha256(f.read()).hexdigest()
    return {
        "schema": "mcs-pipeline environment 1",
        "run": {
            "correlation_id": env.get("CORRELATION_ID"),
            "repository": env.get("GITHUB_REPOSITORY"),
            "run_id": env.get("GITHUB_RUN_ID"),
            "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
            "workflow_ref": env.get("GITHUB_WORKFLOW_REF"),
            "workflow_sha": env.get("GITHUB_WORKFLOW_SHA"),
            "sha": env.get("GITHUB_SHA"),
            "ref": env.get("GITHUB_REF"),
            "environment": env.get("MCS_RUNNER"),
        },
        "runner": {
            "name": env.get("RUNNER_NAME"),
            "version": safe("/runner/version", runner_version),
            "os": env.get("RUNNER_OS"),
            "arch": env.get("RUNNER_ARCH"),
            "environment": env.get("RUNNER_ENVIRONMENT"),
            "image_os": env.get("ImageOS"),
            "image_version": env.get("ImageVersion"),
        },
        "cpu": safe("/cpu", cpu_info),
        "memory_bytes": safe("/memory_bytes", memory_bytes),
        "kernel": platform.release(),
        "kernel_version": platform.version(),
        "os": safe("/os", os_release),
        "clock": {
            "monotonic_resolution_ns": clock_resolution(),
            "clocksource": first_line(read("/sys/devices/system/clocksource/clocksource0/current_clocksource")),
        },
        "versions_json_sha256": versions_sha256,
        "actions": {k: v for k, v in (pins.get("actions") or {}).items() if k != "note"},
        "tools": safe("/tools", tool_versions, pins),
        "docker": safe("/docker", docker_info),
        "oidc_variables_in_job": oidc_variables(),
        "base_image": safe("/base_image", base_image, pins),
        "image": safe("/image", image_details),
        "push": safe("/push", push_log),
        "buildkit": safe("/buildkit", buildkit_log),
        "maven": safe("/maven", maven_logs),
        "maven_repository": safe("/maven_repository", maven_repository, pins),
        "tests": safe("/tests", test_results),
    }


def seconds(ns):
    return f"{ns / 1e9:.3f}" if ns is not None else "n/a"


def summary(timing, environment):
    lines = ["## Measurement run", ""]
    lines.append(f"Correlation ID `{environment['run']['correlation_id']}`, runner `{environment['runner']['name']}`, "
                 f"image `{environment['runner']['image_os']}` `{environment['runner']['image_version']}`.")
    lines += ["", "| Stage | Enabled | Duration (s) | Sub-times (s) |", "|---|---|---|---|"]
    for stage in timing["stages"]:
        subs = ", ".join(f"{s['name']} {seconds(duration(s))}" for s in stage["subtimes"])
        lines.append(f"| {stage['name']} | {str(stage['enabled']).lower()} | {seconds(duration(stage['interval']))} | {subs} |")
    lines.append(f"| sum of enabled stages | | {seconds(timing['totals']['stages_sum_ns'])} | |")
    lines += ["", "| Preparation | Duration (s) |", "|---|---|"]
    for item in timing["preparation"]:
        lines.append(f"| {item['name']} | {seconds(duration(item))} |")
    image = environment.get("image")
    if image:
        lines += ["", f"Image `{image['name']}@{image['digest']}`"]
    lines.append("")
    return "\n".join(lines)


def main():
    if not OUT or not os.path.isdir(OUT):
        sys.exit("collect.py: MCS_OUT is not a directory")
    pins = load_json(os.path.join(REPO, "tools", "versions.json")) or {}
    timing = build_timing(load_events())
    environment = build_environment(pins)
    timing["null_reasons"] = {k: v for k, v in null_reasons.items() if k.startswith("/totals")}
    environment["null_reasons"] = {k: v for k, v in null_reasons.items() if not k.startswith("/totals")}
    with open(os.path.join(OUT, "timing.json"), "w", encoding="utf-8") as f:
        json.dump(timing, f, indent=2)
    with open(os.path.join(OUT, "environment.json"), "w", encoding="utf-8") as f:
        json.dump(environment, f, indent=2)
    text = summary(timing, environment)
    print(text)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
