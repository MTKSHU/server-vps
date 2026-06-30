# 新 GPU 机器接入手册

这份旧版手册已改为兼容入口。当前节点接入流程以平台“节点管理”页面生成的 token、环境文件和 systemd service 为准。

请阅读新的接入文档：

- [node-onboarding.md](node-onboarding.md)

关键变化：

- 每台节点必须使用独立 join token。
- `cluster-node-agent.service` 以 Web 控制台生成为准；仓库 `deploy/systemd/` 下也提供 node agent 和 updater 的示例 systemd 文件。
- Prometheus/Grafana 不在当前 Compose 栈中；节点 exporter 可交给外部监控系统采集。
