#!/bin/sh
# SPDX-License-Identifier: MIT
set -eu

temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
archive="$temporary/moonraker.tar.gz"
curl --fail --location --retry 3 --output "$archive" \
    https://codeload.github.com/Arksine/moonraker/tar.gz/refs/tags/v0.11.0
python - "$archive" <<'PY'
import hashlib
from pathlib import Path
import sys
expected = "bba9042e9fc904b30cd46690320fec2bffe27c4458b539db65560582c6318ab3"
assert hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() == expected
PY
tar -xzf "$archive" -C "$temporary"
export PDM_BUILD_SCM_VERSION=0.11.0
python -m pip install "$temporary/moonraker-0.11.0"
python tests/check_moonraker.py --source "$temporary/moonraker-0.11.0"
