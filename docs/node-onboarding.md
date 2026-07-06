# GPU/存储节点接入

这份文档描述如何把新节点接入当前 server-vps 平台。Web 控制台“节点管理”页也会生成相同内容，实际操作时以页面生成的 token 为准。

## 1. 节点准备

基础要求：

```bash
hostnamectl
timedatectl
ip addr
df -h
lsblk
```

GPU 节点检查：

```bash
nvidia-smi
nvidia-smi -L
```

Incus 检查：

```bash
incus version
incus storage list
incus network list
incus profile show default
```

建议统一：

```text
数据目录：/data
Incus storage pool：data
```

节点类型由平台配置，可选：

- `compute`：主要运行用户容器。
- `storage`：主要承载用户目录、公开资源、ZFS dataset 和存储镜像文件。
- `mixed`：同时承担计算和存储任务。
- `app`：预留给应用型节点，不作为默认 GPU 调度目标。

## 2. 生成 join token

管理员登录平台，进入“节点管理”，生成单节点 token。

要求：

- 每台节点使用独立 token。
- token 只在创建后完整显示一次。
- token 首次注册成功后会绑定节点，不能复用到其他节点。

## 3. 安装 agent

从平台下载发布物，或在管理节点构建后复制到新节点：

```bash
sudo install -m 0755 cluster-node-agent /usr/local/bin/cluster-node-agent
sudo install -m 0755 cluster-agent-updater /usr/local/bin/cluster-agent-updater
```

创建 `/etc/cluster-node-agent.env`：

```text
CLUSTER_SERVER_URL=https://hpc.example.com
CLUSTER_NODE_TOKEN=<join-token>
CLUSTER_DATA_PATH=/data
CLUSTER_INCUS_STORAGE_POOL=data
```

`CLUSTER_INCUS_STORAGE_POOL` 用于容器根盘、workspace 数据卷和节点磁盘容量上报；`CLUSTER_DATA_PATH` 仅用于主机目录挂载和旧任务兼容。

如需启用节点 agent HTTP 文件 API，让后端更快浏览个人文件，可在 agent 环境中加入与管理节点一致的 token 和端口（具体变量以当前 agent 启动参数/页面生成内容为准）：

```text
NODE_AGENT_TOKEN=<与 backend NODE_AGENT_TOKEN 一致>
NODE_AGENT_FILES_PORT=8082
```

如需固定主机名：

```text
CLUSTER_HOSTNAME=gpu-001
```

## 4. systemd 服务

创建 `/etc/systemd/system/cluster-node-agent.service`。Web 控制台生成的内容最贴合当前节点 token 和平台地址；仓库中的 `deploy/systemd/cluster-node-agent.service` 是可调整的示例模板。

```ini
[Unit]
Description=GPU cluster node agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/cluster-node-agent.env
ExecStart=/usr/local/bin/cluster-node-agent --server ${CLUSTER_SERVER_URL} --token ${CLUSTER_NODE_TOKEN} --data-path ${CLUSTER_DATA_PATH} --incus-storage-pool ${CLUSTER_INCUS_STORAGE_POOL} --interval 60
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cluster-node-agent
sudo journalctl -u cluster-node-agent -f
```

## 5. 自动更新

仓库提供 updater 的 systemd 文件：

```bash
sudo cp deploy/systemd/cluster-agent-updater.service /etc/systemd/system/
sudo cp deploy/systemd/cluster-agent-updater.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cluster-agent-updater.timer
```

updater 读取同一个 `/etc/cluster-node-agent.env`，使用节点 token 拉取平台配置的目标 agent 版本。

管理员在平台“节点管理”中构建 Agent 发布物后，可为节点配置 stable/canary、是否自动更新和目标版本；也可以在页面上手动触发单节点更新。

## 6. 验证

在平台节点列表确认：

- 节点状态为 `online`。
- CPU、内存、磁盘已上报。
- GPU 节点能看到 GPU 型号、显存和 UUID。
- Incus 状态正常。
- 如果是 storage/mixed 节点，存储卷、用户 dataset、公开资源缓存根目录正常上报。

创建一个测试容器，确认：

- 容器状态从 `provisioning` 变为 `running`。
- 容器 Shell 可连接。
- 分配 GPU 的容器内 `nvidia-smi` 可用。
- 端口映射可以通过管理节点公开端口访问。
- code-server、JupyterLab 或通用 Web 端口可以通过 `/c/<container-name>/<port-name>/` 路径访问。

## 7. 节点配置建议

节点上线后，在“节点管理”中检查并保存：

- 节点类型、是否可调度、维护模式。
- 最大容器数、最大运行容器数、GPU 共享上限和资源预留。
- Shell SSH 用户/端口、数据同步专用地址 `sync_ip` 和 `sync_ssh_port`。
- 公开资源本地缓存根目录 `resource_cache_base`。留空时使用节点数据盘下的默认 shared-cache；有本地 NVMe/SSD 时建议显式配置。
- WOL MAC 和广播地址（需要远程唤醒时）。

## 常见问题

token 过期或被使用：

```text
在节点管理页重新生成 token，并更新 /etc/cluster-node-agent.env。
```

agent 无法访问管理节点：

```bash
curl -v https://hpc.example.com/api/health
```

Incus 未就绪：

```bash
incus storage list
incus network list
journalctl -u incus -n 100
```

GPU 未上报：

```bash
nvidia-smi
which nvidia-smi
```
