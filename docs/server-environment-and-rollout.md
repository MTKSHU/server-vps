# 服务器环境与上线说明

这份旧版环境规划已收敛为当前部署文档。server-vps 现在按更清晰的边界运行：

- 管理节点：Docker Compose 运行平台自身服务。
- GPU/存储节点：原生运行 Incus、驱动和 `cluster-node-agent`。
- Casdoor：独立项目/独立服务，作为外部 OIDC Provider 接入。
- 监控：不再写入本仓库 Compose，建议外部 Prometheus/Grafana 或现有监控系统统一接入。

请阅读：

- [deployment.md](deployment.md)：管理节点部署和升级。
- [node-onboarding.md](node-onboarding.md)：GPU/存储节点接入。
- [architecture.md](architecture.md)：系统边界和数据流。

宿主机仍建议使用稳定的 Ubuntu Server LTS、最小化安装、固定数据盘挂载路径，并在接入平台前先验证 `nvidia-smi`、Incus storage pool 和网络连通性。
