import json

import pytest
import synthetic

from mcs_analysis.load import DIVERGENCE, FAILED, RERUN, LoadError, load_campaign
from mcs_analysis.provenance import REPO_ROOT

SCHEMA = REPO_ROOT / "harness" / "manifest.schema.json"
CAMPAIGN = "pilot-hosted-2000-01-01"


def durations(entry):
    d = {"checkout": 1.0, "build.maven": 5.0, "build.image": 2.0, "test": 20.0, "push": 7.0}
    if entry["factors"]["sign"]:
        d |= {"sign.tuf": 0.5, "sign.sign": 4.5}
    return d


def make(entry):
    kwargs = {"durations": durations(entry), "job_s": 60.0}
    if entry["position"] == 1:
        return None
    if entry["position"] == 2:
        kwargs["failure"] = ("test", "R08", "test")
    if entry["position"] == 3:
        kwargs["divergences"] = ("reported version of cosign differs",)
    return kwargs


@pytest.fixture
def campaign_dir(tmp_path):
    return synthetic.write_campaign(tmp_path, CAMPAIGN, 3, make)


def test_load(campaign_dir):
    campaign = load_campaign(campaign_dir, SCHEMA)
    assert campaign.metrics == [
        "checkout", "build", "build.maven", "build.image", "test", "push",
        "sign", "sign.tuf", "sign.sign", "stages_sum", "job",
    ]
    assert [c.name for c in campaign.configurations] == ["baseline", "sign"]
    assert campaign.reference.name == "baseline"
    assert campaign.blocks == 3
    by_position = {r.position: r for r in campaign.runs}
    assert not by_position[1].has_manifest and not by_position[1].included
    assert by_position[2].exclusion == FAILED and by_position[2].failure_rule == "R08"
    assert by_position[3].exclusion == DIVERGENCE
    assert by_position[0].included
    assert by_position[0].metrics["build"] == pytest.approx(7.0)
    assert by_position[0].metrics["stages_sum"] == pytest.approx(35.0)
    assert len(campaign.included()) == 3
    assert "manifests/pilot-hosted-2000-01-01-0000.json" in campaign.inputs


def _edit(path, change):
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


def test_rerun_is_excluded(campaign_dir):
    path = campaign_dir / "manifests" / f"{CAMPAIGN}-0004.json"
    _edit(path, lambda d: d["run"].update(run_attempt=2))
    campaign = load_campaign(campaign_dir, SCHEMA)
    assert campaign.runs[4].exclusion == RERUN


@pytest.mark.parametrize("change", [
    lambda d: d["design"].update(configuration="sign"),
    lambda d: d["design"].update(block=2),
    lambda d: d["campaign"].update(seed=1),
    lambda d: d["run"].update(correlation_id="other"),
    lambda d: d.pop("outcome"),
])
def test_inconsistent_manifest_stops(campaign_dir, change):
    _edit(campaign_dir / "manifests" / f"{CAMPAIGN}-0000.json", change)
    with pytest.raises(LoadError):
        load_campaign(campaign_dir, SCHEMA)


def test_changed_plan_stops(campaign_dir):
    (campaign_dir / "plan.yaml").write_text("changed\n")
    with pytest.raises(LoadError):
        load_campaign(campaign_dir, SCHEMA)


def test_unplanned_manifest_stops(campaign_dir):
    (campaign_dir / "manifests" / f"{CAMPAIGN}-0099.json").write_text("{}")
    with pytest.raises(LoadError):
        load_campaign(campaign_dir, SCHEMA)
