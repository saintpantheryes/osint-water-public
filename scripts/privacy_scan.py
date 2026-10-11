"""隐私扫描:公开核心仓里不该出现的东西,提交前(CI 与 tests/test_privacy.py)自动查一遍。

    python scripts/privacy_scan.py                # 扫整个仓库(私有配置目录 private/ 除外),有问题退出码 1
    python scripts/privacy_scan.py --terms 文件    # 另加一份"敏感词"清单(一行一个;空行与 # 开头的忽略)

查两类东西:
1. 通用规则(代码里写死):公网 IP、邮箱、私钥内容、看着像令牌的长串、密钥文件名。
2. 敏感词清单:使用方名称、域名、人名等。清单本身就是敏感的,所以不放在公开仓里:缺省读
   私有配置目录 private/privacy_terms.txt(存在才读),或 --terms / 环境变量 OW_PRIVACY_TERMS 指定的文件。
   公开仓的 CI 没有私有目录,只跑通用规则;在有私有检出的机器上(主机、Mac)两类都查。

不扫:.git、.venv、node_modules、__pycache__、data/、private/、第三方库与地图数据(portal/echarts.min.js、portal/world.geo.json)、本文件。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "data", "private", ".pytest_cache"}
SKIP_FILES = {"portal/echarts.min.js", "portal/world.geo.json", "scripts/privacy_scan.py", "tests/test_privacy.py"}
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".yaml", ".yml", ".json", ".txt", ".sh", ".toml", ".cfg", ".ini", ".caddy", ".example", ""}

# 通用规则:名字 → (正则, 允许的例外正则)
RULES = {
    "公网 IP": (re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),  # 前后紧跟数字或点的不算(条款号 1.1.2.2.1、版本号)
               re.compile(r"^(?:127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|255\.|\d{1,3}\.\d{1,3}\.\d{1,3}\.0$|(?:\d+\.){3}\d+(?=\D*(?:版本|version|v\d)))")),
    "邮箱": (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
             re.compile(r"@(?:example\.|noreply\.|users\.noreply\.|anthropic\.com|github\.com$)|\.(?:test|local|example|invalid)$|^(?:you|user|name|someone|xxx|bot)@|@\w+\.(?:yaml|py|json|md|rq|sparql)$")),
    "私钥内容": (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), None),
    "像令牌的长串": (re.compile(r"\b(?:teable_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"), None),
    "密钥文件": (re.compile(r"\b[\w-]+\.pem\b"), re.compile(r"^(?:\*|<[^>]*>|密钥文件|密钥|xxx)\.pem$")),
}


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.as_posix() in SKIP_FILES:
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"Caddyfile", "Dockerfile", "LICENSE"}:
            continue
        yield p, rel


def load_terms(path: Path | None) -> list[str]:
    if not path or not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def scan(root: Path = ROOT, terms: list[str] | None = None) -> list[tuple[str, int, str, str]]:
    """返回 (文件, 行号, 规则名, 命中片段)。"""
    hits = []
    for p, rel in iter_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for name, (pat, allow) in RULES.items():
                for m in pat.finditer(line):
                    s = m.group(0)
                    if allow and allow.search(s):
                        continue
                    hits.append((rel.as_posix(), no, name, s))
            for t in terms or []:
                if t in line:
                    hits.append((rel.as_posix(), no, "敏感词", t))
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--terms", default=os.environ.get("OW_PRIVACY_TERMS") or str(ROOT / "private" / "privacy_terms.txt"))
    a = ap.parse_args(argv)
    terms = load_terms(Path(a.terms))
    hits = scan(Path(a.root), terms)
    if not hits:
        print(f"隐私扫描通过(通用规则{'' if not terms else f' + {len(terms)} 个敏感词'})")
        return 0
    for f, no, name, s in hits:
        print(f"{f}:{no}: {name}: {s}")
    print(f"共 {len(hits)} 处;公开核心仓不该有这些,请改成占位符、移到私有配置仓,或在 privacy_terms 里说明例外")
    return 1


if __name__ == "__main__":
    sys.exit(main())
