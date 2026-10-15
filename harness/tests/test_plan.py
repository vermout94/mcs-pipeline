import re
from collections import Counter

import pytest
from conftest import load, write_plan

from mcs_harness import REPO
from mcs_harness.plan import (
    CORRELATION_ID,
    PlanError,
    cell,
    correlation_id,
    expected_title,
    load_plan,
    make_order,
    workflow_inputs,
)

FOUR = [
    {"name": "baseline", "factors": {"sbom": False, "sign": False, "provenance": False, "policy": False}},
    {"name": "sign", "factors": {"sbom": False, "sign": True, "provenance": False, "policy": False}},
    {"name": "sbom", "factors": {"sbom": True, "sign": False, "provenance": False, "policy": False}},
    {"name": "all", "factors": {"sbom": True, "sign": True, "provenance": True, "policy": True}},
]


def plan(**overrides):
    base = {
        "campaign": "pilot-hosted-2026-10-15",
        "repository": "vermout94/mcs-pipeline",
        "workflow": "measure.yml",
        "ref": "main",
        "environment": "hosted",
        "seed": 42,
        "runs_per_configuration": 10,
        "first_run_blocks": 1,
        "pause_seconds": 60,
        "poll_seconds": 10,
        "max_wait_minutes": 60,
        "stop_after_consecutive_failures": 3,
        "configurations": FOUR,
    }
    return base | overrides


def order(**overrides):
    return make_order(plan(**overrides), "0" * 64, "a" * 40, "2026-10-15T18:00:00Z")


def test_every_block_has_every_configuration_once():
    runs = order()["runs"]
    assert len(runs) == 40
    for block in range(10):
        names = [r["configuration"] for r in runs if r["block"] == block]
        assert Counter(names) == Counter(c["name"] for c in FOUR)


def test_blocks_run_one_after_the_other_and_positions_are_consecutive():
    runs = order()["runs"]
    assert [r["position"] for r in runs] == list(range(40))
    assert [r["block"] for r in runs] == sorted(r["block"] for r in runs)
    for r in runs:
        assert r["position"] == 4 * r["block"] + r["position_in_block"]


def test_same_seed_same_order_and_other_seed_other_order():
    first = [r["configuration"] for r in order()["runs"]]
    assert first == [r["configuration"] for r in order()["runs"]]
    assert first != [r["configuration"] for r in order(seed=43)["runs"]]


def test_order_within_blocks_is_shuffled():
    runs = order()["runs"]
    sequences = {tuple(r["configuration"] for r in runs if r["block"] == b) for b in range(10)}
    assert len(sequences) > 1


def test_first_runs_marks_the_first_blocks_only():
    runs = order(first_run_blocks=2)["runs"]
    assert all(r["first_runs"] == (r["block"] < 2) for r in runs)


def test_order_records_seed_commit_and_algorithm():
    o = order()
    assert o["seed"] == 42 and o["repo_commit"] == "a" * 40 and o["kind"] == "pilot"
    assert "random.shuffle" in o["algorithm"]


def test_correlation_id_pattern_equals_measure_yml():
    workflow = (REPO / ".github" / "workflows" / "measure.yml").read_text(encoding="utf-8")
    pattern = re.search(r"\$CORRELATION_ID =~ (\S+) \]\]", workflow).group(1)
    assert pattern == CORRELATION_ID.pattern


def test_correlation_ids_derive_from_campaign_and_position_and_are_unique():
    runs = order()["runs"]
    ids = [r["correlation_id"] for r in runs]
    assert len(set(ids)) == len(ids)
    assert ids[0] == "pilot-hosted-2026-10-15-0000"
    assert ids[39] == "pilot-hosted-2026-10-15-0039"
    assert all(CORRELATION_ID.match(i) for i in ids)
    assert correlation_id("dryrun", 0) == "dryrun-0000"
    assert correlation_id("campaign-2026-12-01", 9999) == "campaign-2026-12-01-9999"


def test_correlation_id_outside_the_pattern_is_refused():
    with pytest.raises(PlanError):
        correlation_id("x" * 60, 1)
    with pytest.raises(PlanError):
        correlation_id("Dryrun", 1)


def test_cell_index_uses_bit_order_a_b_c_d():
    assert cell(FOUR[0]["factors"]) == 0
    assert cell(FOUR[1]["factors"]) == 4
    assert cell(FOUR[2]["factors"]) == 8
    assert cell(FOUR[3]["factors"]) == 15


def test_title_and_inputs_match_the_run_name_of_measure_yml():
    o = order()
    run = dict(o["runs"][0], correlation_id="p2-test-20261014-sign",
               factors={"sbom": False, "sign": True, "provenance": False, "policy": False})
    assert expected_title(o, run) == load("run.json")["display_title"]
    assert workflow_inputs(o, run) == {"correlation_id": "p2-test-20261014-sign", "runner": "hosted",
                                       "sbom": "false", "sign": "true", "provenance": "false", "policy": "false"}


def test_load_plan_accepts_the_example(tmp_path):
    write_plan(tmp_path / "dryrun")
    loaded, digest = load_plan(tmp_path / "dryrun" / "plan.yaml")
    assert loaded["campaign"] == "dryrun" and len(digest) == 64


@pytest.mark.parametrize("change", [
    {"campaign": "pilot"},
    {"campaign": "dryrun-2026-10-15"},
    {"configurations": [FOUR[0], dict(FOUR[1], name="baseline")]},
    {"configurations": [FOUR[0], dict(FOUR[0], name="again")]},
    {"seed": -1},
    {"unknown": 1},
])
def test_load_plan_refuses_invalid_plans(tmp_path, change):
    write_plan(tmp_path / "data" / "dryrun", **change)
    with pytest.raises(PlanError) as error:
        load_plan(tmp_path / "data" / "dryrun" / "plan.yaml")
    assert str(error.value).startswith("data/dryrun/plan.yaml: ")
    assert str(tmp_path) not in str(error.value)
