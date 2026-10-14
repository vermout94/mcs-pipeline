# Measurement design

This document fixes the stages of the reference pipeline, their boundaries and the parts of a run that are measured but never counted as a stage. Design approved on 2026-10-14. The workflow [`measure.yml`](../.github/workflows/measure.yml) implements the baseline and factor B on GitHub hosted runners. It refuses runs with `sbom`, `provenance` or `policy` set to `true` and runs with `runner=metal` until these parts exist.

## Workflows

| Workflow | Trigger | Job permissions | Purpose |
|---|---|---|---|
| [`measure.yml`](../.github/workflows/measure.yml) | `workflow_dispatch` | `contents: read`, `packages: write`, `id-token: write`, `attestations: write`, `artifact-metadata: write` | one run of the pipeline in one configuration |
| [`mirror-base-image.yml`](../.github/workflows/mirror-base-image.yml) | `workflow_dispatch` | `contents: read`, `packages: write` | copies the base image to GHCR once |
| [`seed-maven-repository.yml`](../.github/workflows/seed-maven-repository.yml) | `workflow_dispatch`, only on `main` | `contents: read` | creates the Maven repository snapshot |

All workflows set `permissions: {}` at the top level and grant permissions per job. They use actions of GitHub only, pinned to a full commit SHA with the version as a comment. [`tools/check_workflows.py`](../tools/check_workflows.py) compares the pins with [`tools/versions.json`](../tools/versions.json).

`measure.yml` takes the inputs `correlation_id`, `runner` (`hosted` or `metal`), `sbom`, `sign`, `provenance` and `policy`. The run name contains the correlation ID and all input values. The correlation ID has 8 to 64 characters from `a-z`, `0-9` and `-`. It tags the image and names the artifact.

The concurrency group `mcs-measure` with `cancel-in-progress: false` lets one measurement run execute at a time, across all environments. A run dispatched while another one runs waits. GitHub keeps one waiting run per concurrency group and cancels it when a further run is queued (GitHub Docs, "Concurrency"), so the orchestrator dispatches the next run only after the previous one has finished.

## Stages

A run is one job. The eight stages run in this order. Stages 1 to 4 form the baseline and run in every configuration. Stages 5 to 8 run only when their factor is on.

| # | Stage | Factor | Contains | Does not contain | Sub-times | Remote services |
|---|---|---|---|---|---|---|
| 1 | `checkout` | baseline | `actions/checkout` v7.0.1 with `fetch-depth: 1` and `persist-credentials: false`, including the start of the action by the runner | the post-job cleanup of the action | | GitHub (Git) |
| 2 | `build` | baseline | `build.maven`: `./mvnw -B -o -DskipTests -Dcyclonedx.skip=true package` (format and Checkstyle checks in `validate`, compilation of main and test classes, JAR). `build.image`: `docker buildx build` of [`docker/Dockerfile`](../docker/Dockerfile) with the correlation ID label | installation of JDK and Maven, restore of the Maven repository, pull of the base image | `build.maven`, `build.image` | none in `build.maven`. `build.image` may resolve the base image at GHCR, see below |
| 3 | `test` | baseline | `./mvnw -B -o -Dcyclonedx.skip=true -Dspring-javaformat.validate.skip=true -Dcheckstyle.skip=true '-Dtest=!MySqlIntegrationTests,!PostgresIntegrationTests' test` | compilation (the classes from `build` are up to date) | | none |
| 4 | `push` | baseline | `docker push` of the tag `<correlation ID>` to `ghcr.io/vermout94/mcs-pipeline/petclinic` | the registry login, reading the digest from the local image store | | GHCR |
| 5 | `sbom` | A | planned: Syft on the local image (CycloneDX JSON), Grype on the SBOM against a pinned database without update | installation of Syft and Grype | `sbom.generate`, `sbom.scan` | none |
| 6 | `sign` | B | `sign.tuf`: `cosign initialize`. `sign.sign`: `cosign sign --yes --oidc-provider github-actions <image>@<digest>` | installation of cosign | `sign.tuf`, `sign.sign` | Sigstore TUF repository, GitHub OIDC, the services of the Sigstore signing config, GHCR |
| 7 | `provenance` | C | planned: `actions/attest` with `subject-name` and `subject-digest` | | | GitHub OIDC, Fulcio, Rekor, GitHub API |
| 8 | `gate` | D | planned: collect the gate inputs (signature check, attestation check, scan report), then `conftest test` | installation of OPA and Conftest | `gate.verify_*`, `gate.evaluate` | GHCR, GitHub API |

