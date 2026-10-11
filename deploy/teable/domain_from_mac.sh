#!/usr/bin/env bash
# 在 Mac 上运行:把表格与只读页面换到新域名,旧域名留着 301 转到新域名(旧书签、旧的日历订阅链接照样能用)。可以重复运行。
# 用法:bash deploy/teable/domain_from_mac.sh <密钥文件.pem> <主机IP> <新域名>      例:… <密钥文件.pem> <主机IP> water.example.org
# 先决条件:新域名的 DNS 已加一条 A 记录指向主机 IP(没生效时脚本停下,不改任何东西;确实要先改可加 SKIP_DNS_CHECK=1)。
# 主机上做的事(只开一个 ssh 连接):
#   1. 备份 /opt/teable/.env,把 TEABLE_HOST、PUBLIC_ORIGIN 改成新域名(表格据此生成邀请链接、登录后的跳转)
#   2. 在 /opt/teable/caddy.d/ 写 old-<旧域名>.site:旧域名整站 301 转到新域名的同一路径(Caddy 照旧给旧域名续证书)
#   3. 换上新版 Caddyfile(会引入上面的 .site),重建 Caddy 与表格两个容器(数据都在卷里,不动),Caddy 自动给新域名申请证书
#   4. 从主机上自检:新域名能打开登录页,旧域名转到新域名
# 后台任务(worker)走容器内网 http://teable:3000,与域名无关,不用动。登录 cookie 跟着域名走,换完后每个人在新域名重新登录一次。
# 回退:把 .env.bak-<时间> 改回 .env,删掉 caddy.d/old-*.site,docker compose up -d --no-deps caddy teable。
set -euo pipefail
KEY="${1:?密钥文件}"; IP="${2:?主机IP}"; NEW="${3:?新域名}"
NEW="$(printf '%s' "$NEW" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##')"
[[ "$NEW" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "新域名写成 water.example.org 这样"; exit 1; }
KIT="$(cd "$(dirname "$0")" && pwd)"
chmod 600 "$KEY"

if [ "${SKIP_DNS_CHECK:-0}" != 1 ]; then
  # 先经 HTTPS 问公共 DNS(Mac 上开着 Clash 这类代理时,本机解析会拿到 198.18.x.x 的假地址);问不到再用本机解析
  got="$(curl -fsS --max-time 10 -H 'accept: application/dns-json' "https://cloudflare-dns.com/dns-query?name=${NEW}&type=A" 2>/dev/null \
         | grep -o '"data":"[0-9.]*"' | cut -d'"' -f4 || true)"
  [ -n "$got" ] || got="$(dig +short A "$NEW" 2>/dev/null || true)"
  if printf '%s\n' "$got" | grep -q '^198\.1[89]\.'; then
    echo "本机解析到代理软件给的假地址(${got}),查不了真实记录;确认 DNS 已指向 ${IP} 后,在命令前加 SKIP_DNS_CHECK=1 再跑。"
    exit 1
  fi
  if ! printf '%s\n' "$got" | grep -qx "$IP"; then
    echo "新域名 $NEW 现在解析到:${got:-(没有记录)},不是主机 ${IP}。"
    echo "先在域名的 DNS 里加一条 A 记录:名称填 ${NEW%%.*},值填 $IP(用 Cloudflare 的话先关掉橙色云朵,只用 DNS),等几分钟再跑。"
    exit 1
  fi
fi

printf '\n\033[1;34m== 换域名到 %s(一个 ssh 连接)\033[0m\n' "$NEW"
# Caddyfile 经标准输入的前半段传过去,后半段是主机上要跑的脚本
{
  printf 'NEW=%q\n' "$NEW"
  echo "CADDYFILE_B64='$(base64 < "$KIT/Caddyfile" | tr -d '\n')'"
  cat <<'REMOTE'
set -euo pipefail
DIR=/opt/teable
cd "$DIR"
OLD="$(sed -n 's/^TEABLE_HOST=//p' .env | head -1)"
[ -n "$OLD" ] || { echo "主机的 .env 里没有 TEABLE_HOST"; exit 1; }
mkdir -p caddy.d
# 换回曾经用过的域名:它不再转址
rm -f "caddy.d/old-$NEW.site"
if [ "$OLD" != "$NEW" ]; then
  cp -p .env ".env.bak-$(date +%Y%m%d%H%M%S)"
  sed -i -e "s#^TEABLE_HOST=.*#TEABLE_HOST=$NEW#" -e "s#^PUBLIC_ORIGIN=.*#PUBLIC_ORIGIN=https://$NEW#" .env
  echo "  .env:$OLD → $NEW(旧文件已备份)"
  printf '%s {\n\tredir https://%s{uri} 301\n}\n' "$OLD" "$NEW" > "caddy.d/old-$OLD.site"
else
  echo "  .env 已经是 $NEW"
fi
# 更早换下来的旧域名也一并指向现在的新域名(换过不止一次时不绕两跳)
for f in caddy.d/old-*.site; do
  [ -e "$f" ] || continue
  sed -i -E "s#redir https://[^{]+\{uri\}#redir https://$NEW{uri}#" "$f"
done
printf '%s' "$CADDYFILE_B64" | base64 -d > Caddyfile.new && mv Caddyfile.new Caddyfile && chmod 644 Caddyfile
docker compose up -d --no-deps caddy teable </dev/null >/dev/null
echo "  已重建 Caddy 与表格,等表格起来……"
for _ in $(seq 1 60); do curl -fs http://127.0.0.1:3000/health >/dev/null 2>&1 && break; sleep 3; done
curl -fs http://127.0.0.1:3000/health >/dev/null || { echo "表格 3 分钟内没起来:cd $DIR && docker compose logs --tail 100 teable"; exit 1; }
# 证书第一次申请要几十秒
ok=""
for _ in $(seq 1 30); do
  r="$(curl -s -o /dev/null -w '%{http_code}' --resolve "$NEW:443:127.0.0.1" "https://$NEW/auth/login" || true)"
  [ "$r" = 200 ] && { ok=1; break; }
  sleep 4
done
if [ -n "$ok" ]; then echo "  ✓ https://$NEW/ 已能打开(证书已签好)"
else echo "  ✗ https://$NEW/ 还打不开;看 cd $DIR && docker compose logs --tail 50 caddy(多半是 DNS 还没生效或 80 端口不通,生效后 Caddy 会自己重试)"; fi
for f in caddy.d/old-*.site; do
  [ -e "$f" ] || continue
  o="${f#caddy.d/old-}"; o="${o%.site}"
  r="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$o:443:127.0.0.1" "https://$o/portal/" || true)"
  case "$r" in 301*"https://$NEW/portal/") echo "  ✓ 旧域名 $o 转到新域名";; *) echo "  ✗ 旧域名 $o → $r(应为 301 转到 https://$NEW/portal/)";; esac
done
REMOTE
} | ssh -i "$KEY" -o StrictHostKeyChecking=accept-new root@"$IP" "bash -s"

cat <<EOF

换好了。接下来:
  - 浏览器打开 https://$NEW/ 重新登录一次(旧域名的登录不带过来);旧书签、旧的日历订阅链接会自动转到新域名
  - Mac 上跑表格工具(schema 等)用的 TEABLE_URL 改成 https://$NEW
  - 套件以后每次部署都会带上新版 Caddyfile;换下来的旧域名一直转址,不想要了就删主机上的 /opt/teable/caddy.d/old-*.site 再重建 Caddy
EOF
