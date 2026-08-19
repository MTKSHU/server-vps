# NFS 持久 Home 上线指南

本功能默认关闭。必须先升级全部计算节点 agent、配置 TrueNAS NFS 和完成 canary，才能把 `shared_storage_mode` 改为 `enabled`。

节点可使用 `shared_storage_mode=inherit/disabled/enabled` 覆盖平台全局策略：

- `inherit`：默认值，跟随平台全局和 Canary 用户范围。
- `enabled`：适合 2.5/10GbE 且本地容量较小的节点；新容器使用 NFS Home，并在没有 ready 缓存时从 NFS 挂载公开资源。
- `disabled`：适合网络较慢但本地容量较大的节点；新容器不挂载 NFS Home，公开资源必须存在 ready 本地缓存，否则调度失败。

节点覆盖只影响新容器。升级前已有的容器、`managed_mounts=[]` 的容器和 legacy mounts 不会被自动改写、卸载或迁移。

## 1. TrueNAS 准备

继续使用 `/mnt/<pool>/cluster-storage/{users,datasets,models}`。在三个导出根目录分别创建内容相同的只读 sentinel，例如 `.server-vps-nfs`。文件内容必须与平台配置的 Sentinel 签名完全一致，建议使用至少 32 字节随机值。

- 使用 NFSv4.1/TCP并绑定存储 VLAN地址。
- `users` 仅允许计算节点读写；`datasets`、`models` 对计算节点只读。
- 保持 root squash，禁止 no_root_squash；防火墙仅允许存储 VLAN 的 TCP/2049。
- 用户目录为 ZFS 子 dataset 时先验证父级 share 能访问子 dataset；不能跨越时，通过 TrueNAS middleware 为每个用户维护独立 share，不要直接修改 `/etc/exports`。

## 2. 平台上线

1. 升级后端、前端和全部 agent，保持共享模式“关闭”。
2. 在“平台设置 → 共享 NFS”填写服务地址、三个导出、sentinel 文件名、签名和 idmap base。
3. 节点页确认新版 agent 能力。第一次容器任务执行受控挂载，之后心跳显示 NFS延迟。
4. 确保测试用户 ZFS dataset 已应用，切到 `canary` 并填写用户 ID。
5. 新建测试容器，验证持久 Home、只读公开资源和 workspace容量。
6. 旧容器先停止，再调用 `POST /api/containers/{id}/migrate-home`。主容器用 `primary=true`；其他容器用 `primary=false`，文件进入 `.migration/<container>/home` 而不覆盖主副本。
7. 完成故障演练后切换 `enabled`。

## 3. 故障与回滚

- agent 检查 `findmnt` 类型、精确服务端来源和 sentinel 签名；挂载配置以 `0600` 状态文件持久化，宿主重启后会自动尝试恢复。校验失败时容器创建/启动失败，不会创建本地替代数据目录。
- 必须使用 hard mount；平台和 agent 均拒绝 soft。
- 迁移只读取旧容器 Home，原 rootfs不会删除。回滚时停止容器、移除 managed Home device，再把 NFS增量同步回原 Home。
- 旧 workspace 标为 `legacy-persistent`，永不自动清理；只有新建 `temporary` workspace 在最后一个容器删除并过保留期后回收。

## 4. 验收

```bash
iperf3 -c <storage-ip>
ping -c 20 <storage-ip>
findmnt -t nfs,nfs4
incus config show <canary-container> --expanded
```

建议端到端 10GbE、MTU 1500、RTT 小于 1ms；仅在所有端点验证一致时启用 jumbo frame。TrueNAS建议 64GiB ECC、UPS、镜像启动盘，并配置小时/每日/月度快照及独立备份。
