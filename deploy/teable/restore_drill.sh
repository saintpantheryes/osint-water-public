#!/usr/bin/env bash
# 恢复演练(在主机上,root):用 backups 里最新的数据库备份在一个临时数据库容器里恢复一遍,核对空间、库、表、字段、账号与线索条数
# (与线上现在的数对照),再看同一时间的附件包能不能完整读出;完了连同数据卷删掉临时容器。不碰线上的 Teable 与数据库。
# 用法:bash restore_drill.sh [backups/db-<时间>.sql.gz]        在 Mac 上用 check_from_mac.sh 可一步做完(先备份一次再演练)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
envget() {  # 从 .env 取一项;不 source(值里有空格等写法时 bash 会出错,compose 却认)
  local v
  v="$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}
POSTGRES_USER="$(envget POSTGRES_USER)"; POSTGRES_DB="$(envget POSTGRES_DB)"
NAME=teable-restore-drill
dump="${1:-$(ls -1t backups/db-*.sql.gz 2>/dev/null | head -1 || true)}"
if [ -z "$dump" ] || [ ! -f "$dump" ]; then
  echo "没有找到数据库备份(backups/db-*.sql.gz):先跑一次 bash $DIR/backup.sh"; exit 1
fi
stamp="$(basename "$dump" .sql.gz)"; stamp="${stamp#db-}"
assets="$(dirname "$dump")/assets-$stamp.tgz"
echo "数据库备份:$dump($(du -h "$dump" | cut -f1),备份时间 $(date -r "$dump" '+%FT%T%:z'))"
gzip -t "$dump" || { echo "失败:备份文件不完整(gzip 校验不过)"; exit 1; }
# pg_dump 写完时在末尾写"dump complete";没有的是半截(gzip 本身可能完好)
[[ "$(gunzip -c "$dump" | tail -n 10)" == *"PostgreSQL database dump complete"* ]] || { echo "失败:备份没写完(是半截)"; exit 1; }

# 临时库大约占备份解压后大小的 3 倍(含索引);磁盘不够就不做
raw_mb="$(( $(gzip -l "$dump" | awk 'NR==2 {print $2}') / 1048576 + 1 ))"
free_mb="$(df -Pm "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
if [ -n "$free_mb" ] && [ "$free_mb" -lt $((raw_mb * 3 + 512)) ]; then
  echo "失败:磁盘剩余 ${free_mb} MB,恢复这份备份大约要 $((raw_mb * 3 + 512)) MB;先清理或扩容"; exit 1
fi

image="$(awk '/^  teable-db:/ {f = 1} f && /image:/ {print $2; exit}' docker-compose.yml)"  # 与线上同一个 Postgres 镜像,不另下载
# 删容器连同它的数据卷(-v):Postgres 镜像把数据放在匿名卷里,只删容器会把恢复出来的整个库留在磁盘上
trap 'docker rm -fv "$NAME" >/dev/null 2>&1 || true' EXIT
docker rm -fv "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -e POSTGRES_USER="$POSTGRES_USER" -e POSTGRES_DB="$POSTGRES_DB" -e POSTGRES_PASSWORD=restore-drill \
  "${image:-postgres:15.4}" >/dev/null
for _ in $(seq 1 60); do  # 镜像初始化时先起一个临时服务,"init process complete"之后才是正式的
  if [[ "$(docker logs "$NAME" 2>&1)" == *"init process complete"* ]] && docker exec "$NAME" pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"; then
    break
  fi
  sleep 2
done
docker exec "$NAME" pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  || { echo "失败:临时数据库两分钟内没起来"; docker logs --tail 20 "$NAME"; exit 1; }

echo "在临时数据库里恢复…"
errs="$(mktemp)"
t0=$SECONDS
if ! gunzip -c "$dump" | docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>"$errs"; then
  echo "失败:恢复出错"; tail -n 20 "$errs"; rm -f "$errs"; exit 1
fi
rm -f "$errs"

COUNTS="select (select count(*) from space where deleted_time is null), (select count(*) from base where deleted_time is null),
  (select count(*) from table_meta where deleted_time is null), (select count(*) from field where deleted_time is null),
  (select count(*) from users where deleted_time is null and not coalesce(is_system, false))"
# 线索表的行数:表名在 table_meta.db_table_name(<库>.<表>),用 query_to_xml 在一条查询里数
LEADS="select (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', split_part(db_table_name, '.', 1),
  split_part(db_table_name, '.', 2)), false, true, '')))[1]::text from table_meta where name = '线索' and deleted_time is null
  order by created_time limit 1"
drill() { docker exec "$NAME" psql -At -F ' ' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"; }
live() { docker compose exec -T teable-db psql -At -F ' ' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1" </dev/null; }
read -r sp ba tb fd us <<<"$(drill "$COUNTS")"
read -r sp2 ba2 tb2 fd2 us2 <<<"$(live "$COUNTS" 2>/dev/null || echo "? ? ? ? ?")"
ld="$(drill "$LEADS" 2>/dev/null || true)"
ld2="$(live "$LEADS" 2>/dev/null || true)"
echo "恢复用了 $((SECONDS - t0)) 秒。恢复出来的(括号里是线上现在的):"
echo "  空间 $sp($sp2)、库 $ba($ba2)、表 $tb($tb2)、字段 $fd($fd2)、账号 $us($us2)、线索 ${ld:-?} 条(${ld2:-?} 条)"
if ! [[ "$tb" =~ ^[0-9]+$ ]] || [ "$tb" -eq 0 ]; then
  echo "失败:恢复出来的库里没有表,这份备份不能用"; exit 1
fi
if [[ "$ld2" =~ ^[0-9]+$ ]] && ! [[ "$ld" =~ ^[0-9]+$ ]]; then
  echo "失败:线上有线索表的数据,恢复出来的库里没有(数据可能不在这个数据库里,备份没包括)"; exit 1
fi

if [ -f "$assets" ]; then
  n="$(tar tzf "$assets" | wc -l)" || { echo "失败:附件包读不出来:$assets"; exit 1; }
  echo "附件包:$assets($(du -h "$assets" | cut -f1)),$n 个条目,完整读出"
else
  echo "注意:没有同一时间的附件包 $assets(附件没法一起恢复)"
fi
echo "恢复演练通过:这份备份可以完整恢复。临时数据库已删除,线上没有动。"
