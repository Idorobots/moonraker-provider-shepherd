# SPDX-License-Identifier: MIT
"""Require matching Python versions and, for releases, a matching vVERSION tag."""
import argparse
import ast
from pathlib import Path
import tomllib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--tag")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
tree = ast.parse((root / "moonraker_provider_shepherd/__init__.py").read_text())
python_version = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "__version__"
                              for target in node.targets))
assert python_version == version, "Python package versions differ"
if args.tag:
    assert args.tag == f"v{version}", f"Expected tag v{version}, got {args.tag}"
print(version)
