import os


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://cluster:cluster@localhost:5432/cluster")

STALE_AFTER_SECONDS = int(os.environ.get("NODE_STALE_AFTER_SECONDS", "180"))
PORT_RANGE_START = int(os.environ.get("PORT_RANGE_START", "20000"))
PORT_RANGE_END = int(os.environ.get("PORT_RANGE_END", "39999"))
NODE_PORT_RANGE_START = int(os.environ.get("NODE_PORT_RANGE_START", "40000"))
NODE_PORT_RANGE_END = int(os.environ.get("NODE_PORT_RANGE_END", "59999"))
PORT_ROUTER_TOKEN = os.environ.get("PORT_ROUTER_TOKEN", "")
CORS_ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")
SYNC_SSH_USER = os.environ.get("SYNC_SSH_USER", "root")
SYNC_SSH_PORT = int(os.environ.get("SYNC_SSH_PORT", "22"))
SYNC_SSH_IDENTITY_FILE = os.environ.get("SYNC_SSH_IDENTITY_FILE", "")
# 跳板机：当节点间无法直接 SSH 时，通过管理节点中转。格式：user@host:port（port 可省略默认 22）
# 示例：SYNC_SSH_JUMP_HOST=root@api.example.com 或 root@api.example.com:2222
SYNC_SSH_JUMP_HOST = os.environ.get("SYNC_SSH_JUMP_HOST", "")
ADMIN_INITIAL_PASSWORD = os.environ.get("ADMIN_INITIAL_PASSWORD", "change-me-now")
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "168"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
AGENT_RELEASE_DIR = os.environ.get("AGENT_RELEASE_DIR", "/var/lib/cluster-agent-releases")
AGENT_RELEASE_MAX_MB = int(os.environ.get("AGENT_RELEASE_MAX_MB", "100"))
# Agent 编译相关（通过 Docker 在容器内编译 agent 二进制）
# AGENT_SOURCE_HOST_PATH: 宿主机上 agent 源码目录（挂载到编译容器的 /src）
AGENT_SOURCE_HOST_PATH = os.environ.get("AGENT_SOURCE_HOST_PATH", "")

# Root disk size (/) for every container - fixed, not user-configurable
CONTAINER_ROOT_DISK_GB = int(os.environ.get("CONTAINER_ROOT_DISK_GB", "50"))

# HuggingFace / ModelScope 后端下载暂存目录。部署容器固定挂载到该路径，不再作为运维环境变量暴露。
HF_STAGING_DIR = "/tmp/hf-staging"

# HuggingFace 下载代理（国内服务器需要配置能访问 huggingface.co 的 HTTP/SOCKS 代理）
# 示例：http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
HF_HTTPS_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HF_HTTPS_PROXY", "")

RESOURCE_CONTAINER_STATUSES = (
    "provisioning",
    "starting",
    "running",
    "stopping",
    "restarting",
    "stopped",
    "deleting",
)
RUNNING_CONTAINER_STATUSES = ("provisioning", "starting", "running", "restarting", "deleting")
NODE_TYPES = ("compute", "storage", "app", "mixed")
