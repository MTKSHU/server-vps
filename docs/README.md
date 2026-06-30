# 文档目录

这组文档按当前 server-vps 边界整理：平台服务独立运行，默认使用本地账号；外部 OIDC/CAS SSO、独立 Casdoor、监控系统和 TLS 入口都作为可选外部依赖接入。

## 推荐阅读顺序

1. [deployment.md](deployment.md)：先部署管理节点，确认 Web/API、数据库和 `port-router` 正常运行。
2. [authentication.md](authentication.md)：理解默认本地账号、可选平台自助注册、外部 OIDC/CAS SSO 和 Casdoor 待审导入。
3. [node-onboarding.md](node-onboarding.md)：把 GPU/存储节点接入平台。
4. [storage-user-data-sync.md](storage-user-data-sync.md)：理解用户目录、共享数据、模型资源和同步任务。
5. [architecture.md](architecture.md)：查看组件职责、数据流和当前架构边界。

## 部署

- [deployment.md](deployment.md)：管理节点部署、关键环境变量、端口模型、Agent 发布物、数据持久化、常用操作、TLS/反向代理。
- [server-environment-and-rollout.md](server-environment-and-rollout.md)：旧版服务器环境与上线说明的兼容入口，已指向当前部署边界。

## 认证

- [authentication.md](authentication.md)：本地账号默认启用；平台自助注册、外部 OIDC/CAS SSO 和 Casdoor 待审用户导入均为可选能力。

## 节点接入

- [node-onboarding.md](node-onboarding.md)：当前 GPU/存储节点接入流程，以 Web 控制台生成的 join token、环境文件和 systemd service 为准。
- [new-gpu-node-onboarding.md](new-gpu-node-onboarding.md)：旧版新 GPU 机器接入手册的兼容入口，已指向当前节点接入文档。
- [truenas-scale-storage-node-deployment.md](truenas-scale-storage-node-deployment.md)：TrueNAS SCALE 存储节点部署与接入说明。

## 存储与同步

- [storage-user-data-sync.md](storage-user-data-sync.md)：用户 Home、共享数据集、模型缓存、Scratch、Workspace 卷和跨节点同步。
- [storage-zfs-quota-plan.md](storage-zfs-quota-plan.md)：用户目录 ZFS dataset 与 quota 方案、迁移步骤和平台记录。

## 架构设计

- [architecture.md](architecture.md)：当前系统架构、后端职责、agent 职责、认证边界、端口转发和存储模型。
- [cluster-platform-design.md](cluster-platform-design.md)：旧版平台设计说明的兼容入口，已收敛到当前架构说明。

## 运维与发布

- [deployment.md](deployment.md)：包含 Compose 常用操作、备份建议、升级命令和 Agent 发布物说明。
- [node-onboarding.md](node-onboarding.md)：包含 `cluster-node-agent` 与 `cluster-agent-updater` 的安装、systemd 和验证步骤。
- [server-environment-and-rollout.md](server-environment-and-rollout.md)：保留宿主机环境建议和上线边界说明。

## 当前边界提醒

- 管理节点 Compose 只运行 `nginx`、`frontend`、`backend`、`postgres` 和 `port-router`。
- Casdoor 不在 server-vps Compose 中启动，也不由本仓库 nginx 反代 `/sso/`；如需使用，应作为外部 OIDC Provider 接入。
- Prometheus/Grafana 不在当前 Compose 文件中，节点 exporter 可由外部监控系统采集。
- 默认认证方式是本地账号密码；SSO 和平台自助注册需要显式启用。
