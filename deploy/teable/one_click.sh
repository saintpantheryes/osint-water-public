#!/usr/bin/env bash
# 一条命令从零装好整套系统(表格 + 建库建表 + 后台任务 + 页面),给 AI 助手代跑、也可以自己跑。可以重复运行:做完的步骤跳过,
# 停在哪一步重跑就从那里接着做。全程不需要在终端里输入任何东西。
# 用法(在核心仓的检出里运行;macOS、Linux 或 Windows 的 WSL):
#   bash deploy/teable/one_click.sh <密钥文件.pem> <主机IP> <域名> <私有配置仓 所有者/仓库名>
#   例:bash deploy/teable/one_click.sh ~/.ssh/<密钥>.pem <主机IP> water.example.org alice/osint-water-private
# 退出码:0 装好了;10 需要你在网页上做一件事(终端最后几行以「需要你做:」开头,做完重跑同一条命令);其他 = 出错。
#
# 密钥不经过聊天、不出现在命令行:
#   - 模型密钥、告警心跳网址、(可选)后台任务的表格令牌写在本机 ~/.osint-water/secrets.env(只有你能读),由你自己用编辑器填;
#     本脚本只把它们经 ssh 的标准输入送到主机上只 root 可读的 /opt/teable/worker.env,从不打印。
#   - 表格管理员的邮箱、密码与接口令牌留在主机的 /opt/teable/ADMIN.txt,另存一份到本机 ~/.osint-water/<域名>-ADMIN.txt(只有你能读),不打印。
#   - 建库建表时管理员令牌只放在本进程的环境变量里,经 ssh 隧道连主机上的表格,不需要域名先生效。
set -euo pipefail
KEY="${1:?用法:bash deploy/teable/one_click.sh <密钥文件.pem> <主机IP> <域名> <私有配置仓 所有者/仓库名>}"
IP="${2:?主机IP}"; HOST="${3:?域名}"; PRIV_ARG="${4:?私有配置仓 所有者/仓库名}"
HOST="$(printf '%s' "$HOST" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##')"
[[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "域名写成 water.example.org 这样"; exit 2; }
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "主机IP写成四段数字(云主机控制台里的公网 IP)"; exit 2; }
[ -f "$KEY" ] || { echo "找不到密钥文件 $KEY(云主机控制台创建密钥对时下载的 .pem)"; exit 2; }
KIT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$KIT/../.." && pwd)"
HOME_DIR="$HOME/.osint-water"
SECRETS="$HOME_DIR/secrets.env"
chmod 600 "$KEY"
OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
step() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
need() {  # 需要用户动手:说清楚做什么,退出码 10
  printf '\n\033[1;33m需要你做:\033[0m%s\n' "$1"
  shift; for l in "$@"; do printf '  %s\n' "$l"; done
  printf '做完以后重跑同一条命令,会从这里接着做。\n'
  exit 10
}
on_host() { ssh -n "${OPTS[@]}" root@"$IP" "$@"; }
# shellcheck disable=SC2029  # 参数都先校验过,有意在本机展开
kit() { ssh -n "${OPTS[@]}" root@"$IP" "bash /root/teable-kit/worker_install.sh $*"; }
# shellcheck disable=SC2029
kit_in() { ssh "${OPTS[@]}" root@"$IP" "bash /root/teable-kit/worker_install.sh $*"; }
slug_of() {
  printf '%s' "$1" | sed -E 's#^(https://([^@/]*@)?|ssh://git@|git@)github\.com[:/]+##; s#\.git$##; s#/$##' | grep -E '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || true
}
# 读 secrets.env 里一个值(不 source,不执行里面的任何东西)
secret() { [ -f "$SECRETS" ] && sed -n "s/^$1=//p" "$SECRETS" | tail -1 | tr -d '\r' | sed -E "s/^[\"']//; s/[\"']\$//; s/[[:space:]]+\$//" || true; }

core_slug="$(slug_of "$(git -C "$ROOT" config --get remote.origin.url || true)")"
[ -n "$core_slug" ] || { echo "要在从 GitHub 克隆下来的核心仓里运行(origin 应是 github.com 上的地址)"; exit 2; }
priv_slug="$(slug_of "$PRIV_ARG")"
[ -n "$priv_slug" ] || { echo "私有配置仓写成 所有者/仓库名"; exit 2; }
CORE="git@github.com:$core_slug.git"; PRIV="git@github.com:$priv_slug.git"
gh_ok() { command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; }

