"""The campaign loop against a simulated GitHub API and clock."""

import copy
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import artifact_for, load, write_plan

from mcs_harness.__main__ import main
from mcs_harness.campaign import CampaignStop, Paths, Runner, create_order
from mcs_harness.github import ApiError
from mcs_harness.manifest import validate, validator
from mcs_harness.matching import MatchError, parse_title

COMMIT = "9a5b274b778be1a055de73303f628ee1ad0284e6"


class FakeClock:
    def __init__(self):
        self.moment = datetime(2026, 10, 15, 18, 0, 0, tzinfo=UTC)
        self.sleeps = []

    def now(self):
        return self.moment

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.moment += timedelta(seconds=seconds)


class FakeGitHub:
    """Runs complete after two status requests. Artifacts are the Prompt 2 artifact, rewritten."""

    def __init__(self, clock, conclusion="success"):
        self.clock = clock
        self.runs = {}
        self.next_id = 1000
        self.dispatches = []
        self.conclusion = conclusion
        self.fail_dispatch_after_create = False
        self.wrong_dispatch_id = False
        self.template_job = load("jobs.json")["jobs"][0]

    def stamp(self):
        return self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_run(self, title, created_at=None, polls=2, conclusion=None):
        run_id = self.next_id
        self.next_id += 1
        self.runs[run_id] = {
            "id": run_id, "display_title": title, "status": "queued", "conclusion": None,
            "head_sha": COMMIT, "run_attempt": 1, "created_at": created_at or self.stamp(),
            "run_started_at": created_at or self.stamp(), "updated_at": created_at or self.stamp(),
            "html_url": f"https://example.invalid/runs/{run_id}", "_polls": polls,
            "_conclusion": conclusion or self.conclusion,
        }
        if polls == 0:
            self.runs[run_id].update(status="completed", conclusion=conclusion or self.conclusion)
        return run_id

    # API

    def head_commit(self, ref):
        return COMMIT

    def dispatch(self, workflow, ref, inputs):
        title = (f"measure {inputs['correlation_id']} runner={inputs['runner']} sbom={inputs['sbom']} "
                 f"sign={inputs['sign']} provenance={inputs['provenance']} policy={inputs['policy']}")
        run_id = self.add_run(title)
        self.dispatches.append(inputs)
        if self.fail_dispatch_after_create:
            self.fail_dispatch_after_create = False
            raise ApiError("POST", "/dispatches", "network", "TimeoutError")
        reported = run_id - 1 if self.wrong_dispatch_id else run_id
        return {"workflow_run_id": reported, "run_url": "u", "html_url": "h"}

    def get_run(self, run_id):
        run = self.runs[run_id]
        if run["status"] != "completed":
            run["_polls"] -= 1
            run["status"] = "in_progress"
            if run["_polls"] <= 0:
                run["status"] = "completed"
                run["conclusion"] = run["_conclusion"]
                run["updated_at"] = self.stamp()
        return {k: v for k, v in run.items() if not k.startswith("_")}

    def list_runs(self, workflow, created, branch):
        operator, moment = created[:2] if created.startswith(">=") else created[:1], created.lstrip("<>=")
        limit = datetime.fromisoformat(moment)
        found = []
        for run in self.runs.values():
            created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if (operator == ">=" and created_at >= limit) or (operator == "<" and created_at < limit):
                found.append({k: v for k, v in run.items() if not k.startswith("_")})
        return found

    def list_active_runs(self, workflow):
        return [r for r in self.runs.values() if r["status"] != "completed"]

    def job(self, run_id):
        run = self.runs[run_id]
        job = copy.deepcopy(self.template_job)
        job["id"] = run_id * 10
        job["run_id"] = run_id
        job["head_sha"] = run["head_sha"]
        if run["conclusion"] != "success":
            job["conclusion"] = "failure"
            for step in job["steps"]:
                if step["name"] == "Stage test":
                    step["conclusion"] = "failure"
        return job

    def list_jobs(self, run_id):
        return [self.job(run_id)]

    def artifact_bytes(self, run_id):
        parsed = parse_title(self.runs[run_id]["display_title"])
        factors = {k: parsed[k] == "true" for k in ("sbom", "sign", "provenance", "policy")}
        return artifact_for(run_id, parsed["correlation_id"], factors, parsed["runner"])

    def list_run_artifacts(self, run_id, name):
        data = self.artifact_bytes(run_id)
        return [{"id": run_id * 100, "name": name, "size_in_bytes": len(data), "expired": False,
                 "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "expires_at": "2027-01-13T18:00:00Z"}]

    def download_artifact(self, artifact_id):
        return self.artifact_bytes(artifact_id // 100)

    def download_job_log(self, job_id):
        return b"2026-10-15T18:00:00.0000000Z ##[error]Process completed with exit code 1.\n"


@pytest.fixture
def setup(tmp_path):
    directory = tmp_path / "data" / "dryrun"
    write_plan(directory, runs_per_configuration=2, pause_seconds=60, poll_seconds=10)
    clock = FakeClock()
    github = FakeGitHub(clock)
    create_order(directory, github, clock)
    return directory, github, clock


def manifests(directory):
    return {p.stem: json.loads(p.read_text()) for p in (directory / "manifests").glob("*.json")}


def test_campaign_runs_all_positions_in_order(setup):
    directory, github, clock = setup
    order = json.loads((directory / "order.json").read_text())
    Runner(directory, github, clock).run()
    assert [d["correlation_id"] for d in github.dispatches] == [r["correlation_id"] for r in order["runs"]]
    found = manifests(directory)
    assert len(found) == 4
    check = validator()
    for m in found.values():
        assert validate(m, check) == []
        assert m["run"]["matched_by"] == ["dispatch_response", "run_name", "artifact"]
    assert sorted((directory / "artifacts").iterdir())[0].name == "measure-dryrun-0000.zip"
    events = [json.loads(line)["event"] for line in (directory / "journal.jsonl").read_text().splitlines()]
    assert events[0] == "order_written" and events[-1] == "finished"


def test_pause_between_runs(setup):
    directory, github, clock = setup
    Runner(directory, github, clock).run()
    dispatched = [json.loads(line) for line in (directory / "journal.jsonl").read_text().splitlines()
                  if '"dispatched"' in line]
    completed = [json.loads(line) for line in (directory / "journal.jsonl").read_text().splitlines()
                 if '"completed"' in line]
    for done, nxt in zip(completed, dispatched[1:]):
        gap = (datetime.fromisoformat(nxt["dispatched_at"].replace("Z", "+00:00"))
               - datetime.fromisoformat(done["at"].replace("Z", "+00:00"))).total_seconds()
        assert gap == pytest.approx(60, abs=1)


def test_restart_continues_without_new_order_and_without_second_dispatch(setup):
    directory, github, clock = setup
    order_bytes = (directory / "order.json").read_bytes()
    original = github.get_run
    calls = {"n": 0}

    def crash_on_second_run(run_id):
        if run_id == 1001:
            calls["n"] += 1
            raise KeyboardInterrupt
        return original(run_id)

    github.get_run = crash_on_second_run
    with pytest.raises(KeyboardInterrupt):
        Runner(directory, github, clock).run()
    assert len(manifests(directory)) == 1 and len(github.dispatches) == 2
    github.get_run = original
    Runner(directory, github, clock).run()
    assert len(manifests(directory)) == 4
    assert len(github.dispatches) == 4
    assert (directory / "order.json").read_bytes() == order_bytes


def test_failed_dispatch_request_stops_and_restart_adopts_the_run(setup):
    directory, github, clock = setup
    github.fail_dispatch_after_create = True
    with pytest.raises(CampaignStop, match="dispatch failed"):
        Runner(directory, github, clock).run()
    assert len(github.dispatches) == 1
    Runner(directory, github, clock).run()
    assert len(github.dispatches) == 4
    first = manifests(directory)["dryrun-0000"]
    assert first["run"]["matched_by"] == ["run_name", "artifact"]
    assert first["run"]["dispatch_response"] is None and first["run"]["dispatched_at"] is not None
    assert "found by its run name" in first["null_reasons"]["/run/dispatch_response"]
    assert validate(first, validator()) == []


def test_dispatch_response_and_run_name_disagree(setup):
    directory, github, clock = setup
    github.wrong_dispatch_id = True
    github.add_run("other", polls=0)  # the run the dispatch response will name
    with pytest.raises(MatchError, match="dispatch response names run 1000"):
        Runner(directory, github, clock).run()
    assert manifests(directory) == {}


def test_correlation_id_used_before_the_order_stops_the_campaign(tmp_path):
    directory = tmp_path / "data" / "dryrun"
    write_plan(directory)
    clock = FakeClock()
    github = FakeGitHub(clock)
    github.add_run("measure dryrun-0000 runner=hosted sbom=false sign=false provenance=false policy=false",
                   created_at="2026-10-14T10:00:00Z", polls=0)
    create_order(directory, github, clock)
    with pytest.raises(CampaignStop, match="used before order.json"):
        Runner(directory, github, clock).run()
    assert github.dispatches == []


def test_consecutive_failures_stop_the_campaign(tmp_path):
    directory = tmp_path / "data" / "dryrun"
    write_plan(directory, runs_per_configuration=3, stop_after_consecutive_failures=2)
    clock = FakeClock()
    github = FakeGitHub(clock, conclusion="failure")
    create_order(directory, github, clock)
    with pytest.raises(CampaignStop, match="2 runs in a row"):
        Runner(directory, github, clock).run()
    found = manifests(directory)
    assert len(found) == 2
    for m in found.values():
        assert m["outcome"]["conclusion"] == "failure"
        assert m["outcome"]["failure_class"] is not None and m["outcome"]["failure_rule"] is not None
        assert m["run"]["job_log"]["path"].startswith("job-logs/")
        assert validate(m, validator()) == []


def test_changed_plan_after_the_order_stops_the_campaign(setup):
    directory, github, clock = setup
    plan = (directory / "plan.yaml").read_text()
    (directory / "plan.yaml").write_text(plan.replace("seed: 20261015", "seed: 1"))
    with pytest.raises(CampaignStop, match="plan.yaml changed"):
        Runner(directory, github, clock)


def test_order_is_written_once(setup):
    directory, github, clock = setup
    with pytest.raises(CampaignStop, match="^data/dryrun/order.json exists, .* written once"):
        create_order(directory, github, clock)


def test_existing_raw_data_is_never_overwritten(setup):
    directory, github, clock = setup
    paths = Paths(directory)
    paths.artifacts.mkdir(parents=True)
    (paths.artifacts / "measure-dryrun-0000.zip").write_bytes(b"other")
    with pytest.raises(CampaignStop, match="^data/dryrun/artifacts/measure-dryrun-0000.zip exists .* never overwritten"):
        Runner(directory, github, clock).run()


@pytest.mark.parametrize("dirty", [True, None])
def test_pilot_stops_before_the_first_dispatch_if_harness_is_not_committed(tmp_path, monkeypatch, dirty):
    monkeypatch.setattr("mcs_harness.campaign.orchestrator_info",
                        lambda: {"commit": "b" * 40 if dirty else None, "dirty": dirty})
    directory = tmp_path / "data" / "pilot-hosted-2026-10-15"
    write_plan(directory, campaign="pilot-hosted-2026-10-15")
    clock = FakeClock()
    github = FakeGitHub(clock)
    create_order(directory, github, clock)
    with pytest.raises(CampaignStop, match="harness/"):
        Runner(directory, github, clock).run()
    assert github.dispatches == []


def test_pilot_runs_from_a_committed_harness(tmp_path, monkeypatch):
    monkeypatch.setattr("mcs_harness.campaign.orchestrator_info", lambda: {"commit": "b" * 40, "dirty": False})
    directory = tmp_path / "data" / "pilot-hosted-2026-10-15"
    write_plan(directory, campaign="pilot-hosted-2026-10-15")
    clock = FakeClock()
    github = FakeGitHub(clock)
    create_order(directory, github, clock)
    Runner(directory, github, clock).run()
    assert len(github.dispatches) == 2
    assert all(m["orchestrator"]["dirty"] is False for m in manifests(directory).values())


def test_dryrun_runs_with_uncommitted_harness(setup, monkeypatch):
    directory, github, clock = setup
    monkeypatch.setattr("mcs_harness.campaign.orchestrator_info", lambda: {"commit": "b" * 40, "dirty": True})
    Runner(directory, github, clock).run()
    assert len(github.dispatches) == 4


@pytest.fixture
def harness_logger():
    """main() attaches handlers to the logger mcs_harness, closed again after the test."""
    logger = logging.getLogger("mcs_harness")
    yield logger
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_log_and_journal_of_a_campaign_have_no_absolute_paths(tmp_path, monkeypatch, harness_logger):
    data = tmp_path / "data"
    write_plan(data / "dryrun")
    clock = FakeClock()
    github = FakeGitHub(clock)
    monkeypatch.setattr("mcs_harness.__main__.github_for", lambda directory: github)
    monkeypatch.setattr("mcs_harness.campaign.Clock", lambda: clock)
    monkeypatch.setattr("mcs_harness.campaign.orchestrator_info", lambda: {"commit": "b" * 40, "dirty": False})
    assert main(["plan", "dryrun", "--data", str(data)]) == 0
    assert main(["run", "dryrun", "--data", str(data)]) == 0
    (data / "dryrun" / "manifests" / "dryrun-0000.json").unlink()
    (data / "dryrun" / "artifacts" / "measure-dryrun-0000.zip").write_bytes(b"other")
    assert main(["run", "dryrun", "--data", str(data)]) == 2
    log = (data / "dryrun" / "orchestrator.log").read_text()
    journal = (data / "dryrun" / "journal.jsonl").read_text()
    assert "campaign directory data/dryrun\n" in log
    assert "data/dryrun/artifacts/measure-dryrun-0000.zip exists" in log
    assert "data/dryrun/artifacts/measure-dryrun-0000.zip exists" in journal
    for text in (log, journal):
        for absolute in (str(tmp_path), str(tmp_path.resolve()), str(Path.home())):
            assert absolute not in text
