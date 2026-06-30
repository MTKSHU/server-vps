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
  -> port-router
    -> compute node Incus proxy
      -> user container
```

## 后端职责

后端负责：

- 用户、角色、分组和额度。
- 用户偏好，例如节点/容器表格列配置。
- 节点注册、心跳和状态缓存。
- 镜像偏好和 agent 上报的本地镜像。
- 容器资源账本、调度和生命周期 API。
- 节点任务队列。
- 端口分配和路由表。
- 数据目录策略、同步任务和 ZFS dataset 任务。
- Agent 发布物构建、上传、下载和节点升级策略。
- 本地账号和可选 SSO 登录。

后端不直接在管理节点创建用户容器，容器操作由目标节点 agent 执行。

## Agent 职责

`cluster-node-agent` 原生运行在 GPU/存储节点上，负责：

- 采集宿主机 CPU、内存、磁盘、GPU 和 Incus 状态。
- 注册节点并定期心跳。
- 拉取节点任务。
- 执行 Incus 容器创建、启动、停止、删除、端口同步、SSH key 同步。
- 提供容器/节点 Shell 的 WebSocket 通道。
- 执行数据同步和 ZFS 相关任务。
- 执行节点关机、重启、Agent 更新等运维任务。

## 认证边界

本仓库只管理 server-vps 的登录会话。外部身份系统不在本 Compose 中运行。

- 默认：本地账号密码。
- 可选：OIDC/CAS。
- 可选：外部 Casdoor 作为 OIDC Provider。

SSO 登录成功后，平台会创建或绑定 `users` 和 `user_identities` 记录；资源配额仍由平台自己的用户分组控制。

## 端口转发

容器业务端口不直接要求用户访问计算节点。平台分配：

- 管理节点公开端口 `host_port`。
- 计算节点内部转发端口 `node_port`。

`port-router` 在管理节点监听 `host_port`，转发到节点 IP 的 `node_port`，再由 Incus proxy 转到容器内端口。

## 存储

平台当前把存储分为：

- 用户 home。
- 公共数据集。
- Hugging Face / ModelScope 模型缓存。
- scratch 临时目录。
- 用户 workspace 卷。

容器创建时由后端生成挂载策略，agent 在节点上准备目录并交给 Incus 挂载。跨节点同步、ZFS quota 和 workspace 卷通过节点任务执行，任务结果回写到平台。

## 前端入口

Web 控制台采用 Vue 3 + Vite + Element Plus，当前主要入口：

- 仪表盘：集群摘要和节点实时监控。
- 节点管理：节点接入、配置、Shell、Agent 发布与升级。
- 容器管理：容器创建、生命周期、端口、Shell、资源调整、镜像发布、数据同步。
- 镜像管理：平台镜像和节点本地镜像。
- 存储中心：个人文件、共享数据集、模型资源、ZFS 用户 dataset、workspace 卷。
- 用户管理：本地用户、SSO 待审用户、额度和节点权限。
- 个人信息：SSH 公钥和个人额度。
