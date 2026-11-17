# SPDX-License-Identifier: MIT
"""Test the patch and HTTP API against Moonraker 0.11.0.

Usage: python3 tests/check_moonraker.py /gnu/store/...-moonraker-0.11.0
   or: python3 tests/check_moonraker.py --source /path/to/moonraker-0.11.0
The source form uses the current interpreter and its installed dependencies.
All patching and runtime state are confined to a temporary directory.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", nargs="?", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if bool(args.package) == bool(args.source):
        parser.error("Specify either a Guix package or --source")
    env = os.environ.copy()
    if args.package:
        package = args.package.resolve()
        site = next(package.glob("lib/python*/site-packages"))
        wrapper = (package / "bin/moonraker").read_text()
        dependencies = re.search(r'^export GUIX_PYTHONPATH="([^"$]+)', wrapper, re.M).group(1).rstrip(":")
        python = shlex.split((package / "bin/.moonraker-real").read_text().splitlines()[0].removeprefix("#!"))
        env["GUIX_PYTHONPATH"] = dependencies
    else:
        site = args.source.resolve()
        python = [sys.executable]
        dependencies = env.get("PYTHONPATH", "")
    with tempfile.TemporaryDirectory(prefix="moonraker-patch-") as directory:
        source = Path(directory)
        shutil.copytree(site / "moonraker", source / "moonraker")
        for path in (source / "moonraker").rglob("*"):
            path.chmod(path.stat().st_mode | 0o200)
            if path.suffix == ".pyc":
                path.unlink()
        (source / "moonraker").chmod(0o755)
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i",
                        str(ROOT / "patches/moonraker-0.11.0-shepherd.patch")],
                       cwd=source, check=True)
        env.update(PYTHONPATH=os.pathsep.join((str(source), str(ROOT), str(ROOT / "tests"), dependencies)),
                   MOONRAKER_SOURCE=str(source),
                   PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([*python, str(ROOT / "tests/compatibility.py")], env=env, check=True)
        subprocess.run([*python, str(ROOT / "tests/integration.py")], env=env, check=True)


if __name__ == "__main__":
    main()
