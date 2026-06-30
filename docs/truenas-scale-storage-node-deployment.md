# TrueNAS SCALE 存储节点部署指南

本文以 TrueNAS SCALE 独立存储节点的部署形态为蓝本，说明如何把 TrueNAS SCALE 接入 server-vps 平台。

TrueNAS SCALE 底层虽然是 Linux，但它不是普通 Ubuntu 服务器。`cluster-node-agent` 在存储节点上需要直接执行 `zfs`、`rsync`、`ssh`、`rrsync`，还要创建用户目录和写入同步公钥；这些操作必须在 TrueNAS 宿主机上以足够权限运行，不能按计算节点的 Ubuntu/Incus 部署方式照搬。

## 适用边界

推荐把 TrueNAS SCALE 节点只作为 `storage` 节点使用：

- 承载 `/mnt/<pool>/cluster-storage` 作为平台数据根目录。
- 执行用户目录、共享数据集、模型资源的同步和校验任务。
- 执行用户 ZFS dataset 创建、quota 设置、mountpoint 设置和移除任务。
- 不在该节点上创建 Incus 计算容器；计算容器仍放在 Ubuntu GPU/计算节点。

实际生产建议把 agent 二进制放在一个由数据池承载的固定目录，例如：

```text
/mnt/data/cluster-agent/cluster-node-agent
/mnt/data/cluster-agent/cluster-agent-updater
```

这样比放在系统根分区更符合 TrueNAS 的升级模型。

## 1. 准备数据集

在 TrueNAS Web UI 中创建一个专用 dataset，例如：

```text
data/cluster-storage
```

对应宿主机路径：

```text
/mnt/data/cluster-storage
```

建议在 TrueNAS UI 中先把该 dataset 的 ACL 调整为适合平台管理的模式。最简单的部署方式是让 `cluster-node-agent` 以 `root` 运行，由 agent 自己创建下级目录、设置 owner/group 和权限。

如果要使用每个用户一个 ZFS dataset，建议最终形成类似结构：

```text
data/cluster-storage/users/<username>
data/cluster-storage/datasets
data/cluster-storage/models
```

平台中的用户 Home 路径对应：

```text
/mnt/data/cluster-storage/users/<username>
```

注意：如果路径写成 `/data/users/<username>`，需要额外创建 bind mount 或把 dataset mountpoint 改到 `/data`。在 TrueNAS 上更推荐直接使用 `/mnt/<pool>/...`，减少系统升级和 UI 管理时的意外。

## 2. 检查宿主机命令

在 TrueNAS 宿主机 Shell 中检查：

```bash
which zfs rsync ssh ssh-keyscan rrsync
zfs list
rsync --version
ssh -V
```

可用路径示例：

```text
/usr/sbin/zfs
/usr/bin/rsync
/usr/bin/ssh
/usr/bin/ssh-keyscan
/usr/bin/rrsync
```

如果在受限 shell、容器或 sandbox 中运行，`zfs list` 可能报：

```text
/dev/zfs and /proc/self/mounts are required
```

这通常说明命令没有运行在完整宿主机权限下。agent 必须由宿主机 systemd 直接拉起，不能放在 TrueNAS Apps、Docker 容器或其他隔离环境里。

## 3. 生成节点 token

管理员进入平台“节点管理”，为该 TrueNAS 节点生成独立 join token。

要求：

- 每台节点使用独立 token。
- token 只在创建后完整显示一次。
- 注册成功后 token 会绑定该节点，不能复用。
- 节点类型建议在平台中标记为 `storage`，避免调度计算容器到 TrueNAS。

## 4. 安装 agent 二进制

在管理节点构建 agent 发布物后，把二进制复制到 TrueNAS 的数据池目录：

```bash
mkdir -p /mnt/data/cluster-agent
install -m 0755 cluster-node-agent /mnt/data/cluster-agent/cluster-node-agent
install -m 0755 cluster-agent-updater /mnt/data/cluster-agent/cluster-agent-updater
```

## 5. 创建环境文件

创建 `/etc/cluster-node-agent.env`：

```text
CLUSTER_SERVER_URL=https://hpc.example.com
CLUSTER_NODE_TOKEN=<join-token>
CLUSTER_HOSTNAME=<TrueNAS>
CLUSTER_NODE_GROUP=storage
CLUSTER_DATA_PATH=/mnt/data/cluster-storage
CLUSTER_INCUS_STORAGE_POOL=
CLUSTER_TASK_POLL_INTERVAL=5
CLUSTER_AGENT_BINARY=/mnt/data/cluster-agent/cluster-node-agent
CLUSTER_AGENT_SERVICE=cluster-node-agent.service
```

说明：

- `CLUSTER_DATA_PATH` 是平台在该 TrueNAS 节点上的数据根目录。
- `CLUSTER_INCUS_STORAGE_POOL` 对纯存储节点可以留空；如果平台页面模板强制生成，也不要让它指向不存在或不希望调度的 Incus pool。
- `CLUSTER_NODE_GROUP` 用于在平台上识别该节点用途，建议固定为 `storage`。
- `CLUSTER_HOSTNAME` 建议写稳定名称，不依赖 DHCP 或系统显示名变化。

设置权限：

```bash
chmod 0600 /etc/cluster-node-agent.env
chown root:root /etc/cluster-node-agent.env
```

