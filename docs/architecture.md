# 当前架构

server-vps 是一个轻量级 GPU 容器平台，核心思路是“中心管理、节点执行”。

## 组件

```text
Browser
  -> nginx
    -> frontend
    -> backend
      -> PostgreSQL
      -> node agent websocket / task queue
    -> http-path-proxy
      -> compute node Incus proxy
        -> container web service
  -> port-router
    -> compute node Incus proxy
      -> user container
```

## 后端职责

后端负责：

- 用户、角色、分组和额度。
- 用户偏好，例如节点/容器表格列配置。
- 节点注册、心跳和状态缓存。
- 镜像目录、agent 上报的本地镜像、存储镜像文件、镜像导出和节点间分发。
- 容器资源账本、调度和生命周期 API。
- 节点任务队列。
- 端口分配、TCP 路由表和 Web 路径路由表。
- 数据目录策略、同步任务、公开资源请求、节点本地缓存和 ZFS dataset 任务。
- Agent 发布物构建、上传、下载和节点升级策略。
- 本地账号、个人 API Token 和可选 SSO 登录。
- 平台设置、任务摘要、告警 webhook 和用户偏好。

后端不直接在管理节点创建用户容器，容器操作由目标节点 agent 执行。

## Agent 职责

`cluster-node-agent` 原生运行在 GPU/存储节点上，负责：

- 每 2 秒通过独立轻量接口上报 CPU、内存和 GPU 动态指标；该接口不触发容器、镜像或存储同步。
- 从完整心跳响应读取管理端采集策略，并在内存中热更新各轮询周期；systemd 参数只作为离线兜底。

- 采集宿主机 CPU、内存、磁盘、GPU 和 Incus 状态。
- 注册节点并定期心跳。
- 拉取节点任务。
- 执行 Incus 容器创建、启动、停止、删除、端口同步、SSH key 同步。
- 提供容器/节点 Shell 的 WebSocket 通道。
- 执行数据同步、公开资源本地缓存、存储镜像导入/推送和 ZFS 相关任务。
- 执行节点关机、重启、Agent 更新等运维任务。

## 认证边界

本仓库只管理 server-vps 的登录会话。外部身份系统不在本 Compose 中运行。

- 默认：本地账号密码。
- 可选：个人 API Token，明文只在创建时返回一次，后端保存 hash；当前 token 权限等同创建者账号。
- 可选：OIDC/CAS。
- 可选：外部 Casdoor 作为 OIDC Provider。

SSO 登录成功后，平台会创建或绑定 `users` 和 `user_identities` 记录；资源配额仍由平台自己的用户分组控制。

## 端口与 Web 服务访问

容器业务端口不直接要求用户访问计算节点。普通 TCP 端口由平台分配：

- 管理节点公开端口 `host_port`。
- 计算节点内部转发端口 `node_port`。

`port-router` 在管理节点监听 `host_port`，转发到节点 IP 的 `node_port`，再由 Incus proxy 转到容器内端口。

Web 服务还可以走路径代理：

```text
/c/<container-name>/<port-name>/
```

`http-path-proxy` 从 `/api/internal/path-routes` 获取 `(container_name, port_name) -> (node_ip, node_port)` 路由，支持 WebSocket。`web` 和 `code-server` 端口会裁掉路径前缀，`jupyterlab` 由镜像内 base URL 处理。

## 存储

平台当前把存储分为：

- 用户 home。
- 公共数据集。
- Hugging Face / ModelScope 模型缓存。
- scratch 临时目录。
- 用户 workspace 卷。
- 节点本地公开资源缓存。
- 存储节点上的 Incus 镜像文件。

容器创建时由后端生成挂载策略，agent 在节点上准备目录并交给 Incus 挂载。跨节点同步、ZFS quota 和 workspace 卷通过节点任务执行，任务结果回写到平台。

公开资源表仍使用 `version` 字段保持 API/数据库兼容，但产品语义已经调整为“提供者”（例如 openmoss、openai、qwen）。容器可按需拉取/挂载节点本地缓存，减少每次启动时从共享存储读取大资源。

## 前端入口

Web 控制台采用 Vue 3 + Vite + Element Plus，当前主要入口：

- 仪表盘：集群摘要和节点实时监控。
- 节点管理：节点接入、配置、Shell、Agent 发布与升级。
- 容器管理：容器创建、生命周期、端口、Shell、资源调整、镜像发布、数据同步。
- 镜像管理：平台镜像和节点本地镜像。
- 存储中心：个人文件、公开数据集、模型资源、资源请求、资源标签、节点本地缓存、ZFS 用户 dataset、workspace 卷和存储镜像文件。
- 用户管理：本地用户、SSO 待审用户、额度和节点权限。
- 个人信息：个人资料、密码、SSH 公钥、API Token 和个人额度。

## 主要 API 模块

- `/api/auth/*`：本地登录、注册、退出、密码修改和登录配置。
- `/api/auth/sso/*`：OIDC/CAS Provider、登录跳转和回调。
- `/api/me/*`：个人资料、SSH 公钥、API Token 和偏好。
- `/api/users`、`/api/quota-profiles`：用户、待审 SSO 用户、额度和节点权限。
- `/api/nodes`、`/api/node-join-tokens`、`/api/gpus`：节点注册、配置、运维动作和 GPU。
- `/api/containers`：容器生命周期、资源调整、Shell、端口、镜像发布、同步规则和节点缓存。
- `/api/images`、`/api/image-catalog`：平台镜像目录、Ubuntu remote、节点本地镜像拉取/复制/删除。
- `/api/data/*`、`/api/storage/users/*`：个人文件、公开资源、资源请求、存储设置、扫描、预览和下载。
- `/api/storage/*`：存储卷、ZFS 用户 dataset、workspace 卷、存储镜像文件和资源缓存同步。
- `/api/agent-releases`、`/api/agent-updates/*`：agent 发布物、下载、manifest 和节点更新回报。
- `/api/metrics/*`、`/api/tasks/recent`：集群摘要、节点监控历史和任务摘要。
