#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${ZERO3_BRIDGE_REPO:-/opt/zero3-pilot-commander-bridge}"
RUNTIME="${ZERO3_BRIDGE_RUNTIME:-/opt/zero3-pilot-bridge-runtime}"
VENV="${RUNTIME}/venv"
PYTHON="${ZERO3_BRIDGE_PYTHON:-/usr/bin/python3}"
CONFIG_DIR="${ZERO3_BRIDGE_CONFIG_DIR:-/etc/zero3-pilot-bridge}"
ENV_FILE="${CONFIG_DIR}/bridge.env"
UNIT_SOURCE="${REPO}/deploy/zero3-pilot-bridge.service"
UNIT_TARGET="/etc/systemd/system/zero3-pilot-bridge.service"
START=0

usage() {
  cat <<'EOF'
Usage: sudo deploy/install.sh [--start]

Installs the Bridge into an independent virtualenv and installs/enables its
systemd unit. It never creates, reads aloud, copies, rotates, or prints the
Commander token. Production installation requires the checkout to be on main.

  --start   start/restart zero3-pilot-bridge.service after validation
EOF
}

for arg in "$@"; do
  case "$arg" in
    --start) START=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "install.sh must run as root" >&2
  exit 77
fi

for command in git runuser systemctl install; do
  command -v "$command" >/dev/null || {
    echo "required command not found: $command" >&2
    exit 69
  }
done

id zero3pilotbridge >/dev/null 2>&1 || {
  echo "required service account does not exist: zero3pilotbridge" >&2
  exit 67
}

[[ -x "$PYTHON" ]] || {
  echo "Python interpreter is missing or not executable: $PYTHON" >&2
  exit 69
}

"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Bridge requires Python >=3.10, found {sys.version.split()[0]}")
print("python:", sys.version.split()[0])
PY

[[ -d "$REPO/.git" ]] || {
  echo "Bridge repository is not a Git checkout: $REPO" >&2
  exit 66
}

branch="$(git -C "$REPO" branch --show-current)"
if [[ "$branch" != "main" ]]; then
  echo "refusing production install from branch '$branch'; merge/review first and deploy main" >&2
  exit 65
fi

if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
  echo "refusing production install from a dirty Bridge checkout" >&2
  exit 65
fi

[[ -f "$ENV_FILE" ]] || {
  echo "runtime environment file is missing: $ENV_FILE" >&2
  exit 78
}
[[ -f "$UNIT_SOURCE" ]] || {
  echo "systemd unit template is missing: $UNIT_SOURCE" >&2
  exit 66
}

# Only key names are shown. Values are deliberately never printed.
echo "runtime configuration keys:"
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | cut -d= -f1 | sort -u | sed 's/^/  - /'

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${ZERO3_COMMANDER_BASE_URL:?ZERO3_COMMANDER_BASE_URL is not configured}"
: "${ZERO3_COMMANDER_TOKEN_FILE:?ZERO3_COMMANDER_TOKEN_FILE is not configured}"
: "${ZERO3_COMMANDER_ID:?ZERO3_COMMANDER_ID is not configured}"

[[ -s "$ZERO3_COMMANDER_TOKEN_FILE" ]] || {
  echo "Commander token file is missing or empty" >&2
  exit 78
}
runuser -u zero3pilotbridge -- test -r "$ZERO3_COMMANDER_TOKEN_FILE" || {
  echo "Commander token file is not readable by zero3pilotbridge" >&2
  exit 77
}

was_active=0
if systemctl is-active --quiet zero3-pilot-bridge.service 2>/dev/null; then
  was_active=1
  systemctl stop zero3-pilot-bridge.service
fi

install -d -o zero3pilotbridge -g zero3pilotbridge -m 0750 "$RUNTIME"
if [[ ! -x "$VENV/bin/python" ]]; then
  runuser -u zero3pilotbridge -- "$PYTHON" -m venv "$VENV" || {
    echo "could not create venv; ensure the system Python venv module is installed" >&2
    exit 69
  }
fi

# A normal install, not editable: production code in the venv is immutable to
# the service while the repository itself remains a data/mailbox checkout.
runuser -u zero3pilotbridge -- "$VENV/bin/python" -m pip install --upgrade "$REPO"

