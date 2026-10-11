#!/usr/bin/env bash
# 在 Mac 上运行:把主机上的 Teable 镜像固定到现在运行的版本(pin_teable.sh;不重启,不动数据)。架构方案阶段 A。
# 用法:bash deploy/teable/pin_from_mac.sh <密钥文件.pem> <主机IP>
set -euo pipefail
KEY="${1:?密钥文件}"; IP="${2:?主机IP}"
KIT="$(cd "$(dirname "$0")" && pwd)"
chmod 600 "$KEY"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new root@"$IP")
"${SSH[@]}" "mkdir -p /root/teable-kit"
scp -i "$KEY" -o StrictHostKeyChecking=accept-new "$KIT/pin_teable.sh" root@"$IP":/root/teable-kit/
"${SSH[@]}" "bash /root/teable-kit/pin_teable.sh /opt/teable </dev/null"
