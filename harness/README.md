# Orchestrator

The orchestrator runs a campaign of the measurement workflow [`measure.yml`](../.github/workflows/measure.yml). It writes the run order, dispatches one run at a time through the GitHub REST API (version 2026-03-10), stores the artifact of every run unchanged and writes one manifest per run ([`manifest.schema.json`](manifest.schema.json)). It runs outside GitHub Actions. The measurement design, the assignment of runs and the failure classes are described in [`docs/measurement.md`](../docs/measurement.md).

## Setup

Python 3.13.7 ([`.python-version`](.python-version)) with [uv](https://docs.astral.sh/uv/). [`uv.lock`](uv.lock) pins every dependency with its SHA-256: certifi, jsonschema and PyYAML, and pytest for the tests. The REST client uses the Python standard library and checks TLS certificates against the CA bundle of certifi.

```bash
cd harness
uv sync --locked
uv run --locked pytest
```

The token comes from `GH_TOKEN` or `GITHUB_TOKEN`, otherwise from `gh auth token`. A classic token needs the scope `repo` for the dispatch endpoint. The orchestrator never writes the token to a log, a file or an exception message.

## A campaign

1. Copy [`plan.example.yaml`](plan.example.yaml) to `data/<campaign>/plan.yaml` and set the values. [`plan.schema.json`](plan.schema.json) describes every field. The campaign ID is `campaign-YYYY-MM-DD`, `pilot-hosted-YYYY-MM-DD`, `pilot-metal-YYYY-MM-DD` or `dryrun`, and it equals the directory name.
2. Write the run order. This reads the head of the branch and stores it with seed and order in `order.json`. The order is written once and never drawn again.

   ```bash
   uv run --locked python -m mcs_harness plan <campaign>
   ```

3. Run the campaign. After a stop or an interruption the same command continues with the first run without manifest.

   ```bash
   uv run --locked python -m mcs_harness run <campaign>
   ```

4. Check the progress and the manifests.

   ```bash
   uv run --locked python -m mcs_harness status <campaign>
   uv run --locked python -m mcs_harness validate <campaign>
   ```

For every campaign except `dryrun`, `run` stops before the first dispatch if `harness/` has uncommitted changes. Commit the orchestrator first.

While a campaign runs, nothing is pushed to its branch (`ref` in `plan.yaml`). Every run is dispatched on the head of that branch, and the orchestrator checks after each run that it ran the commit in `order.json`. A push changes the commit of the next run: the orchestrator writes the manifest of that run and stops, and a restart stops again, because every later run also runs the new commit.

Exit codes of `run`: 0 when all runs are done, 2 when the orchestrator stopped the campaign (the log names the reason, a restart continues), 130 after Ctrl-C, 1 for other errors.

## Files of a campaign

| Path under `data/<campaign>/` | Written by | Content |
|---|---|---|
| `plan.yaml` | hand | configurations, runs per configuration, environment, seed, pause |
| `order.json` | `plan` | seed, order, commit of the branch, SHA-256 of `plan.yaml` |
| `journal.jsonl` | `plan`, `run` | one JSON line per event (dispatch, completion, collection, stop), the state for a restart |
| `orchestrator.log` | `plan`, `run` | log of the orchestrator |
| `manifests/<correlation ID>.json` | `run` | manifest of the run |
| `artifacts/measure-<correlation ID>.zip` | `run` | artifact of the run as downloaded, SHA-256 equal to the digest in the REST API, not versioned (`.gitignore`) |
| `job-logs/<correlation ID>.log` | `run` | job log as downloaded, only for runs that did not succeed, not versioned (`.gitignore`) |

`data/dryrun/` is not versioned either. Raw data is never overwritten. If a file exists with other content, the orchestrator stops. The log and the journal name these files relative to the repository (`data/<campaign>/…`). A network error in the log names its cause and the time the attempt took, never the token.

## Modules

| Module | Purpose |
|---|---|
| [`plan.py`](mcs_harness/plan.py) | plan, run order, correlation IDs, workflow inputs |
| [`github.py`](mcs_harness/github.py) | REST client: dispatch, workflow runs, jobs, artifacts, job logs |
| [`matching.py`](mcs_harness/matching.py) | assignment of a run to a correlation ID |
| [`artifact.py`](mcs_harness/artifact.py) | read access to the artifact ZIP file |
| [`classify.py`](mcs_harness/classify.py) | failure classes (RQ2) |
| [`manifest.py`](mcs_harness/manifest.py) | manifest, null reasons, validation |
| [`campaign.py`](mcs_harness/campaign.py) | campaign loop, journal, restart |

The tests in [`tests/`](tests/) use the artifact and the REST API answers of the test run 38638959857 of 2026-10-14 ([`tests/fixtures/p2-sign/`](tests/fixtures/p2-sign/)), a local HTTP server for the REST client and a simulated REST API for the campaign loop.
