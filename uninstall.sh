#!/usr/bin/env bash
# Remove Spark Stack from this Spark (and the second Spark it set up). Model weights are kept unless --weights.
#   ~/spark-stack/uninstall.sh [--weights]
# Leaves alone: Tailscale, docker, your user's docker group, the CX7 addresses and the ssh keys (harmless, and the
# next install reuses them).
set -euo pipefail
WEIGHTS=0; [ "${1:-}" = --weights ] && WEIGHTS=1
STACK_DIR=${STACK_DIR:-$HOME/spark-stack}
STATE=$HOME/spark-stack-state
SSHO=(-o BatchMode=yes -o ConnectTimeout=6)
say() { printf '  %s\n' "$*"; }

sudo true   # asks for the password once (sudo -v would insist on one even where sudo needs none)
WORKER=$(sudo docker inspect spark-stack --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^STACK_WORKER=//p' || true)
[ -n "$WORKER" ] || ! [ -f /etc/netplan/40-spark-stack-cx7.yaml ] || WORKER="$(id -un)@192.168.100.11"   # the installer's worker address

echo "Stopping models"
for d in "$STACK_DIR"/vendor/*/; do
  [ -x "$d/stop.sh" ] && (cd "$d" && WORKER=$WORKER ./stop.sh >/dev/null 2>&1 || true)
done

echo "Removing the app, the harness service and the dock entries"
sudo docker rm -f spark-stack >/dev/null 2>&1 || true
sudo docker rmi spark-stack:local >/dev/null 2>&1 || true
sudo systemctl disable --now spark-stack-dsh.service >/dev/null 2>&1 || true
sudo rm -f /etc/systemd/system/spark-stack-dsh.service && sudo systemctl daemon-reload
sudo rm -rf /opt/spark-stack-node
pkg=$(dpkg-query -W -f '${Package}\n' 'opencode*' 2>/dev/null | head -1 || true)
[ -z "$pkg" ] || sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq "$pkg" >/dev/null 2>&1 || true
rm -f ~/.local/share/applications/spark-stack.desktop ~/.local/share/applications/spark-dsh.desktop \
      ~/.local/share/icons/spark-stack.svg ~/.local/share/icons/spark-dsh.svg \
      ~/.local/bin/spark-stack-open ~/.local/bin/spark-dsh-open
rm -rf ~/.local/share/spark-stack/webapp
grep -l 'spark-stack/opencode-data' ~/.local/share/applications/*.desktop 2>/dev/null | xargs -r rm -f   # OpenCode launcher copy
uid=$(id -u)
if [ -S "/run/user/$uid/bus" ]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
  favs=$(gsettings get org.gnome.shell favorite-apps | python3 -c 'import ast,sys
s=sys.stdin.read().strip().removeprefix("@as ")
print(repr([a for a in ast.literal_eval(s) if a not in ("spark-stack.desktop","spark-dsh.desktop") and "opencode" not in a]))')
  gsettings set org.gnome.shell favorite-apps "$favs"
fi

echo "Removing the harness settings the installer wrote"
python3 - <<'PY'
import json, os, re
from pathlib import Path
p = Path.home() / ".dsh/profiles/web/cordis.patch.yml"
if p.exists():
    t = p.read_text()
    t = re.sub(r"\n?# >>> spark-stack providers.*?# <<< spark-stack providers\n?", "\n", t, flags=re.S)
    p.write_text(t.rstrip() + "\n" if t.strip() else "")
o = Path.home() / ".config/opencode/opencode.json"
if o.exists():
    c = json.loads(o.read_text())
    c.get("provider", {}).pop("spark", None)
    if c.get("model", "").startswith("spark/"):
        c.pop("model")
    o.write_text(json.dumps(c, indent=2) + "\n")
PY

UNDO_NODE='systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
rm -f /etc/dconf/db/local.d/00-spark-stack && dconf update 2>/dev/null || true
if grep -q "# spark-stack$" /etc/exports 2>/dev/null; then sed -i "/# spark-stack$/d" /etc/exports; exportfs -ra; fi
echo "  [$(hostname)] sleep restored, NFS share removed"'
# Only the folders the stack's recipes download; other models in your caches stay.
WEIGHT_DIRS=$(cd "$STACK_DIR" && python3 -c 'from stack.registry import MODELS
dirs = {".cache/spark-stack-hf", ".cache/dsv41-tensorfold"}
for m in MODELS.values():
    for r in m.recipes.values():
        dirs.update(f"{r.hf_cache}/hub/models--{w.replace(chr(47), chr(45) * 2)}" for w in r.weights)
print(" ".join(sorted(dirs)))')
RM_WEIGHTS="cd \"\$HOME\" && rm -rf $WEIGHT_DIRS"

sudo bash -c "$UNDO_NODE"
if [ -n "$WORKER" ]; then
  timeout 120 ssh -t -o ConnectTimeout=6 "$WORKER" "sudo bash -c '$UNDO_NODE'" \
    || say "second Spark ($WORKER): run 'sudo systemctl unmask sleep.target suspend.target' there to restore sleep"
  [ "$WEIGHTS" = 0 ] || timeout 600 ssh "${SSHO[@]}" "$WORKER" "$RM_WEIGHTS"
fi

if [ "$WEIGHTS" = 1 ]; then
  echo "Removing the stack's model weights"
  bash -c "$RM_WEIGHTS"
else
  say "model weights kept in ~/.cache (run with --weights to delete the stack's models)"
fi

sudo rm -rf "$STATE"   # the app container writes its job logs there as root
cd "$HOME" && rm -rf "$STACK_DIR"
echo "Spark Stack removed."
