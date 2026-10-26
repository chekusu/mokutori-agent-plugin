#!/usr/bin/env python3
"""Run all plugin tests against an isolated archive of the pinned official gateway. No production credentials."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("yobiru_repository", help="local Yobiru Git repository containing the pinned commit")
    args = parser.parse_args()
    tests = Path(__file__).resolve().parent
    pin = json.loads((tests.parent / "scripts" / "engine" / "SOURCE.json").read_text())
    archive = subprocess.check_output(["git", "-C", args.yobiru_repository, "archive", pin["commit"], "public_bots"])
    with tempfile.TemporaryDirectory(prefix="mokutori-yobiru-e2e-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            for member in source.getmembers():
                parts = Path(member.name).parts
                if not parts or parts[0] != "public_bots" or ".." in parts or not (member.isfile() or member.isdir()):
                    raise ValueError("unexpected gateway archive member")
            source.extractall(temporary)
        environment = dict(os.environ, YOBIRU_SOURCE=temporary)
        print("Testing isolated local Yobiru %s (%s)" % (pin["version"], pin["commit"]), flush=True)
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-v"], env=environment)


if __name__ == "__main__":
    sys.exit(main())
