#!/bin/bash
# code-server-init.sh
# 每次 code-server 服务启动前执行（ExecStartPre），以 root 身份运行。
# 功能：
#   1. 根据容器 hostname 确定 base-path
#   2. 首次启动时生成随机密码，写入配置文件
#   3. 将密码明文保存到用户家目录供 SSH 查看
set -euo pipefail

# ── 可通过环境变量覆盖 ────────────────────────────────────────────────────────
CS_USER="${CODE_SERVER_USER:-ubuntu}"
CS_PORT="${CODE_SERVER_PORT:-8080}"
# 平台域名，用于 code-server proxy-domain（留空则不配置）
CS_PROXY_DOMAIN="${PLATFORM_DOMAIN:-}"
# 路径前缀，与 nginx / http-path-proxy 保持一致
CS_PATH_PREFIX="${PATH_PREFIX:-/c/}"

# ── 计算路径 ──────────────────────────────────────────────────────────────────
CONTAINER_NAME="$(hostname)"
HOME_DIR="$(getent passwd "$CS_USER" | cut -d: -f6 2>/dev/null || echo "/home/$CS_USER")"
CONFIG_DIR="$HOME_DIR/.config/code-server"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
PASSWORD_FILE="$HOME_DIR/.code-server-password"

mkdir -p "$CONFIG_DIR"
chown "$CS_USER":"$CS_USER" "$CONFIG_DIR" 2>/dev/null || true

# ── 生成/读取密码 ─────────────────────────────────────────────────────────────
if [ ! -f "$PASSWORD_FILE" ]; then
    PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '+/=' | head -c 18)"
    echo "$PASSWORD" > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
    chown "$CS_USER":"$CS_USER" "$PASSWORD_FILE" 2>/dev/null || true
else
    PASSWORD="$(cat "$PASSWORD_FILE")"
fi

# ── 写入 code-server 配置 ──────────────────────────────────────────────────────
# 注：code-server 4.100+ 移除了 base-path 选项，不再写入配置文件。
# 路径应层由 nginx sub_filter + http-path-proxy 前缀剥离负责。

cat > "$CONFIG_FILE" <<EOF
bind-addr: 0.0.0.0:${CS_PORT}
auth: password
password: ${PASSWORD}
cert: false
EOF

# 配置 proxy-domain（允许来自反向代理域名的请求）
if [ -n "$CS_PROXY_DOMAIN" ]; then
    echo "proxy-domain: ${CS_PROXY_DOMAIN}" >> "$CONFIG_FILE"
fi

chown "$CS_USER":"$CS_USER" "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

echo "code-server init: container=${CONTAINER_NAME} port=${CS_PORT}"
