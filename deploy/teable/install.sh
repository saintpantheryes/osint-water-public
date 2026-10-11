#!/usr/bin/env bash
# 在云主机上一次装好:Docker → Teable(多维表格)+ Postgres + Redis + Caddy(自动 HTTPS)→ 管理员账号 → 接口令牌 → 每日备份。
# 用法(root):bash install.sh <域名>          例:bash install.sh water.example.uk
# 可选环境变量:ADMIN_EMAIL(默认 admin@<域名>)  DIR(默认 /opt/teable)  FORCE_STOP_WEB=1(80/443 被 nginx/httpd 占用时把它们停掉)
#              RESET_DB=1(上次安装半途失败、管理员密码已丢时:清空 Teable 数据重建;只在库里还没有数据时使用)
# 重复运行是安全的:已有 .env 与管理员账号时不会重建。
set -euo pipefail
HOST="${1:-}"
[ -n "$HOST" ] || { echo "用法: bash $0 <域名>"; exit 2; }
[ "$(id -u)" = 0 ] || { echo "请用 root 运行"; exit 2; }
DIR="${DIR:-/opt/teable}"
KIT="$(cd "$(dirname "$0")" && pwd)"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@$HOST}"
API=http://127.0.0.1:3000/api
log() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
echo "install.sh 版本 4(RESET_DB=${RESET_DB:-0} FORCE_STOP_WEB=${FORCE_STOP_WEB:-0} 目录 $DIR)"

# 调接口:call 方法 路径 [JSON];结果放 HTTP(状态码)和 RESP(响应体)。会话 cookie 手工带上,
# 因为 PUBLIC_ORIGIN 是 https 而这里走 http://127.0.0.1,curl 的 cookie jar 不会回传 Secure cookie。
COOKIES=""
call() {
  local m="$1" p="$2" body="${3:-}" hdr out rc
  hdr="$(mktemp)"; out="$(mktemp)"
  local args=(-sS -o "$out" -D "$hdr" -w '%{http_code}' -X "$m" "$API$p" -H 'Content-Type: application/json')
  [ -n "$COOKIES" ] && args+=(-H "Cookie: $COOKIES")
  [ -n "$body" ] && args+=(--data-binary "$body")
  if ! HTTP="$(curl "${args[@]}")"; then
    rc=$?; echo "调用 $m $p 失败(curl 退出码 $rc)"; cat "$out"; echo
    echo "Teable 日志:"; (cd "$DIR" && docker compose logs --tail 30 teable) || true
    exit 1
  fi
  RESP="$(cat "$out")"
  local c
  c="$(tr -d '\r' < "$hdr" | grep -i '^set-cookie:' | sed 's/^[^:]*: *//; s/;.*$//' | paste -sd';' - | sed 's/;/; /g' || true)"
  [ -n "$c" ] && COOKIES="$c"
  rm -f "$hdr" "$out"
  echo "  $m $p → HTTP $HTTP"
}
jget() { printf '%s' "$1" | grep -o "\"$2\":\"[^\"]*\"" | head -1 | cut -d'"' -f4 || true; }

log "1/6 Docker"
if ! command -v docker >/dev/null 2>&1; then
  . /etc/os-release
  case "${ID:-}" in
    alinux|anolis|centos|rhel|rocky|almalinux|openEuler)
      PM=yum; command -v dnf >/dev/null 2>&1 && PM=dnf
      [ "${ID:-}" = alinux ] && dnf install -y dnf-plugin-releasever-adapter --repo alinux3-plus >/dev/null 2>&1 || true
      $PM install -y dnf-plugins-core yum-utils >/dev/null 2>&1 || true
      $PM config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1 \
        || yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      $PM install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin ;;
    *)
      curl -fsSL https://get.docker.com | sh ;;
  esac
fi
systemctl enable --now docker >/dev/null
docker compose version >/dev/null 2>&1 || { echo "缺少 docker compose 插件:请安装 docker-compose-plugin 后重跑"; exit 1; }

log "2/6 端口 80/443"
busy="$(ss -ltnp 2>/dev/null | awk '$4 ~ /:(80|443)$/' || true)"
if [ -n "$busy" ] && ! echo "$busy" | grep -q docker; then
  echo "$busy"
  if [ "${FORCE_STOP_WEB:-0}" = 1 ]; then
    for s in nginx httpd apache2; do systemctl disable --now "$s" >/dev/null 2>&1 || true; done
    [ -x /etc/init.d/nginx ] && /etc/init.d/nginx stop >/dev/null 2>&1 || true   # 宝塔装的 nginx
  else
    echo "80/443 已被别的程序占用(多半是宝塔面板装的 nginx)。确认不需要它们后加 FORCE_STOP_WEB=1 重跑,或先在宝塔里卸载/停止。"
    exit 1
  fi
fi

log "3/6 写配置到 $DIR"
mkdir -p "$DIR"
cp "$KIT/docker-compose.yml" "$KIT/Caddyfile" "$KIT/backup.sh" "$KIT/restore_drill.sh" "$KIT/restore.sh" "$DIR/"
chmod +x "$DIR/backup.sh" "$DIR/restore_drill.sh" "$DIR/restore.sh"
mkdir -p "$DIR/caddy.d"  # 附加路由(新版只读页面)放这里;空着时 Caddy 什么也不多引入
if [ ! -f "$DIR/.env" ]; then
  gen() { openssl rand -hex 24; }
  sed -e "s#^TEABLE_HOST=.*#TEABLE_HOST=$HOST#" -e "s#^PUBLIC_ORIGIN=.*#PUBLIC_ORIGIN=https://$HOST#" \
      -e "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=$(gen)#" -e "s#^REDIS_PASSWORD=.*#REDIS_PASSWORD=$(gen)#" \
      -e "s#^SECRET_KEY=.*#SECRET_KEY=$(gen)#" "$KIT/env.example" > "$DIR/.env"
  chmod 600 "$DIR/.env"