step "0/8 本机准备"
umask 077; mkdir -p "$HOME_DIR"; chmod 700 "$HOME_DIR"; umask 022
if [ ! -f "$SECRETS" ]; then
  (umask 077; cp "$KIT/secrets.env.example" "$SECRETS")
  echo "  新建了 $SECRETS(只有你能读)"
fi
chmod 600 "$SECRETS"
command -v python3 >/dev/null 2>&1 || need "本机装 Python 3(3.11 或更新)" "macOS:https://www.python.org/downloads/ 下载安装;Ubuntu/WSL:sudo apt install -y python3 python3-venv"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || need "本机的 Python 太旧(要 3.11 或更新)" "macOS:https://www.python.org/downloads/ 下载安装新版;Ubuntu/WSL:sudo apt install -y python3.11 python3.11-venv"
PY="$ROOT/.venv/bin/python"
if ! { [ -x "$PY" ] && "$PY" -c 'import httpx, yaml, ruamel.yaml' 2>/dev/null; }; then
  echo "  准备本机的 Python 环境(建表用,只装三个小包)"
  [ -x "$PY" ] || python3 -m venv "$ROOT/.venv" || need "本机的 Python 建不了虚拟环境" "Ubuntu/WSL:sudo apt install -y python3-venv"
  "$PY" -m pip install -q httpx pyyaml ruamel.yaml
fi
on_host true 2>/dev/null || need "本机连不上主机 root@$IP" \
  "1) 云主机控制台的安全组(防火墙)放行 TCP 22、80、443;" \
  "2) 主机要能用这把密钥以 root 登录(阿里云/腾讯云选 Ubuntu 22.04 或 24.04,创建时绑定密钥对;登录用户不是 root 的,先在控制台重置 root 密钥)。"
echo "  能连上主机;核心仓 $core_slug;私有配置仓 $priv_slug"

step "1/8 私有配置仓(本机检出在核心仓旁边,挂成 private/)"
if [ ! -e "$ROOT/private" ]; then
  dest="$(cd "$ROOT/.." && pwd)/${priv_slug#*/}"
  if [ ! -d "$dest/.git" ]; then
    git clone -q "$PRIV" "$dest" 2>/dev/null || git clone -q "https://github.com/$priv_slug.git" "$dest" 2>/dev/null \
      || { gh_ok && gh repo clone "$priv_slug" "$dest" -- -q; } \
      || need "在 GitHub 上建一个私有仓库 $priv_slug(空的就行),并让本机能推送到它" \
           "建仓库:https://github.com/new → Repository name 填 ${priv_slug#*/},选 Private,点 Create repository。" \
           "本机能推送:装好 GitHub CLI(https://cli.github.com)后运行 gh auth login,一路回车,按提示在浏览器里登录。"
  fi
  ln -s "$dest" "$ROOT/private"
  echo "  $dest → private/"
fi
[ "$(slug_of "$(git -C "$ROOT/private" config --get remote.origin.url || true)")" = "$priv_slug" ] \
  || { echo "本检出的 private/ 指的不是 $priv_slug;要换就先把 private 这个链接删掉再重跑"; exit 2; }
if [ ! -f "$ROOT/private/profile.yaml" ]; then
  cp "$ROOT/profiles/example/profile.yaml" "$ROOT/private/profile.yaml"
  echo "  用样板建了 private/profile.yaml(重点国别是肯尼亚、越南;以后按注释改)"
fi
priv_push() {  # 把私有配置的改动提交并推到 main
  local msg="$1"; shift
  git -C "$ROOT/private" add "$@"
  if ! git -C "$ROOT/private" diff --cached --quiet; then
    git -C "$ROOT/private" -c user.name="$(git config user.name || echo osint-water)" \
        -c user.email="$(git config user.email || echo osint-water@localhost)" commit -q -m "$msg"
  fi
  git -C "$ROOT/private" branch -M main
  # 已经推过、也没有新提交就不用推(上次推送失败留下的提交这次补推)
  git -C "$ROOT/private" fetch -q origin main 2>/dev/null || true
  if git -C "$ROOT/private" rev-parse -q --verify origin/main >/dev/null && [ -z "$(git -C "$ROOT/private" log --oneline origin/main..main)" ]; then
    return 0
  fi
  git -C "$ROOT/private" push -q -u origin main 2>/dev/null || { gh_ok && gh auth setup-git && git -C "$ROOT/private" push -q -u origin main; } \
    || need "本机推送不了私有配置仓 $priv_slug" "装好 GitHub CLI(https://cli.github.com)后运行 gh auth login,按提示在浏览器里登录。"
  echo "  已推到 $priv_slug 的 main"
}
priv_push "初始配置" profile.yaml

