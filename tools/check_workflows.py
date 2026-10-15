#!/usr/bin/env python3
"""Checks the workflows against tools/versions.json.

1. Every uses: refers to an action of GitHub (actions/*) by a full commit SHA
   with the version as a comment, and SHA and version equal the pin in
   tools/versions.json.
2. The seed workflow runs the Maven commands of the stages build and test:
   online without -o, and offline exactly as in the measurement workflow.
3. The default of ARG BASE_IMAGE in docker/Dockerfile is the mirror name and
   digest of the base image in tools/versions.json.

Exit code 1 on any finding. Uses only the standard library.
"""

import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^(actions/[A-Za-z0-9_.-]+)(/[A-Za-z0-9_./-]+)?@([0-9a-f]{40})$")
COMMENT = re.compile(r"^\s*#\s*(v\S+)\s*$")
MVNW = re.compile(r"\./mvnw( [^\n]*?)\s*$", re.M)
BASE_ARG = re.compile(r"^ARG BASE_IMAGE(?:=(\S*))?\s*$", re.M)


def check_actions(pins, findings):
    count = 0
    for path in sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml"))):
        rel = os.path.relpath(path, REPO)
        with open(path, encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                match = USES.match(line)
                if not match:
                    continue
                count += 1
                ref, rest = match.groups()
                pinned = PINNED.match(ref)
                if not pinned:
                    findings.append(f"{rel}:{number}: {ref} is not actions/* at a full commit SHA")
                    continue
                repo, _, sha = pinned.groups()
                pin = pins.get(repo)
                if pin is None:
                    findings.append(f"{rel}:{number}: {repo} has no pin in tools/versions.json")
                    continue
                if sha != pin["sha"]:
                    findings.append(f"{rel}:{number}: {repo} SHA {sha} differs from pin {pin['sha']}")
                comment = COMMENT.match(rest)
                if not comment or comment.group(1) != pin["version"]:
                    findings.append(f"{rel}:{number}: {repo} needs the comment # {pin['version']}")
    return count


def maven_commands(name):
    with open(os.path.join(REPO, ".github", "workflows", name), encoding="utf-8") as f:
        return [" ".join(m.split()) for m in MVNW.findall(f.read())]


def check_maven(findings):
    measure = maven_commands("measure.yml")
    seed = maven_commands("seed-maven-repository.yml")
    if len(measure) != 2:
        findings.append(f"measure.yml: expected 2 Maven commands, found {len(measure)}")
        return
    for command in measure:
        if " -o " not in f"{command} ":
            findings.append(f"measure.yml: Maven command without -o: {command}")
    online = [c.replace(" -o ", " ", 1) for c in measure]
    if seed != online + measure:
        findings.append(
            "seed-maven-repository.yml: Maven commands must be the measurement commands "
            "without -o, then the same commands with -o\n"
            f"  expected: {online + measure}\n  found:    {seed}"
        )


def check_dockerfile(versions, findings):
    mirror = versions["images"]["base"]["mirror"]
    expected = f"{mirror['name']}@{mirror['digest']}"
    with open(os.path.join(REPO, "docker", "Dockerfile"), encoding="utf-8") as f:
        defaults = BASE_ARG.findall(f.read())
    if defaults != [expected]:
        findings.append(
            f"docker/Dockerfile: ARG BASE_IMAGE needs the default {expected}, found {defaults}"
        )


def main():
    with open(os.path.join(REPO, "tools", "versions.json"), encoding="utf-8") as f:
        versions = json.load(f)
    pins = {k: v for k, v in versions["actions"].items() if k != "note"}
    findings = []
    count = check_actions(pins, findings)
    check_maven(findings)
    check_dockerfile(versions, findings)
    for finding in findings:
        print(finding)
    print(f"check_workflows: {count} uses: lines, {len(findings)} findings")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
