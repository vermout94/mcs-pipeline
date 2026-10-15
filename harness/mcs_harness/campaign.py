"""One campaign: order, dispatch, wait, collect, manifest.

Files in data/<campaign>/:

  plan.yaml         the plan, written by hand
  order.json        seed and run order, written by `plan` before the first run
  journal.jsonl     one JSON line per event, the state for a restart
  orchestrator.log  the log of the orchestrator
  manifests/        <correlation ID>.json, one per run
  artifacts/        measure-<correlation ID>.zip, unchanged
  job-logs/         <correlation ID>.log, unchanged, for runs that did not succeed

Runs strictly one after the other. Before each dispatch the orchestrator waits
until the pause after the previous run has passed and no run of the workflow is
queued or in progress. A position counts as done when its manifest exists. A
restart continues with the first position without manifest and never draws the
order again. Before it dispatches a position, it looks for a run with the
correlation ID of that position, so a run that was dispatched before an
interruption is collected, not dispatched a second time.

Runs are never repeated. The orchestrator stops (CampaignStop) before it
dispatches anything if harness/ has uncommitted changes (every campaign except
dryrun), when the ways of assignment disagree, when a download does not match its digest, when a manifest
does not validate, when a run ran another commit, after too many failed runs in
a row and when a dispatch request fails. After the cause is fixed, a restart
continues.
"""

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import REPO
from . import manifest as manifest_module
from .artifact import Artifact
from .classify import Evidence, classify
from .github import ApiError
from .matching import MatchError, check_artifact, match_by_run_name, parse_title
from .plan import expected_title, load_plan, make_order, workflow_inputs

log = logging.getLogger(__name__)

JOB_NAME = "measure"
UPLOAD_STEP = "Collect: upload"
RUN_NAME_ATTEMPTS = 6


class CampaignStop(Exception):
    pass


class Clock:
    def now(self):
        return datetime.now(UTC)

    def sleep(self, seconds):
        time.sleep(seconds)


