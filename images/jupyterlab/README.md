# JupyterLab 镜像模板

## 说明

基于 Ubuntu 24.04 + Python 3.12 的 [JupyterLab](https://jupyterlab.readthedocs.io/) Incus 系统容器镜像模板，提供浏览器内 Jupyter Notebook / Lab 访问。
JupyterLab 安装在 `/opt/jupyter-venv` 虚拟环境中（遵循 Ubuntu 24.04 的 PEP 668 限制）。

## 构建镜像

```bash
# 在管理节点或计算节点上执行
docker build \
  --build-arg JUPYTERLAB_VERSION=4.3.6 \
  -t cluster-jupyterlab:latest \
  ./images/jupyterlab/

# 将 OCI 镜像导入 Incus（在每个计算节点上执行）
docker save cluster-jupyterlab:latest | incus image import - --alias cluster/jupyterlab
```

## CUDA / GPU 版本

如需 GPU 支持，可在 `FROM` 行改用 CUDA 基础镜像：

```dockerfile
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu24.04
```

并在 venv 内额外安装：
```bash
/opt/jupyter-venv/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

## 在平台注册镜像

在平台管理界面的「镜像」页面添加：

| 字段 | 值 |
|------|-----|
| ID | `jupyterlab-ubuntu22` |
| 名称 | `Ubuntu 22.04 + JupyterLab` |
| Incus 镜像引用 | `local:cluster/jupyterlab` |
| CUDA 主版本 | `0`（无 GPU 加速，CUDA 版本填对应值） |

## 创建容器时的端口配置

创建容器时必须添加以下端口，否则路径代理无法工作：

| 端口名称 | 协议 | 容器端口 |
|---------|------|---------|
| `jupyterlab` | TCP | `8888` |
| `ssh` | TCP | `22` |

> **重要**：端口名称必须为 `jupyterlab`，路径代理通过此名称识别路由目标。

## 访问方式

容器创建并启动后，访问：

```
https://hpc.vmip.com.cn/c/<容器名>/
```

首次访问需要输入 token，通过 SSH 获取：

```bash
ssh ubuntu@<管理节点IP> -p <SSH端口>
cat ~/.jupyter-token
```

## 环境变量（Incus 容器配置）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JUPYTER_USER` | `ubuntu` | 运行 JupyterLab 的系统用户 |
| `JUPYTER_PORT` | `8888` | 监听端口 |
| `PATH_PREFIX` | `/c/` | 路径前缀，与 http-path-proxy 保持一致 |
