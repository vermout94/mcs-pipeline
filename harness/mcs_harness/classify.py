"""Failure classes of a run (RQ2).

The rules are tried in order and the first one that matches sets the class.
Inputs are the conclusion of the workflow run, the job and its steps (Jobs API),
timing.json and the logs from the artifact, the test counts from
environment.json and, for runs that did not succeed, the job log.

  R01  -                 conclusion success
  R02  cancelled         conclusion cancelled
  R03  timeout           conclusion timed_out (run or job)
  R04  infrastructure    the job did not start: no job, no runner, startup_failure
  R05  infrastructure    the job log reports a lost runner, or the runner was not
                         fresh (~/.m2/repository or ~/.sigstore existed before use)
  R06  tool_divergence   a check of the workflow found a difference to the pins:
                         SHA-256 of a download, content hash of the Maven
                         repository, image digest, mirror digest
  R07  gate              the stage gate ended with exit code 1 (provisional until
                         factor D exists, Conftest exit codes to be checked)
  R08  test              the stage test failed and the Surefire reports count
                         failures or errors
  R09  network           the log of the failed interval or the job log of the
                         failed step contains a network error
  R10  tool              an interval of a stage ended with an exit code other
                         than 0, or a stage started and did not end
  R11  infrastructure    a step outside the stage intervals failed: preparation,
                         collect, upload, a uses: step, the input check
  R12  unknown           no other rule matched

Changes of the rules raise CLASSIFIER_VERSION. Manifests keep the class of the
version that wrote them. A later classification is a documented filter of the
analysis, never an edit of a manifest.
"""

import re
from dataclasses import dataclass, field

CLASSIFIER_VERSION = 1

FAILED = "failure"

RUNNER_PATTERNS = [
    r"The runner has received a shutdown signal",
    r"lost communication with the server",
    r"The hosted runner encountered an error",
    r"~/\.m2/repository exists before the restore",
    r"~/\.sigstore exists before the stage",
]

DIVERGENCE_PATTERNS = [
    r"install\.sh: SHA-256 mismatch",
    r"Content hash of the restored Maven repository .* differs",
    r"Image digest missing or inconsistent",
    r"mirror digest in tools/versions\.json differs",
]

NETWORK_PATTERNS = [
    r"could not resolve host",
    r"temporary failure in name resolution",
    r"no such host",
    r"connection (reset|refused|timed out)",
    r"i/o timeout",
    r"tls handshake timeout",
    r"context deadline exceeded",
    r"dial tcp",
    r"unexpected eof",
    r"net/http: request canceled",
    r"\b(502 bad gateway|503 service unavailable|504 gateway time-?out)\b",
    r"\b429 too many requests\b",
    r"curl: \((5|6|7|16|18|28|35|52|55|56)\)",
    r"curl: \(22\).*\b5[0-9][0-9]\b",
    r"\b(econnreset|etimedout|econnrefused|enotfound|eai_again)\b",
]

RULES = {
    "R01": (None, "conclusion success"),
    "R02": ("cancelled", "conclusion cancelled"),
    "R03": ("timeout", "conclusion timed_out"),
    "R04": ("infrastructure", "the job did not start"),
    "R05": ("infrastructure", "lost runner or runner not fresh"),
    "R06": ("tool_divergence", "a check of the workflow found a difference to the pins"),
    "R07": ("gate", "stage gate ended with exit code 1"),
    "R08": ("test", "stage test failed with failing tests"),
    "R09": ("network", "network error in the log of the failed interval or step"),
    "R10": ("tool", "an interval of a stage failed"),
    "R11": ("infrastructure", "a step outside the stage intervals failed"),
    "R12": ("unknown", "no other rule matched"),
}

STAGES = ("checkout", "build", "test", "push", "sbom", "sign", "provenance", "gate")


@dataclass
class Evidence:
    conclusion: str | None
    job: dict | None
    timing: dict | None = None
    tests: dict | None = None
    logs: dict = field(default_factory=dict)
    job_log: str | None = None


@dataclass
class Result:
    failure_class: str | None
    rule: str
    evidence: str | None
    failed_step: str | None
    failed_interval: str | None
    failed_stage: str | None


def failed_step(job):
    for step in (job or {}).get("steps") or []:
        if step.get("conclusion") == FAILED:
            return step
    return None


def intervals(timing):
    """All intervals of timing.json as (kind, stage, interval), ordered by start."""
    found = []
    for item in (timing or {}).get("preparation") or []:
        found.append(("prep", None, item))
    for stage in (timing or {}).get("stages") or []:
        if stage.get("interval"):
            found.append(("stage", stage["name"], stage["interval"]))
        for sub in stage.get("subtimes") or []:
            found.append(("sub", stage["name"], sub))
    found.sort(key=lambda x: (x[2].get("start_monotonic_ns") is None, x[2].get("start_monotonic_ns") or 0,
                              0 if x[0] == "sub" else 1))
    return found


