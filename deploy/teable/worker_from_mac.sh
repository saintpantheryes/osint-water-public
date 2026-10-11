#!/usr/bin/env bash
# 在 Mac 上运行:把后台任务(worker,架构方案第 4 节)装到团队表格所在的主机上。可以重复运行:装好的部分跳过,已有的密钥不用重输;
# 每次都会取两个仓库 main 的新提交并重启后台任务。
# 用法:bash deploy/teable/worker_from_mac.sh <密钥文件.pem> <主机IP> [私有配置仓 所有者/仓库名]
#   在公开核心仓的检出里运行;核心仓的地址取本检出的 origin。私有配置仓第一次要给第三个参数,主机会记住,以后不用再给
#   (也可以把私有配置仓检出到本检出的 private/,脚本会从那里的 origin 认出来)。
# 先决条件:已在 Mac 上跑过 worker_token_from_mac.sh(机器账号的令牌已在主机的 worker.env 里)。
# 要你做的:按提示把主机生成的部署密钥(公钥)加到 GitHub 对应仓库的 Deploy keys(只读即可);在本终端输入模型密钥
# (输入时不显示,经 ssh 直接写进主机上只 root 可读的文件,不进仓库、不进聊天)。
set -euo pipefail
KEY="${1:?密钥文件}"; IP="${2:?主机IP}"; PRIV_ARG="${3:-}"
KIT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$KIT/../.." && pwd)"
chmod 600 "$KEY"
OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new)
step() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
# shellcheck disable=SC2029  # 参数(仓库地址、变量名)有意在本机展开,都先校验过
remote() { ssh -n "${OPTS[@]}" root@"$IP" "bash /root/teable-kit/worker_install.sh $*"; }
# shellcheck disable=SC2029
remote_in() { ssh "${OPTS[@]}" root@"$IP" "bash /root/teable-kit/worker_install.sh $*"; }
slug_of() {  # 远端地址 → 所有者/仓库名;认不出返回空
  printf '%s' "$1" | sed -E 's#^(https://([^@/]*@)?|ssh://git@|git@)github\.com[:/]+##; s#\.git$##; s#/$##' | grep -E '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || true
}

core_slug="$(slug_of "$(git -C "$ROOT" remote get-url origin)")"
[ -n "$core_slug" ] || { echo "认不出核心仓(git remote get-url origin 应是 github.com 上的地址;这里不打印它,免得里面带着凭据)"; exit 1; }
CORE="git@github.com:$core_slug.git"

