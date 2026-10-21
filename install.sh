#!/usr/bin/env bash
# Spark Stack: one command sets up one or two DGX Sparks for local AI.
#
#   curl -fsSL https://raw.githubusercontent.com/sybil-solutions/spark-stack/main/install.sh | bash
#
# Run it on the Spark you sit at (it becomes the "head"), as your normal desktop user. It asks for your password once.
# With a QSFP cable to a second Spark (same username and password on both), it finds that Spark on the cable and sets it
# up too. Safe to run again: it repairs whatever is missing and changes nothing that is already right.
#
# It installs: the Spark Stack app (download/start/stop models, http://<tailnet-ip>:8099), the DeepSeek harness and
# OpenCode (both pinned to the dock, both already pointed at the local models), and starts downloading
# Qwen3.8-Flash-Next. It also turns off sleep, addresses the CX7 link, and shares the model cache with the second Spark.
#
# Options (environment variables):
#   WORKER=user@host   use this second Spark instead of finding it on the cable
#   SINGLE=1           set up this Spark only
#   NO_DOWNLOAD=1      do not start the first model download
#   REF=<branch|tag>   spark-stack version to install (default: main)
set -euo pipefail

REPO=https://github.com/sybil-solutions/spark-stack.git
REF=${REF:-main}
STACK_DIR=${STACK_DIR:-$HOME/spark-stack}
STATE=$HOME/spark-stack-state
PORT=8099
IMAGE=spark-stack:local
NODE_VERSION=v22.23.4
DSH_VERSION=0.2.1-rc.2
OPENCODE_VERSION=v1.19.3
HEAD_IPS=(192.168.100.10 192.168.101.10)
WORKER_IPS=(192.168.100.11 192.168.101.11)
U=$(id -un)
SSHO=(-o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new)

