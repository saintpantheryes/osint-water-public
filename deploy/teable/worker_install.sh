#!/usr/bin/env bash
# 主机上(root):把后台任务(worker,架构方案第 4 节)装到表格所在的主机上的各步。由 Mac 上的 worker_from_mac.sh 依次调用;每步都能重复运行。
#   key [core|private]       打印该仓库用的部署密钥(公钥);没有就生成
#   check <仓库> [core|private]  部署密钥能不能读这个仓库(git@github.com:<所有者>/<仓库>.git)
#   has                      worker.env 里已经有哪些设置(只说有没有,不打印值)
#   env <变量名>              从标准输入读两行(模型密钥、告警心跳网址;空行 = 不改),写进 /opt/teable/worker.env(只 root 可读)
#   up <核心仓> <私有配置仓>   取两个仓库的 main、构建镜像、起后台任务、配每天的自动更新
#   status                   服务状态、最近日志
# 两个仓库(docs/PUBLISHING.md):公开核心仓 = 程序,检出到 OW_REPO(默认 /opt/osint-water);私有配置仓 = profile、市场分布表、模板、
# 页面配置,检出到 OW_PRIVATE(默认 /opt/osint-water-private),compose 把它挂成程序里的 private/。各用一把只读部署密钥:
# deploy_key_core(核心仓)、deploy_key(私有配置仓;就是拆分前那把,GitHub 上已登记)。
# 表格的令牌(TEABLE_URL、TEABLE_TOKEN)不在这里输入:先在 Mac 上跑 worker_token_from_mac.sh(机器账号的令牌;管理员令牌不上主机)。
set -euo pipefail
DIR="${DIR:-/opt/teable}"
REPO_DIR="${OW_REPO:-/opt/osint-water}"
PRIV_DIR="${OW_PRIVATE:-/opt/osint-water-private}"
KIT="$(cd "$(dirname "$0")" && pwd)"
KNOWN="$DIR/github_known_hosts"
ENVF="$DIR/worker.env"
ERR="$(mktemp)"
trap 'rm -f "$ERR"' EXIT
log() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m停在这里:%s\033[0m\n' "$*" >&2; exit 1; }
compose() { (cd "$DIR" && docker compose --profile worker "$@"); }
keyfile() { case "${1:-core}" in core) echo "$DIR/deploy_key_core";; private) echo "$DIR/deploy_key";; *) die "仓库种类只能是 core 或 private";; esac; }
# 用哪把密钥连 GitHub:ssh_for core|private
ssh_for() { export GIT_SSH_COMMAND="ssh -i $(keyfile "$1") -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN -o ConnectTimeout=15"; }

[ "$(id -u)" = 0 ] || die "请用 root 运行"
if [ ! -f "$DIR/docker-compose.yml" ] || [ ! -f "$DIR/.env" ]; then
  die "$DIR 里没有表格的安装:先用 push_from_mac.sh 装好表格"
fi

ensure_git() {
  command -v git >/dev/null 2>&1 && return 0
  echo "  安装 git"
  if command -v dnf >/dev/null 2>&1; then dnf install -y -q git; elif command -v yum >/dev/null 2>&1; then yum install -y -q git
  else apt-get update -qq && apt-get install -y -qq git; fi
}

# 取一个仓库的 main 到目录:checkout_repo <仓库> <目录> <core|private>
checkout_repo() {
  local repo="$1" dir="$2" kind="$3"
  ssh_for "$kind"
  if [ ! -d "$dir/.git" ]; then
    (umask 077 && git clone -q --branch main --single-branch "$repo" "$dir") || die "取 $kind 仓库失败(部署密钥、网络)"
  fi
  chmod 700 "$dir"  # 只 root 能进
  git -C "$dir" remote set-url origin "$repo"
  git -C "$dir" fetch -q origin main || die "取不到 GitHub 上的 $kind 仓库(网络或部署密钥)"
  git -C "$dir" checkout -q main 2>/dev/null || true
  git -C "$dir" merge -q --ff-only origin/main || die "主机上的 $kind 检出有本地改动,快进不了:ssh 上去看 git -C $dir status"
  echo "  $kind $(git -C "$dir" rev-parse --short HEAD)"
}

