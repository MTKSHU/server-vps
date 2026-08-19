# Ubuntu 24.04 NVIDIA Docker Incus 镜像

平台可以提供预装 Docker Engine 和 NVIDIA Container Toolkit 的 Incus 系统容器镜像。NVIDIA 驱动不写入镜像：它由 GPU 节点宿主机安装，并由 Incus 在容器启动时注入设备和与驱动匹配的库。

## 构建和注册

在一台已初始化 Incus 且可以访问外网的 Ubuntu 节点上执行：

```bash
./scripts/build-incus-image.sh nvidia-docker
```

构建产物会保留国内软件源配置：Ubuntu 24.04（amd64）使用
`https://mirrors.bfsu.edu.cn/ubuntu/`，Docker CE 使用
`https://mirrors.bfsu.edu.cn/docker-ce/linux/ubuntu`。NVIDIA Container Toolkit
因北外镜像站没有对应仓库，继续使用 NVIDIA 官方软件源。

如需直接分发到计算节点：

```bash
./scripts/build-incus-image.sh nvidia-docker --node gpu-node-01
```

然后在管理端的「镜像管理」中添加：

- ID：`nvidia-docker-ubuntu24`
- 名称：`Ubuntu 24.04 + NVIDIA Docker`
- Incus 镜像引用：`local:cluster/nvidia-docker`
- CUDA 主版本：`0`
- 兼容节点池：选择已安装 NVIDIA 驱动的 GPU 节点池

## 宿主机前置条件

GPU 节点必须满足：

1. `nvidia-smi` 正常，且宿主机驱动支持用户要运行的 CUDA 镜像。
2. Incus 宿主机已安装 `libnvidia-container` 相关工具，使 `nvidia.runtime=true` 可用。
3. Incus 至少为 6.0.6 LTS，或使用已包含 2025 年 11 月 nesting AppArmor 修复的更新版本。Incus 6.0.4 与新版 `runc` 组合会使内层 Docker 报 `ip_unprivileged_port_start ... permission denied`；不要通过将 Incus 容器设为 privileged 或禁用 AppArmor 来规避。
4. Incus 存储驱动支持嵌套 OverlayFS；平台创建容器时会自动设置 `security.nesting` 以及 `mknod` / `setxattr` syscall interception。

不需要在 Incus 容器中再安装 NVIDIA 内核驱动。

## 验收

用户选择该镜像创建容器时，必须同时分配至少一张 GPU。进入容器后执行：

```bash
nvidia-smi
docker info
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

第三条命令显示的 GPU 应与平台分配给该 Incus 容器的 GPU 一致。

镜像内的 `nvidia-docker-incus-setup.service` 会在 Docker 启动前，根据 NVIDIA device minor number 为实际分配的 `/dev/nvidiaN` 自动创建嵌套 NVIDIA runtime 所需的 `/proc/driver/nvidia/gpus/<PCI>` 映射。它不会把未分配 GPU 的设备节点暴露给容器，也不依赖 Incus 容器内会被重排的 GPU index。

如遇到 `ip_unprivileged_port_start ... permission denied`，先升级宿主机 Incus 并重启该 Incus 容器。这是宿主机 Incus/AppArmor 与新版 `runc` 的兼容问题，不是容器内 Docker 组权限问题。

## 安全边界

该方案仍是「非特权 Incus 系统容器内运行嵌套 Docker」。开启 nesting 和 syscall interception 会扩大容器攻击面，应只对受信任的计算用户开放，并保持 Incus、宿主机内核、NVIDIA 驱动和 NVIDIA Container Toolkit 更新。
