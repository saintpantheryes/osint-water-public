---
name: install-osint-water
description: 帮不懂技术的用户从零安装、重装或更新「全球水务开源情报 / Global Water OSINT」(服务器版:云主机、域名、GitHub 仓库、密钥、一键安装脚本;单机试用版:自己电脑上的 Docker)。用户说「安装」「部署」「试用」「装到服务器上」「装在我电脑上」时用。
---

# 安装全球水务开源情报

完整步骤与规则在本仓库的 `docs/AI_INSTALL.md`。先把它整篇读完:只想在自己电脑上试用的走「单机试用版」(`bash deploy/local/start.sh`),团队正式用的按第 1–7 步带用户做。

最要紧的三条(细节见那份文件):

1. 密钥(模型密钥、表格令牌、管理员密码)不经过聊天:用户自己用编辑器填进 `~/.osint-water/secrets.env`;
   你不读、不打印 `~/.osint-water/` 和 `deploy/local/state/` 下的文件,也不读主机上的 `/opt/teable/ADMIN.txt`、`worker.env`、`.env`。
2. 安装只跑一条命令:`bash deploy/teable/one_click.sh <pem> <主机IP> <域名> <所有者>/<私有配置仓>`。
   退出码 10 表示要用户在网页上做一件事(输出末尾「需要你做:」),转述给用户,做完重跑同一条命令。
3. 花钱(服务器、域名、模型充值)和删除数据的事,先问用户。
