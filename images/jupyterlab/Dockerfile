FROM ubuntu:24.04

# ─── 构建参数 ──────────────────────────────────────────────────────────────────
# Ubuntu 24.04 默认 Python 3.12
# JupyterLab 版本
ARG JUPYTERLAB_VERSION=4.3.6

ENV DEBIAN_FRONTEND=noninteractive

# ─── 系统依赖 ──────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl wget ca-certificates gnupg \
        openssh-server rsync sudo \
        git vim nano htop \
        python3 python3-pip python3-venv \
        build-essential \
        locales \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# ─── 安装 JupyterLab（使用 venv，遵循 Ubuntu 24.04 PEP 668 限制）────────────
# /opt/jupyter-venv 对所有用户可读，ubuntu 用户在运行时激活
RUN python3 -m venv /opt/jupyter-venv \
    && /opt/jupyter-venv/bin/pip install --no-cache-dir \
        "jupyterlab==${JUPYTERLAB_VERSION}" \
        notebook \
        ipywidgets \
        numpy pandas matplotlib scipy

# ─── 创建默认用户（与平台 ssh_username 默认值保持一致） ────────────────────────
RUN useradd -m -s /bin/bash ubuntu \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

# ─── 初始化脚本 ───────────────────────────────────────────────────────────────
COPY jupyter-init.sh /usr/local/bin/jupyter-init.sh
RUN chmod +x /usr/local/bin/jupyter-init.sh

# ─── 启动脚本（作为 ubuntu 用户运行 JupyterLab）─────────────────────────────
COPY jupyter-start.sh /usr/local/bin/jupyter-start.sh
RUN chmod +x /usr/local/bin/jupyter-start.sh

# ─── systemd 服务 ─────────────────────────────────────────────────────────────
COPY jupyterlab.service /etc/systemd/system/jupyterlab.service
RUN mkdir -p /etc/systemd/system/multi-user.target.wants \
    && ln -sf /etc/systemd/system/jupyterlab.service \
              /etc/systemd/system/multi-user.target.wants/jupyterlab.service

# ─── SSH 目录 ─────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd

EXPOSE 22 8888
