# code-server 镜像模板

## 说明

基于 Ubuntu 24.04 的 [code-server](https://github.com/coder/code-server) Incus 系统容器镜像模板，提供浏览器内 VS Code 访问。

## 构建镜像

```bash
# 在管理节点或计算节点上执行
docker build \
  --build-arg CODE_SERVER_VERSION=4.96.4 \
  -t cluster-code-server:latest \
  ./images/code-server/

# 将 OCI 镜像导入 Incus（在每个计算节点上执行）
docker save cluster-code-server:latest | incus image import - --alias cluster/code-server
```

## 在平台注册镜像

在平台管理界面的「镜像」页面添加：

| 字段 | 值 |
|------|-----|
| ID | `code-server-ubuntu22` |
| 名称 | `Ubuntu 22.04 + code-server` |
| Incus 镜像引用 | `local:cluster/code-server` |
| CUDA 主版本 | `0`（无 GPU 加速） |

## 创建容器时的端口配置

创建容器时必须添加以下端口，否则路径代理无法工作：

| 端口名称 | 协议 | 容器端口 |
|---------|------|---------|
| `code-server` | TCP | `8080` |
| `ssh` | TCP | `22` |

> **重要**：端口名称必须为 `code-server`，路径代理通过此名称识别路由目标。

## 访问方式

容器创建并启动后，访问：

```
https://hpc.vmip.com.cn/c/<容器名>/
```

初始密码可通过 SSH 获取：

```bash
ssh ubuntu@<管理节点IP> -p <SSH端口>
cat ~/.code-server-password
```

## 环境变量（Incus 容器配置）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CODE_SERVER_USER` | `ubuntu` | 运行 code-server 的系统用户 |
| `CODE_SERVER_PORT` | `8080` | 监听端口 |
| `PLATFORM_DOMAIN` | （空） | 反向代理域名，填写后 code-server 将接受来自该域名的请求 |
| `PATH_PREFIX` | `/c/` | 路径前缀，与 http-path-proxy 保持一致 |

设置环境变量（在计算节点执行）：

```bash
incus config set <容器名> environment.PLATFORM_DOMAIN hpc.vmip.com.cn
```
