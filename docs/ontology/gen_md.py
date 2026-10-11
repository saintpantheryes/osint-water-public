"""按 ontology.yaml 重生成 本体.md 里的表格(对象、关系、行为、规则、视图、现状对照;v2.0 起加分类体系与口径)。说明文字是手写的,只替换标记之间的部分:
<!-- gen:名字 --> … <!-- /gen:名字 -->。改本体后重跑:python docs/ontology/gen_md.py;--check 只检查是否已同步(tests/test_ontology.py 用)。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
YAML_PATH, MD_PATH = HERE / "ontology.yaml", HERE / "本体.md"
BY = {"ai": "机器写", "human": "人写", "derived": "派生(不存)"}
ACTOR = {"ai": "机器", "human": "人"}
MAX_SHOWN = 8  # 单选的选项不多于这么多个时,属性后面列出选项


def _prop(p: dict) -> str:
    if p["type"] == "enum" and len(p.get("enum") or []) <= MAX_SHOWN:
        return f"{p['name']}({' / '.join(p['enum'])})"
    return p["name"]


def _s(x: object) -> str:
    """YAML 里没加引号的日期、数字也当文字用;多行示例换成 ⏎。"""
    return str(x).replace("\n", " ⏎ ")


def _detail(p: dict) -> str:
    parts = []
    if p.get("format"):
        parts.append(f"写法:{p['format']}")
    if p.get("where"):
        parts.append(f"在哪算:{p['where']}")
    if p.get("example") not in (None, ""):
        parts.append(f"示例:`{_s(p['example'])}`")
    return f"- **{p['name']}**:" + ";".join(parts)


def objects(o: dict) -> str:
    out = []
    for ob in o["objects"]:
        out.append(f"### {ob['name']}{'(核心)' if ob.get('core') else ''}\n")
        out.append(f"{ob['definition']}标识:{ob['key']}。\n")
        out.append("| 写入方 | 属性 |\n|---|---|")
        for by, zh in BY.items():
            ps = [_prop(p) for p in ob["properties"] if p["by"] == by]
            if ps:
                out.append(f"| {zh} | {' · '.join(ps)} |")
        st = ob.get("states")
        if st:
            steps = ";".join(f"{t['from']} → {t['to']}" + (f"({t['note']})" if t.get("note") else "") for t in st["transitions"])
            tail = f"机器新建时只能是「{st['initial_by_ai']}」。" if st.get("initial_by_ai") else ""
            out.append(f"\n状态(只由人改):{steps}。{tail}")
        details = [_detail(p) for p in ob["properties"] if p.get("format") or p.get("example") or p.get("where")]
        if details:
            out.append("\n属性细则(怎么写、示例、派生的在哪算):\n")
            out += details
        out.append("")
    return "\n".join(out).rstrip("\n")


def links(o: dict) -> str:
    names = {ob["id"]: ob["name"] for ob in o["objects"]}
    props = {(ob["id"], p["id"]): p for ob in o["objects"] for p in ob["properties"]}
    out = ["| 从 | 关系 | 到 | 基数 | 谁建立 |", "|---|---|---|---|---|"]
    for lk in o["links"]:
        via = props[tuple(lk["via"].split(".", 1))]
        who = lk.get("set_by") or ("机器(人可改)" if via["by"] == "ai" else "人")
        rel = lk["name"] + ("(组合)" if lk.get("kind") == "composition" else "")
        out.append(f"| {names[lk['from']]} | {rel} | {names[lk['to']]} | {lk['cardinality']} | {who} |")
    return "\n".join(out)


def _writes(o: dict, refs: list[str]) -> str:
    names = {ob["id"]: ob["name"] for ob in o["objects"]}
    props = {(ob["id"], p["id"]): p["name"] for ob in o["objects"] for p in ob["properties"]}
    groups: dict[str, list[str]] = {}
    for ref in refs:
        obj, prop = ref.split(".", 1)
        groups.setdefault(obj, []).append(props[(obj, prop)])
    return ";".join(f"{names[k]}:{'、'.join(v)}" for k, v in groups.items())


def actions(o: dict) -> str:
    names = {ob["id"]: ob["name"] for ob in o["objects"]}
    out = ["| 编号 | 行为 | 谁 | 在哪 | 何时 | 新建 | 改写 |", "|---|---|---|---|---|---|---|"]
    for a in o["actions"]:
        where = " / ".join(a.get("channel") or []) if a["actor"] == "human" else "后台"
        out.append(f"| {a['id']} | {a['name']} | {ACTOR[a['actor']]} | {where} | {a['trigger']} | {names.get(a.get('creates'), '—')} | "
                   f"{_writes(o, a.get('writes') or []) or '—'} |")
    notes = [f"- **{a['id']} {a['name']}**:{a['note']}" for a in o["actions"] if a.get("note")]
    return "\n".join(out + (["", "要点(各行为的 note):", "", *notes] if notes else []))


def rules(o: dict) -> str:
    out = ["| 编号 | 规则 | 为什么 |", "|---|---|---|"]
    out += [f"| {r['id']} | **{r['name']}**:{r['text']} | {r.get('why', '')} |" for r in o["rules"]]
    notes = [f"- **{r['id']} 的做法**:{r['note']}" for r in o["rules"] if r.get("note")]
    return "\n".join(out + (["", *notes] if notes else []))


def views(o: dict) -> str:
    out = ["| 编号 | 视图 | 在哪看 | 取什么 |", "|---|---|---|---|"]
    out += [f"| {v['id']} | {v['name']} | {' / '.join(v.get('channel') or [])} | {v['query']} |" for v in o["views"]]
    return "\n".join(out)


def as_is(o: dict) -> str:
    names = {ob["id"]: ob["name"] for ob in o["objects"]}
    out = ["| 本体对象 | 存在哪 | 除表之外还有什么(只能是配置、缓存、兜底) |", "|---|---|---|"]
    out += [f"| {names[k]} | {v[0]} | {';'.join(v[1:]) or '—'} |" for k, v in o["as_is"].items()]
    return "\n".join(out)


def taxonomies(o: dict) -> str:
    out = []
    for tid, t in o["taxonomies"].items():
        used = "、".join(t["used_by"])
        out.append(f"### {t['name']}(`{tid}`;用于 {used})\n")
        if t.get("note"):
            out.append(f"{t['note']}\n")
        grouped = len(t["groups"]) > 1
        has_parent = any("parent" in v for g in t["groups"] for v in g["values"])
        head = (["分组"] if grouped else []) + ["值", "代码键"] + (["上级(线索领域)"] if has_parent else []) + ["定义", "边界", "示例"]
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * len(head))
        for g in t["groups"]:
            for v in g["values"]:
                row = ([g["name"]] if grouped else []) + [f"**{v['v']}**", f"`{v.get('code', '')}`"] + ([v.get("parent", "")] if has_parent else []) \
                    + [v.get("def", ""), v.get("boundary") or "", _s(v.get("example") or "")]
                out.append("| " + " | ".join(row) + " |")
        out.append("")
    return "\n".join(out).rstrip("\n")


def glossary(o: dict) -> str:
    out = ["| 口径 | 含义 | 用在哪 |", "|---|---|---|"]
    for key, g in o["glossary"].items():
        used = "、".join(g.get("used_in") or [])
        extra = f";示例 `{_s(g['example'])}`" if g.get("example") else ""
        out.append(f"| **{g['term']}**(`{key}`) | {g['means']}{extra} | {used} |")
    return "\n".join(out)


def terms(o: dict) -> str:
    out = ["| 术语 | 同义说法 | 跨哪些领域 | 按领域筛 | 相关词(查标题与中文摘要,含任一个即算) | 说明 |", "|---|---|---|---|---|---|"]
    for t in o.get("terms") or []:
        out.append(f"| **{t['term']}** | {'、'.join(t.get('aliases') or [])} | {'、'.join(t.get('covers') or [])} | "
                   f"{'、'.join(t.get('sectors') or []) or '不限领域'} | {'、'.join(t.get('match') or [])} | {t.get('means', '')} |")
    return "\n".join(out)


BLOCKS = {"objects": objects, "links": links, "actions": actions, "rules": rules, "views": views, "as_is": as_is,
          "taxonomies": taxonomies, "glossary": glossary, "terms": terms}


def render(md: str, onto: dict) -> str:
    """把 md 里每个 <!-- gen:名字 --> … <!-- /gen:名字 --> 之间换成按本体生成的内容。缺了标记的报错。"""
    for name, fn in BLOCKS.items():
        pat = re.compile(rf"(<!-- gen:{name} -->\n).*?(\n<!-- /gen:{name} -->)", re.S)
        if not pat.search(md):
            raise SystemExit(f"本体.md 里没有 gen:{name} 的标记")
        md = pat.sub(lambda m, fn=fn: m.group(1) + fn(onto) + m.group(2), md)
    return md


def main(argv: list[str]) -> int:
    onto = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    md = MD_PATH.read_text(encoding="utf-8")
    new = render(md, onto)
    if "--check" in argv:
        if new != md:
            print("本体.md 与 ontology.yaml 不同步:运行 python docs/ontology/gen_md.py")
            return 1
        return 0
    MD_PATH.write_text(new, encoding="utf-8")
    print("已重生成", MD_PATH.name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
