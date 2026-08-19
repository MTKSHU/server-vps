# 用户目录、公开资源与存储同步

本文记录当前 server-vps 的存储模型：用户目录、公开数据集、模型资源、资源请求、节点本地缓存、容器同步任务、ZFS 用户 dataset、workspace 卷和存储镜像文件都由平台统一维护，再通过节点任务交给 agent 执行。

## 数据分类

| 类型 | 默认源路径 | 容器内路径 | 默认权限 | 生命周期 |
| --- | --- | --- | --- | --- |
| 用户 Home | `/data/users/<username>` | `/home/<ssh_username>` | 读写 | 创建容器时准备，后续备份到存储节点 |
| 公共数据集 | `/data/datasets` | `/data/datasets` | 只读 | 管理员维护，容器按策略挂载 |
| 模型缓存 | `/data/models` | `/data/models` | 只读 | 支持 Hugging Face / ModelScope 资源请求、同步或预热 |
| Scratch | `/scratch/users/<username>` | `/scratch/<ssh_username>` | 读写 | 计算节点本地临时数据，不作为冷备份来源 |
| Workspace 卷 | Incus storage pool | `/workspace` | 读写 | 按用户和节点创建，可由管理员回收 |
| 节点本地资源缓存 | `<node-resource-cache-base>` | 按资源挂载 | 只读 | 将公开数据集/模型预热到计算节点本地盘，减少共享存储压力 |

## 已落地对象

后端启动迁移会创建：

- `user_data_policies`：每个用户的 home 源路径、备份开关、创建同步、停止回写和备份间隔。
- `shared_resources`：公开数据集和模型资源的源路径、容器路径、标签、只读开关和同步策略。
- `node_resource_cache` / `node_cache_inventory`：公开资源在各节点本地缓存的任务状态和实际文件清单。
- `data_sync_tasks`：容器创建、手动同步、定时同步和备份产生的任务记录，由存储节点/计算节点 agent 消费。
- `user_storage_datasets`：用户 ZFS dataset 的平台记录和任务状态。
- `user_workspace_volumes`：用户在各节点上的 workspace 卷记录。
- `storage_image_files`：从节点导出到存储节点的 Incus 镜像文件，可继续分发到其他节点。

默认种子：

- `admin` 用户目录：`/data/users/admin`
- 公共数据集：`/data/datasets -> /data/datasets:ro`
- 模型缓存：`/data/models -> /data/models:ro`

## 容器创建流程

1. 前端提交选中的公开资源、workspace/scratch 等选项，不直接拼真实宿主机路径。
2. 后端根据当前用户、`ssh_username`、选中的公开资源和策略生成挂载：
   - home：用户目录。
   - datasets/models：公开数据集和模型资源。
   - scratch：节点本地临时目录。
   - workspace：按用户和节点创建的 Incus volume。
   - node cache：已同步到节点本地的公开资源缓存。
3. 后端写入 `containers.mounts`、`container_resources` 和相关同步任务。
4. 后端为用户 home 和需要 on-create/prewarm 的公开资源写入 `data_sync_tasks`。
5. agent 执行 `incus_create_container` 前会自动创建 `/data/...`、`/scratch/...`、`CLUSTER_DATA_PATH/...` 下缺失的挂载目录。

## 存储中心界面

“存储中心”当前提供：

- 我的文件：浏览、上传、下载、删除和预览个人目录文件，支持文件夹上传和分页浏览。
- 公开数据集：按名称、提供者和标签筛选；管理员可校验、扫描、编辑标签和浏览资源文件。
- 模型资源：按名称、提供者和标签筛选，支持 Hugging Face / ModelScope 来源标识和下载进度展示。
- 资源请求：用户可提交数据集或模型资源请求，管理员可复用请求参数；平台后台下载到存储节点后自动注册为公开资源。
- 节点本地缓存：管理员可将公开数据集/模型同步到单个节点或全部在线节点，也可清理某节点缓存。
- 存储配置：管理员维护数据集、模型和用户目录根路径，以及 Hugging Face endpoint。
- ZFS 与 workspace：管理员可为用户创建或移除 ZFS dataset，并回收节点 workspace 卷。
- 存储镜像文件：管理员可从节点导出 Incus 镜像到存储节点、分发到其他节点或删除存储镜像文件。

说明：`shared_resources.version` 字段在数据库和 API 中继续保留以避免迁移风险，但界面和产品语义中显示为“提供者”，用于表达资源来源组织或作者，例如 `openmoss`、`openai`、`qwen`。
公开资源在存储节点上的标准目录为 `{base}/{provider}/{repo_name}`，例如 `/data/datasets/openmoss/IFMTBench` 或 `/data/models/qwen/Qwen2.5-7B-Instruct`；旧的 `{base}/{repo_name}/{provider}` 布局可通过管理端迁移任务转换。

