#!/bin/sh
# Create the isolated netns/veth link and the initial HTB tree, then run CMD.
set -eu

echo "[entrypoint] setting up isolated Linux link..."
python -m linklab.setup_lab

exec "$@"
