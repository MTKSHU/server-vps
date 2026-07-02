#!/usr/bin/env bash
# build-container-image.sh
# 构建 code-server / jupyterlab Incus 系统容器镜像，并导入到指定计算节点的 Incus。
#
# 用法：
#   ./scripts/build-container-image.sh code-server
#   ./scripts/build-container-image.sh jupyterlab --node gpu-node-01
#   ./scripts/build-container-image.sh code-server --node gpu-node-01 --alias cluster/code-server
#
# 参数：
#   <image>         必填，镜像类型：code-server | jupyterlab
#   --node HOST     可选，目标计算节点 SSH 地址（不填则导入到本机 Incus）
#   --alias ALIAS   可选，Incus 镜像别名（默认 cluster/<image>）
#   --ssh-user USER 可选，SSH 用户名（默认 root）
#   --no-cache      传递给 docker build

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── 解析参数 ──────────────────────────────────────────────────────────────────
usage() {
  sed -n '2,20p' "$0" | sed 's/^# //'
  exit 1
}

IMAGE_TYPE="${1:-}"
case "$IMAGE_TYPE" in
  code-server|jupyterlab) ;;
  *) usage ;;
esac
shift

NODE_HOST=""
ALIAS=""
SSH_USER="root"
BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --node)      NODE_HOST="$2"; shift 2 ;;
    --alias)     ALIAS="$2"; shift 2 ;;
    --ssh-user)  SSH_USER="$2"; shift 2 ;;
    --no-cache)  BUILD_ARGS+=(--no-cache); shift ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

ALIAS="${ALIAS:-cluster/${IMAGE_TYPE}}"
LOCAL_TAG="cluster-${IMAGE_TYPE}:latest"
BUILD_CTX="$REPO_ROOT/images/${IMAGE_TYPE}"

# ── 构建 Docker 镜像 ──────────────────────────────────────────────────────────
echo "▶ 构建镜像  ${LOCAL_TAG}"
echo "  上下文:   ${BUILD_CTX}"
docker build "${BUILD_ARGS[@]}" -t "$LOCAL_TAG" "$BUILD_CTX"

# ── 导入到 Incus ──────────────────────────────────────────────────────────────
if [[ -z "$NODE_HOST" ]]; then
  echo "▶ 导入到本地 Incus（别名: ${ALIAS}）"
  docker save "$LOCAL_TAG" | incus image import - --alias "$ALIAS"
else
  SSH_TARGET="${SSH_USER}@${NODE_HOST}"
  TMP_LOCAL=$(mktemp /tmp/cluster-image-XXXXXX.tar)
  TMP_REMOTE="/tmp/cluster-image-$(date +%s).tar"

  echo "▶ 导出镜像 tar -> ${TMP_LOCAL}"
  docker save "$LOCAL_TAG" > "$TMP_LOCAL"

  echo "▶ 上传到 ${SSH_TARGET}:${TMP_REMOTE}"
  scp "$TMP_LOCAL" "${SSH_TARGET}:${TMP_REMOTE}"
  rm -f "$TMP_LOCAL"

  echo "▶ 在 ${NODE_HOST} 上导入 Incus（别名: ${ALIAS}）"
  # 如果已存在同名别名先删除，避免导入报错
  ssh "${SSH_TARGET}" bash -s << REMOTE_EOF
set -euo pipefail
if incus image list --format csv | grep -q "^${ALIAS},"; then
  echo "  删除旧镜像别名 ${ALIAS}"
  incus image delete "${ALIAS}" 2>/dev/null || true
fi
incus image import "${TMP_REMOTE}" --alias "${ALIAS}"
rm -f "${TMP_REMOTE}"
echo "  导入成功"
REMOTE_EOF
fi

# ── 打印后续操作提示 ──────────────────────────────────────────────────────────
cat <<INFO

✅ 完成！

下一步：在平台「镜像管理」中注册此镜像：
  ID（建议）：  ${IMAGE_TYPE}-ubuntu24
  名称：        ${IMAGE_TYPE^} (Ubuntu 24.04)
  Incus 引用：  local:${ALIAS}
  CUDA 主版本：  0

用户创建容器时，平台会自动检测镜像类型并预填以下端口：
$(if [[ "$IMAGE_TYPE" == "code-server" ]]; then
  echo "  code-server  TCP:8080  → 访问地址 https://<domain>/c/<container>/"
else
  echo "  jupyterlab   TCP:8888  → 访问地址 https://<domain>/c/<container>/"
fi)
INFO