step "2/8 在主机上装表格(Docker、Teable、Caddy、管理员、每日备份;第一次要几分钟)"
on_host "mkdir -p /root/teable-kit"
scp -q "${OPTS[@]}" "$KIT"/* root@"$IP":/root/teable-kit/
if on_host "test -f /opt/teable/ADMIN.txt && grep -q '^TEABLE_HOST=$HOST\$' /opt/teable/.env"; then
  echo "  已装好,跳过"
else
  ssh -n "${OPTS[@]}" root@"$IP" "FORCE_STOP_WEB=${FORCE_STOP_WEB:-0} RESET_DB=${RESET_DB:-0} bash /root/teable-kit/install.sh '$HOST'" \
    | grep -v -e '^接口令牌' -e '^管理员密码' || true
  on_host "test -f /opt/teable/ADMIN.txt" || { echo "安装没完成,看上面的提示(80/443 被占用时在命令前加 FORCE_STOP_WEB=1 重跑)"; exit 1; }
fi
ADMIN_LOCAL="$HOME_DIR/$HOST-ADMIN.txt"
(umask 077; on_host "cat /opt/teable/ADMIN.txt" > "$ADMIN_LOCAL")
echo "  管理员邮箱、密码另存在本机 $ADMIN_LOCAL(只有你能读;这里不显示)"

step "3/8 后台任务的表格令牌(存在主机上只 root 可读的 worker.env)"
tt="$(secret TEABLE_TOKEN)"
if [ -n "$tt" ]; then
  [[ "$tt" =~ ^[A-Za-z0-9._+/=-]+$ ]] || need "secrets.env 里的 TEABLE_TOKEN 有空格或别的字符" "重新复制令牌,保存后重跑。"
  printf '%s\n' "$tt" | ssh "${OPTS[@]}" root@"$IP" 'set -e; umask 077; IFS= read -r T; F=/opt/teable/worker.env; touch "$F";
    { grep -v -e "^TEABLE_TOKEN=" -e "^TEABLE_URL=" "$F" || true; echo "TEABLE_URL=http://teable:3000"; echo "TEABLE_TOKEN=$T"; } > "$F.new";
    mv "$F.new" "$F"; chmod 600 "$F"'
  echo "  用 secrets.env 里的令牌"
elif [[ "$(kit has)" == *"token=1"* ]]; then
  echo "  主机上已有,跳过"
else
  on_host 'set -e; umask 077; T="$(sed -n "s/^接口令牌: *//p" /opt/teable/ADMIN.txt | head -1)"; [ -n "$T" ]; F=/opt/teable/worker.env; touch "$F";
    { grep -v -e "^TEABLE_TOKEN=" -e "^TEABLE_URL=" "$F" || true; echo "TEABLE_URL=http://teable:3000"; echo "TEABLE_TOKEN=$T"; } > "$F.new";
    mv "$F.new" "$F"; chmod 600 "$F"' || { echo "主机的 ADMIN.txt 里没有接口令牌"; exit 1; }
  echo "  用安装时生成的接口令牌(令牌只在主机上搬,没经过本机)"
fi
unset tt

step "4/8 建库、建表(经 ssh 隧道连主机上的表格;只增不删,重跑无妨)"
PORT=$(( 20000 + RANDOM % 20000 ))
ssh -n -N "${OPTS[@]}" -o ExitOnForwardFailure=yes -L "127.0.0.1:$PORT:127.0.0.1:3000" root@"$IP" &
TUN=$!
trap 'kill $TUN 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do curl -fs --noproxy '*' "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fs --noproxy '*' "http://127.0.0.1:$PORT/health" >/dev/null || { echo "ssh 隧道没通,连不上主机上的表格(cd /opt/teable && docker compose ps 看看)"; exit 1; }
(
  TEABLE_ADMIN_TOKEN="$(on_host "sed -n 's/^接口令牌: *//p' /opt/teable/ADMIN.txt | head -1")"
  [ -n "$TEABLE_ADMIN_TOKEN" ] || { echo "主机的 ADMIN.txt 里没有接口令牌"; exit 1; }
  export TEABLE_ADMIN_TOKEN TEABLE_URL="http://127.0.0.1:$PORT" NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost  # 本机开着代理时也直连隧道
  cd "$ROOT" && "$PY" -m core.schema --profile private/profile.yaml --config private/portal/config.json
)
kill $TUN 2>/dev/null || true; trap - EXIT
priv_push "页面配置(建库建表生成)" portal/config.json

