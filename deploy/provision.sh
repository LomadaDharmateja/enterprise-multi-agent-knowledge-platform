#!/usr/bin/env bash
# One-time server setup for a fresh Debian 12 / Ubuntu 24.04 VPS (M8 Task 2).
#
# Run as root on the new instance:
#     bash provision.sh
#
# What it does and why, in the order it matters:
#   1. A firewall, before anything is listening. Hetzner instances ship with no
#      firewall enabled -- an unfirewalled box with Docker on it is a box whose
#      exposed ports are whatever compose decided.
#   2. Docker's iptables rules bypass ufw. Published container ports are reachable
#      even when ufw says DENY. deploy/docker-compose.prod.yml publishes only 80
#      and 443, which is the real control; this sets ufw as the second layer and
#      records the caveat rather than pretending ufw alone is sufficient.
#   3. A non-root user that owns the deployment.
#   4. Unattended security upgrades, because "you can leave it running" is the M8
#      exit criterion and an unpatched box does not satisfy it.

set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [[ "$(id -u)" -ne 0 ]]; then
    echo "run as root" >&2
    exit 1
fi

log "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg ufw fail2ban unattended-upgrades

log "Firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw allow 80/tcp comment 'http -> caddy redirect'
ufw allow 443/tcp comment 'https -> caddy'
ufw --force enable
ufw status verbose

log "Docker Engine"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

log "Deploy user"
if ! id -u "$DEPLOY_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

# Carry the key that got us here, so the deploy user can log in the same way.
if [[ -f /root/.ssh/authorized_keys ]]; then
    install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
    install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
        /root/.ssh/authorized_keys "/home/$DEPLOY_USER/.ssh/authorized_keys"
fi

log "SSH hardening"
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh || systemctl restart sshd

log "Unattended security upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades

log "Done"
cat <<EOF

Provisioned. Next:

  1. As ${DEPLOY_USER}, clone the repo (or upload the image tarballs).
  2. cp deploy/.env.example deploy/.env  and fill every REPLACE value.
     Generate secrets with:  openssl rand -hex 32
  3. Set APP_DOMAIN / API_DOMAIN to <ip>.sslip.io and api.<ip>.sslip.io
     using this machine's address: $(curl -fsS --max-time 5 https://api.ipify.org || echo '<ip>')
  4. bash scripts/import_corpus.sh corpus_bundle/
  5. docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d

Caveat worth knowing: Docker writes its own iptables rules and bypasses ufw for
published ports. The compose file publishes only 80 and 443, so nothing else is
exposed -- but if you add a ports: mapping later, ufw will NOT block it.
EOF
