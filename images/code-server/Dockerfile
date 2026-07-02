FROM ubuntu:24.04

# ─── 构建参数 ──────────────────────────────────────────────────────────────────
# code-server 版本；可通过 --build-arg 覆盖
ARG CODE_SERVER_VERSION=4.96.4
# 目标架构：amd64 | arm64
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive

# ─── 系统依赖 ──────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl wget ca-certificates gnupg \
        openssh-server rsync sudo \
        git vim nano htop \
        locales \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# ─── 安装 code-server ──────────────────────────────────────────────────────────
RUN curl -fsSL \
        "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_${TARGETARCH}.deb" \
        -o /tmp/code-server.deb \
    && dpkg -i /tmp/code-server.deb \
    && rm /tmp/code-server.deb

# ─── 创建默认用户（与平台 ssh_username 默认值保持一致） ────────────────────────
RUN useradd -m -s /bin/bash ubuntu \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

# ─── 初始化脚本（每次服务启动前执行） ─────────────────────────────────────────
COPY code-server-init.sh /usr/local/bin/code-server-init.sh
RUN chmod +x /usr/local/bin/code-server-init.sh

# ─── systemd 服务 ──────────────────────────────────────────────────────────────
COPY code-server.service /etc/systemd/system/code-server.service
# 在构建时创建 enable 符号链接（不依赖正在运行的 systemd）
RUN mkdir -p /etc/systemd/system/multi-user.target.wants \
    && ln -sf /etc/systemd/system/code-server.service \
              /etc/systemd/system/multi-user.target.wants/code-server.service

# ─── SSH 目录（平台 agent 会在运行时配置 authorized_keys） ───────────────────
RUN mkdir -p /run/sshd

EXPOSE 22 8080
