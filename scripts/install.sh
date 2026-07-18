#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="cast-immich"
WEB_HOST="${CAST_IMMICH_WEB_HOST:-127.0.0.1}"
WEB_PORT="${CAST_IMMICH_WEB_PORT:-8080}"

fail() {
    printf 'cast-immich install: %s\n' "$*" >&2
    exit 1
}

if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null 2>&1; then
    fail "this installer requires a Linux system with systemd"
fi
if [[ ! "$WEB_PORT" =~ ^[0-9]+$ ]] || ((WEB_PORT < 1 || WEB_PORT > 65535)); then
    fail "CAST_IMMICH_WEB_PORT must be an integer between 1 and 65535"
fi
if [[ "$WEB_HOST" == *%* || "$WEB_HOST" == *$'\n'* || "$WEB_HOST" == *$'\r'* ]]; then
    fail "CAST_IMMICH_WEB_HOST cannot contain percent signs or newlines"
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(dirname -- "$SCRIPT_DIR")"
if [[ "$APP_DIR" == *%* || "$APP_DIR" == *$'\n'* || "$APP_DIR" == *$'\r'* ]]; then
    fail "the checkout path cannot contain percent signs or newlines"
fi

if [[ $EUID -eq 0 ]]; then
    [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]] || fail "run this script as your normal user, not root"
    APP_USER="$SUDO_USER"
else
    APP_USER="$(id -un)"
fi
APP_GROUP="$(id -gn "$APP_USER")"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
[[ -n "$APP_HOME" ]] || fail "could not determine the home directory for $APP_USER"

command -v sudo >/dev/null 2>&1 || fail "sudo is required"
sudo -v

[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a Git checkout"
sudo -u "$APP_USER" git -C "$APP_DIR" remote get-url origin >/dev/null 2>&1 \
    || fail "the checkout must have an origin remote for updates"

sudo apt-get update
sudo apt-get install -y ca-certificates curl git

UV_BIN="$APP_HOME/.local/bin/uv"
if [[ ! -x "$UV_BIN" ]]; then
    if [[ -x /usr/local/bin/uv ]]; then
        UV_BIN="/usr/local/bin/uv"
    elif [[ -x /usr/bin/uv ]]; then
        UV_BIN="/usr/bin/uv"
    else
        printf 'Installing uv for %s...\n' "$APP_USER"
        curl -LsSf https://astral.sh/uv/install.sh \
            | sudo -u "$APP_USER" env HOME="$APP_HOME" UV_INSTALL_DIR="$APP_HOME/.local/bin" sh
    fi
fi
[[ -x "$UV_BIN" ]] || fail "uv was not installed at $UV_BIN"

printf 'Installing locked application dependencies...\n'
sudo -u "$APP_USER" env HOME="$APP_HOME" "$UV_BIN" sync \
    --project "$APP_DIR" --locked --no-dev

UNIT_TMP="$(mktemp)"
UPDATE_CONFIG_TMP="$(mktemp)"
trap 'rm -f "$UNIT_TMP" "$UPDATE_CONFIG_TMP"' EXIT

escape_systemd() {
    local value="${1//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

APP_DIR_UNIT="$(escape_systemd "$APP_DIR")"
APP_USER_UNIT="$(escape_systemd "$APP_USER")"
APP_GROUP_UNIT="$(escape_systemd "$APP_GROUP")"
EXECUTABLE_UNIT="$(escape_systemd "$APP_DIR/.venv/bin/cast-immich")"
CONFIG_UNIT="$(escape_systemd "$APP_DIR/config.toml")"
WEB_HOST_UNIT="$(escape_systemd "$WEB_HOST")"

cat >"$UNIT_TMP" <<EOF
[Unit]
Description=Cast Immich photos to idle Chromecasts
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$APP_USER_UNIT
Group=$APP_GROUP_UNIT
WorkingDirectory=$APP_DIR_UNIT
ExecStart=$EXECUTABLE_UNIT --config $CONFIG_UNIT --web-host $WEB_HOST_UNIT --web-port $WEB_PORT
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

{
    printf 'CAST_IMMICH_APP_DIR=%q\n' "$APP_DIR"
    printf 'CAST_IMMICH_APP_USER=%q\n' "$APP_USER"
    printf 'CAST_IMMICH_APP_HOME=%q\n' "$APP_HOME"
    printf 'CAST_IMMICH_UV_BIN=%q\n' "$UV_BIN"
    printf 'CAST_IMMICH_SERVICE=%q\n' "$SERVICE_NAME"
} >"$UPDATE_CONFIG_TMP"

sudo install -m 0644 "$UNIT_TMP" "/etc/systemd/system/$SERVICE_NAME.service"
sudo install -m 0600 "$UPDATE_CONFIG_TMP" "/etc/$SERVICE_NAME-install.conf"
sudo install -m 0755 "$SCRIPT_DIR/update.sh" "/usr/local/bin/$SERVICE_NAME-update"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME.service"

printf '\ncast-immich is installed and enabled at boot.\n'
printf 'Dashboard: http://%s:%s\n' "$WEB_HOST" "$WEB_PORT"
printf 'Status:    sudo systemctl status %s\n' "$SERVICE_NAME"
printf 'Logs:      sudo journalctl -u %s -f\n' "$SERVICE_NAME"
printf 'Update:    sudo %s-update\n' "$SERVICE_NAME"
if [[ ! -f "$APP_DIR/config.toml" ]]; then
    printf '\nNo config.toml exists yet. Use the dashboard for initial setup.\n'
fi
