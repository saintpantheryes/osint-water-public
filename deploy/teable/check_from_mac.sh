#!/usr/bin/env bash
# 在 Mac 上运行:主机的例行检查,不动线上的 Teable 与数据。
#   1. 把本目录复制到主机(/root/teable-kit),换上新版的备份、恢复演练与恢复脚本(/opt/teable)
#   2. 看备份日志、定时任务、磁盘和备份文件
#   3. 现在备份一次,再用这份备份在临时数据库里做恢复演练(restore_drill.sh)
#   (旧系统已于 2026-10-03 删除,不再检查)
# 用法:bash deploy/teable/check_from_mac.sh <密钥文件.pem> <主机IP>
set -euo pipefail
KEY="${1:?密钥文件}"; IP="${2:?主机IP}"
KIT="$(cd "$(dirname "$0")" && pwd)"
chmod 600 "$KEY"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new root@"$IP")
"${SSH[@]}" "mkdir -p /root/teable-kit"
scp -i "$KEY" -o StrictHostKeyChecking=accept-new "$KIT"/* root@"$IP":/root/teable-kit/
"${SSH[@]}" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/teable
install -m 755 /root/teable-kit/backup.sh /root/teable-kit/restore_drill.sh /root/teable-kit/restore.sh ./
echo "== 备份日志(最近 10 行)"
tail -n 10 backup.log 2>/dev/null || echo "(还没有 backup.log)"
echo "== 定时任务"
crontab -l 2>/dev/null | grep backup.sh || echo "(没有备份定时任务:重跑 push_from_mac.sh 会配上)"
echo "== 磁盘"
df -h /opt/teable
echo "== 备份文件(最近 6 个)"
ls -lht backups 2>/dev/null | sed -n '2,7p' || echo "(还没有备份)"
echo "== 现在备份一次(写进 backup.log)"
bash backup.sh </dev/null 2>&1 | tee -a backup.log   # </dev/null:别让脚本里的 docker 读走这段远程命令
echo "== 恢复演练"
bash restore_drill.sh </dev/null
REMOTE
