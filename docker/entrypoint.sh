#!/bin/sh
# Start the harness beside the control plane when a dsh home is mounted, then hand the container to uvicorn.
set -eu

if [ "${DSH_AUTOSTART:-0}" = "1" ] && [ -d "${DSH_HOME:-/nonexistent}" ]; then
  # The harness app is the profile's own web app; its port comes from that profile.
  ( dsh web >/var/lib/spark-stack/dsh.log 2>&1 & ) || echo "dsh did not start; see /var/lib/spark-stack/dsh.log" >&2
fi

exec uvicorn stack.app:app --host 0.0.0.0 --port "${STACK_PORT:-8099}"
