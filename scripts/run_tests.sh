#!/usr/bin/env bash
# Run the test suite. Requires Python 3.11+ and Flask (`pip install -r service/requirements.txt`).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${HERE}/service${PYTHONPATH:+:${PYTHONPATH}}"
cd "${HERE}"
exec python3 -m unittest discover -s tests -v
