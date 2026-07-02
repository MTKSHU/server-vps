#!/usr/bin/env bash
# build-incus-image.sh
# 使用 Incus 原生方式构建 code-server / jupyterlab 系统容器镜像。
#
# 说明：
#   以 LXC Ubuntu 24.04 cloud 镜像为基底（systemd 完整可用），通过
#   incus exec 安装软件、推入配置文件后发布为 Incus 镜像。
#   这比 Docker→OCI→Incus 方式更可靠，因为 LXC cloud 镜像天然支持
#   系统容器 + systemd，无需额外适配。
#
# 用法：
#   ./scripts/build-incus-image.sh code-server
#   ./scripts/build-incus-image.sh code-server --node gpu-node-01
#   ./scripts/build-incus-image.sh code-server --node gpu-node-01 --ssh-user root
#
# 参数：
#   <image>           必填：code-server | jupyterlab
#   --node HOST       可选：将镜像 scp + import 到该计算节点
#   --ssh-user USER   可选：SSH 用户名，默认 root
#   --version VER     可选：code-server 版本，默认 4.96.4
#   --arch ARCH       可选：amd64 | arm64，默认 amd64

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── 解析参数 ──────────────────────────────────────────────────────────────────
usage() {
  sed -n '3,20p' "$0" | sed 's/^# //'
  exit 1
}

IMAGE_TYPE="${1:-}"
case "$IMAGE_TYPE" in
  code-server|jupyterlab) ;;
  *) usage ;;
esac
shift

NODE_HOST=""
SSH_USER="root"
CS_VERSION="${CODE_SERVER_VERSION:-4.96.4}"
JL_VERSION="${JUPYTERLAB_VERSION:-4.3.6}"
ARCH="${ARCH:-amd64}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --node)     NODE_HOST="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --version)  CS_VERSION="$2"; JL_VERSION="$2"; shift 2 ;;
    --arch)     ARCH="$2"; shift 2 ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

ALIAS="cluster/${IMAGE_TYPE}"
BUILDER="img-builder-${IMAGE_TYPE}-$$"

# 自动探测第一个可用 storage pool（用于指定根磁盘）
STORAGE_POOL="${INCUS_STORAGE_POOL:-$(incus storage list --format csv 2>/dev/null | cut -d, -f1 | head -1)}"
if [[ -z "$STORAGE_POOL" ]]; then
  echo "错误：未找到 Incus storage pool，请手动设置 INCUS_STORAGE_POOL 环境变量" >&2
  exit 1
fi
echo "▶ 使用 storage pool: ${STORAGE_POOL}"

# ── 清理函数 ──────────────────────────────────────────────────────────────────
cleanup() {
  echo "▶ 清理构建容器 ${BUILDER}..."
  incus delete "$BUILDER" --force 2>/dev/null || true
}
trap cleanup EXIT

# ── 1. 启动基础容器 ───────────────────────────────────────────────────────────
echo "▶ 启动构建容器 ${BUILDER}（images:ubuntu/24.04）"
incus launch images:ubuntu/24.04 "$BUILDER" --storage "$STORAGE_POOL"

echo "▶ 等待容器网络就绪（最长 120 秒）..."
incus exec "$BUILDER" -- bash -c '
  for i in $(seq 1 60); do
    if getent hosts archive.ubuntu.com > /dev/null 2>&1; then
      echo "  网络就绪（第 $((i*2)) 秒）"
      break
    fi
    printf "  等待 DNS... %ds\r" "$((i*2))"
    sleep 2
    if [ "$i" -eq 60 ]; then
      echo "  警告：DNS 仍未就绪，继续尝试（可能安装失败）"
    fi
  done
'

