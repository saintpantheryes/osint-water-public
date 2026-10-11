#!/usr/bin/env bash
# 真要恢复数据时用(在主机上,root):把 Teable 的数据库与附件换成某一份备份时的样子。会停掉表格、覆盖线上数据、所有人要重新登录。
# 用法:bash restore.sh backups/db-<时间>.sql.gz [--yes]        附件包自动取同一时间的 assets-<时间>.tgz
# 先把要用的文件都校验一遍,再动线上的东西。恢复完,脚本最后打印备份时间:在下一次定时任务之前 Run workflow,
# 「表格刚从备份恢复时填备份时间」一栏填它(先对齐到上次同步,免得拉回把系统也改回去)。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
envget() {  # 从 .env 取一项;不 source(值里有空格等写法时 bash 会出错,compose 却认)
  local v
  v="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}
dump="${1:-}"
if [ -z "$dump" ] || [ ! -f "$dump" ]; then
  echo "用法:bash $0 backups/db-<时间>.sql.gz [--yes]"
  echo "现有的备份(新的在前):"; ls -1t "$DIR"/backups/db-*.sql.gz 2>/dev/null | head -n 10 || true
  exit 2
fi
PGU="$(envget POSTGRES_USER)"; PGD="$(envget POSTGRES_DB)"; RP="$(envget REDIS_PASSWORD)"
stamp="$(basename "$dump" .sql.gz)"; stamp="${stamp#db-}"
indir="$(cd "$(dirname "$dump")" && pwd)"
assets="$indir/assets-$stamp.tgz"
echo "校验备份文件…"
gzip -t "$dump" || { echo "数据库备份不完整(gzip 校验不过),没有恢复"; exit 1; }
# pg_dump 写完时在末尾写"dump complete";没有的是半截(gzip 本身可能完好)
[[ "$(gunzip -c "$dump" | tail -n 10)" == *"PostgreSQL database dump complete"* ]] || { echo "数据库备份没写完(是半截),没有恢复"; exit 1; }
if [ -f "$assets" ]; then
  tar tzf "$assets" >/dev/null || { echo "附件包读不出来,没有恢复:$assets"; exit 1; }
else
  echo "注意:没有同一时间的附件包 assets-$stamp.tgz,只恢复数据库,附件保持现状"
fi
at="$(date -r "$dump" '+%FT%T%:z')"
echo "将用 $dump(备份时间 $at)$([ -f "$assets" ] && echo "与附件包")覆盖线上的表格数据;表格会停几分钟,所有人要重新登录。"
if [ "${2:-}" != "--yes" ]; then
  if ! read -r -p "确认请输入 YES:" ans </dev/tty; then
    echo "没法确认(没有终端):用 ssh -t 登录后运行,或在命令最后加 --yes"; exit 1
  fi
  [ "$ans" = YES ] || { echo "没有恢复"; exit 1; }
fi
trap 'echo "恢复中途出错(第 $LINENO 行):看上面的报错。表格可能还停着——数据库那一步出的错换一份备份再跑 restore.sh;其余的修好后再跑,或先 docker compose up -d 把表格起来"' ERR
# 各处 </dev/null:docker compose exec 会读走标准输入,这段命令经管道或 ssh 传进来时后面的命令会被吃掉
docker compose stop teable </dev/null
docker compose exec -T teable-db psql -q -v ON_ERROR_STOP=1 -U "$PGU" -d postgres \
  -c "DROP DATABASE \"$PGD\" WITH (FORCE)" -c "CREATE DATABASE \"$PGD\" OWNER \"$PGU\"" </dev/null
gunzip -c "$dump" | docker compose exec -T teable-db psql -q -v ON_ERROR_STOP=1 -U "$PGU" -d "$PGD" >/dev/null
if [ -f "$assets" ]; then
  vol="$(docker volume ls -q | grep -E '_teable-data$' | head -1 || true)"
  [ -n "$vol" ] || { echo "找不到附件卷(*_teable-data),附件没恢复;数据库已恢复,表格还停着:修好后再跑,或先 docker compose up -d"; exit 1; }
  docker run --rm -v "$vol":/data -v "$indir":/in:ro alpine sh -c "find /data -mindepth 1 -delete && tar xzf /in/assets-$stamp.tgz -C /data" </dev/null
fi
docker compose exec -T teable-cache redis-cli -a "$RP" --no-auth-warning FLUSHALL </dev/null >/dev/null  # 缓存与登录状态
docker compose up -d </dev/null
trap - ERR
echo "恢复完成(备份时间 $at)。"
echo "旧系统已停、不再同步,恢复后不用对齐;新版后台任务按本体只填空、只追加,重跑即可补齐(架构方案第 4 节)。"
