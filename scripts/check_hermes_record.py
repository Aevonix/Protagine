#!/usr/bin/env python3
"""Verify that an installed Hermes is byte-identical to its wheel RECORD.

``pip install`` writes ``hermes_agent-<version>.dist-info/RECORD`` with the
sha256 of every installed file. A Protagine install must leave all of them
untouched: no patch, no prepared runtime. Run this inside Hermes' Python::

    <hermes python> scripts/check_hermes_record.py

Exit status 0 means every recorded file still matches; 1 lists the files that
changed or vanished. Compiled ``.pyc`` files and the RECORD itself carry no
hash in RECORD and are skipped. Hermes' own installer uses an editable checkout
(its ``setup.py`` refuses to build wheels); for such an install the check is
that the checkout is clean, which the script reports with its commit.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata as metadata
import sys
from pathlib import Path


def _is_editable(dist) -> bool:
    import json
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return False
    return bool((origin.get("dir_info") or {}).get("editable"))


def check_editable_checkout(dist) -> int:
    import json
    import subprocess
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        origin = {}
    url = str(origin.get("url", ""))
    if not url.startswith("file://"):
        print("hermes-agent has no RECORD and no local checkout to verify")
        return 1
    checkout = Path(url[len("file://"):])
    status = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
                            capture_output=True, text=True, check=False)
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if status.returncode != 0 or head.returncode != 0:
        print(f"{checkout} is not a version-controlled checkout; cannot verify it is unmodified")
        return 1
    if status.stdout.strip():
        print(status.stdout.rstrip())
        print(f"hermes-agent {dist.version}: the checkout at {checkout} carries local modifications")
        return 1
    print(f"hermes-agent {dist.version}: checkout {checkout} is unmodified at {head.stdout.strip()}")
    return 0


def main() -> int:
    try:
        dist = metadata.distribution("hermes-agent")
    except metadata.PackageNotFoundError:
        print("hermes-agent is not installed in", sys.executable)
        return 1
    record = dist.read_text("RECORD")
    if not record or _is_editable(dist):
        # Hermes ships as a checkout installed editably (its setup.py refuses to
        # build wheels), so "unmodified" means a clean tree at the expected commit.
        return check_editable_checkout(dist)
    root = Path(str(dist.locate_file("")))
    changed: list[str] = []
    checked = 0
    for row in csv.reader(record.splitlines()):
        if len(row) < 2 or not row[1]:
            continue
        algorithm, _, expected = row[1].partition("=")
        if algorithm != "sha256":
            continue
        path = root / row[0]
        if not path.is_file():
            changed.append(f"missing: {row[0]}")
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=").decode()
        checked += 1
        if digest != expected:
            changed.append(f"modified: {row[0]}")
    if changed:
        print("\n".join(changed))
        print(f"{len(changed)} of {checked + len(changed)} recorded files differ from the wheel")
        return 1
    print(f"hermes-agent {dist.version}: {checked} files match the wheel RECORD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