All steps after `push` refer to the image by its digest. The parameters of the Maven invocations and their evidence are listed in [`app/UPSTREAM.md`](../app/UPSTREAM.md).

`build.image` runs `docker buildx build --progress=plain --provenance=false --sbom=false --load` with the build context `app/target`. [`docker/Dockerfile.dockerignore`](../docker/Dockerfile.dockerignore) reduces the context to the application JAR. The Dockerfile has no `RUN` instruction and no syntax directive, so the build runs no code and uses the frontend built into BuildKit. The image runs as uid 1000, the user `ubuntu` of the base image. The base image comes in as a build argument, `ghcr.io/vermout94/mcs-pipeline/base/eclipse-temurin@<index digest>` from `tools/versions.json`.

In `sign`, `cosign initialize` writes the TUF metadata of the Sigstore public instance to `$HOME/.sigstore/root`. The step stops before the stage if that directory exists, so the TUF metadata never comes from an earlier run. `cosign sign` takes the OIDC token of the job (`--oidc-provider github-actions`) and, with `--use-signing-config` at its default `true` in cosign v3.1.3, reads the service URLs from the signing config in the TUF repository (cosign documentation, `cosign_sign.md` at v3.1.3).

## Clock and marks

[`tools/timing.py`](../tools/timing.py) writes one JSON line per mark to `events.jsonl`. Each mark reads `CLOCK_MONOTONIC` and then `CLOCK_REALTIME`, both in nanoseconds. The helper uses the Python standard library only and runs as `python3 -I -S`.

- `timing.py run` takes the start mark, runs the command, takes the end mark and returns the exit code of the command. With `--log` the output of the command goes to a file and is printed to the job log after the end mark, with workflow commands disabled while it is printed.
- A stage with sub-times starts with the start mark of its first sub-time and ends with the end mark of its last sub-time. Between two sub-times the helper starts again. This gap belongs to the stage and appears in the data as the difference between the end of one sub-time and the start of the next.
- A failing sub-time closes its stage with its exit code.
- `checkout` and the restore of the Maven repository are `uses:` steps. Their marks sit in the steps before and after, so these intervals also contain the step change of the runner and the start of the action.
- The time between steps of different stages belongs to no stage.

The total duration of a run is the sum of the enabled stages. `timing.json` also records the span from the start of `checkout` to the end of the last enabled stage. The orchestrator adds the job times of the Jobs API (resolution 1 s).

## Preparation

Preparation intervals are measured with the same clock and never count as a stage or as overhead of a control. They run in every configuration in the same order.

| Interval | Position | Contains | Remote |
|---|---|---|---|
| `jdk` | after `checkout` | Temurin 25.0.4.1+1 tarball, SHA-256 check, unpacking | GitHub releases |
| `maven-distribution` | | Maven 3.9.16 ZIP, SHA-256 check, installation where `mvnw` looks for it | Maven Central |
| `tool.cosign`, `tool.syft`, `tool.grype`, `tool.opa`, `tool.conftest` | | binary or archive, SHA-256 check, installation into `$RUNNER_TEMP/bin` | GitHub releases |
| `maven-repository.restore` | | `actions/cache/restore` v6.1.0 with `fail-on-cache-miss: true` | GitHub cache service |
| `maven-repository.hash` | | content hash of the restored repository, compared with `tools/versions.json` | none |
| `base-image.pull` | before `build` | `docker pull` of the mirror by index digest, without login | GHCR |
| `registry-login` | between `test` and `push` | `docker login ghcr.io` with the `GITHUB_TOKEN` | GHCR |

