#!/usr/bin/env bash
# 每日备份:数据库 dump + 附件卷 → ./backups/{db,assets}-<日期>-<时分>.*(手动多备份一次不会盖掉当天定时的那份),保留 14 天。
# 文件的修改时间设为开始备份的时刻:恢复后在 monitor 里填的"备份时间"就是它(restore.sh 会打印)。恢复与演练见 README.md。
# 日志(backup.log,由定时任务追加):每次一行"备份完成";出错写 ERROR,磁盘剩余不到 2 GB 写 WARNING。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
trap 'echo "$(date "+%F %T") ERROR 备份失败(backup.sh 第 $LINENO 行),看上面的报错"' ERR
envget() {  # 从 .env 取一项;不 source(值里有空格等写法时 bash 会出错,compose 却认)
  local v
  v="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}
free_mb="$(df -Pm "$DIR" | awk 'NR==2 {print $4}')"
if [ "${free_mb:-0}" -lt 2048 ]; then
  echo "$(date '+%F %T') WARNING 磁盘剩余 ${free_mb} MB,不到 2 GB:删掉 backups 里用不着的旧备份,或给云盘扩容"
fi
mkdir -p backups
rm -f backups/*.part
start="$(date +%s)"
t="$(date -d "@$start" +%F-%H%M)"
# 先写成 .part,完整写完再改名:备份中途失败不会留下看起来完整的坏文件
docker compose exec -T teable-db pg_dump -U "$(envget POSTGRES_USER)" "$(envget POSTGRES_DB)" </dev/null | gzip > "backups/db-$t.sql.gz.part"
mv "backups/db-$t.sql.gz.part" "backups/db-$t.sql.gz"
touch -d "@$start" "backups/db-$t.sql.gz"
vol="$(docker volume ls -q | grep -E '_teable-data$' | head -1 || true)"
if [ -n "$vol" ]; then
  docker run --rm -v "$vol":/data:ro -v "$DIR/backups":/out alpine tar czf "/out/assets-$t.tgz.part" -C /data . </dev/null
  mv "backups/assets-$t.tgz.part" "backups/assets-$t.tgz"
  touch -d "@$start" "backups/assets-$t.tgz"
fi
find backups -type f -mtime +14 -delete
echo "$(date '+%F %T') 备份完成 db-$t.sql.gz($(du -h "backups/db-$t.sql.gz" | cut -f1))" \
  "$([ -n "$vol" ] && echo "assets-$t.tgz($(du -h "backups/assets-$t.tgz" | cut -f1))" || echo "没有找到附件卷,附件没备份")"