def iso(moment, timespec="seconds"):
    return moment.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(handle, 0o644)  # mkstemp creates 0600
        with os.fdopen(handle, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def write_once(path, data):
    """Raw data is never overwritten: an existing file must have the same bytes."""
    if path.exists():
        if sha256(path.read_bytes()) != sha256(data):
            raise CampaignStop(f"{path} exists with other content, raw data is never overwritten")
        return
    write_atomic(path, data)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


class Paths:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.plan = self.dir / "plan.yaml"
        self.order = self.dir / "order.json"
        self.journal = self.dir / "journal.jsonl"
        self.log = self.dir / "orchestrator.log"
        self.manifests = self.dir / "manifests"
        self.artifacts = self.dir / "artifacts"
        self.job_logs = self.dir / "job-logs"

    def manifest(self, run):
        return self.manifests / f"{run['correlation_id']}.json"


class Journal:
    def __init__(self, path, clock):
        self.path = path
        self.clock = clock

    def append(self, event, **fields):
        record = {"at": iso(self.clock.now(), "microseconds"), "event": event} | fields
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def events(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def last(self, kinds, position=None):
        for record in reversed(self.events()):
            if record["event"] in kinds and (position is None or record.get("position") == position):
                return record
        return None


def orchestrator_info():
    try:
        commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=30, check=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--", "harness"],
                                capture_output=True, text=True, timeout=30, check=True).stdout
        return {"commit": commit, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError) as error:
        return {"commit": None, "dirty": None, "reason": f"git failed: {type(error).__name__}"}


def create_order(directory, github, clock=None):
    """Write order.json before the first run. Never overwrites an order."""
    clock = clock or Clock()
    paths = Paths(directory)
    if paths.order.exists():
        raise CampaignStop(f"{paths.order} exists, the order of a campaign is written once")
    plan, plan_sha256 = load_plan(paths.plan)
    commit = github.head_commit(plan["ref"])
    order = make_order(plan, plan_sha256, commit, iso(clock.now()))
    write_atomic(paths.order, json_bytes(order))
    Journal(paths.journal, clock).append(
        "order_written", seed=order["seed"], runs=len(order["runs"]), repo_commit=commit,
        order_sha256=sha256(paths.order.read_bytes()),
    )
    return order


def load_order(paths):
    if not paths.order.exists():
        raise CampaignStop(f"{paths.order} missing, write it with the command plan first")
    data = paths.order.read_bytes()
    order = json.loads(data)
    _, plan_sha256 = load_plan(paths.plan)
    if plan_sha256 != order["plan_sha256"]:
        raise CampaignStop("plan.yaml changed after order.json was written")
    order["order_sha256"] = sha256(data)
    return order


class Runner:
    def __init__(self, directory, github, clock=None):
        self.paths = Paths(directory)
        self.github = github
        self.clock = clock or Clock()
        self.plan, _ = load_plan(self.paths.plan)
        self.order = load_order(self.paths)
        self.journal = Journal(self.paths.journal, self.clock)
        self.validator = manifest_module.validator()
        self.info = orchestrator_info()
        self.last_completed = None
        completed = self.journal.last({"completed"})
        if completed:
            self.last_completed = parse_iso(completed["at"])

    # Campaign

    def run(self):
        order = self.order
        done = [r for r in order["runs"] if self.paths.manifest(r).exists()]
        log.info("campaign %s: %d of %d runs done, commit %s, orchestrator %s%s",
                 order["campaign"], len(done), len(order["runs"]), order["repo_commit"][:12],
                 (self.info.get("commit") or "unknown")[:12], " (harness/ changed)" if self.info.get("dirty") else "")
        self.journal.append("started", done=len(done), runs=len(order["runs"]),
                            orchestrator_commit=self.info.get("commit"), dirty=self.info.get("dirty"))
        self.check_clean_harness()
        self.check_conflicts()
        failures = self.trailing_failures()
        for entry in order["runs"]:
            if self.paths.manifest(entry).exists():
                continue
            manifest = self.run_one(entry)
            failures = 0 if manifest["outcome"]["conclusion"] == "success" else failures + 1
            limit = self.plan["stop_after_consecutive_failures"]
            if limit and failures >= limit:
                raise CampaignStop(f"{failures} runs in a row did not succeed (limit {limit})")
        self.journal.append("finished", runs=len(order["runs"]))
        log.info("campaign %s finished", order["campaign"])

    def trailing_failures(self):
        count = 0
        for entry in self.order["runs"]:
            path = self.paths.manifest(entry)
            if not path.exists():
                break
            conclusion = json.loads(path.read_text(encoding="utf-8"))["outcome"]["conclusion"]
            count = 0 if conclusion == "success" else count + 1
        return count

    def check_clean_harness(self):
        """Every campaign except dryrun runs from a committed orchestrator."""
        if self.order["kind"] == "dryrun":
            return
        if self.info.get("dirty") is not False:
            state = "has uncommitted changes" if self.info.get("dirty") else "cannot be checked"
            raise CampaignStop(f"harness/ {state}. Commit the orchestrator before a {self.order['kind']} runs.")

    def created_filter(self, operator, moment):
        return f"{operator}{iso(moment).replace('Z', '+00:00')}"

    def check_conflicts(self):
        """Runs with a correlation ID of this campaign that are older than order.json."""
        created = parse_iso(self.order["created_at"])
        older = self.github.list_runs(self.order["workflow"], self.created_filter("<", created), self.order["ref"])
        ids = {r["correlation_id"] for r in self.order["runs"]}
        clashes = []
        for run in older:
            parsed = parse_title(run.get("display_title"))
            if parsed and parsed["correlation_id"] in ids and parse_iso(run["created_at"]) < created:
                clashes.append((parsed["correlation_id"], run["id"]))
        if clashes:
            raise CampaignStop(f"correlation IDs of this campaign were used before order.json was written: "
                               f"{clashes[:5]}. Choose another campaign ID.")

    # One run

    def find_by_run_name(self, entry, since, dispatch_run_id=None):
        runs = self.github.list_runs(self.order["workflow"], self.created_filter(">=", since), self.order["ref"])
        runs = [r for r in runs if parse_iso(r["created_at"]) >= since.replace(microsecond=0)]
        return match_by_run_name(runs, entry["correlation_id"], expected_title(self.order, entry), dispatch_run_id)

    def run_one(self, entry):
        cid = entry["correlation_id"]
        position = entry["position"]
        record = self.journal.last({"dispatched", "adopted"}, position)
        if record is None:
            record = self.adopt_or_dispatch(entry)
        run_id = record["run_id"]
        run = self.wait_for_completion(entry, run_id)

        # Second way: the run name. Third way: environment.json in the artifact.
        matched_by = ["dispatch_response"] if record.get("response") else []
        since = parse_iso(record["dispatched_at"]) - timedelta(minutes=5) if record.get("dispatched_at") \
            else parse_iso(self.order["created_at"])
        listed = None
        for attempt in range(RUN_NAME_ATTEMPTS):
            listed = self.find_by_run_name(entry, since, run_id)
            if listed:
                break
            self.clock.sleep(self.plan["poll_seconds"])
        if listed is None:
            raise MatchError(f"{cid}: run {run_id} does not appear in the list of workflow runs")
        matched_by.append("run_name")

        jobs = self.github.list_jobs(run_id)
        job = next((j for j in jobs if j.get("name") == JOB_NAME), jobs[0] if jobs else None)
        artifact_meta, artifact = self.fetch_artifact(entry, run, job)
        if artifact is not None:
            problems = check_artifact(artifact.json("environment.json"), run_id, cid)
            if problems:
                raise MatchError(f"{cid}: " + "; ".join(problems))
            matched_by.append("artifact")
        job_log_meta, job_log_text, job_log_reason = self.fetch_job_log(entry, run, job)

        environment = artifact.json("environment.json") if artifact else None
        logs = {}
        if artifact is not None:
            logs = {name: artifact.text(name) for name in artifact.names if name.endswith(".log")}
        result = classify(Evidence(
            conclusion=run.get("conclusion"),
            job=job,
            timing=artifact.json("timing.json") if artifact else None,
            tests=(environment or {}).get("tests"),
            logs=logs,
            job_log=job_log_text,
        ))
        dispatch = {
            "dispatched_at": record.get("dispatched_at"),
            "response": record.get("response"),
            "reason": record.get("reason"),
        }
        manifest = manifest_module.build(
            order=self.order, run_plan=entry, dispatch=dispatch, run=run, job=job,
            artifact_meta=artifact_meta, artifact=artifact, job_log_meta=job_log_meta,
            job_log_reason=job_log_reason, matched_by=matched_by, classification=result,
            orchestrator=self.info, now=iso(self.clock.now(), "microseconds"),
        )
        problems = manifest_module.validate(manifest, self.validator)
        if problems:
            write_atomic(self.paths.manifests / f"{cid}.invalid.json", json_bytes(manifest))
            raise CampaignStop(f"{cid}: manifest does not validate: " + "; ".join(problems[:10]))
        write_once(self.paths.manifest(entry), json_bytes(manifest))
        self.journal.append("collected", position=position, correlation_id=cid, run_id=run_id,
                            conclusion=run.get("conclusion"), failure_class=result.failure_class,
                            failure_rule=result.rule, divergences=len(manifest["checks"]["divergences"]))
        total = (manifest["timing"]["totals"] or {}).get("stages_sum_ns")
        log.info("%s %s run %s: %s%s, stages %s s, job %s s, queue %s s%s",
                 cid, entry["configuration"], run_id, run.get("conclusion"),
                 f" ({result.failure_class}, {result.rule})" if result.failure_class else "",
                 f"{total / 1e9:.3f}" if total is not None else "n/a",
                 (manifest["timing"]["job_api"] or {}).get("duration_s"),
                 (manifest["timing"]["job_api"] or {}).get("queue_s"),
                 f", divergences: {manifest['checks']['divergences']}" if manifest["checks"]["divergences"] else "")
        if run.get("head_sha") != self.order["repo_commit"]:
            raise CampaignStop(f"{cid}: run {run_id} ran commit {run.get('head_sha')}, "
                               f"the campaign commit is {self.order['repo_commit']}")
        return manifest

    def adopt_or_dispatch(self, entry):
        cid = entry["correlation_id"]
        position = entry["position"]
        attempted = self.journal.last({"dispatching"}, position)
        created = parse_iso(self.order["created_at"])
        attempts = RUN_NAME_ATTEMPTS if attempted else 1
        found = None
        for attempt in range(attempts):
            found = self.find_by_run_name(entry, created)
            if found or attempt == attempts - 1:
                break
            self.clock.sleep(self.plan["poll_seconds"])
        if found:
            reason = ("the dispatch request failed or the orchestrator stopped before the journal entry, "
                      "the run was found by its run name")
            record = {"position": position, "correlation_id": cid, "run_id": found["id"],
                      "dispatched_at": attempted["dispatched_at"] if attempted else None,
                      "response": None, "reason": reason}
            self.journal.append("adopted", **record)
            log.warning("%s: run %s found by its run name, collected without dispatch", cid, found["id"])
            return record

        self.pause()
        self.wait_until_idle()
        dispatched_at = iso(self.clock.now(), "microseconds")
        self.journal.append("dispatching", position=position, correlation_id=cid, dispatched_at=dispatched_at)
        try:
            response = self.github.dispatch(self.order["workflow"], self.order["ref"], workflow_inputs(self.order, entry))
        except (ApiError, OSError) as error:
            self.journal.append("dispatch_failed", position=position, correlation_id=cid, error=str(error))
            raise CampaignStop(f"{cid}: dispatch failed ({error}). A restart first looks for the run by its "
                               f"run name.") from None
        record = {"position": position, "correlation_id": cid, "run_id": response["workflow_run_id"],
                  "dispatched_at": dispatched_at, "response": response}
        self.journal.append("dispatched", **record)
        log.info("%s %s dispatched: run %s, block %d, position %d", cid, entry["configuration"],
                 response["workflow_run_id"], entry["block"], position)
        return record

    def pause(self):
        if self.last_completed is None:
            return
        remaining = self.plan["pause_seconds"] - (self.clock.now() - self.last_completed).total_seconds()
        if remaining > 0:
            log.info("pause %.0f s", remaining)
            self.clock.sleep(remaining)

    def deadline(self):
        return self.clock.now() + timedelta(minutes=self.plan["max_wait_minutes"])

    def wait_until_idle(self):
        deadline = self.deadline()
        while True:
            active = self.github.list_active_runs(self.order["workflow"])
            if not active:
                return
            ids = sorted({r["id"] for r in active})
            if self.clock.now() >= deadline:
                raise CampaignStop(f"runs of {self.order['workflow']} still queued or in progress: {ids}")
            log.info("waiting, runs of the workflow queued or in progress: %s", ids)
            self.clock.sleep(self.plan["poll_seconds"])

    def wait_for_completion(self, entry, run_id):
        deadline = self.deadline()
        status = None
        while True:
            run = self.github.get_run(run_id)
            if run.get("status") != status:
                status = run.get("status")
                log.info("%s run %s: %s", entry["correlation_id"], run_id, status)
            if status == "completed":
                self.last_completed = self.clock.now()
                self.journal.append("completed", position=entry["position"], correlation_id=entry["correlation_id"],
                                    run_id=run_id, conclusion=run.get("conclusion"))
                return run
            if self.clock.now() >= deadline:
                raise CampaignStop(f"{entry['correlation_id']}: run {run_id} not completed after "
                                   f"{self.plan['max_wait_minutes']} min. A restart waits for the same run.")
            self.clock.sleep(self.plan["poll_seconds"])

    def fetch_artifact(self, entry, run, job):
        name = f"measure-{entry['correlation_id']}"
        uploaded = any(s.get("name") == UPLOAD_STEP and s.get("conclusion") == "success"
                       for s in (job or {}).get("steps") or [])
        attempts = RUN_NAME_ATTEMPTS if uploaded else 1
        items = []
        for attempt in range(attempts):
            items = [a for a in self.github.list_run_artifacts(run["id"], name) if a.get("name") == name]
            if items or attempt == attempts - 1:
                break
            self.clock.sleep(self.plan["poll_seconds"])
        if not items:
            if uploaded:
                raise CampaignStop(f"{name}: the upload step succeeded, but the run lists no artifact")
            return None, None
        if len(items) > 1:
            raise CampaignStop(f"{name}: the run lists {len(items)} artifacts with this name")
        item = items[0]
        if item.get("expired"):
            raise CampaignStop(f"{name}: the artifact has expired")
        data = self.github.download_artifact(item["id"])
        digest = sha256(data)
        if item.get("digest") and item["digest"] != f"sha256:{digest}":
            raise CampaignStop(f"{name}: SHA-256 of the download {digest} differs from the digest {item['digest']}")
        if len(data) != item.get("size_in_bytes"):
            raise CampaignStop(f"{name}: {len(data)} bytes downloaded, the API names {item.get('size_in_bytes')}")
        path = self.paths.artifacts / f"{name}.zip"
        write_once(path, data)
        meta = {
            "name": name,
            "id": item["id"],
            "size_in_bytes": item["size_in_bytes"],
            "digest": item.get("digest"),
            "sha256": digest,
            "path": str(path.relative_to(self.paths.dir)),
            "expires_at": item.get("expires_at"),
        }
        return meta, Artifact(data)

    def fetch_job_log(self, entry, run, job):
        if run.get("conclusion") == "success":
            return None, None, "the run succeeded, the job log is only kept for runs that did not succeed"
        if job is None:
            return None, None, "the run has no job"
        try:
            data = self.github.download_job_log(job["id"])
        except ApiError as error:
            return None, None, f"job log not available: {error.status}"
        path = self.paths.job_logs / f"{entry['correlation_id']}.log"
        write_once(path, data)
        meta = {"path": str(path.relative_to(self.paths.dir)), "sha256": sha256(data), "bytes": len(data)}
        return meta, data.decode("utf-8", "replace"), None


def status(directory):
    paths = Paths(directory)
    order = json.loads(paths.order.read_text(encoding="utf-8"))
    rows = []
    for entry in order["runs"]:
        path = paths.manifest(entry)
        if path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
            rows.append((entry, m["run"]["run_id"], m["outcome"]["conclusion"], m["outcome"]["failure_class"]))
        else:
            rows.append((entry, None, None, None))
    return order, rows
