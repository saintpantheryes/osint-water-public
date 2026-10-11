"""git 合并驱动:按键三方合并 data 分支上"线索号 → 记录"的 YAML 文件(watch/triage.yaml、watch/ledger_reviewed.yaml)。

这两个文件每次整文件重写(键排好序);定时任务(表格拉回、系统核对)与本机界面各改各的线索时,按行合并会把相邻的两条当成冲突,
变基用 -X theirs 又会丢掉另一边的那条。按键合并:两边改了不同线索都保留;同一条线索两边都改了,取正在合入的一方
(变基时是正在重放的提交,与 -X theirs 一致)。文件开头的注释保留。

git 调用:python yamlmerge.py %O %A %B(共同祖先、当前、合入的一方),结果写回 %A;读不出映射时返回 1,按冲突处理。
配置由 core.sync 写进各检出的 .git/config 与 .git/info/attributes(不进仓库);只依赖 PyYAML,不 import core。
"""
from __future__ import annotations

import sys

import yaml


def _load(path: str) -> tuple[str, dict]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是映射")
    return text, data


def merge(base: dict, ours: dict, theirs: dict) -> dict:
    """按键三方合并;值为 None(没有这条)即删去。"""
    out = {}
    for k in {*base, *ours, *theirs}:
        b, o, t = base.get(k), ours.get(k), theirs.get(k)
        v = o if o == t else t if o == b else o if t == b else t
        if v is not None:
            out[k] = v
    return out


def header(text: str) -> str:
    """文件开头连续的注释行(模板说明)。"""
    lines = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        return 1
    try:
        _, base = _load(argv[1])
        text, ours = _load(argv[2])
        theirs_text, theirs = _load(argv[3])
    except (OSError, ValueError, yaml.YAMLError):
        return 1
    merged = merge(base, ours, theirs)
    body = yaml.safe_dump(merged, allow_unicode=True, sort_keys=True) if merged else ""
    head = header(text) or header(theirs_text)
    with open(argv[2], "w", encoding="utf-8") as f:
        f.write(head + ("\n" + body if body and head else body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