## 个人文件访问路径

个人文件相关接口位于 `/api/storage/users/{user_id}/...`，包括列表、实时列表、上传、扫描、下载、预览和删除。

后端访问存储节点时优先使用节点 agent HTTP 文件 API：

```text
NODE_AGENT_TOKEN
NODE_AGENT_FILES_PORT=8082
```

节点侧变量名分别为 `CLUSTER_AGENT_FILES_TOKEN` 和
`CLUSTER_AGENT_FILES_PORT`；文件 API token 与每台节点独立的 join token 分开管理。

配置可用时，列目录和预览无需等待 SSH 冷启动；不可用时会回退到 SSH/SFTP 路径。上传仍受后端上传限制、用户 `storage_quota_gb` 和目标存储节点实际空间限制。

## 公开资源请求

资源请求接口 `/api/data/resource-requests` 当前支持：

- `source=huggingface`：后端创建公开资源记录后，下发 `download_shared_resource` 节点任务到在线 storage/mixed 节点；节点 agent 维护 Incus 系统下载容器，并在容器内按存储设置选择 Hugging Face 下载引擎。`auto` 模式会先从 `https://hf-mirror.com/hfd/hfd.sh` 安装/刷新官方 hfd 脚本并使用 hfd/aria2 下载，失败后回退 Hugging Face SDK；`hfd` 和 `sdk` 可强制指定单一引擎。下载支持 revision、token 和 `hf_endpoint`，启用镜像时会先尝试镜像，再回退官方默认端点。
- `source=modelscope`：同样由 storage/mixed 节点 agent 调用 Incus 下载容器执行，通过 ModelScope SDK 下载模型或数据集，支持 revision、token。

下载直接落在存储节点目标目录旁边的 `.<resource_id>.partial` 暂存目录，确保暂存目录与正式目录位于同一个 ZFS dataset/mountpoint；成功后由 agent 原子切换到公开数据集/模型的正式目录，并自动触发资源校验。管理节点只负责记录、下发任务和展示进度，不再承载大文件暂存和二次推送。

旧目录迁移接口为 `POST /api/data/shared-resources/migrate-provider-layout`。建议先用 `dry_run=true` 预览候选项，再正式执行；迁移任务由 storage/mixed 节点 agent 在本地执行，默认会在旧路径留下兼容 symlink，并在迁移后自动校验资源。

## 从容器上传自定义数据集/模型

除了平台自动从 HuggingFace/ModelScope 下载，用户也可以先在自己的容器里下载/准备好任意数据（含完全自定义的数据集或模型权重），再通过下面任一方式一步注册为公开资源：

- 前端：容器页面「同步设置」对话框新增「上传为公开数据集/模型」标签页。
- 脚本：`scripts/cluster-upload-resource.sh`（在容器内执行，需要个人 API Token，见 [authentication.md](authentication.md)），提交后持续轮询打印拉取进度和归档/校验结果，方便用户全程监督、随时了解失败原因。

后端接口 `POST /api/containers/{container_id}/upload-as-resource`：

1. 校验容器归属（本人或管理员）与命名（同名资源仅允许发起者在 `uploading`/`failed` 状态下重新提交）。
2. 以 `enabled=TRUE, request_status='uploading'` 创建（或复用）`shared_resources` 记录，`source_path` 先指向与最终目录同级的 `.{resource_id}.partial` 暂存目录。
3. 复用容器同步链路（`data_sync_tasks.task_type='shared_resource_upload'`），跨节点时同样通过一次性临时 SSH 密钥从容器直接 rsync 到存储节点暂存目录，无需在容器内预置任何到存储节点的常驻凭据。
4. 拉取成功后，后端自动下发 `migrate_shared_resource_path` 任务把暂存目录原子切换为正式目录，并按现有逻辑自动触发 `verify_shared_resource`：留空 `source` 时只做本地完整性检查；若额外提供 `source`(huggingface/modelscope) + `repo_id` + `revision`，则和平台自动下载的资源一样核对目录结构、文件数量与哈希。

删除权限与平台其它公开资源一致（仅管理员），上传发起人身份记录在 `shared_resources.requested_by`。

## 节点本地资源缓存

节点配置中的 `resource_cache_base` 用于设置公开资源本地缓存根目录；为空时使用节点数据盘根目录下的默认 shared-cache 路径。

主要流程：

1. 管理员在存储中心选择公开资源，触发“同步到节点”或“同步到全部在线节点”。
2. 后端创建节点任务，agent 将存储节点上的资源同步到目标节点本地缓存目录。
3. 容器页面可查看节点已缓存资源，并将缓存挂载到容器中。
4. 管理员可按节点和资源清理缓存。

适用场景：大模型或大数据集被多台容器重复读取时，先同步到本地 NVMe/SSD，再以只读挂载方式给容器使用。

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