[`tools/install.sh`](../tools/install.sh) installs every download only after the SHA-256 check against `tools/versions.json`, without retries. On AWS the golden image will contain the tools.

`app/.mvn/wrapper/maven-wrapper.properties` sets no `distributionSha256Sum`. `install.sh maven` therefore installs the checked distribution into `~/.m2/wrapper/dists/apache-maven-3.9.16/56ba1f9f`, the directory that `mvnw` 3.3.4 computes from the distribution URL. `mvnw` then starts this installation and downloads nothing. A local test on 2026-10-14 with `MVNW_VERBOSE=true` printed `found existing MAVEN_HOME` for this directory.

The registry login runs after `test`. Build and test execute third party code (Maven plugins, test dependencies), and during these stages no registry credential is on disk. The base image is pulled without login, which needs the mirror package to be public.

## Collect

The last two steps run after every outcome once the checkout succeeded (`if: always()`). [`tools/collect.py`](../tools/collect.py) writes `timing.json` (preparation intervals, the eight stages with sub-times, totals) and `environment.json`, and puts a summary table on the run page. The artifact `measure-<correlation ID>` (90 days) contains these files, `events.jsonl`, `versions.json`, the content hash of the restored Maven repository and the logs of the Maven builds, the image build, the push and the signing.

`environment.json` records:

- runner name and version, `RUNNER_ENVIRONMENT`, `ImageOS`, `ImageVersion`,
- CPU model, logical cores, hash of the CPU flags, memory, kernel, operating system, resolution of `CLOCK_MONOTONIC` and the kernel clock source,
- pinned and reported versions of all tools, Docker server version and storage driver, Buildx version,
- the SHA-256 of `tools/versions.json` and the pinned actions,
- whether `ACTIONS_ID_TOKEN_REQUEST_URL` and `ACTIONS_ID_TOKEN_REQUEST_TOKEN` exist in the job (presence only, never the values),
- image name, tag, digest and the value of the correlation ID label,
- the push log counts of pushed, existing and mounted layers,
- the lines of the BuildKit log that show how `FROM` was resolved: the vertex `load metadata for <base image>` with its duration and any `[auth]` vertex, which BuildKit prints when it requests a registry token,
- per Maven log the number of `Downloading from` and `Downloaded from` lines,
- the content hash of the Maven repository after the restore and after the run,
- test counts from the Surefire reports.

A value that cannot be read is `null`, and `null_reasons` names the reason.

## Job structure and permissions

All stages run in one job. The measured values contain no job start, every run uses one VM on AWS, and no files pass between jobs.

The job has the same permissions in every configuration: `contents: read`, `packages: write`, `id-token: write`, `attestations: write`, `artifact-metadata: write`. Constant permissions keep the permission set out of the comparison between configurations. Maven runs in `build` and `test` without the variables `ACTIONS_ID_TOKEN_REQUEST_TOKEN` and `ACTIONS_ID_TOKEN_REQUEST_URL` (`env -u`), and `actions/checkout` keeps no credential in `.git/config`. This removes the direct way for build plugins and tests to request an OIDC token or to read a token from disk. It does not isolate them from the signing credentials. The build steps and the signing share one job, which does not meet the SLSA Build L3 requirement that build steps cannot access signing material.

The L3 configuration will move the whole job into a reusable workflow.

## Maven repository snapshot

`seed-maven-repository.yml` runs on `main` and starts from an empty `~/.m2/repository`. It runs the Maven commands of `build` and `test` online, that is without `-o`, and then deletes `app/target` and runs both commands as in the measurement workflow with `-o`. The offline run must not download anything and must leave the content hash unchanged. The workflow then saves the repository with `actions/cache/save`, with `maven_repository.cache_name` from `tools/versions.json` as the cache key. It stops before the build if a cache with that key exists, so a key is never reused.

