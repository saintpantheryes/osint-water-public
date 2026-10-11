#!/usr/bin/env bash
# 在 Mac 上运行:把本目录复制到云主机并执行安装,最后把凭据打印到你的终端。
# 用法:bash deploy/teable/push_from_mac.sh <密钥文件.pem> <主机IP> <域名>
set -euo pipefail
KEY="${1:?密钥文件}"; IP="${2:?主机IP}"; HOST="${3:?域名}"
KIT="$(cd "$(dirname "$0")" && pwd)"
chmod 600 "$KEY"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new root@"$IP")
# 复制目录里的文件并覆盖(scp -r 整个目录到已存在的目标会变成子目录,导致跑的还是旧脚本)
"${SSH[@]}" "mkdir -p /root/teable-kit"
scp -i "$KEY" -o StrictHostKeyChecking=accept-new "$KIT"/* root@"$IP":/root/teable-kit/
"${SSH[@]}" "FORCE_STOP_WEB=${FORCE_STOP_WEB:-0} RESET_DB=${RESET_DB:-0} bash /root/teable-kit/install.sh '$HOST'"
echo; echo "===== 凭据(只在你的终端显示,请妥善保存) ====="
"${SSH[@]}" "cat /opt/teable/ADMIN.txt"
