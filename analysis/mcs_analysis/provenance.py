"""Commit, state of analysis/, lock settings and library versions."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import subprocess
import tomllib
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ANALYSIS_DIR.parent
LIBRARIES = ("jsonschema", "matplotlib", "numpy", "scipy")


UNKNOWN = "unknown"


def _git(*args: str) -> tuple[str | None, str | None]:
    """Output of a git command, or None and the reason why there is none."""
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, check=True,
        )
    except OSError:
        return None, "git is not available"
    except subprocess.CalledProcessError as error:
        # Fixed reasons only: the messages of git can contain paths.
        message = (error.stderr or "").lower()
        if "not a git repository" in message:
            return None, "not a Git repository"
        if args[0] == "rev-parse" and ("unknown revision" in message or "ambiguous argument" in message):
            return None, "the repository has no commit"
        return None, f"git {args[0]} failed with exit code {error.returncode}"
    return done.stdout, None


def git_state() -> dict:
    """HEAD of the repository and uncommitted changes in analysis/.

    commit is None and dirty is None if git cannot tell, with the reason.
    """
    head, head_reason = _git("rev-parse", "HEAD")
    status, status_reason = _git("status", "--porcelain", "--untracked-files=all", "--", "analysis")
    commit = head.strip() if head else None
    dirty = None if status is None else bool(status.strip())
    reasons = list(dict.fromkeys(r for r in (head_reason, status_reason) if r))
    return {"commit": commit, "dirty": dirty, "reason": ", ".join(reasons) or None}


def describe(git: dict) -> tuple[str, str]:
    """Commit and state of analysis/ as written in the headers of the outputs."""
    commit = git["commit"] or UNKNOWN
    dirty = UNKNOWN if git["dirty"] is None else str(git["dirty"]).lower()
    return commit, dirty


def uv_version() -> str | None:
    """Version of the uv that runs the analysis ('uv 0.13.1'), without paths."""
    binary = os.environ.get("UV") or shutil.which("uv")
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    words = out.split()
    return " ".join(words[:2]) if len(words) >= 2 else None


def lock_settings() -> dict:
    pyproject = tomllib.loads((ANALYSIS_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    return {"exclude_newer": pyproject["tool"]["uv"]["exclude-newer"]}


def environment() -> dict:
    return {
        "python": platform.python_version(),
        "uv": uv_version(),
        "libraries": {name: importlib.metadata.version(name) for name in LIBRARIES},
    }
