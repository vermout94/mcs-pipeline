import copy
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"
P2 = FIXTURES / "p2-sign"
P2_ZIP = P2 / "measure-p2-test-20261014-sign.zip"
EXAMPLE_PLAN = Path(__file__).parent.parent / "plan.example.yaml"


def load(name):
    return json.loads((P2 / name).read_text(encoding="utf-8"))


def zip_members(path=P2_ZIP):
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def make_zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


def artifact_for(run_id, correlation_id, factors, environment="hosted", changes=None):
    """Artifact of the Prompt 2 run, rewritten for another run ID and configuration."""
    members = zip_members()
    env = json.loads(members["environment.json"])
    env["run"]["run_id"] = str(run_id)
    env["run"]["correlation_id"] = correlation_id
    env["run"]["environment"] = environment
    env["image"]["tag"] = correlation_id
    env["image"]["label_correlation_id"] = correlation_id
    timing = json.loads(members["timing.json"])
    timing["factors"] = dict(factors)
    for stage in timing["stages"]:
        if stage["name"] == "sign" and not factors["sign"]:
            stage["enabled"] = False
            stage["interval"] = None
            stage["subtimes"] = []
    if changes:
        changes(env, timing, members)
    members["environment.json"] = json.dumps(env).encode()
    members["timing.json"] = json.dumps(timing).encode()
    return make_zip(members)


def write_plan(directory, **overrides):
    plan = yaml.safe_load(EXAMPLE_PLAN.read_text(encoding="utf-8"))
    plan.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.yaml").write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    return plan


@pytest.fixture
def p2_run():
    return load("run.json")


@pytest.fixture
def p2_job():
    return copy.deepcopy(load("jobs.json")["jobs"][0])


@pytest.fixture
def p2_artifact_meta():
    return load("artifacts.json")["artifacts"][0]
