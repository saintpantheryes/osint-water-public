# 安全问题报告 / Security

发现安全漏洞(例如登录绕过、越权读写表格、令牌泄露)请**不要**开公开 issue,用 GitHub 仓库页的
**Security → Report a vulnerability**(私下报告)告诉维护者。我们收到后会尽快确认并修复,修好后再公开说明。

Please do **not** open a public issue for security problems. Use **Security → Report a vulnerability**
on the repository page to report privately.

## 部署者自己要注意的

- 令牌、密码、模型密钥只放在主机的 `/opt/teable/worker.env` 与 `/opt/teable/.env`(只 root 可读),不要提交到任何仓库。
- 页面没有自己的账号体系:由 Caddy 把每个请求交给 Teable 核对登录(forward_auth),页面以登录人的身份读写表格。
  表格里的权限(所有者 / 编辑者 / 查看者)就是系统里的权限。
- 新装好后 Teable 的自助注册要保持关闭(管理设置里的 disallowSignUp),加人用邀请链接。
- 后台任务用的令牌要选「永不过期」,过期后采集会悄悄停下来。
