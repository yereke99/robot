#!/usr/bin/env bash
# Idempotent VPS bootstrap. Re-runnable.
# Usage (on the Mac, after ssh-copy-id):
#   ./deploy/provision.sh root@213.155.22.163
set -euo pipefail

REMOTE="${1:?usage: $0 user@host}"

echo "=== 1. waiting for sshd ==="
until ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$REMOTE" 'echo ok' > /dev/null 2>&1; do
    sleep 2
done

echo "=== 2. base packages ==="
ssh "$REMOTE" 'bash -se' <<'REMOTE_EOF'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Wait for unattended-upgrades / cloud-init to release the apt lock.
for i in {1..60}; do
    if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then break; fi
    echo "  apt lock held; waiting…"; sleep 5
done

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl gnupg lsb-release ufw unattended-upgrades \
    git rsync

# Docker (official APT repo)
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    arch=$(dpkg --print-architecture)
    codename=$(lsb_release -cs)
    echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $codename stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# Basic firewall
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

# Disable password-only SSH (key is already set up by ssh-copy-id).
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'CFG'
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
CFG
systemctl reload ssh || systemctl reload sshd

# Project dir
mkdir -p /opt/temka /opt/temka/secrets
chmod 700 /opt/temka/secrets

echo "  docker:   $(docker --version)"
echo "  compose:  $(docker compose version --short)"
REMOTE_EOF

echo "=== 3. sync repo + secrets ==="
LOCAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
rsync -avz --delete \
    --include='goszakup/' \
    --include='goszakup/jars/' \
    --include='goszakup/jars/**' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.pytest_cache' \
    --exclude='*.har' \
    --exclude='secrets' \
    --exclude='.env' \
    --exclude='config.yaml' \
    --exclude='*.p12' \
    --exclude='~/.goszakup' \
    "$LOCAL_REPO/" "$REMOTE:/opt/temka/"

echo "  (config.yaml, .env, .p12 are copied via separate scp steps — see deploy/upload-secrets.sh)"

echo "=== done provisioning ==="
echo "Next: run ./deploy/upload-secrets.sh $REMOTE"
