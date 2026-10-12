"""部署套件(deploy/teable)的安全约束:只有 Caddy 对外、表格只在本机 127.0.0.1;附加路由目录挂进 Caddy;
GitHub 主机公钥是官方公布的那把。旧网页界面(/app)已于 2026-10-03 删除,compose 里不再有 app 服务。"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "deploy" / "teable"

GITHUB_ED25519 = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"  # docs.github.com「GitHub's SSH key fingerprints」


def test_gateway_serves_the_table_and_imports_extra_routes():
    main = (KIT / "Caddyfile").read_text(encoding="utf-8")
    assert "import /etc/caddy/caddy.d/*.caddy" in main and re.search(r"handle \{\s*reverse_proxy teable:3000\s*\}", main)
    # 换域名后旧域名的转址站点(domain_from_mac.sh 写 caddy.d/old-*.site)在站点块之外引入
    assert main.rstrip().endswith("import /etc/caddy/caddy.d/*.site")


def test_domain_switch_script_is_one_connection_and_keeps_old_links():
    s = (KIT / "domain_from_mac.sh").read_text(encoding="utf-8")
    assert len(re.findall(r"^[^#]*\bssh -", s, re.M)) == 1 and "scp" not in s  # 只开一个 ssh 连接(断线时不会停在半路)
    assert "TEABLE_HOST=$NEW" in s and "PUBLIC_ORIGIN=https://$NEW" in s and "cp -p .env" in s
    # Mac 自带的 bash 3.2 会把紧跟在变量后的中文标点当成变量名的一部分(「$IP。」→ unbound variable):这类地方要写成 ${IP}
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", s)
    assert "redir https://%s{uri} 301" in s and "</dev/null" in s  # 旧域名 301;compose 不吃掉经标准输入传的脚本


def test_only_caddy_is_public_and_the_old_app_is_gone():
    compose = yaml.safe_load((KIT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "app" not in compose["services"]
    assert "./caddy.d:/etc/caddy/caddy.d:ro" in compose["services"]["caddy"]["volumes"]
    for name, svc in compose["services"].items():  # 只有 Caddy 对外;表格只在本机 127.0.0.1
        assert name == "caddy" or all(str(p).startswith("127.0.0.1:") for p in svc.get("ports", [])), name
    assert not (KIT / "app.caddy").exists() and not (KIT / "app.Dockerfile").exists() and not (ROOT / "app").exists()


def test_pinned_github_host_key():
    line = (KIT / "github_known_hosts").read_text(encoding="utf-8").strip()
    host, kind, blob = line.split()
    assert (host, kind) == ("github.com", "ssh-ed25519")
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")
    assert fp == GITHUB_ED25519


def test_worker_is_internal_only_and_opt_in():
    compose = yaml.safe_load((KIT / "docker-compose.yml").read_text(encoding="utf-8"))
    w = compose["services"]["worker"]
    assert w["profiles"] == ["worker"] and "ports" not in w  # 只在 --profile worker 下起;不对外开端口
    assert "worker.env" in w["env_file"] and w["build"]["dockerfile"] == "deploy/teable/worker.Dockerfile"
    assert any(v.startswith("/opt/osint-water:") and v.endswith(":ro") for v in w["volumes"])  # 程序只读挂载
    assert "worker-cache" in compose["volumes"]
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert "*" in ignore and "!requirements.txt" in ignore and not any(x.startswith("!private") for x in ignore)
    for name in ("worker_install.sh", "worker_update.sh", "worker_from_mac.sh", "worker.Dockerfile"):
        assert (KIT / name).exists(), name
    assert "ADMIN" not in (KIT / "worker_install.sh").read_text(encoding="utf-8")  # 管理员令牌不上后台任务


# 假的 docker:记下每次调用。Caddy 里挂着的页面配置用 $SERVED 这个文件代替;restart caddy 时换成私有配置检出里的
# (真的 Docker 重启容器时按路径重新挂载,挂上的就是 git 新建的那个文件)
FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$*" in
  *"exec -T caddy cat /srv/portal/config.json"*) cat "$SERVED" ;;
  *"restart caddy"*) cp "$OW_PRIVATE/portal/config.json" "$SERVED" ;;
  *"ps -q worker"*) echo w1 ;;
  "inspect -f"*) echo true ;;
esac
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def test_page_config_update_restarts_caddy(tmp_path):
    """私有配置仓里的页面配置(portal/config.json)单个文件挂进 Caddy:git 更新时删掉重建,不重启 Caddy 页面读到的还是旧文件,
    新建的表(如「城市」)页面认不出。每天的更新与 Mac 上的部署都要在配置变了时重启 Caddy;没变时不动它。"""
    compose = yaml.safe_load((KIT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "/opt/osint-water-private/portal/config.json:/srv/portal/config.json:ro" in compose["services"]["caddy"]["volumes"]
    install = (KIT / "worker_install.sh").read_text(encoding="utf-8")
    assert "cat /srv/portal/config.json" in install and "compose restart caddy" in install

    # "GitHub 上的"两个仓库(本地仓库代替)、主机上的两个检出与 /opt/teable
    gh_core, gh_priv, core, priv, opt = (tmp_path / n for n in ("gh_core", "gh_priv", "core", "priv", "opt"))
    for f in ("requirements.txt", "deploy/teable/docker-compose.yml", "deploy/teable/worker.Dockerfile",
              "deploy/teable/worker_update.sh", "deploy/teable/worker_install.sh", "deploy/teable/portal.caddy"):
        (gh_core / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / f, gh_core / f)
    (gh_priv / "portal").mkdir(parents=True)
    (gh_priv / "portal" / "config.json").write_text('{"tables": {}}\n', encoding="utf-8")
    for repo in (gh_core, gh_priv):
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "1")
    _git(tmp_path, "clone", "-q", str(gh_core), str(core))
    _git(tmp_path, "clone", "-q", str(gh_priv), str(priv))
    (opt / "caddy.d").mkdir(parents=True)
    shutil.copy(KIT / "docker-compose.yml", opt / "docker-compose.yml")
    shutil.copy(KIT / "portal.caddy", opt / "caddy.d" / "portal.caddy")
    (opt / "worker_rev").write_text(_git(core, "rev-parse", "HEAD") + "\n")
    (opt / "private_rev").write_text(_git(priv, "rev-parse", "HEAD") + "\n")
    served = tmp_path / "served.json"
    shutil.copy(priv / "portal" / "config.json", served)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DIR": str(opt), "OW_REPO": str(core),
           "OW_PRIVATE": str(priv), "SERVED": str(served), "DOCKER_LOG": str(log)}

    def update() -> tuple[str, int]:
        log.write_text("")
        out = subprocess.run(["bash", str(KIT / "worker_update.sh")], env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout, log.read_text().count("restart caddy")

    out, restarts = update()  # 什么都没变:不碰 Caddy
    assert "程序已是最新" in out and restarts == 0, out

    (gh_priv / "portal" / "config.json").write_text('{"tables": {"city": {"id": "tblCity"}}}\n', encoding="utf-8")
    _git(gh_priv, "commit", "-qam", "城市表")
    out, restarts = update()  # 私有配置仓换了页面配置:取下来、重启 Caddy,Caddy 里挂的是新的
    assert "已更新" in out and "已重启 Caddy" in out and restarts == 1, out
    assert served.read_text() == (gh_priv / "portal" / "config.json").read_text()

    out, restarts = update()  # 再跑一次:已是最新,不再重启
    assert "程序已是最新" in out and restarts == 0, out

    served.write_text('{"tables": {}}\n')  # 之前的更新(旧版脚本)换了配置却没重启 Caddy:没有新提交也补上
    out, restarts = update()
    assert "已重启 Caddy" in out and "程序已是最新" in out and restarts == 1, out
    assert served.read_text() == (priv / "portal" / "config.json").read_text()


def test_example_profile_loads():
    """自己部署的样板 profile(docs/部署指南.md 让人复制到私有配置仓)要能读:有重点国别、全球信源与模型设置。"""
    from core.profile import load_profile
    p = load_profile(ROOT / "profiles" / "example" / "profile.yaml")
    assert p.countries and p.watch.get("global") == "../global/watch.yaml" and p.watch.get("llm", {}).get("provider")
    assert (ROOT / "global" / "watch.yaml").exists()


def test_default_report_templates_are_neutral():
    """程序自带的 Word 模板不带使用方信息(作者、单位等元数据为空)。"""
    import zipfile
    tdir = ROOT / "core" / "templates"
    names = sorted(p.name for p in tdir.glob("*.docx"))
    assert names == ["国别报告模板.docx", "项目分析报告模板.docx", "项目分析报告模板_英文.docx"]
    for p in tdir.glob("*.docx"):
        core = zipfile.ZipFile(p).read("docProps/core.xml").decode()
        assert "<dc:creator></dc:creator>" in core or "<dc:creator/>" in core or "dc:creator" not in core, p.name


def test_one_click_installer_keeps_secrets_out_of_the_terminal():
    """一键安装(给 AI 代跑):不读终端输入;密钥不打印、不上命令行;AI 向导与技能指向它。"""
    s = (KIT / "one_click.sh").read_text(encoding="utf-8")
    subprocess.run(["bash", "-n", str(KIT / "one_click.sh")], check=True)
    assert not re.search(r"^[^#]*\bread -r -(s|p)", s, re.M)  # 不在终端里等输入,AI 代跑不会卡住
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", s)  # Mac 自带 bash 3.2,见上面换域名脚本的测试
    # ADMIN.txt 只另存到本机(只有用户能读),不打到终端;secrets.env 只按名字取值,不 source
    assert re.search(r'on_host "cat /opt/teable/ADMIN.txt" > "\$ADMIN_LOCAL"', s) and "umask 077" in s
    assert not re.search(r'^[^#]*(\bsource\b|^\s*\. )', s, re.M)
    assert "exit 10" in s and "需要你做" in s
    guide = (ROOT / "docs" / "AI_INSTALL.md").read_text(encoding="utf-8")
    assert "deploy/teable/one_click.sh" in guide and "secrets.env" in guide
    skill = (ROOT / ".claude" / "skills" / "install-osint-water" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: install-osint-water\n") and "docs/AI_INSTALL.md" in skill
    assert "docs/AI_INSTALL.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_local_trial_stays_on_this_computer():
    """单机试用版(deploy/local):只在本机 127.0.0.1 开端口;和服务器版共用页面路由;本机状态不进仓库。"""
    compose = yaml.safe_load((ROOT / "deploy" / "local" / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        assert all(str(p).startswith("127.0.0.1:") for p in svc.get("ports", [])), name
    assert "../teable/portal.caddy:/etc/caddy/caddy.d/portal.caddy:ro" in compose["services"]["caddy"]["volumes"]
    assert compose["services"]["setup"]["profiles"] == ["setup"]
    s = (ROOT / "deploy" / "local" / "start.sh").read_text(encoding="utf-8")
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / "local" / "start.sh")], check=True)
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", s)  # Mac 自带 bash 3.2
    assert not re.search(r"^[^#]*(\bsource\b|^\s*\. |\bread -r -(s|p))", s, re.M)
    assert "deploy/local/state/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
