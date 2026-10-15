"""Assignment of a workflow run to a correlation ID.

Three independent ways, all of which must agree:

1. dispatch_response: workflow_run_id in the response of the dispatch request.
2. run_name: the run name (display_title) of measure.yml contains the
   correlation ID and all inputs. Exactly one run of the workflow may carry
   the correlation ID, and its title must equal the title for the planned inputs.
3. artifact: environment.json in the artifact records GITHUB_RUN_ID and the
   correlation ID of the job.
"""

import re

TITLE = re.compile(
    r"^measure (?P<correlation_id>\S+) runner=(?P<runner>\S+) sbom=(?P<sbom>\S+) "
    r"sign=(?P<sign>\S+) provenance=(?P<provenance>\S+) policy=(?P<policy>\S+)$"
)


class MatchError(Exception):
    """The ways of assignment disagree. The campaign stops."""


def parse_title(title):
    match = TITLE.match(title or "")
    return match.groupdict() if match else None


def runs_with_correlation_id(runs, correlation_id):
    found = []
    for run in runs:
        parsed = parse_title(run.get("display_title"))
        if parsed and parsed["correlation_id"] == correlation_id:
            found.append(run)
    return found


def match_by_run_name(runs, correlation_id, title, dispatch_run_id=None):
    """The run with this correlation ID, None if it is not listed yet.

    Raises MatchError if several runs carry the correlation ID, if the run ID
    differs from the dispatch response or if the title differs from the plan.
    """
    found = runs_with_correlation_id(runs, correlation_id)
    if not found:
        return None
    if len(found) > 1:
        ids = sorted(run["id"] for run in found)
        raise MatchError(f"correlation ID {correlation_id} appears in {len(found)} runs: {ids}")
    run = found[0]
    if dispatch_run_id is not None and run["id"] != dispatch_run_id:
        raise MatchError(
            f"{correlation_id}: dispatch response names run {dispatch_run_id}, "
            f"the run name names run {run['id']}"
        )
    if run.get("display_title") != title:
        raise MatchError(
            f"{correlation_id}: run {run['id']} has the title {run.get('display_title')!r}, "
            f"expected {title!r}"
        )
    return run


def check_artifact(environment, run_id, correlation_id):
    """Differences between environment.json and the assigned run, empty if none."""
    problems = []
    recorded = (environment or {}).get("run") or {}
    if str(recorded.get("run_id")) != str(run_id):
        problems.append(f"environment.json records run {recorded.get('run_id')}, assigned run {run_id}")
    if recorded.get("correlation_id") != correlation_id:
        problems.append(
            f"environment.json records correlation ID {recorded.get('correlation_id')!r}, "
            f"expected {correlation_id!r}"
        )
    return problems
