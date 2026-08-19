#!/usr/bin/env bash
# cluster-upload-resource.sh
#
# 在自己的容器内下载/准备好数据集或模型之后，用这个脚本一步注册为平台的公开
# 数据集/模型：脚本会调用平台 API 创建资源记录并触发"容器 -> 存储节点"的拉取
# 任务，然后持续轮询打印进度，直到平台完成归档与校验（或失败），方便你随时
# 观察、中断（Ctrl+C 只会退出脚本的等待，不会取消已提交的任务）。
#
# 用法:
#   export CLUSTER_API_TOKEN=xxxx   # 在"个人信息"页创建的 API Token
#   ./cluster-upload-resource.sh \
#       --api-base https://cluster.example.com \
#       --container-name a100-mzn4 \
#       --path /workspace/my-dataset \
#       --type dataset \
#       --name my-dataset \
#       --provider myself \
#       [--tags nlp,zh] \
#       [--source huggingface --repo-id owner/repo --revision main] \
#       [--conflict overwrite|skip]
#
# --container-name 是你自己容器管理页面显示的容器名称（如 a100-mzn4），脚本会
#   自动调用 /api/containers 查出对应的容器 ID。
# --source 可选：如果你上传的其实是某个已知 HuggingFace/ModelScope 仓库，填写
#   后平台会额外核对目录结构、文件数量与哈希；留空则视为自定义数据，只做本地
#   完整性检查（非空、无残留的下载中间文件等）。

set -euo pipefail

API_BASE=""
CONTAINER_NAME=""
CONTAINER_PATH=""
RESOURCE_TYPE="dataset"
NAME=""
PROVIDER="default"
TAGS=""
SOURCE=""
REPO_ID=""
REVISION=""
CONFLICT="overwrite"
POLL_INTERVAL=5

usage() {
  grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --api-base) API_BASE="$2"; shift 2 ;;
    --container-name) CONTAINER_NAME="$2"; shift 2 ;;
    --path) CONTAINER_PATH="$2"; shift 2 ;;
    --type) RESOURCE_TYPE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --tags) TAGS="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --repo-id) REPO_ID="$2"; shift 2 ;;
    --revision) REVISION="$2"; shift 2 ;;
    --conflict) CONFLICT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1" >&2; usage ;;
  esac
done

: "${CLUSTER_API_TOKEN:?请先 export CLUSTER_API_TOKEN=<在个人信息页创建的 API Token>}"
[ -n "$API_BASE" ] || { echo "缺少 --api-base" >&2; exit 1; }
[ -n "$CONTAINER_NAME" ] || { echo "缺少 --container-name" >&2; exit 1; }
[ -n "$CONTAINER_PATH" ] || { echo "缺少 --path" >&2; exit 1; }
[ -n "$NAME" ] || { echo "缺少 --name" >&2; exit 1; }

command -v curl >/dev/null || { echo "需要 curl" >&2; exit 1; }
command -v jq >/dev/null || { echo "需要 jq（apt/yum/apk install jq）" >&2; exit 1; }

API_BASE="${API_BASE%/}"
AUTH_HEADER="Authorization: Bearer ${CLUSTER_API_TOKEN}"

echo "==> 按名称查找容器 ${CONTAINER_NAME}..."
containers_resp=$(curl -sS -f "${API_BASE}/api/containers" -H "$AUTH_HEADER")
CONTAINER_ID=$(printf '%s' "$containers_resp" | jq -r --arg name "$CONTAINER_NAME" '[.[] | select(.name == $name)] | .[0].id // empty')
[ -n "$CONTAINER_ID" ] || { echo "!! 未找到名为 ${CONTAINER_NAME} 的容器（确认名称拼写，以及该容器属于当前 Token 对应用户）" >&2; exit 1; }
echo "==> 容器 ${CONTAINER_NAME} 对应 ID=${CONTAINER_ID}"

# 把逗号分隔的 tags 转成 JSON 数组
tags_json="[]"
if [ -n "$TAGS" ]; then
  tags_json=$(printf '%s' "$TAGS" | tr ',' '\n' | jq -R . | jq -s .)
fi

body=$(jq -n \
  --arg resource_type "$RESOURCE_TYPE" \
  --arg name "$NAME" \
  --arg version "$PROVIDER" \
  --argjson tags "$tags_json" \
  --arg container_path "$CONTAINER_PATH" \
  --arg conflict_policy "$CONFLICT" \
  --arg source "$SOURCE" \
  --arg repo_id "$REPO_ID" \
  --arg revision "$REVISION" \
  '{resource_type: $resource_type, name: $name, version: $version, tags: $tags,
    container_path: $container_path, conflict_policy: $conflict_policy,
    source: $source, repo_id: $repo_id, revision: $revision}')

echo "==> 提交上传注册请求..."
resp=$(curl -sS -f -X POST "${API_BASE}/api/containers/${CONTAINER_ID}/upload-as-resource" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d "$body")

resource_id=$(printf '%s' "$resp" | jq -r '.resource.id')
sync_task_id=$(printf '%s' "$resp" | jq -r '.sync_task.id')
echo "==> 已创建资源 #${resource_id}，同步任务 #${sync_task_id}，开始拉取到存储节点..."

last_status=""
while true; do
  tasks=$(curl -sS -f "${API_BASE}/api/containers/${CONTAINER_ID}/sync-tasks" -H "$AUTH_HEADER")
  task=$(printf '%s' "$tasks" | jq -r --argjson id "$sync_task_id" '.[] | select(.id == $id)')
  status=$(printf '%s' "$task" | jq -r '.status // "unknown"')
  pct=$(printf '%s' "$task" | jq -r '.progress.pct // empty')
  rate=$(printf '%s' "$task" | jq -r '.progress.rate // empty')
  if [ "$status" != "$last_status" ] || [ -n "$pct" ]; then
    printf "\r==> 状态: %-10s 进度: %-5s%% %s\n" "$status" "${pct:-?}" "$rate"
    last_status="$status"
  fi
  case "$status" in
    succeeded) break ;;
    failed)
      echo "!! 同步失败: $(printf '%s' "$task" | jq -r '.last_error // .detail.error // "未知错误"')" >&2
      exit 1
      ;;
  esac
  sleep "$POLL_INTERVAL"
done

echo "==> 数据已同步到存储节点，等待平台归档与校验..."
while true; do
  resources=$(curl -sS -f "${API_BASE}/api/data/shared-resources" -H "$AUTH_HEADER")
  resource=$(printf '%s' "$resources" | jq -r --argjson id "$resource_id" '.[] | select(.id == $id)')
  request_status=$(printf '%s' "$resource" | jq -r '.request_status // "unknown"')
  check_status=$(printf '%s' "$resource" | jq -r '.check_status // "unknown"')
  echo "==> request_status=${request_status} check_status=${check_status}"
  case "$request_status" in
    ready)
      if [ "$check_status" = "ok" ]; then
        echo "✅ 上传完成并校验通过：${RESOURCE_TYPE} ${PROVIDER}/${NAME}（resource_id=${resource_id}）"
        exit 0
      fi
      ;;
    failed)
      echo "!! 校验/归档失败: $(printf '%s' "$resource" | jq -r '.check_error // "未知错误"')" >&2
      exit 1
      ;;
  esac
  sleep "$POLL_INTERVAL"
done