The content hash ([`tools/m2hash.py`](../tools/m2hash.py)) is the SHA-256 of the lines `<SHA-256 of the file>  <path>` over all regular files, sorted by path in byte order. It leaves out the Maven Resolver files that contain timestamps (`_remote.repositories`, `*.lastUpdated`, `resolver-status.properties`). The same value results from

```bash
cd ~/.m2/repository
find . -type f ! -name _remote.repositories ! -name '*.lastUpdated' \
  ! -name resolver-status.properties | sed 's|^\./||' | LC_ALL=C sort |
  xargs -d '\n' sha256sum | sha256sum
```

`tools/versions.json` records the cache name, the hash, the number of files, their size and the ID of the seed run. Every measurement run restores the snapshot with `fail-on-cache-miss: true` and stops if the hash differs. GitHub removes cache entries that have not been accessed for 7 days (GitHub Docs, "Dependency caching reference", usage limits and eviction policy). After a gap of more than 7 days between runs, a new seed run with a new key creates the snapshot again, and its hash is recorded with a commit. A changed hash is never accepted silently.

## Base image mirror

`mirror-base-image.yml` copies `docker.io/library/eclipse-temurin@<index digest>` with crane v0.22.1 to `ghcr.io/vermout94/mcs-pipeline/base/eclipse-temurin:25.0.4.1_1-jre-noble`. The copy contains the OCI index with all platforms and their attestation manifests, so the digest stays the same. The workflow checks the digest of the source before the copy and afterwards the index digest of the mirror, the bytes of the index manifest, the digest of every child manifest and the digest of the `linux/amd64` manifest. A second run finds the mirror and only checks it. The measurement runs pull `mirror@index digest`.

## Network dependent parts

`build.maven`, `test` and the future `sbom` run without remote access: Maven runs offline, and the Grype database will be imported in `prepare`. Whether BuildKit contacts the registry for `FROM` although the base image is present locally is recorded in every run (see Collect).

`push`, `sign`, `provenance` and `gate` contact remote services. `sign.tuf` is a separate sub-time. The requests to the GitHub OIDC endpoint, the Sigstore services and GHCR inside `cosign sign`, and all requests inside `actions/attest`, cannot be separated. The overhead of factors B and C therefore includes the network latency to Sigstore and GitHub and is reported that way. The network paths of GitHub hosted runners and of AWS differ, which is a threat to validity.

## Image digest per run

Two clean Maven builds produce a byte identical JAR, because `project.build.outputTimestamp` is set. The image gets the label `io.github.vermout94.mcs-pipeline.correlation-id` with the correlation ID of the run, so each run has its own digest, and signatures and attestations of different runs never refer to the same digest. `SOURCE_DATE_EPOCH` is not set. The build time in the image configuration and the build host in `git.properties` also differ between runs. The first test runs check that two runs get different digests.

## First run effects

Each job runs on a fresh VM, so every run is a cold start inside the job. State outside the VM persists across a campaign:

- GHCR: the first push of a campaign uploads or mounts the base layers into the repository of the application image, later pushes find them there (`push` details in `environment.json`).
- The GitHub cache service, the Sigstore TUF CDN and the hosts carry state of their own.

The manifest marks the first runs of each campaign, and the analysis checks the trend over the run number.

## Deviation from the initial stage order

The stage design of September 2026 (slide 14 of the first supervision meeting) ordered the stages Checkout, Build, Test, SBOM, Sign, Provenance, Gate, Push. In this design `push` moves to position 4:

- `cosign sign` signs an image reference in a registry and stores the signature in that registry, so the image has to be there before `sign` (checked in the first signed run),
- `actions/attest` with `push-to-registry` publishes the attestation to the registry of the image and needs its fully qualified name (README of `actions/attest` v4.2.2, section "Container Image"),
- with `push` at position 4, the baseline is the first four stages and every control adds stages after it.

The gate therefore decides after the image is published. It sets the result of the run, which in a production pipeline would decide whether the digest is promoted or deployed.

## Local checks

Before a commit that changes a workflow:

```bash
actionlint -shellcheck shellcheck   # actionlint v1.7.12, tools/versions.json dev_tools
python3 -I tools/check_workflows.py
```
