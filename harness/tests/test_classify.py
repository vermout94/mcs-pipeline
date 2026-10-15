import copy
import json

import pytest
from conftest import zip_members

from mcs_harness.classify import RULES, Evidence, classify, step_log

MEMBERS = zip_members()
TIMING = json.loads(MEMBERS["timing.json"])
TESTS = json.loads(MEMBERS["environment.json"])["tests"]


def fail_step(job, name):
    """Job whose step `name` failed and whose later steps were skipped, like the runner reports it."""
    job = copy.deepcopy(job)
    job["conclusion"] = "failure"
    hit = False
    for step in job["steps"]:
        if step["name"] == name:
            step["conclusion"] = "failure"
            hit = True
        elif hit and not step["name"].startswith(("Collect", "Post", "Complete")):
            step["conclusion"] = "skipped"
    assert hit, name
    return job


def fail_interval(name, code=1, timing=TIMING):
    """timing.json in which the interval `name` ended with `code` and later intervals are missing."""
    timing = copy.deepcopy(timing)
    for stage in timing["stages"]:
        for sub in stage["subtimes"]:
            if sub["name"] == name:
                sub["exit_code"] = code
                stage["interval"]["exit_code"] = code
        if stage["interval"] and stage["name"] == name:
            stage["interval"]["exit_code"] = code
    for item in timing["preparation"]:
        if item["name"] == name:
            item["exit_code"] = code
    return timing


def job_log(step, *lines):
    stamp = step["started_at"].replace("Z", ".1234567Z")
    return "\n".join(["2026-10-14T21:00:00.0000000Z earlier output"] + [f"{stamp} {line}" for line in lines])


def step_named(job, name):
    return next(s for s in job["steps"] if s["name"] == name)


def test_every_rule_has_a_class_and_a_description():
    assert list(RULES) == [f"R{i:02d}" for i in range(1, 13)]


def test_success(p2_job):
    r = classify(Evidence("success", p2_job, TIMING, TESTS))
    assert (r.failure_class, r.rule, r.failed_step, r.failed_stage) == (None, "R01", None, None)


def test_cancelled(p2_job):
    assert classify(Evidence("cancelled", p2_job, TIMING)).failure_class == "cancelled"


def test_timed_out(p2_job):
    job = dict(p2_job, conclusion="timed_out")
    assert classify(Evidence("failure", job, TIMING)).rule == "R03"


@pytest.mark.parametrize("job", [None, {"id": 1, "runner_name": None, "started_at": None, "steps": []}])
def test_job_did_not_start(job):
    r = classify(Evidence("failure", job))
    assert (r.failure_class, r.rule) == ("infrastructure", "R04")


def test_lost_runner(p2_job):
    job = fail_step(p2_job, "Stage test")
    log = "2026-10-14T21:17:10.0000000Z ##[error]The runner has received a shutdown signal."
    r = classify(Evidence("failure", job, fail_interval("test"), TESTS, job_log=log))
    assert (r.failure_class, r.rule) == ("infrastructure", "R05")


def test_runner_not_fresh(p2_job):
    job = fail_step(p2_job, "Prepare: Maven repository, start mark")
    log = job_log(step_named(job, "Prepare: Maven repository, start mark"),
                  "##[error]~/.m2/repository exists before the restore.")
    assert classify(Evidence("failure", job, TIMING, job_log=log)).rule == "R05"


@pytest.mark.parametrize("step,line", [
    ("Prepare: Maven repository, end mark and hash",
     "##[error]Content hash of the restored Maven repository (abc) differs from tools/versions.json (def)."),
    ("Stage push", "##[error]Image digest missing or inconsistent (image store: '', push output: '')."),
    ("Prepare: cosign", "install.sh: SHA-256 mismatch for https://github.com/sigstore/cosign/x"),
])
def test_divergence_from_the_pins(p2_job, step, line):
    job = fail_step(p2_job, step)
    r = classify(Evidence("failure", job, TIMING, job_log=job_log(step_named(job, step), line)))
    assert (r.failure_class, r.rule, r.failed_step) == ("tool_divergence", "R06", step)


