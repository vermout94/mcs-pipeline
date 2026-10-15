import re
import subprocess

from mcs_analysis import provenance
from mcs_analysis.provenance import describe, git_state


def test_state_of_the_checkout_has_the_format():
    state = git_state()
    assert state["commit"] is None or re.fullmatch(r"[0-9a-f]{40}", state["commit"])
    assert state["dirty"] in (True, False, None)
    if state["commit"] is not None and state["dirty"] is not None:
        assert state["reason"] is None


def test_outside_a_git_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "REPO_ROOT", tmp_path)
    state = git_state()
    assert state == {"commit": None, "dirty": None, "reason": "not a Git repository"}
    assert str(tmp_path) not in state["reason"]


def test_git_not_available(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(provenance.subprocess, "run", missing)
    assert git_state() == {"commit": None, "dirty": None, "reason": "git is not available"}


def test_repository_without_commit(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(provenance, "REPO_ROOT", tmp_path)
    state = git_state()
    assert state["commit"] is None and state["dirty"] is False
    assert state["reason"] == "the repository has no commit"


def test_describe():
    assert describe({"commit": "a" * 40, "dirty": False, "reason": None}) == ("a" * 40, "false")
    assert describe({"commit": "a" * 40, "dirty": True, "reason": None}) == ("a" * 40, "true")
    assert describe({"commit": None, "dirty": None, "reason": "x"}) == ("unknown", "unknown")