def failed_interval(timing):
    """First interval with an exit code other than 0 or with a start and no end.

    A failing sub-time also closes its stage with its exit code. The sub-time
    names the failing command, so stage intervals of stages with sub-times count
    only if none of their sub-times failed.
    """
    with_subtimes = {s["name"] for s in (timing or {}).get("stages") or [] if s.get("subtimes")}

    def failed(item):
        code = item.get("exit_code")
        unfinished = item.get("start_monotonic_ns") is not None and item.get("end_monotonic_ns") is None
        return (code is not None and code != 0) or unfinished

    found = intervals(timing)
    for kind, stage, item in found:
        if not (kind == "stage" and stage in with_subtimes) and failed(item):
            return kind, stage, item
    for kind, stage, item in found:
        if failed(item):
            return kind, stage, item
    return None


def stage_of_step(name):
    match = re.match(r"^Stage (\w+)", name or "")
    if match and match.group(1) in STAGES:
        return match.group(1)
    return None


def log_name(interval_name):
    """Artifact log of an interval: build.maven -> build-maven.log."""
    return interval_name.replace(".", "-") + ".log"


def step_log(job_log, step):
    """Lines of the job log written while the step ran (timestamps at 1 s)."""
    if not job_log or not step or not step.get("started_at") or not step.get("completed_at"):
        return ""
    start = step["started_at"][:19]
    end = step["completed_at"][:19]
    lines = []
    for line in job_log.splitlines():
        stamp = line.lstrip("﻿")[:19]
        if start <= stamp <= end:
            lines.append(line)
    return "\n".join(lines)


def error_lines(job_log):
    return [line for line in (job_log or "").splitlines() if "##[error]" in line]


def search(patterns, text, flags=0):
    for pattern in patterns:
        match = re.search(pattern, text or "", flags)
        if match:
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start:line_end if line_end != -1 else len(text)]
            return line.strip()[:300]
    return None


def result(rule, evidence, step, interval_found):
    failure_class, _ = RULES[rule]
    step_name = step.get("name") if step else None
    interval_name = interval_found[2].get("name") if interval_found else None
    stage = None
    if interval_found and interval_found[0] in ("stage", "sub"):
        stage = interval_found[1]
    elif step_name:
        stage = stage_of_step(step_name)
    return Result(failure_class, rule, evidence, step_name, interval_name, stage)


def classify(evidence):
    job = evidence.job
    step = failed_step(job)
    found = failed_interval(evidence.timing)
    conclusion = evidence.conclusion
    job_conclusion = (job or {}).get("conclusion")

    if conclusion == "success":
        return result("R01", None, None, None)
    if conclusion == "cancelled":
        return result("R02", "conclusion cancelled", step, found)
    if "timed_out" in (conclusion, job_conclusion):
        return result("R03", f"conclusion {conclusion}, job {job_conclusion}", step, found)
    if (
        job is None
        or conclusion == "startup_failure"
        or not job.get("runner_name")
        or not job.get("started_at")
        or not job.get("steps")
    ):
        return result("R04", f"conclusion {conclusion}, job {'missing' if job is None else 'not started'}",
                      step, found)

    errors = "\n".join(error_lines(evidence.job_log))
    hit = search(RUNNER_PATTERNS, evidence.job_log or "")
    if hit:
        return result("R05", hit, step, found)
    hit = search(DIVERGENCE_PATTERNS, errors) or search(DIVERGENCE_PATTERNS, step_log(evidence.job_log, step))
    if hit:
        return result("R06", hit, step, found)

    if found and found[0] in ("stage", "sub") and found[1] == "gate" and found[2].get("exit_code") == 1:
        return result("R07", "gate exit code 1", step, found)
    if found and found[1] == "test" and evidence.tests:
        bad = (evidence.tests.get("failures") or 0) + (evidence.tests.get("errors") or 0)
        if bad > 0:
            return result("R08", f"{evidence.tests.get('failures')} failures, "
                                 f"{evidence.tests.get('errors')} errors", step, found)

    texts = []
    if found:
        texts.append(evidence.logs.get(log_name(found[2]["name"])) or "")
    texts.append(step_log(evidence.job_log, step))
    texts.append(errors)
    hit = search(NETWORK_PATTERNS, "\n".join(texts), re.IGNORECASE)
    if hit:
        return result("R09", hit, step, found)

    if found and found[0] in ("stage", "sub"):
        code = found[2].get("exit_code")
        return result("R10", f"{found[2]['name']} exit code {code}", step, found)
    if step is not None:
        return result("R11", f"step {step.get('name')!r} failed", step, found)
    return result("R12", f"conclusion {conclusion}", step, found)
