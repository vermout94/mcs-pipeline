import pytest

from mcs_harness.matching import MatchError, check_artifact, match_by_run_name, parse_title

TITLE = "measure dryrun-0001 runner=hosted sbom=false sign=true provenance=false policy=false"


def run(run_id, title=TITLE):
    return {"id": run_id, "display_title": title, "created_at": "2026-10-15T18:00:00Z"}


def other(run_id, cid):
    return run(run_id, TITLE.replace("dryrun-0001", cid))


def test_parse_title_of_a_real_run(p2_run):
    parsed = parse_title(p2_run["display_title"])
    assert parsed == {"correlation_id": "p2-test-20261014-sign", "runner": "hosted", "sbom": "false",
                      "sign": "true", "provenance": "false", "policy": "false"}
    assert parse_title("measure") is None
    assert parse_title(None) is None


def test_run_name_and_dispatch_response_agree():
    runs = [other(1, "dryrun-0000"), run(2), other(3, "dryrun-0002")]
    assert match_by_run_name(runs, "dryrun-0001", TITLE, dispatch_run_id=2)["id"] == 2


def test_run_not_listed_yet_gives_none():
    assert match_by_run_name([other(1, "dryrun-0000")], "dryrun-0001", TITLE, 2) is None


def test_correlation_id_must_not_be_a_prefix_match():
    runs = [other(1, "dryrun-00011")]
    assert match_by_run_name(runs, "dryrun-0001", TITLE) is None


def test_two_runs_with_one_correlation_id_stop_the_campaign():
    with pytest.raises(MatchError, match="appears in 2 runs"):
        match_by_run_name([run(2), run(5)], "dryrun-0001", TITLE, 2)


def test_dispatch_response_and_run_name_disagree():
    with pytest.raises(MatchError, match="dispatch response names run 7"):
        match_by_run_name([run(2)], "dryrun-0001", TITLE, dispatch_run_id=7)


def test_title_with_other_inputs_stops_the_campaign():
    wrong = TITLE.replace("sign=true", "sign=false")
    with pytest.raises(MatchError, match="has the title"):
        match_by_run_name([run(2, wrong)], "dryrun-0001", TITLE, 2)


def test_artifact_confirms_the_run():
    env = {"run": {"run_id": "2", "correlation_id": "dryrun-0001"}}
    assert check_artifact(env, 2, "dryrun-0001") == []
    assert len(check_artifact(env, 3, "dryrun-0001")) == 1
    assert len(check_artifact(env, 2, "dryrun-0002")) == 1
    assert len(check_artifact(None, 2, "dryrun-0001")) == 2