## 6. 创建 systemd 服务

创建 `/etc/systemd/system/cluster-node-agent.service`：

```ini
[Unit]
Description=server-vps TrueNAS storage node agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/cluster-node-agent.env
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/mnt/data/cluster-agent/cluster-node-agent --server ${CLUSTER_SERVER_URL} --token ${CLUSTER_NODE_TOKEN} --hostname ${CLUSTER_HOSTNAME} --node-group ${CLUSTER_NODE_GROUP} --data-path ${CLUSTER_DATA_PATH} --interval 2
User=root
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动：

```bash
systemctl daemon-reload
systemctl enable --now cluster-node-agent
journalctl -u cluster-node-agent -f
```

TrueNAS SCALE 的系统由平台管理，升级后需要复查自定义 systemd unit 是否仍在、是否仍启用：

```bash
systemctl is-enabled cluster-node-agent
systemctl status cluster-node-agent
```

## 7. 可选：自动更新

如果要使用平台的 agent 自动更新，创建 `/etc/systemd/system/cluster-agent-updater.service`。updater 会读取 `CLUSTER_AGENT_BINARY` 和 `CLUSTER_AGENT_SERVICE`，下载新版本后替换对应二进制并重启 agent 服务。

```ini
[Unit]
Description=server-vps TrueNAS storage node agent updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/cluster-node-agent.env
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/mnt/data/cluster-agent/cluster-agent-updater
User=root
Nice=10

[Install]
WantedBy=multi-user.target
```

再创建 `/etc/systemd/system/cluster-agent-updater.timer`：

```ini
[Unit]
Description=Run server-vps agent updater periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Unit=cluster-agent-updater.service

[Install]
WantedBy=timers.target
```

启用：

```bash
systemctl daemon-reload
systemctl enable --now cluster-agent-updater.timer
```

如果 TrueNAS 节点希望完全手动升级，可以不启用 updater，只在维护窗口替换 `/mnt/data/cluster-agent/cluster-node-agent` 后重启服务。

## 8. 配置平台存储路径

进入平台“存储中心 / 存储配置”，把默认路径调整为 TrueNAS 上的真实路径：

```text
用户目录根路径：/mnt/data/cluster-storage/users
公共数据集路径：/mnt/data/cluster-storage/datasets
模型缓存路径：/mnt/data/cluster-storage/models
```

用户 ZFS dataset 任务应落到 TrueNAS storage 节点。创建或刷新用户 dataset 后，agent 会在宿主机执行：

```bash
zfs create -p <dataset>
zfs set quota=<N>G <dataset>
zfs set mountpoint=<mountpoint> <dataset>
```

因此 `dataset_name` 与 `mountpoint` 必须同时指向 TrueNAS 的同一数据池范围。不要让平台把 dataset 写到计算节点，也不要把 mountpoint 写到不存在的 `/data` 路径。

## 9. 验证

服务验证：

```bash
systemctl status cluster-node-agent
journalctl -u cluster-node-agent -n 100
curl -I https://hpc.example.com/api/health
```

ZFS 验证：

```bash
zfs list data/cluster-storage
zfs get mountpoint,quota data/cluster-storage
```

平台验证：

- 节点列表显示 TrueNAS 节点 `online`。
- 节点分组或类型为 `storage`。
- CPU、内存、磁盘容量已上报。
- 在“存储中心”创建测试用户 ZFS dataset 后，TrueNAS 上能看到对应 dataset 和 quota。
- 上传、浏览、删除“我的文件”正常。
- 如果启用了跨节点用户 home 备份，再从计算节点容器触发一次备份，确认 `data_sync_tasks` 和 `node_tasks` 都成功。

## 常见问题

`zfs command not found`

```text
systemd 服务 PATH 没包含 /usr/sbin，或 agent 没在宿主机运行。服务中显式设置 PATH。
```

`/dev/zfs and /proc/self/mounts are required`

```text
agent 运行环境被隔离，无法访问宿主机 ZFS 设备。不要用 TrueNAS Apps/Docker 跑 agent，改用宿主机 systemd。
```

`permission denied` 或无法写入 `/mnt/data/cluster-storage`

```text
检查服务是否以 root 运行，以及 TrueNAS dataset ACL 是否允许 root 管理下级目录。必要时在 TrueNAS UI 中重置该 dataset ACL。
```

用户 dataset 创建成功但路径不对

```text
检查平台存储配置中的根路径、任务 payload 的 mountpoint，以及 ZFS dataset 的 mountpoint 是否都使用 /mnt/<pool>/...。
```

同步任务报 `rrsync not found`

```text
确认 /usr/bin/rrsync 存在。若系统只有 rsync 包但没有 rrsync，需要把 rsync 自带脚本安装到 /usr/local/bin/rrsync，并设置可执行权限。
```

平台显示 agent 频繁掉线

```text
检查管理节点域名、TLS、反向代理和 /api/health。TrueNAS 节点上可以用 curl 直接访问平台地址定位网络或 502 问题。
```

TrueNAS 升级后服务不见了

```text
重新检查 /etc/systemd/system/cluster-node-agent.service、/etc/cluster-node-agent.env 和 enable 状态。二进制放在 /mnt/data/cluster-agent 这类数据池目录可以减少升级影响。
```
