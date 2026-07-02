#!/bin/bash
# jupyter-init.sh
# 每次 JupyterLab 服务启动前执行（ExecStartPre），以 root 身份运行。
# 功能：
#   1. 根据容器 hostname 确定 base-url
#   2. 首次启动时生成随机 token，写入配置文件
#   3. 将 token 保存到用户家目录供 SSH 查看
set -euo pipefail

# ── 可通过环境变量覆盖 ────────────────────────────────────────────────────────
JL_USER="${JUPYTER_USER:-ubuntu}"
JL_PORT="${JUPYTER_PORT:-8888}"
PATH_PREFIX="${PATH_PREFIX:-/c/}"

# ── 计算路径 ──────────────────────────────────────────────────────────────────
CONTAINER_NAME="$(hostname)"
HOME_DIR="$(getent passwd "$JL_USER" | cut -d: -f6 2>/dev/null || echo "/home/$JL_USER")"
CONFIG_DIR="$HOME_DIR/.jupyter"
TOKEN_FILE="$HOME_DIR/.jupyter-token"
JUPYTER_CONFIG="$CONFIG_DIR/jupyter_lab_config.py"

mkdir -p "$CONFIG_DIR"
chown "$JL_USER":"$JL_USER" "$CONFIG_DIR" 2>/dev/null || true

# ── 生成/读取 token ───────────────────────────────────────────────────────────
if [ ! -f "$TOKEN_FILE" ]; then
    TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '+/=' | head -c 32)"
    echo "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    chown "$JL_USER":"$JL_USER" "$TOKEN_FILE" 2>/dev/null || true
else
    TOKEN="$(cat "$TOKEN_FILE")"
fi

# ── 写入 JupyterLab 配置 ──────────────────────────────────────────────────────
BASE_URL="${PATH_PREFIX}${CONTAINER_NAME}/jupyterlab/"

cat > "$JUPYTER_CONFIG" <<PYEOF
# 由 jupyter-init.sh 自动生成，请勿手动修改
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.port = ${JL_PORT}
c.ServerApp.base_url = '${BASE_URL}'
c.ServerApp.open_browser = False
c.ServerApp.allow_remote_access = True
c.ServerApp.allow_origin = '*'
c.ServerApp.allow_origin_pat = '.*'
c.ServerApp.token = '${TOKEN}'
c.ServerApp.password = ''
c.ServerApp.disable_check_xsrf = False
# 允许来自反向代理的请求（Host 头不匹配时不拒绝）
c.ServerApp.allow_password_change = False
PYEOF

chown "$JL_USER":"$JL_USER" "$JUPYTER_CONFIG"
chmod 600 "$JUPYTER_CONFIG"

echo "jupyter init: container=${CONTAINER_NAME} base-url=${BASE_URL} port=${JL_PORT}"
echo "  token saved to ${TOKEN_FILE}"
