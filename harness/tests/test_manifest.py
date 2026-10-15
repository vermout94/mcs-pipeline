import copy
import hashlib
import json

import pytest
from conftest import P2_ZIP, artifact_for

from mcs_harness.artifact import Artifact
from mcs_harness.classify import Evidence, classify
from mcs_harness.manifest import build, null_pointers, validate, validator

ORDER = {
    "campaign": "dryrun",
    "kind": "dryrun",
    "plan_sha256": "0" * 64,
    "order_sha256": "1" * 64,
    "seed": 20261015,
    "repo_commit": "9a5b274b778be1a055de73303f628ee1ad0284e6",
    "workflow": "measure.yml",
    "ref": "main",
    "environment": "hosted",
}
SIGN = {"sbom": False, "sign": True, "provenance": False, "policy": False}
RUN_PLAN = {"position": 1, "block": 0, "position_in_block": 1, "configuration": "sign", "factors": SIGN,
            "cell": 4, "correlation_id": "p2-test-20261014-sign", "first_runs": True}
VALIDATOR = validator()


def make(p2_run, p2_job, p2_artifact_meta, data=None, **overrides):
    data = P2_ZIP.read_bytes() if data is None else data
    artifact = Artifact(data) if data else None
    meta = None
    if artifact:
        meta = {k: p2_artifact_meta[k] for k in ("name", "id", "size_in_bytes", "digest", "expires_at")}
        meta |= {"sha256": hashlib.sha256(data).hexdigest(), "path": "artifacts/x.zip"}
    env = artifact.json("environment.json") if artifact else None
    args = dict(
        order=ORDER, run_plan=RUN_PLAN,
        dispatch={"dispatched_at": "2026-10-14T21:16:26.123456Z",
                  "response": {"workflow_run_id": p2_run["id"], "run_url": "u", "html_url": "h"}},
        run=p2_run, job=p2_job, artifact_meta=meta, artifact=artifact, job_log_meta=None,
        job_log_reason="the run succeeded, the job log is only kept for runs that did not succeed",
        matched_by=["dispatch_response", "run_name", "artifact"],
        classification=classify(Evidence(p2_run["conclusion"], p2_job,
                                         artifact.json("timing.json") if artifact else None,
                                         (env or {}).get("tests"))),
        orchestrator={"commit": "b" * 40, "dirty": False},
        now="2026-10-15T18:00:00.000000Z",
    )
    args |= overrides
    return build(**args)


def test_manifest_of_the_prompt_2_run_is_valid(p2_run, p2_job, p2_artifact_meta):
    m = make(p2_run, p2_job, p2_artifact_meta)
    assert validate(m, VALIDATOR) == []
    assert m["run"]["matched_by"] == ["dispatch_response", "run_name", "artifact"]
    assert m["design"]["position_in_campaign"] == 1 and m["design"]["cell"] == 4
    assert m["environment"]["runner"]["labels"] == ["ubuntu-24.04"]
    assert m["environment"]["runner"]["image_version"] == "20261012.334.1"
    assert m["timing"]["job_api"]["queue_s"] == 3.0 and m["timing"]["job_api"]["duration_s"] == 62.0
    assert m["timing"]["totals"]["stages_sum_ns"] == 34268270471
    assert [s["name"] for s in m["timing"]["stages"]] == ["checkout", "build", "test", "push", "sbom", "sign",
                                                          "provenance", "gate"]
    assert m["versions"]["caches"]["maven_repository"]["after_run_sha256"].startswith("50cff19e")
    assert m["checks"]["divergences"] == []
    assert m["outcome"]["failure_rule"] == "R01" and m["outcome"]["failure_class"] is None


