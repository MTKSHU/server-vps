#!/bin/bash
# jupyter-start.sh
# 以 ubuntu 用户身份启动 JupyterLab，使用 /opt/jupyter-venv 中的安装
set -euo pipefail

JL_USER="${JUPYTER_USER:-ubuntu}"

exec su -s /bin/bash "$JL_USER" -c \
    "exec /opt/jupyter-venv/bin/jupyter lab --config \"\$HOME/.jupyter/jupyter_lab_config.py\""
