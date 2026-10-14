#!/usr/bin/env python3
"""Content hash of a local Maven repository.

The hash is the SHA-256 of the lines "<sha256 of the file>  <path>\\n" over all
regular files, with paths relative to the repository root and sorted in byte
order. Maven Resolver bookkeeping files that contain timestamps are left out:
_remote.repositories, *.lastUpdated and resolver-status.properties. They are
still part of the cached snapshot.

The same value with coreutils:

  cd <repository>
  find . -type f ! -name _remote.repositories ! -name '*.lastUpdated' \\
    ! -name resolver-status.properties | sed 's|^\\./||' | LC_ALL=C sort |
    xargs -d '\\n' sha256sum | sha256sum

Usage: m2hash.py <repository> [--out FILE]. Prints JSON.
"""

import argparse
import hashlib
import json
import os
import sys

EXCLUDED_NAMES = {"_remote.repositories", "resolver-status.properties"}
EXCLUDED_SUFFIXES = (".lastUpdated",)


def excluded(name):
    return name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def repository_hash(root):
    files = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            if excluded(name):
                skipped += 1
                continue
            files.append(os.path.relpath(full, root))
    files.sort(key=lambda p: p.encode("utf-8"))
    listing = hashlib.sha256()
    total = 0
    for rel in files:
        full = os.path.join(root, rel)
        listing.update(f"{file_sha256(full)}  {rel}\n".encode("utf-8"))
        total += os.path.getsize(full)
    return {
        "content_sha256": listing.hexdigest(),
        "files": len(files),
        "bytes": total,
        "excluded_files": skipped,
        "rule": "tools/m2hash.py",
    }


def main():
    parser = argparse.ArgumentParser(description="Content hash of a local Maven repository.")
    parser.add_argument("repository")
    parser.add_argument("--out")
    args = parser.parse_args()
    if not os.path.isdir(args.repository):
        sys.exit(f"m2hash.py: {args.repository} is not a directory")
    result = repository_hash(args.repository)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