else
  sed -i -e "s#^TEABLE_HOST=.*#TEABLE_HOST=$HOST#" -e "s#^PUBLIC_ORIGIN=.*#PUBLIC_ORIGIN=https://$HOST#" "$DIR/.env"
fi
if [ "${RESET_DB:-0}" = 1 ] && [ ! -f "$DIR/ADMIN.txt" ]; then
  echo "RESET_DB=1:清空 Teable 数据重建"
  (cd "$DIR" && docker compose down -v --remove-orphans) || true
  rm -f "$DIR/.admin_pw"
  sed -i 's#^DISABLE_SIGNUP=.*#DISABLE_SIGNUP=false#' "$DIR/.env"
fi

log "4/6 启动容器"
cd "$DIR"
docker compose pull -q
docker compose up -d
for _ in $(seq 1 60); do curl -fs http://127.0.0.1:3000/health >/dev/null 2>&1 && break; sleep 3; done
curl -fs http://127.0.0.1:3000/health >/dev/null || { echo "Teable 3 分钟内没起来,看日志:cd $DIR && docker compose logs --tail 100 teable"; exit 1; }

log "5/6 管理员账号与接口令牌"
if [ -f "$DIR/ADMIN.txt" ]; then
  echo "  已有 $DIR/ADMIN.txt,跳过"
else
  umask 077
  if [ -f "$DIR/.admin_pw" ]; then
    PW="$(cat "$DIR/.admin_pw")"
  else
    PW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)"
    printf '%s' "$PW" > "$DIR/.admin_pw"   # 先存下:万一下面半途失败,重跑还能登录
  fi
  echo "  注册管理员 $ADMIN_EMAIL"
  call POST /auth/signup "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$PW\",\"defaultSpaceName\":\"水务项目机会\"}"
  case "$HTTP" in
    200|201) ;;
    *)
      echo "  注册未成功:$RESP"; echo "  改为登录已有账号"
      call POST /auth/signin "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$PW\"}"
      case "$HTTP" in 200|201) ;; *)
        echo "登录也失败:$RESP"
        echo "若这是上次安装半途失败后的重跑、账号密码已丢,且库里还没有数据,用 RESET_DB=1 重跑清空重建。"
        exit 1;;
      esac;;
  esac
  [ -n "$COOKIES" ] || { echo "登录后没拿到会话 cookie,无法继续。响应:$RESP"; exit 1; }
  call GET /space
  space_id="$(jget "$RESP" id)"
  if [ -z "$space_id" ]; then   # 自托管版注册时不会自动建空间
    echo "  还没有空间,新建「水务项目机会」"
    call POST /space '{"name":"水务项目机会"}'
    space_id="$(jget "$RESP" id)"
  fi
  [ -n "$space_id" ] || { echo "建空间失败:$RESP"; exit 1; }
  exp="$(date -d '+10 years' +%F 2>/dev/null || date -v+10y +%F)"
  scopes='"space|read","base|create","base|read","base|update","table|create","table|read","table|update","view|create","view|read","view|update","field|create","field|read","field|update","record|create","record|read","record|update","record|delete"'
  echo "  创建接口令牌"
  call POST /access-token "{\"name\":\"osint-water sync\",\"description\":\"监测系统每日写入\",\"scopes\":[$scopes],\"spaceIds\":[\"$space_id\"],\"expiredTime\":\"$exp\"}"
  token="$(jget "$RESP" token)"
  [ -n "$token" ] || { echo "创建接口令牌失败:$RESP"; exit 1; }
  cat > "$DIR/ADMIN.txt" <<T
网址:            https://$HOST
管理员邮箱:      $ADMIN_EMAIL
管理员密码:      $PW      (首次登录后请在个人设置里改掉)
接口令牌:        $token
                 (建库建表时作 TEABLE_ADMIN_TOKEN 用,见 docs/部署指南.md 第 3 步;TEABLE_URL=https://$HOST)
空间 ID:         $space_id
T
  rm -f "$DIR/.admin_pw"
  # 关掉自助注册,之后只能凭邀请加入。新版 Teable 不认 .env 里的 DISABLE_SIGNUP,要用管理设置(实测 2026-10-08:
  # 只改 .env 时 POST /auth/signup 不带邀请仍然 201);两处都写,哪个版本都关得上
  call PATCH /admin/setting '{"disallowSignUp":true}'
  case "$HTTP" in 200|201|204) echo "  已关闭自助注册";; *) echo "  ! 没能关掉自助注册($HTTP):登录后在 管理设置 里手动关掉。$RESP";; esac
  sed -i 's#^DISABLE_SIGNUP=.*#DISABLE_SIGNUP=true#' "$DIR/.env"
  docker compose up -d teable >/dev/null
fi

log "6/6 每日备份(本机时间 03:30,保留 14 天)"
( crontab -l 2>/dev/null | grep -v 'teable/backup.sh' || true; echo "30 3 * * * $DIR/backup.sh >> $DIR/backup.log 2>&1" ) | crontab -

ip="$(curl -fs --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
echo
echo "完成。凭据在 $DIR/ADMIN.txt(只有 root 可读)。接下来:"
echo "  1) Cloudflare:给 $HOST 加 A 记录 → $ip,代理状态选「仅 DNS」(灰云)。"
echo "  2) 云主机安全组:放行 TCP 80、443。域名生效后 Caddy 几分钟内自动签发证书:cd $DIR && docker compose logs caddy"
echo "  3) 浏览器打开 https://$HOST,用 ADMIN.txt 里的邮箱和密码登录,先改密码。"