step "5/8 部署密钥(主机读两个仓库用,每个仓库一把,只读)"
for kind in core private; do
  repo="$CORE"; slug="$core_slug"
  [ "$kind" = private ] && { repo="$PRIV"; slug="$priv_slug"; }
  kit check "'$repo'" "$kind" >/dev/null 2>&1 && { echo "  $kind:已能读 $slug"; continue; }
  pub="$(kit key "$kind" 2>/dev/null)"
  pubf="$(mktemp)"; printf '%s\n' "$pub" > "$pubf"
  if gh_ok && gh repo deploy-key add "$pubf" --repo "$slug" --title "water 主机($kind)" >/dev/null 2>&1; then
    rm -f "$pubf"
    echo "  $kind:已用 GitHub CLI 把部署密钥加到 $slug"
    sleep 3
    kit check "'$repo'" "$kind" >/dev/null 2>&1 && continue
  fi
  rm -f "$pubf"
  need "把下面这一行(主机的公钥,可以公开)加到 GitHub 仓库 $slug 的部署密钥" \
    "$pub" \
    "打开 https://github.com/$slug/settings/keys → Add deploy key;Title 填 water 主机($kind),Key 粘贴上面那一行," \
    "不要勾 Allow write access,点 Add key。(装了 GitHub CLI 并 gh auth login 过的话,重跑时脚本会自己加。)"
done

step "6/8 模型密钥与告警心跳(从本机 secrets.env 送到主机的 worker.env,不显示)"
ds="$(secret DEEPSEEK_API_KEY)"; an="$(secret ANTHROPIC_API_KEY)"; hb="$(secret HEALTHCHECK_URL)"
for v in "$ds" "$an"; do
  [[ "$v" =~ ^[A-Za-z0-9._+/=-]*$ ]] || need "secrets.env 里的模型密钥有空格、引号或中文字符" "重新复制密钥,保存后重跑。"
done
[[ "$hb" =~ ^(https://[^[:space:]\"\']*)?$ ]] || need "secrets.env 里的 HEALTHCHECK_URL 要以 https:// 开头" "改好保存后重跑。"
printf '%s\n%s\n' "$ds" "$hb" | kit_in env DEEPSEEK_API_KEY
printf '%s\n\n' "$an" | kit_in env ANTHROPIC_API_KEY
have="$(kit has)"
if [[ "$have" != *"deepseek=1"* && "$have" != *"anthropic=1"* ]]; then
  echo "  还没有模型密钥:先只用规则跑。以后把 DEEPSEEK_API_KEY 填进 $SECRETS 再重跑本命令即可。"
fi
unset ds an hb

step "7/8 取两个仓库、构建、起后台任务和页面(第一次要几分钟)"
kit up "'$CORE'" "'$PRIV'"

step "8/8 检查"
kit status | tail -n 25 || true
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/portal/" || true)"
echo
if [[ "$code" =~ ^(200|302|303|307)$ ]]; then
  echo "装好了。浏览器打开 https://$HOST/portal/ ,用 $ADMIN_LOCAL 里的管理员邮箱和密码登录,登录后先改密码。"
else
  echo "主机上都装好了,但 https://$HOST/portal/ 现在还打不开(HTTP ${code:-无响应})。多半是域名还没指向主机:"
  echo "  在域名的 DNS 里加一条 A 记录:名称填 ${HOST%%.*},值填 ${IP}(Cloudflare 选「仅 DNS」灰云);主机安全组放行 80、443。"
  echo "  生效后几分钟内自动签好证书,不用重跑。管理员邮箱和密码在 ${ADMIN_LOCAL}。"
fi
echo "之后:每天北京时间 08:00 自动更新程序,08:30 采集、判定、出日报;第二天上午就能看到第一批线索。"
