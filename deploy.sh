#!/usr/bin/env bash
# Build the image on a Spark and run it against the host it lands on.
#   ./deploy.sh build          build the image here
#   ./deploy.sh run            start the controller (host network, host home mounted at the same path)
#   ./deploy.sh logs           follow the controller log
#   ./deploy.sh stop           remove the controller
# The controller reaches the head Spark with `ssh localhost` and the worker with `ssh $WORKER`, both using the
# host's own keys, so nothing but openssh needs to exist inside the container.
set -euo pipefail
cd "$(dirname "$0")"

NAME=${NAME:-spark-stack}
IMAGE=${IMAGE:-spark-stack:dev}
PORT=${PORT:-8099}
WORKER=${WORKER:-valentine@10.10.10.13}
# The controller holds the host's ssh keys: it may only listen on the tailnet, or on loopback for local-only runs.
BIND=${STACK_BIND:-}
if [ -z "$BIND" ]; then
  BIND=$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1 || true)
  if [ -z "$BIND" ]; then
    echo "no tailnet address: 'tailscale ip -4' returned nothing." >&2
    echo "refusing to fall back to a LAN/WAN interface; start tailscaled or set STACK_BIND to a 100.64.0.0/10 or 127.0.0.0/8 address." >&2
    exit 2
  fi
fi
case "$BIND" in
  0.0.0.0|::|"[::]") echo "refusing wildcard bind ($BIND); only tailnet (100.64.0.0/10) or loopback (127.0.0.0/8)" >&2; exit 2 ;;
esac
python3 - "$BIND" <<'PY' || { echo "refusing bind $BIND: only tailnet (100.64.0.0/10) or loopback (127.0.0.0/8) is allowed" >&2; exit 2; }
import ipaddress, sys
addr = ipaddress.ip_address(sys.argv[1])
allowed = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("127.0.0.0/8"))
sys.exit(0 if any(addr in net for net in allowed) else 1)
PY
PUBLIC=${STACK_PUBLIC_HOST:-$BIND}


case "${1:-run}" in
  build)
    docker build --platform linux/arm64 -t "$IMAGE" "$(dirname "$0")"
    ;;
  run)
    mkdir -p "$HOME/spark-stack-state"
    TOKEN_FILE="$HOME/spark-stack-state/token"
    if [ ! -s "$TOKEN_FILE" ]; then
      python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$TOKEN_FILE"
      chmod 600 "$TOKEN_FILE"
    fi
    TOKEN=$(cat "$TOKEN_FILE")
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    KEY="$HOME/.ssh/id_ed25519"
    [ -f "$KEY" ] || KEY="$HOME/.ssh/id_rsa"
    [ -f "$KEY" ] || { echo "no ssh key under $HOME/.ssh" >&2; exit 1; }
    docker run -d --name "$NAME" \
      --network host \
      --restart unless-stopped \
      -v "$HOME:$HOME" \
      -v "$HOME/spark-stack-state:/var/lib/spark-stack" \
      -e HOME="$HOME" \
      -e STACK_HEAD=localhost \
      -e STACK_HEAD_NAME="$(hostname)" \
      -e STACK_WORKER="$WORKER" \
      -e STACK_WORKER_NAME="${WORKER#*@}" \
      -e STACK_RECIPES="$HOME/spark-stack/vendor" \
      -e STACK_PUBLIC_HOST="$PUBLIC" \
      -e STACK_BIND="$BIND" \
      -e STACK_PORT="$PORT" \
      -e STACK_HARNESS_URL="http://$PUBLIC:3080" \
      -e DSH_PATCH="$HOME/.dsh/profiles/web/cordis.patch.yml" \
      -e STACK_TOKEN="$TOKEN" \
      -e STACK_SSH_KEY="$KEY" \
      -e STACK_SSH_USER="$(basename "$HOME")" \
      -e STACK_SSH_KNOWN_HOSTS="$HOME/.ssh/known_hosts" \
      "$IMAGE"
    sleep 3
    echo "UI: http://$PUBLIC:$PORT/  (bound to $BIND only, token: ~/spark-stack-state/token)"
    docker logs --tail 20 "$NAME"
    ;;
  logs) docker logs -f --tail 100 "$NAME" ;;
  stop) docker rm -f "$NAME" ;;
  *) echo "usage: $0 [build|run|logs|stop]" >&2; exit 2 ;;
esac