# Validate that wheel-style package data is really usable before touching the
# service. This specifically catches missing protocol schemas.
runuser -u zero3pilotbridge -- "$VENV/bin/python" - <<'PY'
from zero3_pilot_commander_bridge.validation import load_schema
for name in (
    "execution-submit.schema.json",
    "state-mirror.schema.json",
    "result.schema.json",
    "bridge-health.schema.json",
):
    load_schema(name)
print("installed package schemas: ok")
PY

# Runtime commits have a dedicated audit identity. Do not change global Git
# configuration and do not add credentials here; SSH authentication remains the
# repo-scoped Deploy Key already configured on the host.
git -C "$REPO" config user.name "Zero3 Pilot Commander Bridge"
git -C "$REPO" config user.email "zero3-pilot-commander-bridge@localhost"

# Pin how this checkout authenticates to GitHub. The unit runs with
# ProtectHome=read-only, so ssh can never write $HOME/.ssh/known_hosts: host
# verification has to be satisfied entirely by an existing known_hosts file, and
# the key has to be named explicitly rather than discovered. Leaving this to
# ssh defaults produces a service that authenticates by hand and then fails
# under the sandbox with no way to recover.
# Resolve the service account home from passwd rather than hardcoding a path:
# the installer should follow the account it was told to use, and a literal
# home directory in a committed file is exactly what the secret gate rejects.
BRIDGE_HOME="$(getent passwd zero3pilotbridge | cut -d: -f6)"
[[ -n "$BRIDGE_HOME" ]] || {
  echo "cannot resolve the home directory of zero3pilotbridge" >&2
  exit 67
}
DEPLOY_KEY="${ZERO3_BRIDGE_DEPLOY_KEY:-$BRIDGE_HOME/.ssh/id_ed25519}"
KNOWN_HOSTS="${ZERO3_BRIDGE_KNOWN_HOSTS:-$BRIDGE_HOME/.ssh/known_hosts}"

runuser -u zero3pilotbridge -- test -r "$DEPLOY_KEY" || {
  echo "Git deploy key is not readable by zero3pilotbridge: $DEPLOY_KEY" >&2
  exit 77
}
[[ -s "$KNOWN_HOSTS" ]] || {
  echo "known_hosts is missing or empty: $KNOWN_HOSTS" >&2
  echo "the sandbox cannot create it at runtime; populate it before installing" >&2
  exit 78
}

git -C "$REPO" config core.sshCommand   "ssh -i ${DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${KNOWN_HOSTS} -o ConnectTimeout=20 -o BatchMode=yes"

# Prove the credential actually authenticates against this repository before
# the service depends on it. Read-only: it lists refs and writes nothing.
runuser -u zero3pilotbridge -- git -C "$REPO" ls-remote --heads origin >/dev/null || {
  echo "Git deploy key cannot reach origin; refusing to install a service that cannot push" >&2
  exit 77
}
echo "git deploy key: PASS"

install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
systemctl daemon-reload
systemctl enable zero3-pilot-bridge.service >/dev/null

# Validate the real Commander path with the Bridge client before starting the
# long-running loop. The token value is read by the client from its file and is
# never placed in argv or output.
env_args=(
  "ZERO3_COMMANDER_BASE_URL=$ZERO3_COMMANDER_BASE_URL"
  "ZERO3_COMMANDER_TOKEN_FILE=$ZERO3_COMMANDER_TOKEN_FILE"
  "ZERO3_COMMANDER_ID=$ZERO3_COMMANDER_ID"
)
[[ -n "${ZERO3_COMMANDER_CA_BUNDLE:-}" ]] && env_args+=("ZERO3_COMMANDER_CA_BUNDLE=$ZERO3_COMMANDER_CA_BUNDLE")
[[ -n "${ZERO3_COMMANDER_TIMEOUT:-}" ]] && env_args+=("ZERO3_COMMANDER_TIMEOUT=$ZERO3_COMMANDER_TIMEOUT")

runuser -u zero3pilotbridge -- env "${env_args[@]}" \
  "$VENV/bin/python" -m zero3_pilot_commander_bridge --root "$REPO" health >/dev/null
echo "CommanderClient.health: PASS"

if (( START == 1 || was_active == 1 )); then
  systemctl restart zero3-pilot-bridge.service
  systemctl is-active --quiet zero3-pilot-bridge.service
  echo "zero3-pilot-bridge.service: active"
else
  echo "zero3-pilot-bridge.service: installed and enabled, not started (use --start)"
fi

echo "installed Bridge HEAD: $(git -C "$REPO" rev-parse HEAD)"