# ── 2. 安装系统依赖 ───────────────────────────────────────────────────────────
echo "▶ 安装系统依赖"
incus exec "$BUILDER" -- bash -c '
  export DEBIAN_FRONTEND=noninteractive
  # 最多重试 3 次，忽略可选包下载失败
  for attempt in 1 2 3; do
    apt-get update -qq && break || echo "  apt-get update 第 $attempt 次失败，重试..."; sleep 3
  done
  apt-get install -y --no-install-recommends \
    -o Acquire::Retries=5 \
    --fix-missing \
    curl wget ca-certificates gnupg \
    openssh-server rsync sudo \
    git vim nano htop \
    locales
  locale-gen en_US.UTF-8
  rm -rf /var/lib/apt/lists/*
'

# ── 3. 安装目标软件 ───────────────────────────────────────────────────────────
if [[ "$IMAGE_TYPE" == "code-server" ]]; then
  echo "▶ 安装 code-server v${CS_VERSION} (${ARCH})"
  LOCAL_DEB="${SCRIPT_DIR}/code-server_${CS_VERSION}_${ARCH}.deb"
  if [[ -f "$LOCAL_DEB" ]]; then
    echo "  使用本地 deb 文件：${LOCAL_DEB}"
    incus file push "$LOCAL_DEB" "$BUILDER/tmp/code-server.deb"
  else
    echo "  从 GitHub 下载..."
    incus exec "$BUILDER" -- bash -c "
      curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors \
        'https://github.com/coder/code-server/releases/download/v${CS_VERSION}/code-server_${CS_VERSION}_${ARCH}.deb' \
        -o /tmp/code-server.deb
    "
  fi
  incus exec "$BUILDER" -- bash -c "dpkg -i /tmp/code-server.deb && rm /tmp/code-server.deb"
else
  echo "▶ 安装 JupyterLab v${JL_VERSION}（Python 3.12 venv）"
  incus exec "$BUILDER" -- bash -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv build-essential
    rm -rf /var/lib/apt/lists/*
  '
  incus exec "$BUILDER" -- bash -c "
    python3 -m venv /opt/jupyter-venv
    /opt/jupyter-venv/bin/pip install --no-cache-dir \
      'jupyterlab==${JL_VERSION}' \
      notebook ipywidgets numpy pandas matplotlib scipy
  "
fi

# ── 4. 创建默认用户 ───────────────────────────────────────────────────────────
echo "▶ 创建 ubuntu 用户"
incus exec "$BUILDER" -- bash -c '
  useradd -m -s /bin/bash ubuntu
  echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu
  chmod 0440 /etc/sudoers.d/ubuntu
  mkdir -p /run/sshd
'

# ── 5. 推入服务文件 ───────────────────────────────────────────────────────────
IMAGE_DIR="$REPO_ROOT/images/${IMAGE_TYPE}"
echo "▶ 推入 systemd 服务和初始化脚本"

if [[ "$IMAGE_TYPE" == "code-server" ]]; then
  incus file push "$IMAGE_DIR/code-server-init.sh" \
    "$BUILDER/usr/local/bin/code-server-init.sh" --mode 0755
  incus file push "$IMAGE_DIR/code-server.service" \
    "$BUILDER/etc/systemd/system/code-server.service"
  incus exec "$BUILDER" -- bash -c '
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf /etc/systemd/system/code-server.service \
           /etc/systemd/system/multi-user.target.wants/code-server.service
  '
else
  incus file push "$IMAGE_DIR/jupyter-init.sh" \
    "$BUILDER/usr/local/bin/jupyter-init.sh" --mode 0755
  incus file push "$IMAGE_DIR/jupyter-start.sh" \
    "$BUILDER/usr/local/bin/jupyter-start.sh" --mode 0755
  incus file push "$IMAGE_DIR/jupyterlab.service" \
    "$BUILDER/etc/systemd/system/jupyterlab.service"
  incus exec "$BUILDER" -- bash -c '
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf /etc/systemd/system/jupyterlab.service \
           /etc/systemd/system/multi-user.target.wants/jupyterlab.service
  '
fi

# ── 6. 收尾清理 ───────────────────────────────────────────────────────────────
echo "▶ 清理构建产物"
incus exec "$BUILDER" -- bash -c '
  apt-get clean 2>/dev/null || true
  rm -rf /tmp/* /var/cache/apt 2>/dev/null || true
  truncate -s 0 /var/log/*.log /var/log/**/*.log 2>/dev/null || true
  history -c 2>/dev/null || true
'

# ── 7. 停止 & 发布 ────────────────────────────────────────────────────────────
echo "▶ 停止构建容器"
incus stop "$BUILDER"

echo "▶ 发布 Incus 镜像（别名: ${ALIAS}）"
incus image delete "$ALIAS" 2>/dev/null || true
incus publish "$BUILDER" \
  --alias "$ALIAS" \
  --compression bzip2 \
  description="Ubuntu 24.04 + ${IMAGE_TYPE}"

# 取消 trap，手动删除容器
trap - EXIT
incus delete "$BUILDER"

# ── 8. 导入到远程节点（可选）─────────────────────────────────────────────────
if [[ -n "$NODE_HOST" ]]; then
  SSH_TARGET="${SSH_USER}@${NODE_HOST}"
  EXPORT_BASE="/tmp/${IMAGE_TYPE}-ubuntu24-$(date +%Y%m%d%H%M%S)"

  echo "▶ 导出镜像到 ${EXPORT_BASE}.tar.gz"
  incus image export "$ALIAS" "$EXPORT_BASE"

  echo "▶ 上传到 ${SSH_TARGET}"
  scp "${EXPORT_BASE}.tar.gz" "${SSH_TARGET}:/tmp/"
  rm -f "${EXPORT_BASE}.tar.gz"

  echo "▶ 在 ${NODE_HOST} 上导入"
  REMOTE_FILE="/tmp/$(basename "${EXPORT_BASE}").tar.gz"
  ssh "${SSH_TARGET}" bash -s <<REMOTE
set -e
incus image delete "${ALIAS}" 2>/dev/null || true
incus image import "${REMOTE_FILE}" --alias "${ALIAS}"
rm -f "${REMOTE_FILE}"
echo "  节点导入完成: ${ALIAS}"
REMOTE
fi

# ── 完成提示 ──────────────────────────────────────────────────────────────────
echo
echo "✅ 镜像构建完成"
echo
echo "本机 Incus 镜像列表："
incus image list "$ALIAS" --format table 2>/dev/null || true
echo
echo "━━ 下一步：注册到管理平台 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  1. 确认镜像已导入到每个计算节点（用 --node 参数或手动 scp + incus image import）"
echo "  2. 登录管理端 → 镜像管理 → 添加镜像，填写："
if [[ "$IMAGE_TYPE" == "code-server" ]]; then
echo "       ID:             code-server-ubuntu24"
echo "       名称:           Ubuntu 24.04 + code-server"
echo "       Incus 镜像引用: local:${ALIAS}"
echo "       CUDA 主版本:    0"
echo "       兼容节点池:     填写你的计算节点 driver_pool（多个用逗号分隔）"
else
echo "       ID:             jupyterlab-ubuntu24"
echo "       名称:           Ubuntu 24.04 + JupyterLab"
echo "       Incus 镜像引用: local:${ALIAS}"
echo "       CUDA 主版本:    0"
echo "       兼容节点池:     填写你的计算节点 driver_pool（多个用逗号分隔）"
fi
echo "  3. 用户创建容器时选择该镜像，SSH 端口和 code-server 端口会自动预填"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
