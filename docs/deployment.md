# 管理节点部署

## 运行边界

`deploy/docker-compose.yml` 当前启动这些服务：

| 服务 | 作用 | 对外暴露 |
| --- | --- | --- |
| `nginx` | Web/API 入口 | `HTTP_PORT` |
| `frontend` | Vue 管理后台 | 仅 Compose 内部 |
| `backend` | FastAPI 后端 | 仅 Compose 内部 |
| `postgres` | 平台数据库 | 仅 Compose 内部 |
| `port-router` | 容器业务端口转发 | host network，监听平台分配端口 |
| `http-path-proxy` | 容器 Web 服务路径代理，支持 WebSocket | 仅 Compose 内部，由 `nginx` 转发 `/c/` |

Casdoor、Prometheus、Grafana 不属于当前 Compose 栈。它们可以独立部署，再通过 SSO 或监控配置与平台协作。

## 初始化

```bash
cp deploy/.env.example deploy/.env
```

至少修改：

```text
POSTGRES_PASSWORD=<强密码>
ADMIN_INITIAL_PASSWORD=<强密码>
PORT_ROUTER_TOKEN=<随机长令牌>
HTTP_PORT=80
```

SSO Provider、回调基础地址、注册策略和 Casdoor 待审导入参数在管理员登录后的“平台设置”中配置。

启动：

```bash
./scripts/docker-build-run.sh
```

健康检查：

```bash
curl http://127.0.0.1:${HTTP_PORT:-80}/api/health
docker compose -f deploy/docker-compose.yml ps
```

## 端口模型

用户容器端口分两类访问入口。

### TCP 端口转发

普通 TCP 端口分两层：

- `host_port`：用户访问管理节点时使用，例如 `管理节点IP:20080`。
- `node_port`：计算节点上的 Incus proxy 端口，由 agent 配置。

`port-router` 运行在 host network 中，定期从后端读取路由表，把 `host_port` 转发到对应节点的 `node_port`。

相关变量：

```text
PORT_RANGE_START=20000
PORT_RANGE_END=39999
NODE_PORT_RANGE_START=40000
NODE_PORT_RANGE_END=59999
PORT_ROUTER_SYNC_INTERVAL=3
```

### Web 路径代理

code-server、JupyterLab 和通用 Web 服务可通过路径访问：

```text
http://<管理节点>/c/<container-name>/<port-name>/
```

`nginx` 将 `/c/` 请求转发到 `http-path-proxy`，后者定期读取 `/api/internal/path-routes`，再连接目标节点上的 `node_port`。代理支持 WebSocket，适用于 code-server 终端和 Jupyter kernel 等长连接。

相关变量：

```text
PATH_PREFIX=/c/
PATH_PROXY_PORT=8890
PATH_ROUTER_SYNC_INTERVAL=5
```

## Agent 发布物

后端可通过 Docker 构建 agent 发布物。需要让 backend 容器能访问宿主机 Docker socket，并配置源码路径：

```text
AGENT_SOURCE_HOST_PATH=/mnt/data/apps/server-vps/agent
AGENT_RELEASE_MAX_MB=100
```

发布物保存在 Docker volume `agent-releases` 中。

管理员可在“节点管理”的 Agent 发布窗口中构建 stable/canary 版本、下载二进制、配置节点自动更新并手动触发升级。若节点启用了 `cluster-agent-updater.timer`，updater 会读取 `/etc/cluster-node-agent.env` 中的平台地址和节点 token，按平台配置拉取目标版本。

## 数据持久化

Compose volume：

- `postgres-data`：平台数据库。
- `agent-releases`：agent 二进制发布物。
- `hf-staging`：Hugging Face / ModelScope 后端下载内部暂存卷，容器内固定路径为 `/tmp/hf-staging`。

管理节点只保存平台数据库、agent 发布物和后端下载暂存。用户数据、公开数据集、模型资源、ZFS dataset、workspace 卷和节点本地缓存位于 storage/mixed 或 compute 节点的实际数据盘上，由节点 agent 上报和执行任务。

生产环境升级前建议备份：

```bash
docker compose -f deploy/docker-compose.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backup.sql
```

## 常用操作

```bash
docker compose -f deploy/docker-compose.yml logs -f backend
docker compose -f deploy/docker-compose.yml logs -f nginx
docker compose -f deploy/docker-compose.yml restart backend
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d --build
```

后端本地开发可直接运行：

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

或使用仓库入口脚本：

```bash
cd backend
RELOAD=true python server.py
```

## 文件浏览与节点 agent HTTP API

个人文件浏览、扫描、下载、预览会优先使用节点 agent 的 HTTP 文件 API，以减少 SSH 冷启动延迟。需要在 backend 和节点 agent 之间保持一致：

```text
NODE_AGENT_TOKEN=<与节点 agent 文件 API 一致的 token>
NODE_AGENT_FILES_PORT=8082
```

如果未配置或节点 agent HTTP API 不可用，后端会回退到已有的 SSH/SFTP 路径。

## 资源与任务限流

Hugging Face / ModelScope 下载、公开资源同步、镜像导出/分发和目录扫描都可能占用 CPU、磁盘和网络。管理节点与存储节点共用机器时建议设置：

```text
BACKEND_CPU_LIMIT=<管理节点可接受的 CPU 核数>
HTTPS_PROXY=<可选，用于访问 Hugging Face 等外部站点>
HTTP_PROXY=<可选>
NO_PROXY=<可选>
```

平台设置页还提供 `transfer_bandwidth_limit_mbps`，用于给部分后台传输任务提供统一限速依据。

## TLS 和反向代理

当前内置 nginx 只监听 HTTP。若生产环境需要 HTTPS，推荐在管理节点前方放置外部反向代理或负载均衡，并把请求转发到 `HTTP_PORT`。

启用 SSO 时，外部代理必须正确传递：

```text
Host
X-Forwarded-For
X-Forwarded-Proto
```

启用 SSO 时，在“平台设置”中确保回调基础地址与浏览器访问平台的公网地址一致。
