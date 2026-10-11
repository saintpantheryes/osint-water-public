#!/usr/bin/env bash
# 主机上的定时任务(每天北京时间 08:00,worker_install.sh 配好):取两个仓库(公开核心仓 = 程序,私有配置仓 = 配置)main 的新提交并重启后台任务;
# 依赖清单或镜像定义变了先重建镜像。两个仓库都没有新提交、后台任务跑的也是现在的程序时什么也不做。
# 每次写一行结果(worker_update.log):"程序已是最新" / "已更新 …" / "ERROR …"(出错时后台任务照旧用手上的程序);
# 页面配置换了、Caddy 还挂着旧文件时,前面多一行"页面配置有更新,已重启 Caddy"。
# 手动更新:ssh 上去运行 bash /opt/teable/worker_update.sh。
set -uo pipefail
DIR="${DIR:-/opt/teable}"
REPO_DIR="${OW_REPO:-/opt/osint-water}"
PRIV_DIR="${OW_PRIVATE:-/opt/osint-water-private}"
cd "$DIR" || exit 1
now() { date '+%F %T'; }
compose() { docker compose --profile worker "$@"; }
ssh_for() { export GIT_SSH_COMMAND="ssh -i $DIR/$1 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$DIR/github_known_hosts -o ConnectTimeout=15"; }
stamp() { cat "$REPO_DIR/requirements.txt" "$REPO_DIR/deploy/teable/worker.Dockerfile" 2>/dev/null | sha256sum | cut -c1-16; }
restart() {
  mkdir -p "$REPO_DIR/private" "$REPO_DIR/portal/media" "$PRIV_DIR/portal/media"; [ -e "$REPO_DIR/portal/config.json" ] || : > "$REPO_DIR/portal/config.json"  # 嵌套挂载点(见 worker_install.sh)
  if ! compose up -d --no-deps --force-recreate worker >/dev/null 2>&1; then
    echo "$(now) ERROR 后台任务没起来:cd $DIR && docker compose --profile worker logs --tail 100 worker"
    exit 1
  fi
  git -C "$REPO_DIR" rev-parse HEAD > "$DIR/worker_rev"
  git -C "$PRIV_DIR" rev-parse HEAD > "$DIR/private_rev"
}
# 页面配置(私有配置仓的 portal/config.json)是单个文件挂进 Caddy 的;git 更新文件是删掉重建,起着的 Caddy 看到的还是旧文件,
# 页面就认不出新建的表和字段(如「城市」表)。和检出里的不一样就重启 Caddy(断几秒),重启后挂上的是新文件。
caddy_config() {
  [ -f "$PRIV_DIR/portal/config.json" ] || return 0
  compose exec -T caddy cat /srv/portal/config.json 2>/dev/null | cmp -s - "$PRIV_DIR/portal/config.json" && return 0
  if compose restart caddy >/dev/null 2>&1; then
    echo "$(now) 页面配置有更新,已重启 Caddy"
  else
    echo "$(now) ERROR 页面配置有更新,重启 Caddy 失败:cd $DIR && docker compose logs --tail 50 caddy"
  fi
}
# fetch_main <目录> <密钥文件名> <叫法>:取 origin/main;失败重试一次
fetch_main() {
  ssh_for "$2"
  git -C "$1" fetch -q origin main 2>/dev/null || { sleep 20; git -C "$1" fetch -q origin main; } \
    || { echo "$(now) ERROR 取不到 GitHub 上的$3(网络或部署密钥),后台任务照旧"; exit 1; }
}
behind() { git -C "$1" rev-list --count HEAD..origin/main; }

[ -d "$PRIV_DIR/.git" ] || { echo "$(now) ERROR 还没有私有配置仓的检出 $PRIV_DIR:在 Mac 上重跑 worker_from_mac.sh 完成拆分"; exit 1; }
fetch_main "$REPO_DIR" deploy_key_core 程序
fetch_main "$PRIV_DIR" deploy_key 私有配置
old="$(git -C "$REPO_DIR" rev-parse --short HEAD)"; old_p="$(git -C "$PRIV_DIR" rev-parse --short HEAD)"
running="$(cat "$DIR/worker_rev" 2>/dev/null || true)"; running_p="$(cat "$DIR/private_rev" 2>/dev/null || true)"
if [ "$(behind "$REPO_DIR")" = 0 ] && [ "$(behind "$PRIV_DIR")" = 0 ]; then
  caddy_config  # 没有新提交也查:前一次更新换了配置却没重启 Caddy 的(如这一版之前的更新脚本),这里补上
  if [ "$running" = "$(git -C "$REPO_DIR" rev-parse HEAD)" ] && [ "$running_p" = "$(git -C "$PRIV_DIR" rev-parse HEAD)" ] \
     && [ "$(docker inspect -f '{{.State.Running}}' "$(compose ps -q worker)" 2>/dev/null)" = true ]; then
    echo "$(now) 程序已是最新(程序 $old,配置 $old_p)"
    exit 0
  fi
  restart
  echo "$(now) 已按现在的程序重启(程序 $old,配置 $old_p)"
  exit 0
fi

before="$(stamp)"
for d in "$REPO_DIR" "$PRIV_DIR"; do
  if ! git -C "$d" merge -q --ff-only origin/main 2>/dev/null; then
    echo "$(now) ERROR 主机上的检出 $d 快进不了(有本地改动?):git -C $d status;后台任务照旧"
    exit 1
  fi
done
new="$(git -C "$REPO_DIR" rev-parse --short HEAD)"; new_p="$(git -C "$PRIV_DIR" rev-parse --short HEAD)"
# 新版配置(compose、更新脚本、只读页面的路由)也换上;路由变了重载 Caddy(页面文件本身是挂载的,随程序一起更新);
# compose 里 Caddy 的挂载变了(如首页视频目录)要重建 Caddy 容器才生效,中断几秒
compose_changed=0
cmp -s "$REPO_DIR/deploy/teable/docker-compose.yml" "$DIR/docker-compose.yml" || compose_changed=1
install -m 644 "$REPO_DIR/deploy/teable/docker-compose.yml" "$DIR/docker-compose.yml"
if [ "$compose_changed" = 1 ]; then
  mkdir -p "$REPO_DIR/portal/media" "$PRIV_DIR/portal/media"  # 嵌套挂载点:程序检出里的 portal/ 已只读挂进 Caddy,media 目录得先在主机上有
  compose up -d --no-deps caddy >/dev/null 2>&1 || echo "$(now) 注意:按新 compose 重建 Caddy 失败,只读页面的挂载没更新"
fi
install -m 755 "$REPO_DIR/deploy/teable/worker_update.sh" "$REPO_DIR/deploy/teable/worker_install.sh" "$DIR/"
if [ -f "$REPO_DIR/deploy/teable/portal.caddy" ] && ! cmp -s "$REPO_DIR/deploy/teable/portal.caddy" "$DIR/caddy.d/portal.caddy"; then
  install -m 644 "$REPO_DIR/deploy/teable/portal.caddy" "$DIR/caddy.d/portal.caddy"
  compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || echo "$(now) 注意:重载 Caddy 失败,只读页面的路由没更新"
fi
if [ "$(stamp)" != "$before" ] || ! docker image inspect osint-water-worker:local >/dev/null 2>&1; then
  if ! compose build worker >> "$DIR/worker_build.log" 2>&1; then
    echo "$(now) ERROR 依赖变了、重建镜像失败(见 $DIR/worker_build.log),先用旧镜像启动"
  fi
fi
restart
caddy_config
echo "$(now) 已更新 程序 $old → $new,配置 $old_p → $new_p"
