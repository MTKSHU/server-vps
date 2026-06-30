# 平台设计说明

这份旧版设计文档已收敛为当前架构说明。

当前 server-vps 的真实边界是：

- 管理节点 Compose 只运行 `nginx`、`frontend`、`backend`、`postgres`、`port-router`。
- Casdoor 已独立运行，不再属于本仓库，也不再由本仓库 nginx 反代。
- Prometheus/Grafana 不在当前 Compose 栈中；如需监控，建议作为外部监控系统接入节点 exporter。
- 默认认证是本地账号密码；OIDC/CAS 是可选外部 Provider。

请阅读：

- [architecture.md](architecture.md)：当前架构和模块职责。
- [deployment.md](deployment.md)：管理节点部署。
- [authentication.md](authentication.md)：本地账号与外部 SSO。
- [node-onboarding.md](node-onboarding.md)：节点接入。
