# 用户目录 ZFS quota 方案

## 目标

“我的文件”使用每个用户一个独立 ZFS dataset 管理容量，例如：

```text
tank/data/users/<username>
```

平台中的 `storage_quota_gb` 作为权威配置。用户审核通过后创建目录时，同步创建或更新对应 dataset，并设置：

```bash
zfs set quota=<N>G tank/data/users/<username>
zfs set mountpoint=/data/users/<username> tank/data/users/<username>
```

当 quota 为 0 时，表示不限额：

```bash
zfs inherit quota tank/data/users/<username>
```

## 新用户流程

1. 管理员审核或创建平台用户，平台创建 `users`、`quotas`、`user_data_policies`。
2. 后端选择 online 的 storage/mixed 节点。
3. 后端下发节点任务 `ensure_user_zfs_dataset`。
4. 节点 agent 在宿主机执行幂等操作：
   - 确认父 dataset 存在。
   - `zfs create -p <pool>/data/users/<username>`。
   - 设置 mountpoint 到 `user_data_policies.home_path`。
   - 按 `quotas.storage_quota_gb` 设置 quota。
   - 设置目录 owner/group 和权限。
5. 任务成功后，“我的文件”即可通过现有 SFTP/扫描接口访问。

## 现有用户迁移

对每个 `/data/users/<username>`：

1. 暂停该用户上传、容器同步和后台扫描。
2. 创建临时 dataset：
   ```bash
   zfs create -p tank/data/users-new/<username>
   zfs set mountpoint=/data/users-new/<username> tank/data/users-new/<username>
   ```
3. 用 `rsync -aHAX --numeric-ids --info=progress2` 复制旧目录到新 dataset。
4. 二次增量同步一次，缩短切换窗口。
5. 原目录改名为备份：
   ```bash
   mv /data/users/<username> /data/users/<username>.bak-<date>
   zfs set mountpoint=/data/users/<username> tank/data/users-new/<username>
   ```
6. 设置 quota，刷新平台扫描缓存。
7. 验证文件数、容量、用户权限和页面浏览。
8. 稳定后销毁旧备份目录。

## 平台记录

当前平台使用 `user_storage_datasets` 表记录平台与 ZFS 的绑定关系：

```text
user_id, node_id, dataset_name, mountpoint, quota_gb,
status, last_error, created_at, updated_at
```

这样可以在用户额度变更时下发 quota 更新任务，并在页面显示 quota 是否已真正应用到存储节点。

管理员可在“存储中心”中对用户 dataset 执行创建、刷新和移除操作；实际 `zfs create`、`zfs set quota`、`zfs set mountpoint` 由 storage/mixed 节点 agent 执行。

## 注意事项

- 不建议多个用户共用一个 dataset 后只靠 `du` 或应用层限制；上传以外的写入路径会绕过应用层限制。
- ZFS quota 对 dataset 内所有写入生效，更适合 SFTP、容器同步、后台任务等多入口场景。
- 用户名必须继续保持平台现有的安全校验，只允许安全字符，避免拼接 dataset 名称时越界。
- 首次上线前先对 1 个测试用户演练迁移，再批量处理。
