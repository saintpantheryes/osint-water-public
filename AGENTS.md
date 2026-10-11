# 给 AI 助手 / For AI agents

- 用户要你**安装或部署**本系统时:读 [docs/AI_INSTALL.md](docs/AI_INSTALL.md),照着一步步带用户做。密钥不经过聊天,规则见那份文件。
  To install or deploy this system for a user, read docs/AI_INSTALL.md and follow it; never ask for secrets in chat.
- 改代码时:测试 `python -m pytest -q`;提交前跑 `python scripts/privacy_scan.py`。对象、字段、选项以 `docs/ontology/ontology.yaml` 为准。
