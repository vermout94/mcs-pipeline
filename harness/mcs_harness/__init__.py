"""Orchestrator of the measurement campaigns of mcs-pipeline.

Plans the run order of a campaign, dispatches the measurement workflow one run
at a time through the GitHub REST API, stores the artifact of each run and
writes one manifest per run (harness/manifest.schema.json).
"""

from pathlib import Path

VERSION = "0.1.0"

HARNESS = Path(__file__).resolve().parent.parent
REPO = HARNESS.parent
DATA = REPO / "data"
MANIFEST_SCHEMA = HARNESS / "manifest.schema.json"
PLAN_SCHEMA = HARNESS / "plan.schema.json"
