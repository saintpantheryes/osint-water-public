"""按本体与 Teable 映射在新库里建表、补字段、补选项、补视图(架构方案第 3 节、阶段 B)。只增不删、可重复运行;建好把库、表、字段、视图的
编号写进 private/portal/config.json(页面按编号取数,同事改了字段名也不坏;主机上 compose 把它挂到页面目录)。

    python -m core.schema --profile private/profile.yaml              # 用管理员令牌(TEABLE_ADMIN_TOKEN)建表、补字段
    python -m core.schema --profile private/profile.yaml --dry-run    # 只列出要建的表与字段,不连表格

顺序:先建七张表(主字段 + 普通字段)→ 加关联字段(要对方表的编号),把 Teable 自动生成的反向字段改成映射里的名字 →
辅助公式 → 派生字段(汇总、查找、最后修改人/时间)→ 主字段换成公式 → 缺省值 → 视图。映射里 verify: A 的(阶段 A 核验的能力)
建不成时记警告、用 fallback 或跳过,不中断;其余建不成的报错。只动新库(库名以「新版」结尾)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from core import ontology as onto_mod
from core import tstore
from core.ontology import Ontology, Prop

ROOT = Path(__file__).resolve().parent.parent
# 编号属于一次部署,放私有配置目录(private/portal/config.json,主机上由 compose 挂到页面目录);没有私有目录(演示、测试)时写 portal/config.json
CONFIG_PATH = (ROOT / "private" / "portal" / "config.json") if (ROOT / "private").is_dir() else (ROOT / "portal" / "config.json")
PRIMARY = "<primary>"


@dataclass
class LinkPlan:
    prop: Prop
    target: str  # 对方对象
    relationship: str
    reverse: str | None
    one_way: bool


@dataclass
class TablePlan:
    obj: str
    name: str
    primary: dict  # 建表时的主字段(公式主字段先用同名单行文本占位)
    primary_formula: dict | None
    fields: list[dict]
    links: list[LinkPlan]
    helpers: list[dict]
    derived: list[tuple[Prop, dict]]
    system: list[dict]
    defaults: dict[str, object]
    views: list[dict]
    default_sort: list = field(default_factory=list)


@dataclass
class Result:
    base: dict
    tables: dict[str, dict] = field(default_factory=dict)  # 对象 → 表
    fields: dict[str, dict[str, dict]] = field(default_factory=dict)  # 对象 → 字段名 → 字段
    views: dict[str, dict[str, dict]] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def latest_start_formula(lead_times: dict | None, kinds: dict[str, str]) -> str:
    """最晚启动(辅助):不是"已发生"且日期不早于今天的节点,日期 − 该类节点的内部流程周期(profile 的 lead_times,各项天数相加);没有周期的类型为空。
    核自 Teable 源码:TODAY() 是公式时区里的当天零点,IS_BEFORE 不给单位时按天比。
    本地注册的天数看投标资格,不在公式里(资格依据里写)。"""
    expr = "BLANK()"
    for typ, kind in reversed(list(kinds.items())):
        comp = (lead_times or {}).get(typ)
        if isinstance(comp, dict) and comp:
            days = sum(int(v) for v in comp.values())
            expr = f'IF({{类型}} = "{kind}", DATE_ADD({{日期}}, -{days}, "day"), {expr})'
    return f'IF(OR({{状态}} = "已发生", IS_BEFORE({{日期}}, TODAY())), BLANK(), {expr})'


def build_plan(onto: Ontology, tmap: dict, lead_times: dict | None = None) -> list[TablePlan]:
    tz = tmap["timezone"]
    plans = []
    for oid in tmap["order"]:
        tm, obj = tmap["tables"][oid], onto.objects[oid]
        prim = tm["primary"]
        opts = tm.get("options") or {}
        fields: list[dict] = []
        if isinstance(prim, str):
            primary, pformula = onto_mod.field_specs(obj.prop(prim), tz, opts.get(prim))[0], None
        else:
            primary = {"name": prim["name"], "type": "singleLineText", "description": "派生(表格里算,机器不写):主字段,公式"}
            pformula = prim
        if key := tm.get("key"):
            by = key.get("by", "ai")
            fields.append({"name": key["name"], "type": "singleLineText",
                           "description": f"{onto_mod.BY_ZH[by]}:标识,{key.get('note', '')}".rstrip(",")})
        links, derived = [], []
        for p in obj.props:
            if not isinstance(prim, str) or p.id != prim:
                if p.by == "derived":
                    spec = (tmap.get("derived") or {}).get(p.ref)
                    if spec is None:
                        raise ValueError(f"{p.ref} 是派生属性,teable_map.yaml 的 derived 里没写做法")
                    derived.append((p, spec))
                elif p.is_link:
                    lk = (tmap.get("links") or {}).get(p.ref) or {}
                    links.append(LinkPlan(p, p.target, onto_mod.relationship(p), lk.get("reverse"), bool(lk.get("one_way"))))
                else:
                    fields += onto_mod.field_specs(p, tz, opts.get(p.id))
        helpers = []
        for h in tm.get("helpers") or []:
            expr = latest_start_formula(lead_times, tmap.get("lead_time_kinds") or {}) if h["formula"] == "latest_start" else h["formula"]
            helpers.append({**h, "formula": expr})
        defaults = {obj.prop(k).name: v for k, v in (tm.get("defaults") or {}).items()}
        system = [{**s, "track": [obj.prop(x).name for x in s.get("track") or []]} for s in tm.get("system") or []]
        plans.append(TablePlan(oid, tm["table"], primary, pformula, fields, links, helpers, derived, system,
                               defaults, list((tmap.get("views") or {}).get(oid) or []), list(tm.get("default_sort") or [])))
    return plans


def resolve_formula(expr: str, fields: dict[str, dict]) -> str:
    """公式里的 {字段名} → {字段编号};名字不存在时报错。"""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in fields:
            raise KeyError(f"公式引用的字段不存在:{name}")
        return "{" + fields[name]["id"] + "}"
    return re.sub(r"\{([^{}]+)\}", sub, expr)


def resolve_filter(spec: dict | list, fields: dict[str, dict], tz: str) -> dict:
    """映射里的条件 {and: [[字段名, 运算, 值], {or: [...]}]} → Teable 的 filter。"""
    if isinstance(spec, dict):
        (conj, items), = spec.items()
        return {"conjunction": conj, "filterSet": [resolve_filter(x, fields, tz) for x in items]}
    name, op, *rest = spec
    value = rest[0] if rest else None
    if isinstance(value, dict) and "mode" in value:
        value = {**value, "timeZone": tz}
    return {"fieldId": fields[name]["id"], "operator": op, "value": value}


class Builder:
    def __init__(self, api: tstore.Client, base_name: str, log=print):
        self.api, self.log = api, log
        self.res = Result(base=api.ensure_new_base(base_name))

    def _refresh(self, oid: str) -> dict[str, dict]:
        self.res.fields[oid] = {f["name"]: f for f in self.api.fields(self.res.tables[oid]["id"])}
        return self.res.fields[oid]

    def _try(self, what: str, verify: bool, fn) -> bool:
        """做一步;verify(阶段 A 核验的能力)的失败记警告,其余照抛。"""
        try:
            fn()
            return True
        except (tstore.TStoreError, KeyError) as e:
            if not verify:
                raise
            self.res.warnings.append(f"{what}没建成(阶段 A 核验的能力,用后备做法):{str(e)[:200]}")
            return False

    def _new_field(self, oid: str, spec: dict, verify: bool = False) -> None:
        name = spec["name"]
        if name in self.res.fields[oid]:
            return
        t = self.res.tables[oid]

        def make():
            self.api.create_field(t["id"], spec)
            self.res.created.append(f"{t['name']}.{name}")
            self.log(f"  {t['name']} 加字段 {name}")
        if self._try(f"{t['name']} 的字段「{name}」", verify, make):
            self._refresh(oid)

    def tables(self, plans: list[TablePlan]) -> None:
        have = {t["name"]: t for t in self.api.tables(self.res.base["id"])}
        for tp in plans:
            if tp.name not in have:
                self.log(f"建表 {tp.name}")
                have[tp.name] = self.api.create_table(self.res.base["id"], tp.name, [tp.primary, *tp.fields],
                                                      views=[{"name": "全部", "type": "grid"}])
                self.res.created.append(tp.name)
            self.res.tables[tp.obj] = have[tp.name]
            fields = self._refresh(tp.obj)
            for spec in tp.fields:
                if spec["name"] not in fields:
                    self._new_field(tp.obj, spec)
                else:
                    self._add_choices(tp, fields[spec["name"]], spec)

    def _add_choices(self, tp: TablePlan, f: dict, spec: dict) -> None:
        """本体给单选 / 多选加了选项(如报告类型加「日报」):已有字段只补缺的选项,不删不改已有的(字段锁了自动加选项,写记录时加不上)。"""
        if spec.get("type") not in ("singleSelect", "multipleSelect") or f.get("type") != spec["type"]:
            return
        have = [c["name"] for c in (f.get("options") or {}).get("choices") or []]
        missing = [c for c in (spec.get("options") or {}).get("choices") or [] if c["name"] not in have]
        if not missing:
            return
        opts = {**(f.get("options") or {}), "choices": [*((f.get("options") or {}).get("choices") or []), *missing]}
        self.api.convert_field(self.res.tables[tp.obj]["id"], f["id"], {"type": f["type"], "options": opts})
        self.res.created.append(f"{tp.name}.{f['name']} 选项 {'、'.join(c['name'] for c in missing)}")
        self.log(f"  {tp.name} 字段 {f['name']} 补选项 {'、'.join(c['name'] for c in missing)}")
        self._refresh(tp.obj)

    def links(self, plans: list[TablePlan]) -> None:
        for tp in plans:
            for lp in tp.links:
                name = lp.prop.name
                if name not in self.res.fields[tp.obj]:
                    opts = {"relationship": lp.relationship, "foreignTableId": self.res.tables[lp.target]["id"]}
                    if lp.one_way:
                        opts["isOneWay"] = True
                    self._new_field(tp.obj, {"name": name, "type": "link", "options": opts, "description": onto_mod.describe(lp.prop)})
                    self._refresh(lp.target)
                if lp.reverse and not lp.one_way:
                    self._rename_reverse(tp, lp)

    def _rename_reverse(self, tp: TablePlan, lp: LinkPlan) -> None:
        sym = ((self.res.fields[tp.obj][lp.prop.name].get("options") or {}).get("symmetricFieldId"))
        other = self._refresh(lp.target)
        cur = next((f for f in other.values() if f["id"] == sym), None)
        if cur is None or cur["name"] == lp.reverse:
            return
        if lp.reverse in other:
            self.res.warnings.append(f"{self.res.tables[lp.target]['name']} 已有字段「{lp.reverse}」,反向字段「{cur['name']}」没改名")
            return
        self.api.update_field(self.res.tables[lp.target]["id"], sym, name=lp.reverse,
                              description=f"派生(表格里算,机器不写):{tp.name}「{lp.prop.name}」的反向关联")
        self.log(f"  {self.res.tables[lp.target]['name']} 的反向字段「{cur['name']}」改名为「{lp.reverse}」")
        self._refresh(lp.target)

    def helpers(self, plans: list[TablePlan], tz: str) -> None:
        for tp in plans:
            for h in tp.helpers:
                if h["name"] in self.res.fields[tp.obj]:
                    continue
                opts: dict = {"expression": resolve_formula(h["formula"], self.res.fields[tp.obj]), "timeZone": tz}
                if h.get("format") == "date":
                    opts["formatting"] = onto_mod.date_options(tz)["formatting"]
                self._new_field(tp.obj, {"name": h["name"], "type": "formula", "options": opts,
                                         "description": "派生(表格里算,机器不写):辅助公式"}, verify=bool(h.get("verify")))

    def _lookup_spec(self, oid: str, p: Prop, how: dict, tz: str) -> dict:
        kind = "rollup" if "rollup" in how else "lookup"
        d = how[kind]
        via = self.res.fields[oid][d["via"]]
        ft_obj = next(k for k, t in self.res.tables.items() if t["id"] == via["options"]["foreignTableId"])
        ff = self.res.fields[ft_obj]
        target = next(f for f in ff.values() if f.get("isPrimary")) if d["field"] == PRIMARY else ff[d["field"]]
        lo: dict = {"foreignTableId": via["options"]["foreignTableId"], "linkFieldId": via["id"], "lookupFieldId": target["id"]}
        if d.get("filter"):
            lo["filter"] = resolve_filter({"and": d["filter"]}, ff, tz)
        if d.get("sort"):
            lo["sort"] = {"fieldId": ff[d["sort"][0]]["id"], "order": d["sort"][1]}
        if d.get("limit"):
            lo["limit"] = int(d["limit"])
        spec: dict = {"name": p.name, "description": onto_mod.describe(p), "lookupOptions": lo}
        if kind == "rollup":
            opts: dict = {"expression": d["expression"]}
            if how.get("format") == "date":
                opts["formatting"] = onto_mod.date_options(tz)["formatting"]
            spec.update(type="rollup", options=opts)
        elif d.get("sort") or d.get("limit"):
            # 条件查找(isConditionalLookup,核自 Teable 源码):经关联的查找只收 filter、不收 sort 与 limit;"按日期倒序取第一条"要用它——
            # 不经关联字段,靠 filter 关联:对方表里指回本表的关联字段(via 的反向字段)等于本行(value 写 {type: field, fieldId: 本表主字段}),
            # 再加映射里的条件。实例不支持时接口会静默丢掉 sort、limit:derived() 查回显,丢了就删掉字段、走后备。
            sym = (via.get("options") or {}).get("symmetricFieldId")
            if not sym:
                raise KeyError(f"{d['via']} 没有反向字段,条件查找关联不到本行")
            prim = next(f for f in self.res.fields[oid].values() if f.get("isPrimary"))
            rel = {"fieldId": sym, "operator": "is", "value": {"type": "field", "fieldId": prim["id"]}}
            flt = lo.get("filter") or {"conjunction": "and", "filterSet": []}
            spec["lookupOptions"] = {"foreignTableId": lo["foreignTableId"], "lookupFieldId": lo["lookupFieldId"],
                                     "filter": {"conjunction": "and", "filterSet": [rel, *flt["filterSet"]]},
                                     **{k: lo[k] for k in ("sort", "limit") if k in lo}}
            spec.update(type=target["type"], isLookup=True, isConditionalLookup=True)
            if target.get("options") and target["type"] != "formula":  # 公式的 options(表达式)不能照搬:Teable 自己从对方字段取格式
                spec["options"] = target["options"]
        else:
            spec.update(type=target["type"], isLookup=True)
            if target.get("options") and target["type"] != "formula":  # 公式的 options(表达式)不能照搬:Teable 自己从对方字段取格式
                spec["options"] = target["options"]
        return spec

    def _check_echo(self, tp: TablePlan, name: str, spec: dict) -> None:
        """条件查找:旧版本的接口会静默丢掉 isConditionalLookup、sort、limit——那这个字段返回的是全部值而不是最近一条。
        回显里缺了的,删掉字段、记警告、走后备(后台算)。"""
        fld = self.res.fields[tp.obj][name]
        lo = fld.get("lookupOptions") or {}
        lost = [k for k in ("sort", "limit") if k in spec["lookupOptions"] and not lo.get(k)]
        if not fld.get("isConditionalLookup"):
            lost.append("isConditionalLookup")
        if lost:
            self.api.delete_field(self.res.tables[tp.obj]["id"], fld["id"])
            self.res.created = [x for x in self.res.created if x != f"{tp.name}.{name}"]
            self.res.warnings.append(f"{tp.name} 的「{name}」:接口没收下 {'、'.join(lost)},查找会返回全部值而不是最近一条——已删掉,"
                                     "用后备做法(后台算)")
            self._refresh(tp.obj)

    def derived(self, plans: list[TablePlan], tz: str) -> None:
        for tp in plans:
            for p, how in tp.derived:
                if "reverse_of" in how:  # 反向关联就是它:核对名字
                    if p.name not in self.res.fields[tp.obj]:
                        self.res.warnings.append(f"{tp.name} 没有「{p.name}」:{how['reverse_of']} 的反向字段没建成或没改名")
                    continue
                if p.name in self.res.fields[tp.obj]:
                    continue
                verify = how.get("verify") == "A"
                try:
                    spec = self._lookup_spec(tp.obj, p, how, tz)
                except KeyError as e:
                    if not verify:
                        raise
                    self.res.warnings.append(f"{tp.name} 的「{p.name}」没建成:缺字段 {e}")
                    continue
                self._new_field(tp.obj, spec, verify=verify)
                if spec.get("isConditionalLookup") and p.name in self.res.fields[tp.obj]:
                    self._check_echo(tp, p.name, spec)
            for s in tp.system:
                if s["name"] in self.res.fields[tp.obj]:
                    continue
                tracked = [self.res.fields[tp.obj][n]["id"] for n in s["track"]]
                opts: dict = {"trackedFieldIds": tracked}
                if s["type"] == "lastModifiedTime":
                    opts["formatting"] = onto_mod.date_options(tz, with_time=True)["formatting"]
                self._new_field(tp.obj, {"name": s["name"], "type": s["type"], "options": opts,
                                         "description": "派生(表格里算,机器不写):只跟踪「我方阶段」,阶段变更记跟进(A18)用"},
                                verify=s.get("verify") == "A")

    def primaries(self, plans: list[TablePlan], tz: str) -> None:
        for tp in plans:
            pf = tp.primary_formula
            if not pf:
                continue
            fields = self.res.fields[tp.obj]
            prim = next(f for f in fields.values() if f.get("isPrimary"))
            if prim["type"] == "formula":
                continue
            done, last = False, None
            for i, expr in enumerate([pf["formula"], pf.get("fallback")]):
                if not expr:
                    continue
                spec = {"type": "formula", "options": {"expression": resolve_formula(expr, fields), "timeZone": tz}}
                try:
                    self.api.convert_field(self.res.tables[tp.obj]["id"], prim["id"], spec)
                    self.log(f"  {tp.name} 的主字段「{prim['name']}」换成公式" + ("(后备写法)" if i else ""))
                    if i:
                        self.res.warnings.append(f"{tp.name} 的主字段用了后备公式:{pf.get('fallback')}")
                    done = True
                    break
                except tstore.TStoreError as e:
                    last = e
            if not done:
                self.res.warnings.append(f"{tp.name} 的主字段没能换成公式,仍是单行文本「{prim['name']}」(机器写):{str(last)[:160]}")
            self._refresh(tp.obj)

    def defaults(self, plans: list[TablePlan]) -> None:
        for tp in plans:
            for name, value in tp.defaults.items():
                f = self.res.fields[tp.obj][name]
                if (f.get("options") or {}).get("defaultValue") == value:
                    continue
                spec = {"type": f["type"], "options": {**(f.get("options") or {}), "defaultValue": value}}
                self._try(f"{tp.name}「{name}」的缺省值", True, lambda tp=tp, f=f, spec=spec: self.api.convert_field(
                    self.res.tables[tp.obj]["id"], f["id"], spec))
            self._refresh(tp.obj)

    def views(self, plans: list[TablePlan], tz: str, new_tables: set[str]) -> None:
        for tp in plans:
            tid, fields = self.res.tables[tp.obj]["id"], self.res.fields[tp.obj]
            have = {v["name"]: v for v in self.api.views(tid)}
            if tp.obj in new_tables and tp.default_sort and "全部" in have:
                self._view_parts(tp, have["全部"], {"sort": tp.default_sort}, fields, tz)
            for vs in tp.views:
                if vs["name"] in have:
                    continue
                spec: dict = {"name": vs["name"], "type": vs.get("type", "grid")}
                prim = next(f for f in fields.values() if f.get("isPrimary"))
                if spec["type"] == "kanban":
                    spec["options"] = {"stackFieldId": fields[vs["stack"]]["id"]}
                elif spec["type"] == "calendar":
                    d = fields[vs["date"]]["id"]
                    spec["options"] = {"startDateFieldId": d, "endDateFieldId": d, "titleFieldId": prim["id"]}
                v = self.api.create_view(tid, spec)
                have[vs["name"]] = v
                self.res.created.append(f"{tp.name} 视图 {vs['name']}")
                self.log(f"  {tp.name} 建视图 {vs['name']}")
                self._view_parts(tp, v, vs, fields, tz)
            self.res.views[tp.obj] = {v["name"]: v for v in self.api.views(tid)}

    def _view_parts(self, tp: TablePlan, v: dict, vs: dict, fields: dict, tz: str) -> None:
        tid = self.res.tables[tp.obj]["id"]
        parts = (("筛选", "filter", lambda: self.api.set_view_filter(tid, v["id"], resolve_filter(vs["filter"], fields, tz))),
                 ("排序", "sort", lambda: self.api.set_view_sort(
                     tid, v["id"], [{"fieldId": fields[n]["id"], "order": o} for n, o in vs["sort"]])),
                 ("分组", "group", lambda: self.api.set_view_group(
                     tid, v["id"], [{"fieldId": fields[n]["id"], "order": o} for n, o in vs["group"]])))
        for zh, key, fn in parts:
            if vs.get(key):
                self._try(f"{tp.name} 视图「{v['name']}」的{zh}", True, fn)


def apply(api: tstore.Client, plans: list[TablePlan], base_name: str, tz: str = "Asia/Shanghai", log=print) -> Result:
    b = Builder(api, base_name, log)
    before = {t["name"] for t in api.tables(b.res.base["id"])}
    b.tables(plans)
    new_tables = {tp.obj for tp in plans if tp.name not in before}
    b.links(plans)
    b.primaries(plans, tz)  # 先换主字段公式(只引用普通字段与关联),再建对 <primary> 的查找:类型才跟着是公式(审查二-9)
    b.helpers(plans, tz)
    b.derived(plans, tz)
    b.defaults(plans)
    b.views(plans, tz, new_tables)
    return b.res


def _link_kind(f: dict) -> str | None:
    """关联 / 人员字段写值的形状:one = {id},many = [{id}];别的字段 None。"""
    o = f.get("options") or {}
    if f.get("type") == "link":
        return "many" if o.get("relationship") in ("manyMany", "oneMany") else "one"
    if f.get("type") == "user":
        return "many" if o.get("isMultiple") else "one"
    return None


def config(res: Result, onto: Ontology) -> dict:
    """portal/config.json:库、表、字段、视图的编号。props 按本体属性(对象 → 属性 → 字段编号),fields 按字段名。"""
    out: dict = {"base": {"id": res.base["id"], "name": res.base["name"]}, "tables": {}}
    for oid, t in res.tables.items():
        fields = res.fields.get(oid) or {}
        obj = onto.objects[oid]
        key_name = ((onto_mod.load_map()["tables"].get(oid) or {}).get("key") or {}).get("name")
        out["tables"][oid] = {
            "id": t["id"], "name": t["name"],
            "primary": next((f["id"] for f in fields.values() if f.get("isPrimary")), None),
            "key": fields[key_name]["id"] if key_name and key_name in fields else None,  # 标识字段(线索号、ISO3、信源编号、文件名)
            "props": {p.id: fields[p.name]["id"] for p in obj.props if p.name in fields},
            "fields": {n: f["id"] for n, f in fields.items()},
            "views": {n: v["id"] for n, v in (res.views.get(oid) or {}).items()},
            "choices": {p.id: list(p.enum) for p in obj.props if p.enum},  # 页面上的下拉筛选用
            "links": {p.id: _link_kind(fields[p.name]) for p in obj.props if p.name in fields and _link_kind(fields[p.name])},  # 页面写关联、人员字段时用
        }
    return out


def read_config(api: tstore.Client, base_name: str, onto: Ontology) -> dict:
    """只读地收集编号(不建不改):库 → 表 → 字段、视图;表按名字对应本体对象(teable_map 的 table)。机器账号的令牌也能跑。"""
    tmap = onto_mod.load_map()
    base = api.find_base(base_name)
    if not base:
        raise tstore.TStoreError(f"令牌看不到库「{base_name}」")
    by_name = {tp["table"]: oid for oid, tp in tmap["tables"].items()}
    res = Result(base=base)
    for t in api.tables(base["id"]):
        oid = by_name.get(t["name"])
        if not oid:
            continue
        res.tables[oid] = t
        res.fields[oid] = {f["name"]: f for f in api.fields(t["id"])}
        res.views[oid] = {v["name"]: v for v in api.views(t["id"])}
    missing = [tmap["tables"][oid]["table"] for oid in onto.objects if oid not in res.tables and oid in tmap["tables"]]
    if missing:
        raise tstore.TStoreError(f"库「{base_name}」里没有表:{'、'.join(missing)}(先跑 python -m core.schema 建表)")
    return config(res, onto)


def describe_plan(plans: list[TablePlan]) -> str:
    lines = []
    for tp in plans:
        lines.append(f"## {tp.name}({tp.obj})")
        lines.append(f"- 主字段:{tp.primary['name']}" + (f"(公式 {tp.primary_formula['formula']})" if tp.primary_formula else ""))
        lines.append("- 字段:" + "、".join(f"{f['name']}[{f['type']}]" for f in tp.fields))
        if tp.links:
            lines.append("- 关联:" + "、".join(f"{lp.prop.name} → {lp.target}" + (f"(反向「{lp.reverse}」)" if lp.reverse else "(单向)")
                                              for lp in tp.links))
        if tp.helpers:
            lines.append("- 辅助公式:" + "、".join(h["name"] for h in tp.helpers))
        if tp.derived:
            lines.append("- 派生:" + "、".join(p.name for p, _ in tp.derived))
        if tp.system:
            lines.append("- 系统字段:" + "、".join(s["name"] for s in tp.system))
        if tp.defaults:
            lines.append("- 缺省值:" + "、".join(f"{k}={v}" for k, v in tp.defaults.items()))
        lines.append("- 视图:全部、" + "、".join(v["name"] for v in tp.views))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.schema", description="按本体在新库里建表、补字段、补视图(只增不删)")
    ap.add_argument("--profile", help="读 lead_times(最晚启动的公式);不给则最晚启动为空")
    ap.add_argument("--base", help="新库名(缺省见 teable_map.yaml,要以「新版」结尾)")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="编号写到哪里(缺省 private/portal/config.json,没有私有目录时 portal/config.json)")
    ap.add_argument("--dry-run", action="store_true", help="只列出要建的表与字段,不连表格")
    ap.add_argument("--config-only", action="store_true", help="不建不改,只把现有的编号写进 --config(只读页面要用;机器账号令牌也行)")
    a = ap.parse_args(argv)
    onto, tmap = onto_mod.load(), onto_mod.load_map()
    if a.config_only:
        base_name = tstore.require_new_base(a.base or tmap["base"])
        url, token = tstore.credentials(admin=bool(os.environ.get("TEABLE_ADMIN_TOKEN")))
        cfg = read_config(tstore.Client(url, token), base_name, onto)
        Path(a.config).parent.mkdir(parents=True, exist_ok=True)
        Path(a.config).write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"库「{base_name}」:{len(cfg['tables'])} 张表的编号写进 {a.config}")
        return 0
    lead_times = None
    if a.profile:
        from core.profile import load_profile
        lead_times = load_profile(a.profile).lead_times
    plans = build_plan(onto, tmap, lead_times)
    base_name = tstore.require_new_base(a.base or tmap["base"])
    if a.dry_run:
        print(f"# 新库「{base_name}」的表结构(由本体与 teable_map.yaml 生成)\n")
        print(describe_plan(plans))
        return 0
    url, token = tstore.credentials(admin=True)
    res = apply(tstore.Client(url, token), plans, base_name, tmap["timezone"])
    Path(a.config).parent.mkdir(parents=True, exist_ok=True)
    Path(a.config).write_text(json.dumps(config(res, onto), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"新库「{base_name}」:新建 {len(res.created)} 项;编号写进 {a.config}")
    for w in res.warnings:
        print("  ! " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