def test_gate_exit_code_1(p2_job):
    timing = copy.deepcopy(TIMING)
    gate = next(s for s in timing["stages"] if s["name"] == "gate")
    gate["enabled"] = True
    gate["interval"] = dict(timing["stages"][0]["interval"], name="gate", start_monotonic_ns=10**12,
                            end_monotonic_ns=10**12 + 5, exit_code=1)
    r = classify(Evidence("failure", fail_step(p2_job, "Stage sign"), timing))
    assert (r.failure_class, r.rule, r.failed_stage) == ("gate", "R07", "gate")


def test_failing_tests(p2_job):
    tests = dict(TESTS, failures=2)
    r = classify(Evidence("failure", fail_step(p2_job, "Stage test"), fail_interval("test"), tests))
    assert (r.failure_class, r.rule, r.failed_stage, r.failed_interval) == ("test", "R08", "test", "test")


def test_test_stage_without_failing_tests_is_a_tool_error(p2_job):
    r = classify(Evidence("failure", fail_step(p2_job, "Stage test"), fail_interval("test"), TESTS,
                          logs={"test.log": "[ERROR] Failed to execute goal"}))
    assert (r.failure_class, r.rule) == ("tool", "R10")


def test_network_error_in_the_log_of_the_interval(p2_job):
    logs = {"push.log": "error parsing HTTP 503 Service Unavailable response body"}
    r = classify(Evidence("failure", fail_step(p2_job, "Stage push"), fail_interval("push"), TESTS, logs))
    assert (r.failure_class, r.rule, r.failed_stage) == ("network", "R09", "push")


def test_network_error_in_a_sub_time(p2_job):
    logs = {"sign-sign.log": "Post \"https://rekor.sigstore.dev/api/v1/log/entries\": dial tcp: i/o timeout"}
    r = classify(Evidence("failure", fail_step(p2_job, "Stage sign"), fail_interval("sign.sign"), TESTS, logs))
    assert (r.failure_class, r.rule, r.failed_interval, r.failed_stage) == ("network", "R09", "sign.sign", "sign")


def test_network_error_in_the_job_log_of_a_preparation_step(p2_job):
    job = fail_step(p2_job, "Prepare: JDK")
    log = job_log(step_named(job, "Prepare: JDK"), "curl: (56) Recv failure: Connection reset by peer")
    r = classify(Evidence("failure", job, fail_interval("jdk"), job_log=log))
    assert (r.failure_class, r.rule, r.failed_interval, r.failed_stage) == ("network", "R09", "jdk", None)


def test_network_error_outside_the_failed_step_is_ignored(p2_job):
    job = fail_step(p2_job, "Prepare: JDK")
    log = "2026-10-14T21:00:00.0000000Z curl: (56) Recv failure: Connection reset by peer"
    r = classify(Evidence("failure", job, fail_interval("jdk"), job_log=log))
    assert (r.failure_class, r.rule) == ("infrastructure", "R11")


def test_stage_interval_with_other_exit_code(p2_job):
    r = classify(Evidence("failure", fail_step(p2_job, "Stage build"), fail_interval("build.maven"), TESTS,
                          {"build-maven.log": "[ERROR] COMPILATION ERROR"}))
    assert (r.failure_class, r.rule, r.failed_interval, r.failed_stage) == ("tool", "R10", "build.maven", "build")


def test_stage_without_end_mark(p2_job):
    timing = copy.deepcopy(TIMING)
    push = next(s for s in timing["stages"] if s["name"] == "push")
    push["interval"].update(end_monotonic_ns=None, end_wall=None, exit_code=None)
    r = classify(Evidence("failure", fail_step(p2_job, "Stage push"), timing, TESTS))
    assert (r.rule, r.failed_interval) == ("R10", "push")


def test_failed_uses_step_without_evidence(p2_job):
    r = classify(Evidence("failure", fail_step(p2_job, "Collect: upload"), TIMING, TESTS))
    assert (r.failure_class, r.rule, r.failed_step) == ("infrastructure", "R11", "Collect: upload")


def test_no_failed_step(p2_job):
    job = dict(p2_job, conclusion="failure")
    r = classify(Evidence("failure", job, TIMING, TESTS))
    assert (r.failure_class, r.rule) == ("unknown", "R12")


def test_step_log_uses_the_time_window_of_the_step(p2_job):
    step = step_named(p2_job, "Prepare: JDK")
    log = job_log(step, "inside")
    assert step_log(log, step) == log.splitlines()[1]
