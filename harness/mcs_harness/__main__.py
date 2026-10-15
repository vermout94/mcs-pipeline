"""Command line of the orchestrator.

  uv run --locked python -m mcs_harness plan <campaign>      write order.json from plan.yaml
  uv run --locked python -m mcs_harness run <campaign>       dispatch and collect, continues after a stop
  uv run --locked python -m mcs_harness status <campaign>    progress of the campaign
  uv run --locked python -m mcs_harness validate <campaign>  check all manifests against the schema

Run from harness/. <campaign> is a directory name under data/. The token comes
from GH_TOKEN, GITHUB_TOKEN or gh auth token and is never written anywhere.
Exit codes: 0 done, 1 error, 2 campaign stopped (restart continues), 130 interrupted.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import DATA, VERSION
from . import manifest as manifest_module
from .campaign import CampaignStop, Clock, Journal, Paths, Runner, create_order, status
from .github import ApiError, GitHub, TokenError, token_from_environment
from .matching import MatchError
from .plan import PlanError, load_plan

log = logging.getLogger("mcs_harness")


def setup_logging(log_file=None):
    formatter = logging.Formatter("%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    root = logging.getLogger("mcs_harness")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)


def github_for(directory):
    plan, _ = load_plan(directory / "plan.yaml")
    return GitHub(plan["repository"], token_from_environment())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mcs_harness", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["plan", "run", "status", "validate"])
    parser.add_argument("campaign")
    parser.add_argument("--data", type=Path, default=DATA, help="directory of the campaigns (default: data/)")
    args = parser.parse_args(argv)
    directory = args.data / args.campaign
    if not (directory / "plan.yaml").exists():
        print(f"{directory / 'plan.yaml'} missing", file=sys.stderr)
        return 1

    if args.command in ("plan", "run"):
        setup_logging(Paths(directory).log)
    else:
        setup_logging()

    try:
        if args.command == "plan":
            order = create_order(directory, github_for(directory))
            log.info("order.json written: campaign %s, seed %d, %d runs, commit %s",
                     order["campaign"], order["seed"], len(order["runs"]), order["repo_commit"])
            for run in order["runs"]:
                log.info("  %4d block %d  %-24s %s%s", run["position"], run["block"], run["correlation_id"],
                         run["configuration"], "  first run" if run["first_runs"] else "")
            return 0
        if args.command == "run":
            log.info("mcs_harness %s, campaign directory %s", VERSION, directory)
            Runner(directory, github_for(directory)).run()
            return 0
        if args.command == "status":
            order, rows = status(directory)
            done = sum(1 for row in rows if row[1] is not None)
            print(f"{order['campaign']}: {done} of {len(rows)} runs done, seed {order['seed']}")
            for entry, run_id, conclusion, failure_class in rows:
                print(f"  {entry['position']:4d} {entry['correlation_id']:28s} {entry['configuration']:16s} "
                      f"{run_id or '':>12} {conclusion or 'open'} {failure_class or ''}")
            return 0
        if args.command == "validate":
            paths = Paths(directory)
            validator = manifest_module.validator()
            files = sorted(paths.manifests.glob("*.json"))
            bad = 0
            for path in files:
                if path.name.endswith(".invalid.json"):
                    continue
                problems = manifest_module.validate(json.loads(path.read_text(encoding="utf-8")), validator)
                bad += bool(problems)
                print(f"{path.name}: {'valid' if not problems else 'INVALID'}")
                for problem in problems:
                    print(f"  {problem}")
            print(f"{len(files)} manifests, {bad} invalid")
            return 1 if bad else 0
    except (CampaignStop, MatchError) as error:
        log.error("campaign stopped: %s", error)
        if args.command == "run":
            Journal(Paths(directory).journal, Clock()).append("stopped", reason=str(error))
        return 2
    except (PlanError, TokenError, ApiError) as error:
        log.error("%s", error)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        if args.command == "run":
            Journal(Paths(directory).journal, Clock()).append("interrupted")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