cmd="${1:-}"
case "$cmd" in
key)
  kind="${2:-core}"; key="$(keyfile "$kind")"
  [ "$KIT" = "$DIR" ] || install -m 644 "$KIT/github_known_hosts" "$KNOWN"
  if [ ! -f "$key" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "osint-water-$kind@$(hostname)" -f "$key"
    echo "  新生成部署密钥 $key(只 root 可读)" >&2
  fi
  chmod 600 "$key"
  cat "$key.pub"
  ;;

check)
  repo="${2:?仓库地址}"; kind="${3:-core}"
  ensure_git
  [ "$KIT" = "$DIR" ] || install -m 644 "$KIT/github_known_hosts" "$KNOWN"
  [ -f "$(keyfile "$kind")" ] || die "还没有 $kind 的部署密钥:先运行 worker_install.sh key $kind"
  ssh_for "$kind"
  if git ls-remote "$repo" refs/heads/main >/dev/null 2>"$ERR"; then
    echo "  部署密钥能读 $kind 仓库"
  else
    echo "  部署密钥还读不了 $kind 仓库:$(head -c 300 "$ERR")"
    exit 1
  fi
  ;;

has)
  have() { [ -f "$ENVF" ] && grep -q "^$1=." "$ENVF"; }
  echo "token=$(have TEABLE_TOKEN && echo 1 || echo 0) deepseek=$(have DEEPSEEK_API_KEY && echo 1 || echo 0)" \
       "anthropic=$(have ANTHROPIC_API_KEY && echo 1 || echo 0) heartbeat=$(have HEALTHCHECK_URL && echo 1 || echo 0)" \
       "private_repo=$([ -f "$DIR/private_repo" ] && cat "$DIR/private_repo" || echo -)"
  ;;

