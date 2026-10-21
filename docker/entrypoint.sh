#!/bin/sh
# One tailnet or loopback interface only: the controller holds the host's ssh key, so it must never answer on LAN/WAN.
set -eu
: "${STACK_BIND:=127.0.0.1}"
case "$STACK_BIND" in
  0.0.0.0|::|"[::]") echo "refusing wildcard bind ($STACK_BIND); only tailnet (100.64.0.0/10) or loopback (127.0.0.0/8)" >&2; exit 2 ;;
esac
python3 - "$STACK_BIND" <<'PY' || { echo "refusing bind $STACK_BIND: only tailnet (100.64.0.0/10) or loopback (127.0.0.0/8) is allowed" >&2; exit 2; }
import ipaddress, sys
addr = ipaddress.ip_address(sys.argv[1])
allowed = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("127.0.0.0/8"))
sys.exit(0 if any(addr in net for net in allowed) else 1)
PY

exec uvicorn stack.app:app --host "$STACK_BIND" --port "${STACK_PORT:-8099}"
