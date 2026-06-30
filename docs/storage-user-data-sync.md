# 用户目录、共享数据与存储同步

本文记录当前 server-vps 的存储模型：用户目录、共享数据集、模型资源、容器同步任务、ZFS 用户 dataset 和 workspace 卷都由平台统一维护，再通过节点任务交给 agent 执行。

## 数据分类

| 类型 | 默认源路径 | 容器内路径 | 默认权限 | 生命周期 |
| --- | --- | --- | --- | --- |
| 用户 Home | `/data/users/<username>` | `/home/<ssh_username>` | 读写 | 创建容器时准备，后续备份到存储节点 |
| 公共数据集 | `/data/datasets` | `/data/datasets` | 只读 | 管理员维护，容器按策略挂载 |
| 模型缓存 | `/data/models/huggingface` | `/data/models/huggingface` | 只读 | 支持 Hugging Face / ModelScope 资源请求、同步或预热 |
| Scratch | `/scratch/users/<username>` | `/scratch/<ssh_username>` | 读写 | 计算节点本地临时数据，不作为冷备份来源 |
| Workspace 卷 | Incus storage pool | `/workspace` | 读写 | 按用户和节点创建，可由管理员回收 |

## 已落地对象

后端启动迁移会创建：

- `user_data_policies`：每个用户的 home 源路径、备份开关、创建同步、停止回写和备份间隔。
- `shared_resources`：共享数据集和模型资源的源路径、容器路径、标签、只读开关和同步策略。
- `data_sync_tasks`：容器创建、手动同步、定时同步和备份产生的任务记录，由存储节点/计算节点 agent 消费。
- `user_storage_datasets`：用户 ZFS dataset 的平台记录和任务状态。
- `user_workspace_volumes`：用户在各节点上的 workspace 卷记录。

默认种子：

- `admin` 用户目录：`/data/users/admin`
- 公共数据集：`/data/datasets -> /data/datasets:ro`
- 模型缓存：`/data/models/huggingface -> /data/models/huggingface:ro`

## 容器创建流程

1. 前端只提交 `data_profile`，不再拼真实宿主机路径。
2. 后端根据当前用户、`ssh_username` 和策略生成挂载：
   - `default`：home + datasets + huggingface + scratch
   - `minimal`：home
3. 后端写入 `containers.mounts` 和 `containers.data_profile`。
4. 后端为用户 home 和需要 on-create/prewarm 的共享资源写入 `data_sync_tasks`。
5. agent 执行 `incus_create_container` 前会自动创建 `/data/...`、`/scratch/...`、`CLUSTER_DATA_PATH/...` 下缺失的挂载目录。

## 存储中心界面

“存储中心”当前提供：

- 我的文件：浏览、上传、下载、删除和预览个人目录文件，支持文件夹上传和分页浏览。
- 共享数据集：按名称、版本和标签筛选；管理员可校验、扫描、编辑标签和浏览资源文件。
- 模型资源：支持 Hugging Face / ModelScope 来源标识和下载进度展示。
- 资源请求：用户可提交数据集或模型资源请求，管理员审核后由平台下载到存储节点。
- 存储配置：管理员维护数据集、模型和用户目录根路径，以及 Hugging Face endpoint。
- ZFS 与 workspace：管理员可为用户创建或移除 ZFS dataset，并回收节点 workspace 卷。

## 跨节点同步 MVP

当前已实现 storage 节点主动拉取 compute 节点数据的第一版链路：

```text
data_sync_tasks
  -> node_tasks
  -> storage agent
  -> rsync over ssh 经源容器的 Incus node_port 拉取容器内 home
  -> 写入 storage 节点 target_path
  -> 回写 succeeded/failed
```

用户目录备份会优先选择该用户最近使用且正在运行的 compute/mixed 节点容器作为源，选择可用的 storage/mixed 节点作为目标。跨节点备份使用该容器 TCP/22 映射的 Incus `node_port`，连接地址为 `源节点 IP:node_port`，源路径为容器内 `/home/<ssh_username>`。如果找不到运行中的 compute 源容器，则回退为 storage 节点本地备份。

后端同步 SSH 配置通过环境变量控制：

```text
SYNC_SSH_USER=root
SYNC_SSH_PORT=22
SYNC_SSH_IDENTITY_FILE=
SYNC_SSH_PUBLIC_KEY=
```

`SYNC_SSH_PORT` 和 `SYNC_SSH_USER` 仍用于节点间的普通同步；用户 home 跨节点备份会改用源容器的 `node_port` 和 `ssh_username`。`SYNC_SSH_PUBLIC_KEY` 会在创建容器及端口同步时与用户公钥一同写入容器的 `authorized_keys`，对应私钥路径由 `SYNC_SSH_IDENTITY_FILE` 指定并必须存在于执行备份的 storage agent 节点。

部署要求：

- storage 节点必须安装 `rsync` 和 `ssh` client。
- compute 源容器必须安装 `rsync` 和 `sshd`，并配置 TCP/22 端口映射。
- storage 节点运行 agent 的系统用户必须能使用对应容器的 `ssh_username` 免密登录源容器。
- 如果使用专用密钥，把私钥放在 storage 节点本地，例如 `/root/.ssh/cluster-sync`，并设置 `SYNC_SSH_IDENTITY_FILE=/root/.ssh/cluster-sync`。
- 源容器对应用户的 `authorized_keys` 需要加入该公钥。

失败定位：

- `Permission denied (publickey,password)`：storage 节点到 compute 节点的 SSH 免密未配置。
- `rsync: command not found`：源节点或目标节点缺少 `rsync`。
- `No such file or directory`：源路径在 source 节点不存在，或用户目录主副本位置推断错误。
