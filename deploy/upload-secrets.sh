#!/usr/bin/env bash
# Upload sensitive files to the VPS and bring the bot up.
# Run AFTER deploy/provision.sh has succeeded.
set -euo pipefail

REMOTE="${1:?usage: $0 user@host}"
P12_LOCAL="${P12_PATH:-$HOME/Downloads/GOST512_2cb2068ca4e4449534ec1c1f9ce2f446acccb0a2.p12}"
LOCAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "$P12_LOCAL" ]]; then
    echo "ECP .p12 not found at $P12_LOCAL"
    echo "Set P12_PATH=/full/path/to/cert.p12 and re-run."
    exit 1
fi

echo "=== 1. .p12 ==="
scp "$P12_LOCAL" "$REMOTE:/opt/temka/secrets/ecp.p12"
ssh "$REMOTE" 'chmod 600 /opt/temka/secrets/ecp.p12 && chown -R root:root /opt/temka/secrets'

echo "=== 2. config.yaml ==="
# Bot reads ecp.p12_path from config; rewrite it to the mounted path.
TMP_CFG=$(mktemp)
trap 'rm -f $TMP_CFG' EXIT
.venv/bin/python -c "
import yaml, sys
data = yaml.safe_load(open('$LOCAL_REPO/config.yaml'))
data['ecp']['p12_path'] = '/secrets/ecp.p12'
yaml.safe_dump(data, open('$TMP_CFG', 'w'), allow_unicode=True, sort_keys=False)
"
scp "$TMP_CFG" "$REMOTE:/opt/temka/config.yaml"
ssh "$REMOTE" 'chmod 600 /opt/temka/config.yaml'

echo "=== 3. .env ==="
ECP_PW=$(security find-generic-password -s goszakup-ecp -a default -w)
ACC_PW=$(security find-generic-password -s goszakup-acc -a default -w)
TG_TOKEN="${TG_BOT_TOKEN:?env TG_BOT_TOKEN must be set on the Mac when running this script}"

ssh "$REMOTE" 'cat > /opt/temka/.env && chmod 600 /opt/temka/.env' <<ENV_EOF
TG_BOT_TOKEN=$TG_TOKEN
GOSZAKUP_GOSZAKUP_ECP_DEFAULT=$ECP_PW
GOSZAKUP_GOSZAKUP_ACC_DEFAULT=$ACC_PW
TZ=Asia/Almaty
ENV_EOF

echo "=== 4. docker compose up ==="
ssh "$REMOTE" 'cd /opt/temka && docker compose build && docker compose up -d'

echo "=== 5. tail logs (Ctrl-C to exit) ==="
ssh "$REMOTE" 'cd /opt/temka && docker compose logs -f --tail=50'
