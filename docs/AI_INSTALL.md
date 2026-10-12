# AI 安装向导(写给 AI 助手看的)

> For AI agents: this file tells you how to install Global Water OSINT for a non-technical user, end to end.
> Talk to the user in their language (default Chinese). Follow the rules below strictly, especially the secret-handling rules.

用户把这样一句话交给你:

> 请帮我安装全球水务开源情报:先读 https://raw.githubusercontent.com/saintpantheryes/osint-water-public/main/docs/AI_INSTALL.md ,然后一步步带我做。

你的任务:替用户跑命令,把只有用户本人能做的事(买服务器、加 DNS 记录、在网页上点按钮、拿密钥)一件一件讲清楚,直到浏览器里能打开系统。
用户不懂技术:每次只让他做一件事,说清楚去哪个网页、点什么、做完告诉你什么。不要甩一大段命令给他看。

## 先问:试用还是正式用?

- **只想在自己电脑上试试**(不买服务器、不要域名,只有自己能用,电脑关机时不采集):走下面的「单机试用版」,几步就完。
- **团队正式用**(同事都能登录、每天自动采集):走第 1–7 步的服务器版。试用版的配置以后可以原样搬上服务器。

## 单机试用版

1. 看第 1 步把 `git` 装好;Windows 先装 WSL,后面的命令都在 Ubuntu 里跑。
2. 让用户装 **Docker Desktop**(https://www.docker.com/products/docker-desktop/ ,它会在电脑里跑一个小虚拟机),装好后打开它,
   等它显示 running。电脑要有 8 GB 以上内存、10 GB 空闲硬盘。Linux 可以直接装 Docker Engine(当前用户要能不加 sudo 运行 docker)。
3. 克隆本仓库(不用 fork):`git clone https://github.com/saintpantheryes/osint-water-public.git && cd osint-water-public`
4. 运行 `bash deploy/local/start.sh`。第一次要 5–15 分钟(下载、构建)。它会新建 `~/.osint-water/secrets.env`;
   想要中文摘要和项目研判,按第 5 步让用户**自己**填 `DEEPSEEK_API_KEY`,然后重跑同一条命令。
5. 让用户打开 http://localhost:8080/portal/ ,邮箱和密码在 `deploy/local/state/ADMIN.txt`,让用户**自己**打开它
   (macOS:`open -e deploy/local/state/ADMIN.txt`),你不要读。
6. 想马上有东西看:`bash deploy/local/start.sh collect`(补国别资料、采集一次,十几分钟到半小时)。
   否则每天北京时间 08:30 之后电脑开着时自动采集。
7. 其他:`bash deploy/local/start.sh stop` 停下(数据留着);`status` 看状态;端口被占用时在命令前加 `OW_PORT=8090`。
   改重点国别:改 `private/profile.yaml` 后重跑 `start.sh`。更新程序:`git pull` 后重跑 `start.sh`。

## 规则(必须遵守)

1. **密钥不经过聊天。** 模型密钥、表格令牌、管理员密码都不要让用户粘贴给你,你也不要读、不要打印。
   - 用户自己用编辑器把密钥填进本机的 `~/.osint-water/secrets.env`;安装脚本经 ssh 把它直接送到主机上只 root 可读的文件里。
   - 不要 `cat`、`less`、`grep` 出 `~/.osint-water/` 和 `deploy/local/state/` 下文件的内容,也不要读主机上的 `/opt/teable/ADMIN.txt`、`/opt/teable/worker.env`、`/opt/teable/.env`。
     只想确认某个密钥填了没有,用只输出数字的命令:`grep -c '^DEEPSEEK_API_KEY=.' ~/.osint-water/secrets.env`。
   - 用户要是把密钥贴进了聊天,提醒他这把密钥已经暴露,去对应网站删掉、重新生成一把,再填进文件。
2. **花钱的事先问。** 买服务器、买域名、给模型充值,都要用户自己确认、自己付款。
3. **不要删用户的东西。** 不删服务器上的数据,不加 `RESET_DB=1`,除非用户明确说这是一台新装失败、里面没有数据的主机。
4. **主机 IP、域名、GitHub 用户名不是密钥**,可以直接问用户要。
5. 你不能在用户电脑上执行命令时(比如你是网页里的聊天机器人):把要跑的命令一条一条给用户,让他复制到「终端」里运行,把输出贴回来(输出里不会有密钥)。

## 第 1 步:看看用户的电脑

- macOS 或 Linux:直接用自带的「终端」。
- Windows:先装 WSL(在 PowerShell 里以管理员身份运行 `wsl --install`,重启后打开「Ubuntu」),后面所有命令都在 Ubuntu 里跑。
- 需要 `git`、`ssh`、`python3`(3.11 或更新)、`curl`。缺什么就帮用户装(macOS 运行 `xcode-select --install` 会带上 git;Python 去 python.org 下载;Ubuntu 用 `sudo apt install -y git python3 python3-venv curl`)。
- **强烈建议装 GitHub CLI**(https://cli.github.com ,macOS 可用 `brew install gh`),然后让用户运行 `gh auth login`、一路回车、在浏览器里登录。
  装了它,建仓库、加部署密钥都由脚本自动完成,用户少点很多次网页。

## 第 2 步:一台云服务器

让用户买一台(或用已有的):

- 2 核 4 GB 内存起,系统选 **Ubuntu 22.04 或 24.04**,有公网 IP。
- 地区:在中国内地的主机要 ICP 备案才能用域名;不想备案就选**香港、新加坡或海外**。
- 创建时选「密钥对」登录,下载 `.pem` 文件;让用户把它放到 `~/.ssh/`(你可以帮他 `mv ~/Downloads/xxx.pem ~/.ssh/ && chmod 600 ~/.ssh/xxx.pem`)。
- 安全组(防火墙)放行 TCP **22、80、443**。
- 登录用户要能是 root(有的云默认是 ubuntu 用户:在控制台把 root 登录打开,或让用户在控制台里给 root 绑定同一把密钥)。

问用户要:**主机公网 IP**、**.pem 文件在哪**。用 `ssh -i <pem> -o StrictHostKeyChecking=accept-new root@<IP> true` 试一下能不能连上。

## 第 3 步:一个域名

问用户有没有域名(子域名就行,比如 `water.example.com`)。没有就让他在 Cloudflare、Namecheap、阿里云国际等处买一个。
然后让他在域名的 DNS 里加一条 **A 记录**:名称填子域名前缀(如 `water`),值填主机 IP;用 Cloudflare 的话把橙色云朵点成灰色(「仅 DNS」)。
DNS 生效要几分钟到几小时,不用等,可以先往下做。

## 第 4 步:GitHub 上的两个仓库

用户要有 GitHub 帐号(没有就去 https://github.com/signup 注册)。

1. **核心仓(程序)**:fork 本仓库并克隆到本机。
   - 有 gh:`gh repo fork saintpantheryes/osint-water-public --clone && cd osint-water-public`
   - 没有 gh:让用户在 https://github.com/saintpantheryes/osint-water-public 点右上角 Fork,然后 `git clone https://github.com/<用户名>/osint-water-public.git && cd osint-water-public`
2. **私有配置仓**:放这家单位自己的配置(重点国别、页面配置)。
   - 有 gh:`gh repo create <用户名>/osint-water-private --private`
   - 没有 gh:让用户打开 https://github.com/new ,名字填 `osint-water-private`,选 **Private**,点 Create repository。
   - 安装脚本会自己把它克隆到核心仓旁边、放进样板配置、推上去。

## 第 5 步:让用户填密钥

先在核心仓目录里跑一次安装命令(见第 6 步),它会新建 `~/.osint-water/secrets.env`(只有用户能读)。也可以现在就跑,停在哪一步都没关系。

然后让用户**自己**打开这个文件填密钥(你不要读它):

- macOS:`open -e ~/.osint-water/secrets.env`
- Windows(WSL):`notepad.exe "$(wslpath -w ~/.osint-water/secrets.env)"`
- Linux:`nano ~/.osint-water/secrets.env`(填好按 Ctrl+O 回车保存,Ctrl+X 退出)

要填的(文件里每项都有说明):

| 名字 | 要不要 | 去哪拿 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 建议(或者填下面的 Claude) | https://platform.deepseek.com → 充值几块钱 → API keys → 创建,复制 `sk-…` |
| `ANTHROPIC_API_KEY` | 可选 | https://console.anthropic.com → API Keys,`sk-ant-…` |
| `HEALTHCHECK_URL` | 可选 | https://healthchecks.io 免费建一个检查,复制 ping 网址;一天没采集成功会发邮件 |
| `TEABLE_TOKEN` | 一般留空 | 留空就用安装时自动生成的表格令牌 |

没有模型密钥也能装:只用规则初筛,以后填上再重跑安装命令就行。用户说填好了,用第 1 条规则里的 `grep -c` 确认一下。

## 第 6 步:跑安装命令

在核心仓目录里:

```bash
bash deploy/teable/one_click.sh ~/.ssh/<密钥>.pem <主机IP> <域名> <用户名>/osint-water-private
```

- 不需要在终端里输入任何东西;第一次要 5–15 分钟(装 Docker、拉镜像、构建)。你在跑的时候告诉用户在等什么。
- **退出码 10** = 需要用户在网页上做一件事。输出最后几行以「需要你做:」开头,把它用大白话转述给用户(比如把一行公钥加到 GitHub 的 Deploy keys,
  给他链接、告诉他 Title 填什么、不要勾 Allow write access)。用户说做好了,**重跑同一条命令**,会从停下的地方接着做。
- 退出码 0 = 装好了。其他退出码 = 出错,看输出里的中文提示处理(见下面的「常见问题」)。
- 脚本可以放心重跑:做完的步骤会跳过。

## 第 7 步:交给用户

1. 让用户在浏览器打开 `https://<域名>/portal/`。打不开多半是 DNS 还没生效或安全组没放行 80/443;生效后几分钟内自动签好证书,不用重跑。
2. 管理员邮箱和密码在用户本机的 `~/.osint-water/<域名>-ADMIN.txt`。让用户**自己**打开它(macOS:`open -e ~/.osint-water/<域名>-ADMIN.txt`),
   登录后马上在个人设置里改密码。你不要读这个文件。
3. 告诉用户接下来会发生什么:每天北京时间 08:00 自动更新程序,08:30 采集、判定、出日报;第二天上午能看到第一批线索。
4. 加同事:自助注册是关着的。登录表格(`https://<域名>`)→ 打开库「水务项目机会 新版」→ 右上角「邀请」→ 生成邀请链接(查看者只读、编辑者能改),发给同事。

## 之后可以帮用户做的

- **改重点国别、行业口径、金额门槛**:改私有配置仓里的 `profile.yaml`(核心仓的 `private/profile.yaml` 就是它),按文件里的注释改,
  然后在 `private/` 里 `git commit` 并 `git push`。主机每天 08:00 自动取新配置;想马上生效就重跑第 6 步的命令。
- **更新程序**:在核心仓里 `git pull`(fork 的话先在 GitHub 上点 Sync fork),再重跑第 6 步的命令。
- 更多运维(备份、恢复、换域名、迁主机):[deploy/teable/README.md](../deploy/teable/README.md);手动的分步说明:[部署指南](部署指南.md)。

## 常见问题

| 现象 | 处理 |
|---|---|
| 「本机连不上主机」 | 安全组放行 22;IP、.pem 对不对;登录用户是不是 root |
| 「80/443 已被别的程序占用」 | 主机上装了宝塔之类的面板。确认不需要后在命令前加 `FORCE_STOP_WEB=1` 重跑 |
| 「本机推送不了私有配置仓」 | 装 GitHub CLI 并 `gh auth login`,重跑 |
| 部署密钥加了还是读不了 | 看是不是加错了仓库(core 那把加到核心仓,private 那把加到私有仓);同一把公钥 GitHub 不允许加到两个仓库 |
| 页面打不开 | DNS 的 A 记录是否指向主机 IP(Cloudflare 要灰云);安全组放行 80、443;等几分钟再试 |
| 想看后台任务状态 | `ssh -i <pem> root@<IP> bash /opt/teable/worker_install.sh status`(只显示状态和日志,不显示密钥) |
