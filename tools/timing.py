#!/usr/bin/env python3
"""Time marks for the measurement workflow.

Every mark reads CLOCK_MONOTONIC and then CLOCK_REALTIME, both in nanoseconds,
and appends one JSON line to $MCS_OUT/events.jsonl. tools/collect.py turns the
events into timing.json at the end of the job.

  timing.py mark start|end prep|stage NAME [--network] [--exit-code N]
  timing.py run prep NAME [--network] [--log FILE] -- CMD...
  timing.py run stage STAGE [--sub NAME [--first] [--last]] [--network]
                            [--log FILE] -- CMD...

run takes the start mark, runs CMD, takes the end mark and exits with the exit
code of CMD. Without --sub the interval is the whole stage. With --sub it is a
sub-time of STAGE: --first also opens the stage at the start of this sub-time,
--last closes it at its end, and a failing sub-time always closes the stage.
With --log the output of CMD goes to FILE and is printed after the end mark,
so writing the job log is not part of the interval. Workflow commands in that
output are disabled while it is printed.

Uses only the standard library. Run it with python3 -I -S.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time


def now():
    mono = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    wall = time.clock_gettime_ns(time.CLOCK_REALTIME)
    return mono, wall


def events_path():
    out = os.environ.get("MCS_OUT")
    if not out:
        sys.exit("timing.py: MCS_OUT is not set")
    return os.path.join(out, "events.jsonl")


def write(path, kind, name, event, clock, exit_code=None, network=False, stage=None):
    record = {
        "kind": kind,
        "name": name,
        "event": event,
        "monotonic_ns": clock[0],
        "wall_ns": clock[1],
        "exit_code": exit_code,
        "network": network,
    }
    if stage is not None:
        record["stage"] = stage
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def print_log(name, log):
    token = secrets.token_hex(16)
    out = sys.stdout
    out.write(f"::group::{name} output\n::stop-commands::{token}\n")
    out.flush()
    with open(log, "rb") as f:
        while chunk := f.read(1 << 16):
            out.buffer.write(chunk)
    out.buffer.flush()
    out.write(f"\n::{token}::\n::endgroup::\n")
    out.flush()


def cmd_mark(args):
    path = events_path()
    write(path, args.kind, args.name, args.event, now(), args.exit_code, args.network)
    return 0


def cmd_run(args):
    command = args.command
    if not command:
        sys.exit("timing.py run: no command given after --")
    if args.kind == "prep" and (args.sub or args.first or args.last):
        sys.exit("timing.py run: --sub, --first and --last apply to stages only")
    if (args.first or args.last) and not args.sub:
        sys.exit("timing.py run: --first and --last need --sub")

    path = events_path()
    if args.sub:
        kind, name, stage = "sub", args.sub, args.name
    else:
        kind, name, stage = args.kind, args.name, None

    log = open(args.log, "wb") if args.log else None
    start = now()
    write(path, kind, name, "start", start, None, args.network, stage)
    if args.first:
        write(path, "stage", args.name, "start", start, None, args.network)
    returncode = None
    try:
        returncode = subprocess.run(
            command, stdout=log, stderr=subprocess.STDOUT if log else None
        ).returncode
    finally:
        end = now()
        code = returncode if returncode is not None else -1
        write(path, kind, name, "end", end, code, args.network, stage)
        if args.last or (args.sub and code != 0):
            write(path, "stage", args.name, "end", end, code, args.network)
        if log:
            log.close()
            print_log(name, args.log)
    if returncode < 0:
        return 128 - returncode
    return returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command_name", required=True)

    mark = sub.add_parser("mark")
    mark.add_argument("event", choices=["start", "end"])
    mark.add_argument("kind", choices=["prep", "stage"])
    mark.add_argument("name")
    mark.add_argument("--network", action="store_true")
    mark.add_argument("--exit-code", type=int)
    mark.set_defaults(func=cmd_mark)

    run = sub.add_parser("run")
    run.add_argument("kind", choices=["prep", "stage"])
    run.add_argument("name")
    run.add_argument("--sub")
    run.add_argument("--first", action="store_true")
    run.add_argument("--last", action="store_true")
    run.add_argument("--network", action="store_true")
    run.add_argument("--log")
    run.set_defaults(func=cmd_run)

    # Everything after the first "--" is the command of run.
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    args = parser.parse_args(argv)
    args.command = command
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
