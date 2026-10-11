#!/usr/bin/env bash
# 在 Mac 上运行:把机器账号的令牌存进主机 /opt/teable/worker.env(只 root 可读),给后台任务(worker,阶段 D)用。架构方案阶段 A。
# 令牌在终端里输入(不回显),经 ssh 的标准输入传过去:不出现在任何命令行、不写进本机文件。管理员的令牌不放这里。
# 用法:bash deploy/teable/worker_token_from_mac.sh <密钥文件.pem> <主机IP>
set -euo pipefail
KEY="${1:?密钥文件}"; IP="${2:?主机IP}"
chmod 600 "$KEY"
read -r -s -p "机器账号的令牌(输入时不显示):" TOKEN; echo
[ -n "$TOKEN" ] || { echo "没输入,什么也没改"; exit 1; }
# 远端命令里没有令牌;令牌是标准输入的第一行
REMOTE='set -euo pipefail; umask 077; IFS= read -r TOKEN; F=/opt/teable/worker.env; touch "$F";
grep -v -e "^TEABLE_TOKEN=" -e "^TEABLE_URL=" "$F" > "$F.new" || true;
{ echo "TEABLE_URL=http://teable:3000"; echo "TEABLE_TOKEN=$TOKEN"; } >> "$F.new";
mv "$F.new" "$F"; chmod 600 "$F"; echo "已写进 $F(只 root 可读):TEABLE_URL、TEABLE_TOKEN"'
printf '%s\n' "$TOKEN" | ssh -i "$KEY" -o StrictHostKeyChecking=accept-new root@"$IP" "bash -c '$REMOTE'"
unset TOKEN