step "1/5 复制套件到主机"
ssh -n "${OPTS[@]}" root@"$IP" "mkdir -p /root/teable-kit"
scp -q "${OPTS[@]}" "$KIT"/* root@"$IP":/root/teable-kit/
echo "  已复制到 /root/teable-kit"

# 私有配置仓:参数 > 主机记住的 > 本检出 private/ 的 origin
have="$(remote has)"
priv_slug=""
if [ -n "$PRIV_ARG" ]; then
  priv_slug="$(slug_of "$PRIV_ARG")"; [ -n "$priv_slug" ] || { echo "私有配置仓要写成 所有者/仓库名"; exit 1; }
elif [[ "$have" =~ private_repo=git@github.com:([A-Za-z0-9_./-]+)\.git ]]; then
  priv_slug="${BASH_REMATCH[1]}"
elif [ -d "$ROOT/private/.git" ] || [ -L "$ROOT/private" ]; then
  priv_slug="$(slug_of "$(git -C "$ROOT/private" remote get-url origin 2>/dev/null || true)")"
fi
[ -n "$priv_slug" ] || { echo "不知道私有配置仓是哪个:第一次请给第三个参数,如 bash deploy/teable/worker_from_mac.sh $KEY $IP 所有者/私有仓库名"; exit 1; }
PRIV="git@github.com:$priv_slug.git"
echo "  核心仓 $core_slug;私有配置仓 $priv_slug"

step "2/5 部署密钥(每个仓库一把;主机上只 root 可读)"
for kind in core private; do
  repo="$CORE"; [ "$kind" = private ] && repo="$PRIV"
  slug="$core_slug"; [ "$kind" = private ] && slug="$priv_slug"
  if remote check "'$repo'" "$kind" >/dev/null 2>&1; then
    echo "  $kind:部署密钥已能读 $slug"
    continue
  fi
  pub="$(remote key "$kind")"
  echo
  printf '%s\n' "$pub"
  echo "请把上面以 ssh-ed25519 开头的那一行(公钥,可以公开)加到 GitHub:"
  echo "  https://github.com/$slug/settings/keys → Add deploy key"
  echo "  Title 填「water 主机($kind)」,Key 粘贴那一行,不用勾 Allow write access,点 Add key。"
  until remote check "'$repo'" "$kind"; do
    read -r -p "加好以后按回车继续(输入 q 退出,重跑本脚本可接着做):" a
    [ "$a" = q ] && exit 1
  done
done

step "3/5 密钥(只在本终端输入,输入时不显示)"
if [[ "$have" != *"token=1"* ]]; then
  echo "主机上还没有机器账号的令牌:先运行 bash deploy/teable/worker_token_from_mac.sh $KEY $IP,再重跑本脚本"; exit 1
fi
# 模型密钥:DeepSeek(platform.deepseek.com,密钥形如 sk-…)或 Claude(密钥形如 sk-ant-…),按粘贴的密钥前缀自动分辨存到哪个变量;
# 用哪家由私有配置 profile.yaml 的 watch.llm.provider 决定(助手页沿用同一家,assistant.llm 可另选)。两家都可以配,各存各的。
llm=""; hb=""
if [[ "$have" == *"deepseek=1"* || "$have" == *"anthropic=1"* ]]; then
  echo "  主机上已有模型密钥:DeepSeek $([[ "$have" == *"deepseek=1"* ]] && echo 有 || echo 无)、Claude $([[ "$have" == *"anthropic=1"* ]] && echo 有 || echo 无)"
  read -r -s -p "要换或补一把就粘贴(DeepSeek sk-… / Claude sk-ant-…),不改直接回车:" llm; echo
else
  read -r -s -p "粘贴模型密钥(DeepSeek 的 sk-… 或 Claude 的 sk-ant-…;判定线索、中文摘要、助手页要用;暂时没有就直接回车,只用规则):" llm; echo
fi
LLM_VAR=DEEPSEEK_API_KEY
[[ "$llm" == sk-ant-* ]] && LLM_VAR=ANTHROPIC_API_KEY
[ -z "$llm" ] || echo "  认作 $([ "$LLM_VAR" = DEEPSEEK_API_KEY ] && echo DeepSeek || echo Claude) 的密钥($LLM_VAR)"
if [[ "$have" == *"heartbeat=1"* ]]; then
  read -r -p "告警心跳网址已配置;要换就粘贴新的,不换直接回车:" hb
else
  read -r -p "告警心跳网址(healthchecks.io 之类,采集成功后 ping 一下,一天没 ping 对方发邮件;没有就直接回车):" hb
fi
[[ "$llm" =~ ^[A-Za-z0-9._+/=-]*$ ]] || { echo "密钥里有空格、引号或中文字符,请重新复制后重跑(没有写进主机)"; exit 1; }
printf '%s\n%s\n' "$llm" "$hb" | remote_in env "$LLM_VAR"
unset llm

step "4/5 取两个仓库、构建、起后台任务(第一次要几分钟)"
remote up "'$CORE'" "'$PRIV'"

step "5/5 检查"
remote status
echo
echo "完成。后台任务每 2 分钟补全上传的线索、把人改的阶段记成跟进;每天北京时间 08:30 采集与判定、建议商机。"
echo "以后:程序与配置每天北京时间 08:00 自动更新;看状态 ssh 上去运行 bash /opt/teable/worker_install.sh status;"
echo "手动跑一次采集:cd /opt/teable && docker compose --profile worker run --rm worker python -m core.cli run collect --profile private/profile.yaml"
