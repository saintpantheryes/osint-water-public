#!/usr/bin/env bash
# 单机版:在自己电脑上试用,不用买服务器和域名。表格、页面、后台任务都跑在本机的 Docker 里,浏览器打开 http://localhost:8080/portal/。
# 先装好 Docker Desktop(https://www.docker.com/products/docker-desktop/)并打开它。Windows 在 WSL 的 Ubuntu 里运行本脚本。
# 用法(在核心仓的检出里):
#   bash deploy/local/start.sh           第一次:装好并启动(要 5–15 分钟);以后:启动(改了配置、更新了程序也用它)
#   bash deploy/local/start.sh collect   马上补国别资料、采集一次(不用等每天 08:30),试用时先跑它就有东西看
#   bash deploy/local/start.sh status    看各部分是否在跑、后台任务最近的日志
#   bash deploy/local/start.sh stop      停下(数据留着,下次 start 接着用)
# 端口被占用时在前面加 OW_PORT=8090 换一个。电脑关机或 Docker 没开时后台任务不跑,开机后会补上错过的采集。
# 密钥:模型密钥等写在 ~/.osint-water/secrets.env(和服务器版同一个文件,只有你能读),本脚本只把它复制进本机 deploy/local/state/,不打印。
# 管理员邮箱与密码在 deploy/local/state/ADMIN.txt(只有你能读)。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
STATE="$HERE/state"
SECRETS="$HOME/.osint-water/secrets.env"
PORT="${OW_PORT:-8080}"
export OW_PORT="$PORT" OW_UID="$(id -u)" OW_GID="$(id -g)"
step() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
dc() { docker compose -f "$HERE/docker-compose.yml" "$@"; }

command -v docker >/dev/null 2>&1 || { echo "没找到 Docker:先装 Docker Desktop(https://www.docker.com/products/docker-desktop/),打开它,再重跑"; exit 2; }
docker info >/dev/null 2>&1 || { echo "Docker 没在运行:打开 Docker Desktop,等左下角显示 running,再重跑"; exit 2; }
docker compose version >/dev/null 2>&1 || { echo "缺少 docker compose:请把 Docker Desktop 更新到新版"; exit 2; }

case "${1:-start}" in
stop) dc stop; echo "已停下;数据都留着,下次 bash deploy/local/start.sh 接着用。"; exit 0;;
status) dc ps; echo; dc logs --tail 30 --no-log-prefix worker 2>/dev/null || true; exit 0;;
collect)
  echo "补国别资料、采集一次、建议商机(要十几分钟到半小时,看信源多少;日志会滚动显示)"
  for job in countries collect suggest; do
    dc run --rm --no-deps worker python -m core.cli run "$job" --profile private/profile.yaml || echo "  $job 没跑完(见上面的提示),接着做下一步"
  done
  echo "采集完了:刷新 http://localhost:$PORT/portal/ 看线索。"; exit 0;;
start) ;;
*) echo "用法:bash deploy/local/start.sh [start|collect|status|stop]"; exit 2;;
esac

step "1/5 本机配置"
(umask 077; mkdir -p "$STATE")
if [ ! -f "$STATE/.env" ]; then
  gen() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }
  pg="$(gen)"; rd="$(gen)"
  (umask 077; cat > "$STATE/.env" <<T
PUBLIC_ORIGIN=http://localhost:$PORT
SECRET_KEY=$(gen)
POSTGRES_HOST=teable-db
POSTGRES_PORT=5432
POSTGRES_DB=teable
POSTGRES_USER=teable
POSTGRES_PASSWORD=$pg
REDIS_HOST=teable-cache
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=$rd
PRISMA_DATABASE_URL=postgresql://teable:$pg@teable-db:5432/teable
BACKEND_CACHE_PROVIDER=redis
BACKEND_CACHE_REDIS_URI=redis://default:$rd@teable-cache:6379/0
T
  )
  echo "  生成了本机的数据库密码等(deploy/local/state/.env,只有你能读)"
