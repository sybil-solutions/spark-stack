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
PUBLIC=${STACK_PUBLIC_HOST:-$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1 || hostname -I | awk "{print \$1}")}


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
      -e STACK_HARNESS_URL="http://$PUBLIC:3080" \
      -e DSH_PATCH="$HOME/.dsh/profiles/web/cordis.patch.yml" \
      -e STACK_TOKEN="$TOKEN" \
      -e STACK_SSH_KEY="$KEY" \
      -e STACK_SSH_USER="$(basename "$HOME")" \
      -e STACK_SSH_KNOWN_HOSTS="$HOME/.ssh/known_hosts" \
      "$IMAGE"
    sleep 3
    echo "UI: http://$PUBLIC:8099/  (token: ~/spark-stack-state/token)"
    docker logs --tail 20 "$NAME"
    ;;
  logs) docker logs -f --tail 100 "$NAME" ;;
  stop) docker rm -f "$NAME" ;;
  *) echo "usage: $0 [build|run|logs|stop]" >&2; exit 2 ;;
esac
