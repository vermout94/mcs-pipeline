"""Plan of a campaign (plan.yaml) and its randomized block order (order.json).

The order follows a randomized complete block design: every block contains
each configuration exactly once, in a random order of its own, and the blocks
run one after the other. random.Random(seed) draws the order (Mersenne Twister,
random.shuffle). order.json stores seed and order before the first run, so a
restart never draws again.
"""

import hashlib
import json
import platform
import random
import re

import jsonschema
import yaml

from . import PLAN_SCHEMA

# Same pattern as the input check in .github/workflows/measure.yml.
CORRELATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")

# Factor order A B C D, also the bit order of the cell index.
FACTORS = ("sbom", "sign", "provenance", "policy")

ORDER_ALGORITHM = (
    "random.Random(seed); for each block in turn, random.shuffle of the "
    "configuration indices in plan order"
)


class PlanError(Exception):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def campaign_kind(campaign):
    if campaign.startswith("campaign"):
        return "campaign"
    if campaign.startswith("pilot-"):
        return "pilot"
    return "dryrun"


def cell(factors):
    """Index in the 2^4 plan: 8 A + 4 B + 2 C + D."""
    return sum(8 >> i for i, name in enumerate(FACTORS) if factors[name])


def correlation_id(campaign, position):
    cid = f"{campaign}-{position:04d}"
    if not CORRELATION_ID.match(cid):
        raise PlanError(f"correlation ID {cid!r} does not match {CORRELATION_ID.pattern}")
    return cid


def load_plan(path):
    """Plan as dict and the SHA-256 of the file."""
    data = path.read_bytes()
    plan = yaml.safe_load(data)
    schema = json.loads(PLAN_SCHEMA.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(plan, schema, cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as error:
        raise PlanError(f"{path}: {error.message} at {error.json_path}") from None
    if path.parent.name != plan["campaign"]:
        raise PlanError(f"{path}: campaign {plan['campaign']!r} differs from the directory name")
    names = [c["name"] for c in plan["configurations"]]
    if len(set(names)) != len(names):
        raise PlanError(f"{path}: configuration names are not unique")
    cells = [cell(c["factors"]) for c in plan["configurations"]]
    if len(set(cells)) != len(cells):
        raise PlanError(f"{path}: two configurations have the same factors")
    return plan, sha256_bytes(data)


def make_order(plan, plan_sha256, repo_commit, created_at):
    rng = random.Random(plan["seed"])
    configurations = plan["configurations"]
    runs = []
    for block in range(plan["runs_per_configuration"]):
        indices = list(range(len(configurations)))
        rng.shuffle(indices)
        for position_in_block, index in enumerate(indices):
            configuration = configurations[index]
            position = len(runs)
            runs.append(
                {
                    "position": position,
                    "block": block,
                    "position_in_block": position_in_block,
                    "configuration": configuration["name"],
                    "factors": dict(configuration["factors"]),
                    "cell": cell(configuration["factors"]),
                    "correlation_id": correlation_id(plan["campaign"], position),
                    "first_runs": block < plan["first_run_blocks"],
                }
            )
    return {
        "schema": "mcs-pipeline order 1",
        "campaign": plan["campaign"],
        "kind": campaign_kind(plan["campaign"]),
        "plan_sha256": plan_sha256,
        "seed": plan["seed"],
        "algorithm": ORDER_ALGORITHM,
        "python": platform.python_version(),
        "created_at": created_at,
        "repository": plan["repository"],
        "workflow": plan["workflow"],
        "ref": plan["ref"],
        "repo_commit": repo_commit,
        "environment": plan["environment"],
        "runs": runs,
    }


def workflow_inputs(order, run):
    """Inputs of measure.yml for one run. Strings, as the REST API passes them on."""
    inputs = {"correlation_id": run["correlation_id"], "runner": order["environment"]}
    for name in FACTORS:
        inputs[name] = "true" if run["factors"][name] else "false"
    return inputs


def expected_title(order, run):
    """Run name of measure.yml for these inputs (run-name in the workflow)."""
    inputs = workflow_inputs(order, run)
    return (
        f"measure {inputs['correlation_id']} runner={inputs['runner']} "
        f"sbom={inputs['sbom']} sign={inputs['sign']} "
        f"provenance={inputs['provenance']} policy={inputs['policy']}"
    )
