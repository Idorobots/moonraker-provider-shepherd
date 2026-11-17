# SPDX-License-Identifier: MIT
"""Build from a Git-free source tree and verify the complete release contents."""
from contextlib import chdir
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile

from setuptools import build_meta

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix="shepherd-distribution-") as directory:
    temporary = Path(directory)
    source = temporary / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.egg-info", "build", "dist", "*.log"))
    output = temporary / "dist"
    output.mkdir()
    os.environ["SOURCE_DATE_EPOCH"] = "1767225600"
    with chdir(source):
        sdist = build_meta.build_sdist(str(output))
    with tarfile.open(output / sdist) as archive:
        names = archive.getnames()
        for name in ("LICENSE", "scheme/moonraker/shepherd.scm",
                     "patches/moonraker-0.11.0-shepherd.patch", "tests/helper.scm"):
            assert any(path.endswith("/" + name) for path in names), name
        extracted = temporary / "extracted"
        archive.extractall(extracted, filter="data")
    with chdir(next(extracted.iterdir())):
        wheel = build_meta.build_wheel(str(output))
    with zipfile.ZipFile(output / wheel) as archive:
        names = archive.namelist()
        for name in ("moonraker_provider_shepherd/provider.py",
                     "scheme/moonraker/shepherd.scm", "bin/moonraker-shepherd",
                     "patches/moonraker-0.11.0-shepherd.patch", "examples/policy.scm"):
            assert any(path.endswith(name) for path in names), name
        metadata = archive.read(next(name for name in names if name.endswith("/METADATA"))).decode()
        assert "License-Expression: MIT" in metadata
    print("Git-free source archive and wheel contents verified")
