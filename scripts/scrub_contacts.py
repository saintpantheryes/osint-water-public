"""维护:去掉数据目录里已入库线索与正文缓存中的邮箱、电话(2026-09-26 起新采集的在入库前已去掉,这里补处理此前的)。

用法:python scripts/scrub_contacts.py data/private      # 数据目录(data 分支的检出);改完照常提交推送
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.watch.text import scrub_contacts  # noqa: E402


def _clean(v):
    """递归处理线索里的字符串(摘录、字段、模型给出的依据与摘要)。"""
    if isinstance(v, str):
        return scrub_contacts(v)
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: (x if k in ("id", "url", "key", "source", "first_seen", "published") else _clean(x)) for k, x in v.items()}
    return v


def main(data_dir: str) -> None:
    w = Path(data_dir) / "watch"
    docs = sigs = 0
    for fp in sorted((w / "docs").glob("*.txt")):
        t = fp.read_text(encoding="utf-8")
        c = scrub_contacts(t)
        if c != t:
            fp.write_text(c, encoding="utf-8")
            docs += 1
    for fp in sorted((w / "signals").glob("*.jsonl")):
        rows, changed = [], False
        for line in fp.read_text(encoding="utf-8").splitlines():
            s = json.loads(line)
            c = _clean(s)
            if c != s:
                changed, sigs = True, sigs + 1
            rows.append(json.dumps(c, ensure_ascii=False))
        if changed:
            fp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    for fp in sorted((w / "submitted").glob("*.json")):
        s = json.loads(fp.read_text(encoding="utf-8"))
        c = _clean(s)
        if c != s:
            fp.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
            sigs += 1
    print(f"正文缓存改了 {docs} 个文件,线索改了 {sigs} 条")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/private")
