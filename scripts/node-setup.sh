#!/usr/bin/env bash
# Privileged setup for one Spark, run as root by install.sh on the head and (over ssh) on the worker.
#   node-setup.sh <user> <role: head|worker|single> [<ip for the first CX7 twin> <ip for the second> [<peer ip> <peer ip>]]
# Idempotent: re-running converges to the same state.
set -euo pipefail
U=$1 ROLE=$2 IP_A=${3:-} IP_B=${4:-} PEER_A=${5:-} PEER_B=${6:-}
UHOME=$(getent passwd "$U" | cut -d: -f6)
say() { printf '  [%s] %s\n' "$(hostname)" "$*"; }

# ---- packages the recipes need (rsync for the weight copy, NFS for DeepSeek's shared checkpoint)
pkgs="rsync curl git"
[ "$ROLE" = head ] && pkgs+=" nfs-kernel-server"
[ "$ROLE" = worker ] && pkgs+=" nfs-common"
missing=$(for p in $pkgs; do dpkg -s "$p" >/dev/null 2>&1 || echo "$p"; done)
if [ -n "$missing" ]; then
  say "installing: $(echo $missing)"
  DEBIAN_FRONTEND=noninteractive timeout 600 apt-get update -qq
  DEBIAN_FRONTEND=noninteractive timeout 1200 apt-get install -y -qq $missing >/dev/null
fi

# ---- docker without sudo for the desktop user (the recipes call docker as that user)
id -nG "$U" | grep -qw docker || { usermod -aG docker "$U"; say "added $U to the docker group"; }
timeout 60 systemctl enable --now docker >/dev/null 2>&1 || true

# ---- never sleep: a sleeping Spark drops its model and its tailnet address
systemctl mask --now sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
mkdir -p /etc/dconf/profile /etc/dconf/db/local.d
[ -f /etc/dconf/profile/user ] || printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
cat > /etc/dconf/db/local.d/00-spark-stack <<'EOF'
[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
EOF
dconf update 2>/dev/null || true
uid=$(id -u "$U")
if [ -S "/run/user/$uid/bus" ]; then
  sudo -u "$U" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
fi
say "sleep disabled (system targets masked, GNOME auto-suspend off)"

# ---- the CX7 link: an address on each logical twin of the cabled port (NVIDIA's two-Spark playbook layout)
if [ -n "$IP_A" ]; then
  mapfile -t UP < <(ibdev2netdev 2>/dev/null | awk '/\(Up\)/ {print $5}' | sort)
  [ "${#UP[@]}" -ge 2 ] || { echo "CX7 link is not up on $(hostname): $(ibdev2netdev | tr '\n' ' ')" >&2; exit 3; }
  plan=$(mktemp)
  cat > "$plan" <<EOF
# Written by spark-stack: the QSFP link between the two Sparks.
network:
  version: 2
  ethernets:
    ${UP[0]}:
      addresses: [${IP_A}/24]
      dhcp4: false
      dhcp6: false
    ${UP[1]}:
      addresses: [${IP_B}/24]
      dhcp4: false
      dhcp6: false
EOF
  have=$(ip -4 -br addr show "${UP[0]}" 2>/dev/null)
  if cmp -s "$plan" /etc/netplan/40-spark-stack-cx7.yaml && [[ "$have" == *"$IP_A/24"* ]]; then
    rm -f "$plan"   # already in place: netplan apply would only bounce the Wi-Fi
  else
    install -m 600 "$plan" /etc/netplan/40-spark-stack-cx7.yaml && rm -f "$plan"
    if [ "$ROLE" = worker ]; then   # applied after this ssh session returns: netplan apply can bounce the link we came in on
      setsid -f sh -c 'sleep 2; netplan apply' >/dev/null 2>&1
    else
      timeout 120 netplan apply 2>/dev/null
    fi
  fi
  say "CX7 link: ${UP[0]}=$IP_A ${UP[1]}=$IP_B"
fi

# ---- head: share DeepSeek's weights-only cache with the worker, read-only, to the worker's CX7 addresses only.
# A dedicated tree (not ~/.cache/huggingface, which can hold a Hugging Face login token); root is squashed.
if [ "$ROLE" = head ] && [ -n "$PEER_A" ]; then
  share="$UHOME/.cache/spark-stack-hf"
  sudo -u "$U" mkdir -p "$share/hub"
  chmod 755 "$share" "$share/hub"
  sed -i '/# spark-stack$/d' /etc/exports
  echo "$share $PEER_A(ro,root_squash,no_subtree_check) $PEER_B(ro,root_squash,no_subtree_check) # spark-stack" >> /etc/exports
  timeout 120 systemctl enable --now nfs-kernel-server >/dev/null 2>&1
  timeout 60 exportfs -ra
  say "NFS: $share exported read-only to $PEER_A $PEER_B"
fi

# ---- sshd must answer on the link (the recipes ssh to the worker's CX7 address)
timeout 60 systemctl enable --now ssh.socket >/dev/null 2>&1 || timeout 60 systemctl enable --now ssh >/dev/null 2>&1 || true
say "node ready ($ROLE)"