env)
  var="${2:?变量名}"
  [[ "$var" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "变量名不对:$var"
  IFS= read -r llm || true
  IFS= read -r hb || true
  [[ "$llm" =~ ^[A-Za-z0-9._+/=-]*$ ]] || die "密钥里有空格、引号或中文字符,请重新复制(本机没有保存)"
  [[ "$hb" =~ ^(https://[A-Za-z0-9._~:/?#@!$\&()*+,;=%-]*)?$ ]] || die "告警心跳网址要以 https:// 开头"
  [ -f "$ENVF" ] || die "还没有 $ENVF:先在 Mac 上运行 worker_token_from_mac.sh 存机器账号的令牌"
  grep -q '^TEABLE_TOKEN=.' "$ENVF" || die "$ENVF 里没有 TEABLE_TOKEN:先在 Mac 上运行 worker_token_from_mac.sh"
  umask 077
  get() { sed -n "s/^$1=//p" "$ENVF" | head -1; }
  [ -n "$llm" ] || llm="$(get "$var")"
  [ -n "$hb" ] || hb="$(get HEALTHCHECK_URL)"
  { grep -v -e "^$var=" -e "^HEALTHCHECK_URL=" -e "^#" "$ENVF" || true
    echo "# 后台任务的设置:只 root 可读,不进仓库;令牌由 worker_token_from_mac.sh 写,其余由 worker_install.sh env 写"
    [ -z "$llm" ] || echo "$var=$llm"
    [ -z "$hb" ] || echo "HEALTHCHECK_URL=$hb"
  } > "$ENVF.tmp"
  chmod 600 "$ENVF.tmp"
  mv "$ENVF.tmp" "$ENVF"
  echo "  worker.env:表格令牌已配置;$var $([ -n "$llm" ] && echo 已配置 || echo '未配置(只用规则初筛)');" \
       "告警心跳 $([ -n "$hb" ] && echo 已配置 || echo 未配置)"
  ;;

up)
  core="${2:?核心仓地址}"; priv="${3:?私有配置仓地址}"
  ensure_git
  [ "$KIT" = "$DIR" ] || install -m 644 "$KIT/github_known_hosts" "$KNOWN"
  [ -f "$(keyfile core)" ] && [ -f "$(keyfile private)" ] || die "还没有两把部署密钥:先运行 worker_install.sh key core / key private"
  [ -f "$ENVF" ] || die "还没有 worker.env:先在 Mac 上运行 worker_token_from_mac.sh"

  log "私有配置仓(main)到 $PRIV_DIR"
  if [ ! -d "$PRIV_DIR/.git" ] && [ -d "$REPO_DIR/.git" ] && [ "$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null)" = "$priv" ]; then
    # 拆分前的布局:程序检出就是这个仓库。原地改名,省一次克隆
    mv "$REPO_DIR" "$PRIV_DIR"
    echo "  拆分前的检出 $REPO_DIR 改作私有配置仓的检出"
  fi
  checkout_repo "$priv" "$PRIV_DIR" private
  [ -f "$PRIV_DIR/profile.yaml" ] || die "私有配置仓的 main 里没有 profile.yaml(整理私有配置仓的合并还没做?先合并,再重跑)"
  [ -f "$PRIV_DIR/portal/config.json" ] || die "私有配置仓里没有 portal/config.json(页面配置)"

  log "公开核心仓(main)到 $REPO_DIR"
  checkout_repo "$core" "$REPO_DIR" core
  [ -f "$REPO_DIR/core/worker.py" ] && [ -f "$REPO_DIR/portal/index.html" ] || die "核心仓里没有程序(core/worker.py、portal/index.html)"
  printf '%s\n' "$core" > "$DIR/core_repo"
  printf '%s\n' "$priv" > "$DIR/private_repo"

  log "配置:compose(profile worker)、每天的自动更新"
  mkdir -p "$DIR/caddy.d"
  if [ "$KIT" != "$DIR" ]; then  # 在套件目录(/root/teable-kit)里运行时,把新版配置换到 $DIR
    install -m 644 "$KIT/docker-compose.yml" "$KIT/Caddyfile" "$DIR/"
    install -m 755 "$KIT/worker_update.sh" "$KIT/worker_install.sh" "$DIR/"
    install -m 644 "$KIT/portal.caddy" "$DIR/caddy.d/portal.caddy"
  fi

  # 两个检出都只读挂进容器,私有配置仓挂在 private/、页面配置挂在 portal/config.json 之下:
  # 嵌套挂载点要先在主机上存在(两个路径都在 .gitignore 里,不会碍着更新)
  mkdir -p "$REPO_DIR/private" "$REPO_DIR/portal/media" "$PRIV_DIR/portal/media"  # 首页视频目录:源(私有配置仓)与挂载点(程序检出)都要先存在
  [ -e "$REPO_DIR/portal/config.json" ] || : > "$REPO_DIR/portal/config.json"

  log "构建后台任务镜像(第一次要几分钟)并启动"
  compose build worker || die "构建镜像失败"
  compose up -d --no-deps --force-recreate worker >/dev/null || die "启动失败:cd $DIR && docker compose --profile worker logs --tail 100 worker"
  git -C "$REPO_DIR" rev-parse HEAD > "$DIR/worker_rev"  # 跑的是哪一版(每天的自动更新据此判断要不要重启)
  git -C "$PRIV_DIR" rev-parse HEAD > "$DIR/private_rev"
  sleep 8
  if [ "$(docker inspect -f '{{.State.Running}}' "$(compose ps -q worker)" 2>/dev/null)" != true ]; then
    compose logs --tail 40 worker || true
    die "后台任务没起来(见上面的日志)"
  fi
  echo "  后台任务已启动"

  log "只读页面(/portal/):挂载程序里的 portal/ 与私有配置里的 config.json,重载 Caddy"
  compose up -d --no-deps caddy >/dev/null || die "Caddy 没起来:cd $DIR && docker compose logs --tail 50 caddy"
  # 挂载变了 compose 会重建容器,新容器本来就读的是现在的配置;刚起来的几秒内管理接口还没就绪,重载会失败,所以等它跑起来再试,试不成只提醒
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ "$(docker inspect -f '{{.State.Running}}' "$(compose ps -q caddy)" 2>/dev/null)" = true ] && break
    sleep 2
  done
  [ "$(docker inspect -f '{{.State.Running}}' "$(compose ps -q caddy)" 2>/dev/null)" = true ] \
    || die "Caddy 没起来:cd $DIR && docker compose logs --tail 50 caddy"
  # 页面配置是单个文件挂进来的:git 换了文件,没重建的容器还挂着旧的(见 worker_update.sh 的 caddy_config);不一样就重启 Caddy
  if ! compose exec -T caddy cat /srv/portal/config.json 2>/dev/null | cmp -s - "$PRIV_DIR/portal/config.json"; then
    compose restart caddy >/dev/null || die "Caddy 没重启起来:cd $DIR && docker compose logs --tail 50 caddy"
    echo "  页面配置有更新,已重启 Caddy"
    sleep 5
  fi
  sleep 3
  if ! compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    sleep 5
    compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
      || echo "  注意:重载 Caddy 没成功(容器已重建、用的已是新配置;如页面打不开再看:cd $DIR && docker compose logs --tail 50 caddy)"
  fi
  echo "  只读页面已挂上:https://$(sed -n 's/^TEABLE_HOST=//p' "$DIR/.env" | head -1)/portal/"

  # 每天北京时间 08:00(采集之前)取两个仓库的新提交;换算成主机本地时间写进 crontab
  at="$(TZ=Asia/Shanghai date -d 'today 08:00' +%s)"
  cron_time="$(date -d "@$at" '+%M %H')"
  ( crontab -l 2>/dev/null | grep -v 'teable/worker_update.sh' || true
    echo "$cron_time * * * $DIR/worker_update.sh >> $DIR/worker_update.log 2>&1" ) | crontab -
  echo "  每天自动更新:主机时间 $(echo "$cron_time" | awk '{print $2":"$1}')(北京 08:00)"
  ;;

status)
  log "服务"
  compose ps
  log "后台任务最近的日志"
  compose logs --tail 30 --no-log-prefix worker 2>/dev/null || echo "  还没有日志"
  log "只读页面"
  h="$(sed -n 's/^TEABLE_HOST=//p' "$DIR/.env" | head -1)"
  r="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$h:443:127.0.0.1" "https://$h/portal/" || true)"
  echo "  没登录打开 /portal/ → $r $(case "$r" in 302*"/auth/login?redirect=%2Fportal%2F") echo '✓ 先到登录页';; *) echo '✗ 应为 302 转到登录页';; esac)"
  n="$(curl -s --resolve "$h:443:127.0.0.1" "https://$h/auth/login" | grep -c '登录到全球水务开源情报' || true)"
  echo "  登录页 /auth/login → $(if [ "${n:-0}" -ge 1 ] 2>/dev/null; then echo '✓ 自己的登录页(标志、站名、首页视频)'; else echo '✗ 还是表格原来的登录页(portal.caddy 没更新或 Caddy 没重载)'; fi)"
  log "程序版本"
  echo "  核心仓检出 $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo ?);后台任务跑的 $(cut -c1-7 "$DIR/worker_rev" 2>/dev/null || echo ?)"
  echo "  私有配置仓检出 $(git -C "$PRIV_DIR" rev-parse --short HEAD 2>/dev/null || echo '?(还没拆成两个仓库)')"
  [ -f "$DIR/worker_update.log" ] && { echo "  最近一次自动更新:$(tail -1 "$DIR/worker_update.log")"; } || true
  ;;

*)
  echo "用法: bash $0 key [core|private] | check <仓库> [core|private] | has | env <变量名> | up <核心仓> <私有配置仓> | status"; exit 2;;
esac