def test_every_null_has_a_reason(p2_run, p2_job, p2_artifact_meta):
    m = make(p2_run, p2_job, p2_artifact_meta)
    nulls = list(null_pointers(m))
    assert "/environment/vm" in nulls and "/timing/stages/4/interval" in nulls
    assert m["null_reasons"]["/timing/stages/4/interval"] == "stage disabled in this configuration"
    # Fields that collect.py of Prompt 2 did not write yet.
    assert m["null_reasons"]["/checks/tests/report_files"] == "environment.json has no /tests/report_files"


def test_null_without_reason_is_reported(p2_run, p2_job, p2_artifact_meta):
    m = make(p2_run, p2_job, p2_artifact_meta)
    del m["null_reasons"]["/environment/vm"]
    assert "null without reason at /environment/vm" in validate(m, VALIDATOR)


def test_reason_without_null_is_reported(p2_run, p2_job, p2_artifact_meta):
    m = make(p2_run, p2_job, p2_artifact_meta)
    m["null_reasons"]["/design/cell"] = "made up"
    assert "null_reasons names /design/cell, which holds no null value" in validate(m, VALIDATOR)


@pytest.mark.parametrize("pointer,value", [
    (("run", "correlation_id"), "Dryrun-1"),
    (("design", "cell"), 16),
    (("timing", "run_api", "created_at"), "2026-13-01T00:00:00Z"),
    (("outcome", "failure_class"), "flaky"),
    (("versions", "image", "digest"), "sha256:1234"),
])
def test_schema_violations_are_reported(p2_run, p2_job, p2_artifact_meta, pointer, value):
    m = make(p2_run, p2_job, p2_artifact_meta)
    node = m
    for key in pointer[:-1]:
        node = node[key]
    node[pointer[-1]] = value
    assert any(p.startswith("schema:") for p in validate(m, VALIDATOR))


def test_unknown_field_is_reported(p2_run, p2_job, p2_artifact_meta):
    m = make(p2_run, p2_job, p2_artifact_meta)
    m["run"]["extra"] = 1
    assert any("Additional properties" in p for p in validate(m, VALIDATOR))


def test_run_without_artifact_is_valid_with_reasons(p2_run, p2_job, p2_artifact_meta):
    run = dict(p2_run, conclusion="failure")
    m = make(run, p2_job, p2_artifact_meta, data=b"", job_log_meta={"path": "job-logs/x.log", "sha256": "c" * 64,
                                                                      "bytes": 10})
    assert validate(m, VALIDATOR) == []
    assert m["null_reasons"]["/run/artifact"].startswith("the run uploaded no artifact")
    assert m["null_reasons"]["/environment/cpu"] == "the run uploaded no artifact"
    assert m["timing"]["stages"] is None


def test_divergences_are_listed(p2_run, p2_job, p2_artifact_meta):
    def change(env, timing, members):
        env["tools"]["cosign"]["reported_version"] = "v3.1.4"
        env["maven"]["test"]["download_lines"] = 2
        env["run"]["run_id"] = str(p2_run["id"])
        env["run"]["correlation_id"] = "p2-test-20261014-sign"

    data = artifact_for(p2_run["id"], "p2-test-20261014-sign", SIGN, changes=change)
    run = dict(p2_run, head_sha="f" * 40)
    m = make(run, p2_job, p2_artifact_meta, data=data)
    text = "\n".join(m["checks"]["divergences"])
    assert "tool cosign: reported 'v3.1.4', pinned v3.1.3" in text
    assert "test.log: 2 download lines" in text
    assert "differs from the campaign commit" in text
    assert validate(m, VALIDATOR) == []


def test_policy_factor_is_refused_until_factor_d_exists(p2_run, p2_job, p2_artifact_meta):
    plan = copy.deepcopy(RUN_PLAN)
    plan["factors"]["policy"] = True
    with pytest.raises(Exception, match="factor D"):
        make(p2_run, p2_job, p2_artifact_meta, run_plan=plan)


def test_manifest_is_json_serializable(p2_run, p2_job, p2_artifact_meta):
    m = make(p2_run, p2_job, p2_artifact_meta)
    assert json.loads(json.dumps(m)) == m
