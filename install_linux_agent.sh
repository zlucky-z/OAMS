#!/usr/bin/env bash
set -euo pipefail

SERVER_URL="${SE9_SERVER:-}"
HEARTBEAT_TOKEN="${SE9_TOKEN:-}"
HEARTBEAT_INTERVAL="${SE9_INTERVAL:-30}"
TS_AUTH_KEY="${SE9_TAILSCALE_AUTH_KEY:-}"
TS_HOSTNAME="${SE9_TAILSCALE_HOSTNAME:-}"
SUDO_PASSWORD="${SE9_SUDO_PASSWORD:-linaro}"

AGENT_DIR="/opt/se9-heartbeat-agent"
SERVICE_NAME="se9-heartbeat-agent"
SCRIPT_PATH="${AGENT_DIR}/linux_heartbeat_agent.py"

require_value() {
    local label="$1"
    local value="$2"
    if [[ -z "$value" ]]; then
        echo "[ERROR] 缺少参数: ${label}" >&2
        exit 1
    fi
}

extract_host() {
    local server="$1"
    local host="${server#*://}"
    host="${host%%/*}"
    host="${host%%:*}"
    printf "%s" "$host"
}

should_bypass_proxy() {
    local host
    host="$(extract_host "$1")"
    if [[ -z "$host" ]]; then
        return 1
    fi

    case "$host" in
        localhost|*.local|*.ts.net|127.*|10.*|192.168.*)
            return 0
            ;;
        172.*)
            local second
            second="$(printf "%s" "$host" | cut -d. -f2)"
            [[ "$second" =~ ^[0-9]+$ ]] && (( second >= 16 && second <= 31 ))
            return
            ;;
        100.*)
            local second
            second="$(printf "%s" "$host" | cut -d. -f2)"
            [[ "$second" =~ ^[0-9]+$ ]] && (( second >= 64 && second <= 127 ))
            return
            ;;
        *)
            return 1
            ;;
    esac
}

run_as_root() {
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        bash -s
        return
    fi

    {
        printf "%s\n" "$SUDO_PASSWORD"
        cat
    } | sudo -S -p '' -k bash -s
}

require_value "SE9_SERVER" "$SERVER_URL"
require_value "SE9_TOKEN" "$HEARTBEAT_TOKEN"
require_value "SE9_TAILSCALE_AUTH_KEY" "$TS_AUTH_KEY"

SERVER_URL="${SERVER_URL%/}"
BYPASS_PROXY="0"
if should_bypass_proxy "$SERVER_URL"; then
    BYPASS_PROXY="1"
fi

echo "[STEP] 开始安装 Tailscale 与 SE9 Heartbeat Agent..."

run_as_root <<ROOT
set -euo pipefail

SERVER_URL=${SERVER_URL@Q}
HEARTBEAT_TOKEN=${HEARTBEAT_TOKEN@Q}
HEARTBEAT_INTERVAL=${HEARTBEAT_INTERVAL@Q}
TS_AUTH_KEY=${TS_AUTH_KEY@Q}
TS_HOSTNAME=${TS_HOSTNAME@Q}
AGENT_DIR=${AGENT_DIR@Q}
SERVICE_NAME=${SERVICE_NAME@Q}
SCRIPT_PATH=${SCRIPT_PATH@Q}
BYPASS_PROXY=${BYPASS_PROXY@Q}

TAILSCALE_INSTALL_URL="https://tailscale.com/install.sh"
AGENT_SCRIPT_URL="\${SERVER_URL}/static/agent/linux_heartbeat_agent.py"

CURL_ARGS=(-fsSL)
if [[ "\$BYPASS_PROXY" == "1" ]]; then
    CURL_ARGS=(--noproxy '*' -fsSL)
fi

if ! command -v tailscale >/dev/null 2>&1; then
    TS_INSTALL_SCRIPT="\$(mktemp)"
    curl "\${CURL_ARGS[@]}" "\$TAILSCALE_INSTALL_URL" -o "\$TS_INSTALL_SCRIPT"
    sh "\$TS_INSTALL_SCRIPT"
    rm -f "\$TS_INSTALL_SCRIPT"
fi

systemctl enable --now tailscaled

TS_UP_ARGS=(--auth-key "\$TS_AUTH_KEY")
if [[ -n "\$TS_HOSTNAME" ]]; then
    TS_UP_ARGS+=(--hostname "\$TS_HOSTNAME")
fi
tailscale up "\${TS_UP_ARGS[@]}"

mkdir -p "\$AGENT_DIR"
curl "\${CURL_ARGS[@]}" "\$AGENT_SCRIPT_URL" -o "\$SCRIPT_PATH"
chmod 755 "\$SCRIPT_PATH"

cat >/etc/systemd/system/\${SERVICE_NAME}.service <<EOF
[Unit]
Description=SE9 Heartbeat Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=\${AGENT_DIR}
ExecStart=/usr/bin/env python3 \${SCRIPT_PATH} --server \${SERVER_URL} --token \${HEARTBEAT_TOKEN} --interval \${HEARTBEAT_INTERVAL}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "\$SERVICE_NAME"
systemctl restart "\$SERVICE_NAME"
ROOT

TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
if [[ -n "$TS_IP" ]]; then
    echo "[INFO] Tailscale IPv4: $TS_IP"
fi

systemctl status tailscaled --no-pager || true
systemctl status "${SERVICE_NAME}" --no-pager || true

echo "[DONE] Tailscale 与心跳 Agent 已配置为开机自启。"
echo "[NEXT] 设备首次通过 Tailnet 上报心跳后，平台会自动把设备地址同步为当前 Tailscale IP。"