n=0
step() { n=$((n + 1)); printf '\n\033[1m[%s/9] %s\033[0m\n' "$n" "$*"; }
info() { printf '  %s\n' "$*"; }
die() { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal desktop user, not root (it asks for your password)"
[ "$(uname -m)" = aarch64 ] || die "this installer is for DGX Spark / GB10 systems (arm64)"
for c in nvidia-smi docker git curl python3 ssh; do command -v "$c" >/dev/null || die "$c is missing"; done
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
[ -n "$TS_IP" ] || die "Tailscale is not connected. Run 'sudo tailscale up' first (see the guide, section Tailscale)."

# ------------------------------------------------------------------------------------------------ password
step "Your password"
PW=""
if sudo -n true 2>/dev/null; then
  info "sudo works without a password here"
else
  read -rsp "  Password for $U (the same on both Sparks): " PW </dev/tty; echo
  printf '%s\n' "$PW" | sudo -S -p '' -v 2>/dev/null || die "that password did not work for sudo"
fi
( while kill -0 $$ 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done ) &
ASKPASS=$(mktemp); trap 'rm -f "$ASKPASS"' EXIT
printf '#!/bin/sh\nprintf "%%s\\n" "$SPARK_PW"\n' > "$ASKPASS"; chmod 700 "$ASKPASS"
export SPARK_PW="$PW"

# ------------------------------------------------------------------------------------------------ code
step "Spark Stack code ($REF)"
if [ -d "$STACK_DIR/.git" ]; then
  git -C "$STACK_DIR" fetch -q origin "$REF" && git -C "$STACK_DIR" checkout -q -B "$REF" FETCH_HEAD
else
  git clone -q --branch "$REF" "$REPO" "$STACK_DIR"
fi
info "$STACK_DIR at $(git -C "$STACK_DIR" rev-parse --short HEAD)"
registry() { (cd "$STACK_DIR" && python3 -c "from stack.registry import MODELS, DEFAULT_MODEL
$1"); }

# ------------------------------------------------------------------------------------------------ second Spark
step "Second Spark"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519 -C "$U@$(hostname) spark-stack"
PUB=$(cat ~/.ssh/id_ed25519.pub)

link_peer() {   # the IPv6 link-local address of whatever is on the other end of the CX7 cable
  local ifc
  ifc=$(ibdev2netdev 2>/dev/null | awk '/\(Up\)/ {print $5}' | sort | head -1)
  [ -n "$ifc" ] || return 0
  ping -6 -c 2 -W 1 "ff02::1%$ifc" >/dev/null 2>&1 || true
  ip -6 neigh show dev "$ifc" | awk '/^fe80/ && !/FAILED/ {print $1; exit}' | sed "s/\$/%$ifc/"
}

BOOT=""   # how we reach the second Spark before its CX7 address exists
if [ "${SINGLE:-0}" != 1 ]; then
  if [ -n "${WORKER:-}" ]; then
    BOOT=$WORKER
  else
    LL=$(link_peer)
    if [ -n "$LL" ]; then
      info "found a Spark on the cable ($LL)"
      if ssh "${SSHO[@]}" "$U@$LL" true 2>/dev/null; then
        BOOT="$U@$LL"
      else
        # Tailscale SSH needs no password: find the tailnet peer that owns that link-local address.
        for peer in $(tailscale status --json 2>/dev/null | python3 -c 'import sys,json
d=json.load(sys.stdin)
print(" ".join(p["DNSName"].rstrip(".") for p in (d.get("Peer") or {}).values() if p.get("Online") and p.get("OS")=="linux"))'); do
          if ssh "${SSHO[@]}" -o ConnectTimeout=4 "$U@$peer" "ip -6 addr | grep -q '${LL%\%*}'" 2>/dev/null; then BOOT="$U@$peer"; break; fi
        done
        [ -n "$BOOT" ] || BOOT="$U@$LL"
      fi
    else
      info "no Spark on the CX7 cable: setting up this Spark only"
    fi
  fi
fi

wsudo() {   # run a root script on the second Spark: sudo -n when it needs no password, else feed it the password
  if ssh "${SSHO[@]}" "$BOOT" sudo -n true 2>/dev/null; then
    ssh "${SSHO[@]}" "$BOOT" "sudo -n bash -s -- $*"
  else
    [ -n "$PW" ] || die "the second Spark needs a sudo password; run this installer from a terminal"
    { printf '%s\n' "$PW"; cat; } | ssh "${SSHO[@]}" "$BOOT" "sudo -S -p '' bash -s -- $*"
  fi
}

WORKER_TARGET=""
if [ -n "$BOOT" ]; then
  if ! ssh "${SSHO[@]}" "$BOOT" true 2>/dev/null; then
    [ -n "$PW" ] || die "cannot ssh to $BOOT without a password; run: ssh-copy-id $BOOT"
    info "copying this Spark's ssh key to $BOOT (uses your password)"
    SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=${DISPLAY:-:0} \
      ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no "$BOOT" \
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" </dev/null \
      || die "could not log in to $BOOT with your password"
  fi
  # Key-based ssh over the real sshd too (Tailscale SSH does not cover the CX7 address).
  ssh "${SSHO[@]}" "$BOOT" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && (grep -qxF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys) && chmod 600 ~/.ssh/authorized_keys"
  WORKER_HOST=$(ssh "${SSHO[@]}" "$BOOT" hostname)
  [ "$WORKER_HOST" != "$(hostname)" ] || die "the second Spark has the same hostname as this one"
  info "second Spark: $WORKER_HOST"
  WORKER_TARGET="$U@${WORKER_IPS[0]}"
fi

# ------------------------------------------------------------------------------------------------ both nodes
step "System setup (sleep off, docker, CX7 link, model sharing)"
if [ -n "$BOOT" ]; then
  wsudo "$U worker ${WORKER_IPS[*]}" < "$STACK_DIR/scripts/node-setup.sh"
  printf '%s\n' "$PW" | sudo -S -p '' bash "$STACK_DIR/scripts/node-setup.sh" "$U" head "${HEAD_IPS[@]}" "${WORKER_IPS[@]}"
  for i in $(seq 1 20); do ping -c1 -W1 "${WORKER_IPS[0]}" >/dev/null 2>&1 && break; sleep 1; done
  ssh "${SSHO[@]}" "$WORKER_TARGET" true || die "ssh to the second Spark over the CX7 link ($WORKER_TARGET) failed"
  ssh "${SSHO[@]}" "$WORKER_TARGET" "ssh-keyscan -q ${HEAD_IPS[0]} >> ~/.ssh/known_hosts 2>/dev/null; true"
  info "CX7 link up: $(hostname)=${HEAD_IPS[0]}  $WORKER_HOST=${WORKER_IPS[0]}"
else
  printf '%s\n' "$PW" | sudo -S -p '' bash "$STACK_DIR/scripts/node-setup.sh" "$U" single
fi
# The controller reaches this Spark with `ssh localhost` using the same key.
grep -qxF "$PUB" ~/.ssh/authorized_keys 2>/dev/null || echo "$PUB" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
ssh-keygen -F localhost >/dev/null 2>&1 || ssh-keyscan -q localhost >> ~/.ssh/known_hosts 2>/dev/null
ssh "${SSHO[@]}" localhost true || die "ssh localhost does not work (is openssh-server running?)"

# ------------------------------------------------------------------------------------------------ tools
step "Tools (Hugging Face CLI, Node.js, DeepSeek harness, OpenCode)"
hf_cli='command -v ~/.local/bin/hf >/dev/null 2>&1 || { command -v ~/.local/bin/uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; ~/.local/bin/uv tool install -q "huggingface_hub[hf_xet]"; }; ~/.local/bin/hf version 2>/dev/null | head -1'
info "hf here: $(bash -c "$hf_cli")"
[ -z "$BOOT" ] || info "hf on $WORKER_HOST: $(ssh "${SSHO[@]}" "$WORKER_TARGET" "$hf_cli")"

NODE_DIR=/opt/spark-stack-node
if [ "$("$NODE_DIR/bin/node" --version 2>/dev/null)" != "$NODE_VERSION" ]; then
  tmp=$(mktemp -d)
  curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-arm64.tar.xz" | tar -xJ -C "$tmp"
  sudo rm -rf "$NODE_DIR" && sudo mv "$tmp/node-$NODE_VERSION-linux-arm64" "$NODE_DIR" && rm -rf "$tmp"
fi
if [ "$("$NODE_DIR/bin/dsh" --version 2>/dev/null | tail -1)" != "$DSH_VERSION" ]; then
  sudo env PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/npm" install -g --silent --no-fund --no-audit "@deepseek-ai/dsh@$DSH_VERSION" >/dev/null
fi
info "DeepSeek harness $("$NODE_DIR/bin/dsh" --version 2>/dev/null | tail -1)"

OC_DEB=/tmp/opencode-desktop-$OPENCODE_VERSION-arm64.deb
OC_PKG=""
[ -s "$OC_DEB" ] || curl -fsSL -o "$OC_DEB" "https://github.com/anomalyco/opencode/releases/download/$OPENCODE_VERSION/opencode-desktop-linux-arm64.deb"
OC_PKG=$(dpkg-deb -f "$OC_DEB" Package)
if [ "$(dpkg-query -W -f '${Version}' "$OC_PKG" 2>/dev/null)" != "$(dpkg-deb -f "$OC_DEB" Version)" ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$OC_DEB" >/dev/null 2>&1 || die "OpenCode install failed"
fi
OC_DESKTOP=$(dpkg -L "$OC_PKG" | grep -m1 '/applications/.*\.desktop$' | xargs -r basename)
info "OpenCode $(dpkg-query -W -f '${Version}' "$OC_PKG") ($OC_DESKTOP)"

# ------------------------------------------------------------------------------------------------ recipes
step "Model recipes"
mkdir -p "$STACK_DIR/vendor"
registry 'for m in MODELS.values():
    for r in m.recipes.values():
        print(r.dir, r.repo, r.commit, r.sparks)' | while read -r dir repo commit sparks; do
  [ "$sparks" = 1 ] || [ -n "$BOOT" ] || continue
  d="$STACK_DIR/vendor/$dir"
  [ -d "$d/.git" ] || git clone -q "$repo" "$d"
  git -C "$d" cat-file -e "$commit^{commit}" 2>/dev/null || git -C "$d" fetch -q origin
  git -C "$d" -c advice.detachedHead=false checkout -q "$commit"
  info "$dir @ ${commit:0:8}"
done

# ------------------------------------------------------------------------------------------------ controller
step "Spark Stack app"
mkdir -p "$STATE" && chmod 700 "$STATE"
[ -s "$STATE/token" ] || { python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$STATE/token"; chmod 600 "$STATE/token"; }
sudo docker build -q -t "$IMAGE" "$STACK_DIR" >/dev/null
sudo docker rm -f spark-stack >/dev/null 2>&1 || true
sudo docker run -d --name spark-stack --network host --restart unless-stopped \
  -v "$HOME:$HOME" -v "$STATE:/var/lib/spark-stack" \
  -e HOME="$HOME" -e STACK_HEAD=localhost -e STACK_HEAD_NAME="$(hostname)" \
  -e STACK_WORKER="$WORKER_TARGET" -e STACK_WORKER_NAME="${WORKER_HOST:-}" \
  -e STACK_RECIPES="$STACK_DIR/vendor" -e STACK_PUBLIC_HOST="$TS_IP" -e STACK_BIND="$TS_IP" -e STACK_PORT="$PORT" \
  -e STACK_SSH_KEY="$HOME/.ssh/id_ed25519" -e STACK_SSH_USER="$U" \
  -e STACK_SSH_KNOWN_HOSTS="$HOME/.ssh/known_hosts" \
  "$IMAGE" >/dev/null
for i in $(seq 1 30); do curl -fs -m 2 "http://$TS_IP:$PORT/healthz" >/dev/null && break; sleep 1; done
curl -fs -m 2 "http://$TS_IP:$PORT/healthz" >/dev/null || die "the app did not start: sudo docker logs spark-stack"
info "running at http://$TS_IP:$PORT"

# ------------------------------------------------------------------------------------------------ harnesses + dock
step "Harnesses and dock"
ENDPOINT="http://$TS_IP:$PORT/v1"
mkdir -p ~/.dsh/profiles/web ~/.config/opencode ~/.local/bin ~/.local/share/applications ~/.local/share/icons ~/.local/state/spark-stack
(cd "$STACK_DIR" && python3 -m stack.harness dsh ~/.dsh/profiles/web/cordis.patch.yml "$ENDPOINT" >/dev/null)
(cd "$STACK_DIR" && python3 -m stack.harness opencode ~/.config/opencode/opencode.json "$ENDPOINT" >/dev/null)

sudo tee /etc/systemd/system/spark-stack-dsh.service >/dev/null <<EOF
[Unit]
Description=DeepSeek harness (web) for Spark Stack
After=network-online.target

[Service]
User=$U
WorkingDirectory=$HOME
Environment=PATH=$NODE_DIR/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$NODE_DIR/bin/dsh web --port 3080 --no-open
StandardOutput=append:$HOME/.local/state/spark-stack/dsh-web.log
StandardError=append:$HOME/.local/state/spark-stack/dsh-web.log
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q spark-stack-dsh.service
sudo systemctl restart spark-stack-dsh.service

cp "$STACK_DIR"/assets/*.svg ~/.local/share/icons/
cat > ~/.local/bin/spark-stack-open <<EOF
#!/bin/sh
exec xdg-open "http://$TS_IP:$PORT/"
EOF
cat > ~/.local/bin/spark-dsh-open <<EOF
#!/bin/sh
# Opens the DeepSeek harness with its sign-in link (written by dsh web at start).
log="$HOME/.local/state/spark-stack/dsh-web.log"
i=0; while [ \$i -lt 40 ] && ! curl -s -o /dev/null -m 1 http://127.0.0.1:3080/; do i=\$((i+1)); sleep 0.25; done
url=\$(sed -n 's/^dsh web: //p' "\$log" | tail -n 1)
exec xdg-open "\${url:-http://127.0.0.1:3080/}"
EOF
chmod 700 ~/.local/bin/spark-stack-open ~/.local/bin/spark-dsh-open
cat > ~/.local/share/applications/spark-stack.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Spark Stack
Comment=Download, start and stop local AI models
Exec=$HOME/.local/bin/spark-stack-open
Icon=$HOME/.local/share/icons/spark-stack.svg
Categories=Development;
Terminal=false
EOF
cat > ~/.local/share/applications/spark-dsh.desktop <<EOF
[Desktop Entry]
Type=Application
Name=DeepSeek Harness
Comment=AI agent that works with the local models
Exec=$HOME/.local/bin/spark-dsh-open
Icon=$HOME/.local/share/icons/spark-dsh.svg
Categories=Development;
Terminal=false
EOF
update-desktop-database ~/.local/share/applications 2>/dev/null || true
uid=$(id -u)
if [ -S "/run/user/$uid/bus" ]; then
  current=$(DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" gsettings get org.gnome.shell favorite-apps)
  favs=$(python3 - "$current" spark-stack.desktop spark-dsh.desktop "$OC_DESKTOP" <<'PY'
import ast, sys
cur = ast.literal_eval(sys.argv[1].removeprefix("@as ")) if sys.argv[1].strip() not in ("@as []", "[]") else []
for app in sys.argv[2:]:
    if app and app not in cur:
        cur.append(app)
print(repr(cur))
PY
)
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" gsettings set org.gnome.shell favorite-apps "$favs"
  info "dock: Spark Stack, DeepSeek Harness, OpenCode"
else
  info "no desktop session right now: find the apps under Show Apps and pin them (right click, Pin to Dash)"
fi

# ------------------------------------------------------------------------------------------------ first model
step "First model"
read -r DKEY DSPARKS < <(registry 'print(*DEFAULT_MODEL)')
[ -n "$BOOT" ] || DSPARKS=1
if [ "${NO_DOWNLOAD:-0}" = 1 ]; then
  info "skipped (NO_DOWNLOAD=1)"
else
  curl -fs -m 20 -X POST \
    "http://$TS_IP:$PORT/api/models/$DKEY/prepare?sparks=$DSPARKS" >/dev/null \
    && info "downloading $DKEY for $DSPARKS Spark(s); watch it in Spark Stack" \
    || info "the download did not start (already running?); start it from Spark Stack"
fi

printf '\n\033[1mDone.\033[0m\n'
printf '  Spark Stack:   http://%s:%s  (dock icon "Spark Stack"; key in %s/token)\n' "$TS_IP" "$PORT" "$STATE"
printf '  API for apps:  %s   model "spark" = whatever is running\n' "$ENDPOINT"
printf '  Harnesses:     DeepSeek Harness and OpenCode on the dock\n'
printf '  Repair:        run the same command again\n'
[ -z "$BOOT" ] || printf '  Second Spark:  %s over the CX7 link (%s)\n' "$WORKER_HOST" "${WORKER_IPS[0]}"
printf '  Docker access: log out and back in once so your user can run docker without sudo\n'
