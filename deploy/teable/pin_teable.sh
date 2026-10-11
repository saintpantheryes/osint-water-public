#!/usr/bin/env bash
# 在主机上运行(架构方案阶段 A,审查一-9):把 Teable 镜像固定到"现在正在运行的版本"(按摘要),以后 docker compose pull / up
# 不会再换版本。不重启任何东西;下次有人 docker compose up -d 时 teable 会按同一个镜像重建一次(版本不变)。
# 升级改为手动:备份 → 把 .env 的 TEABLE_IMAGE 改成新版本 → docker compose up -d teable → 在本机重跑 scripts/teable_probe.py。
# 用法:bash pin_teable.sh [/opt/teable]
set -euo pipefail
DIR="${1:-/opt/teable}"
cd "$DIR"
cid="$(docker compose ps -q teable)"
[ -n "$cid" ] || { echo "teable 容器没在运行:先 docker compose up -d"; exit 1; }
img="$(docker inspect --format '{{.Image}}' "$cid")"
digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$img" | grep '^ghcr.io/teableio/teable@' | head -n1 || true)"
[ -n "$digest" ] || { echo "认不出镜像摘要(镜像不是从 ghcr.io/teableio/teable 拉的?):$img"; exit 1; }
ver="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$img" 2>/dev/null || true)"
created="$(docker image inspect --format '{{.Created}}' "$img")"
echo "现在运行的 Teable:${ver:-(镜像没写版本号)} · 构建于 $created"
echo "  镜像摘要:$digest"
if grep -q '^TEABLE_IMAGE=' .env; then
  sed -i "s#^TEABLE_IMAGE=.*#TEABLE_IMAGE=$digest#" .env
else
  printf '\n# Teable 镜像按摘要固定(pin_teable.sh 写;升级改为手动)\nTEABLE_IMAGE=%s\n' "$digest" >> .env
fi
if grep -q 'image: ghcr.io/teableio/teable:latest' docker-compose.yml; then
  sed -i 's#image: ghcr.io/teableio/teable:latest#image: ${TEABLE_IMAGE:-ghcr.io/teableio/teable:latest}#' docker-compose.yml
  echo "docker-compose.yml 的 teable 镜像改为读 TEABLE_IMAGE"
fi
printf '%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "${ver:-?}" "$digest" >> teable_versions.log
echo "已固定(记在 .env 的 TEABLE_IMAGE 与 teable_versions.log)。核对:docker compose config | grep 'image: ghcr.io/teableio'"
docker compose config 2>/dev/null | grep 'image: ghcr.io/teableio' || true
