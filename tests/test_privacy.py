"""公开核心仓的隐私扫描(scripts/privacy_scan.py):公网 IP、邮箱、私钥、令牌、密钥文件名不该出现;有私有配置目录时还查敏感词清单。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location("privacy_scan", ROOT / "scripts" / "privacy_scan.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_rules_catch_the_obvious(tmp_path):
    m = _mod()
    (tmp_path / "a.md").write_text("主机 203.0.113.9,联系 someone.real@company.example.org;密钥 server.pem\n", encoding="utf-8")
    (tmp_path / "b.py").write_text('TOKEN = "teable_" + "x" * 30\nKEY = "-----BEGIN RSA PRIVATE KEY-----"\n', encoding="utf-8")
    (tmp_path / "ok.md").write_text("本机 127.0.0.1;条款 art. 1.1.2.2.1;you@example.com;a@b.test;*.pem 与 <密钥文件>.pem 已在 .gitignore\n", encoding="utf-8")
    names = {(f, name) for f, _, name, _ in m.scan(tmp_path, ["公司名"])}
    assert ("a.md", "公网 IP") in names and ("a.md", "密钥文件") in names and ("b.py", "私钥内容") in names
    assert not any(f == "ok.md" for f, _ in names)
    (tmp_path / "c.md").write_text("公司名在此\n", encoding="utf-8")
    assert ("c.md", "敏感词") in {(f, name) for f, _, name, _ in m.scan(tmp_path, ["公司名"])}


def test_repo_is_clean():
    m = _mod()
    terms = m.load_terms(ROOT / "private" / "privacy_terms.txt")
    hits = m.scan(ROOT, terms)
    assert not hits, "\n".join(f"{f}:{no}: {name}: {s}" for f, no, name, s in hits)
