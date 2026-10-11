"""读本体(docs/ontology/ontology.yaml)与 Teable 映射(docs/ontology/teable_map.yaml),给建表(core/schema.py)、迁移与后台任务用。

类型映射(架构方案第 3 节):text → 单行文本;url → 单行文本(显示为网址);longtext、list → 长文本(list 一行一项);date → 日期;
datetime → 日期(含时间);pdate → 日期(取区间起点)+「<名>原文」单行文本;number → 数字;bool → 勾选;enum / enums → 单选 / 多选
(选项照本体,不自动加);file → 附件;link:X / links:X → 关联(单条 / 多条);link:member → 成员。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ONTO_PATH = ROOT / "docs" / "ontology" / "ontology.yaml"
MAP_PATH = ROOT / "docs" / "ontology" / "teable_map.yaml"
BY_ZH = {"ai": "机器写", "human": "人写", "derived": "派生(表格里算,机器不写)"}
PDATE_SUFFIX = "原文"


@dataclass(frozen=True)
class Prop:
    obj: str
    id: str
    name: str
    type: str
    by: str
    enum: tuple[str, ...] = ()
    note: str = ""
    required: bool = False

    @property
    def ref(self) -> str:
        return f"{self.obj}.{self.id}"

    @property
    def target(self) -> str | None:
        """关联的对象(link:X / links:X 的 X;成员为 member)。"""
        return self.type.split(":", 1)[1] if self.type.startswith(("link:", "links:")) else None

    @property
    def is_link(self) -> bool:
        return self.target is not None and self.target != "member"


@dataclass(frozen=True)
class Obj:
    id: str
    name: str
    key: str
    core: bool
    props: tuple[Prop, ...] = field(default_factory=tuple)

    def prop(self, pid: str) -> Prop:
        return next(p for p in self.props if p.id == pid)

    @property
    def props_by_id(self) -> dict[str, Prop]:
        return {p.id: p for p in self.props}

    def by_name(self, name: str) -> Prop | None:
        return next((p for p in self.props if p.name == name), None)


@dataclass(frozen=True)
class Ontology:
    objects: dict[str, Obj]
    actions: tuple[dict, ...]
    raw: dict

    def prop(self, ref: str) -> Prop:
        obj, pid = ref.split(".", 1)
        return self.objects[obj].prop(pid)

    def machine_writes(self) -> set[str]:
        """机器的行为写的属性(对象.属性)。"""
        return {r for a in self.actions if a["actor"] == "ai" for r in a.get("writes") or []}


def load(path: Path | None = None) -> Ontology:
    raw = yaml.safe_load(Path(path or ONTO_PATH).read_text(encoding="utf-8"))
    objs = {}
    for o in raw["objects"]:
        props = tuple(Prop(o["id"], p["id"], p["name"], p["type"], p["by"], tuple(p.get("enum") or ()), str(p.get("note") or ""),
                           bool(p.get("required"))) for p in o["properties"])
        objs[o["id"]] = Obj(o["id"], o["name"], str(o.get("key") or ""), bool(o.get("core")), props)
    return Ontology(objs, tuple(raw["actions"]), raw)


@lru_cache(maxsize=4)
def _map_cached(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# ---------------- 运行时读本体(v2.2):代码里不再抄枚举,从这里取 ----------------
# 分类体系(taxonomies)每个值带 code(英文键),判定程序用英文键、表里存中文值;口径(glossary)与归组(groupings)页面与后台共用。
# 改选项:只改 ontology.yaml(再在 Teable 里加选项),代码不用动;tests/test_ontology.py 核对代码里剩下的映射仍指向存在的值。

@lru_cache(maxsize=1)
def raw(path: str | None = None) -> dict:
    """整个 ontology.yaml(缓存);页面读的是同一份转成的 portal/ontology.json。"""
    return yaml.safe_load(Path(path or ONTO_PATH).read_text(encoding="utf-8"))


def taxonomy(tid: str) -> dict:
    return raw()["taxonomies"][tid]


def values(tid: str) -> tuple[str, ...]:
    """一套分类体系的全部值,按本体里的顺序(也就是表里选项的顺序、页面的排序)。"""
    return tuple(v["v"] for g in taxonomy(tid)["groups"] for v in g["values"])


def codes(tid: str) -> dict[str, str]:
    """英文键 → 中文值。"""
    return {str(v["code"]): v["v"] for g in taxonomy(tid)["groups"] for v in g["values"] if v.get("code") is not None}


def code_of(tid: str) -> dict[str, str]:
    """中文值 → 英文键。"""
    return {v: c for c, v in codes(tid).items()}


def group_values(tid: str, group_prefix: str) -> tuple[str, ...]:
    """某一组(按组名开头匹配,如「采购类」)的值。"""
    for g in taxonomy(tid)["groups"]:
        if str(g["name"]).startswith(group_prefix):
            return tuple(v["v"] for v in g["values"])
    raise KeyError(f"taxonomies.{tid} 里没有以「{group_prefix}」开头的组")


def glossary(key: str) -> dict:
    return raw()["glossary"][key]


def glossary_stages(key: str) -> frozenset[str]:
    """口径里列的阶段集合(如 tracked = 跟踪中的商机)。"""
    return frozenset(glossary(key)["stages"])


def grouping(gid: str) -> dict:
    return raw()["groupings"][gid]


def load_map(path: Path | None = None) -> dict:
    return _map_cached(str(path or MAP_PATH))


def date_options(tz: str, with_time: bool = False) -> dict:
    return {"formatting": {"date": "YYYY-MM-DD", "time": "HH:mm" if with_time else "None", "timeZone": tz}}


def describe(p: Prop) -> str:
    return BY_ZH[p.by] + (f":{p.note}" if p.note else "")


def field_specs(p: Prop, tz: str, extra_options: dict | None = None) -> list[dict]:
    """一个非关联、非派生属性 → Teable 字段的建法(pdate 是两个字段)。关联与成员在 schema 里建(要对方表的编号)。"""
    desc = describe(p)
    t = p.type
    if t in ("text", "url"):
        spec = {"type": "singleLineText"}
        if t == "url":
            spec["options"] = {"showAs": {"type": "url"}}
    elif t in ("longtext", "list"):
        spec = {"type": "longText"}
    elif t == "date":
        spec = {"type": "date", "options": date_options(tz)}
    elif t == "datetime":
        spec = {"type": "date", "options": date_options(tz, with_time=True)}
    elif t == "pdate":
        return [{"name": p.name, "type": "date", "options": date_options(tz), "description": desc + "(可只到月、季、半年的,取区间起点)"},
                {"name": p.name + PDATE_SUFFIX, "type": "singleLineText",
                 "description": f"{BY_ZH[p.by]}:{p.name}的原文写法,如 2027-H2、2026-Q4、2026-12"}]
    elif t == "number":
        spec = {"type": "number", "options": {"formatting": {"type": "decimal", "precision": 0}}}
    elif t == "bool":
        spec = {"type": "checkbox"}
    elif t in ("enum", "enums"):
        spec = {"type": "singleSelect" if t == "enum" else "multipleSelect",
                "options": {"choices": [{"name": c} for c in p.enum], "preventAutoNewOptions": True}}
    elif t == "file":
        spec = {"type": "attachment"}
    elif t == "link:member":
        spec = {"type": "user", "options": {"isMultiple": False, "shouldNotify": False, **(extra_options or {})}}
    else:
        raise ValueError(f"{p.ref}:{t} 不是简单字段(关联在 schema 里建)")
    return [{"name": p.name, **spec, "description": desc}]


def relationship(p: Prop) -> str:
    """本体的 link:X(单条)→ manyOne;links:X(多条)→ manyMany。"""
    return "manyMany" if p.type.startswith("links:") else "manyOne"