fi
sed -i.bak "s#^PUBLIC_ORIGIN=.*#PUBLIC_ORIGIN=http://localhost:$PORT#" "$STATE/.env" && rm -f "$STATE/.env.bak"
# 私有配置:没有就用样板建一个本机目录(不必是 GitHub 仓库;以后要上服务器再推到私有仓库)
if [ ! -e "$ROOT/private" ]; then
  mkdir -p "$ROOT/private"
  echo "  建了本机的 private/(私有配置)"
fi
[ -f "$ROOT/private/profile.yaml" ] || { cp "$ROOT/profiles/example/profile.yaml" "$ROOT/private/profile.yaml"; echo "  用样板建了 private/profile.yaml(重点国别是肯尼亚、越南;以后按注释改)"; }
mkdir -p "$ROOT/private/portal/media" "$ROOT/portal/media"
[ -e "$ROOT/portal/config.json" ] || : > "$ROOT/portal/config.json"  # 嵌套挂载点要先在本机存在(两个都在 .gitignore 里)
[ -s "$ROOT/private/portal/config.json" ] || echo '{}' > "$ROOT/private/portal/config.json"
mkdir -p "$HOME/.osint-water" && chmod 700 "$HOME/.osint-water"
if [ ! -f "$SECRETS" ]; then
  (umask 077; cp "$ROOT/deploy/teable/secrets.env.example" "$SECRETS")
  echo "  新建了 $SECRETS(填模型密钥用,只有你能读)"
fi
touch "$STATE/worker.env"; chmod 600 "$STATE/worker.env"

step "2/5 下载与构建(第一次要几分钟)"
dc pull -q teable teable-db teable-cache caddy
dc build -q worker

step "3/5 启动表格"
dc up -d teable-db teable-cache teable

step "4/5 管理员、建库建表"
dc run --rm setup
# 模型密钥与告警心跳:从 secrets.env 按名字取值(不执行文件里的任何东西),和表格令牌一起放进 state/worker.env
secret() { sed -n "s/^$1=//p" "$SECRETS" | tail -1 | tr -d '\r' | sed -E "s/^[\"']//; s/[\"']\$//; s/[[:space:]]+\$//"; }
{
  grep -e '^TEABLE_URL=' -e '^TEABLE_TOKEN=' "$STATE/worker.env" || true
  for v in DEEPSEEK_API_KEY ANTHROPIC_API_KEY HEALTHCHECK_URL; do
    val="$(secret "$v")"
    [[ "$val" =~ ^[A-Za-z0-9._+/=:?\&%~-]*$ ]] || { echo "secrets.env 里的 $v 有空格、引号或中文字符,请重新复制后重跑" >&2; exit 1; }
    [ -z "$val" ] || echo "$v=$val"
  done
} > "$STATE/worker.env.new"
chmod 600 "$STATE/worker.env.new"; mv "$STATE/worker.env.new" "$STATE/worker.env"
if grep -q -e '^DEEPSEEK_API_KEY=' -e '^ANTHROPIC_API_KEY=' "$STATE/worker.env"; then echo "  模型密钥已配置"
else echo "  还没有模型密钥:先只用规则初筛。以后把 DEEPSEEK_API_KEY 填进 $SECRETS 再重跑本命令。"; fi

step "5/5 启动后台任务与页面"
dc up -d --force-recreate worker caddy   # 重建:页面配置是单个文件挂进去的,建表后要换上新的
for _ in $(seq 1 30); do
  code="$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "http://localhost:$PORT/portal/" || true)"
  [[ "$code" =~ ^(200|302)$ ]] && break; sleep 2
done
echo
if [[ "${code:-}" =~ ^(200|302)$ ]]; then
  echo "装好了。浏览器打开 http://localhost:$PORT/portal/"
  echo "管理员邮箱和密码在 deploy/local/state/ADMIN.txt(只有你能读;这里不显示)。"
  echo "想马上有线索看:bash deploy/local/start.sh collect(否则每天北京时间 08:30 自动采集)。"
else
  echo "都启动了,但 http://localhost:$PORT/portal/ 还打不开(HTTP ${code:-无响应})。等一分钟再试;还不行看:bash deploy/local/start.sh status"
fi
