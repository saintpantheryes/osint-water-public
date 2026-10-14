"""本体(docs/ontology/ontology.yaml)自洽:引用都存在,机器不写人写的属性,派生的不被写,机器新建商机只能是「建议」;
v2.0 起还查:枚举属性与分类体系(taxonomies)一致、口径(glossary)引用的阶段存在、代码里的枚举副本(code_enums)与本体一致。"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ONTO = yaml.safe_load((ROOT / "docs" / "ontology" / "ontology.yaml").read_text(encoding="utf-8"))
OBJECTS = {o["id"]: o for o in ONTO["objects"]}
PROPS = {(o["id"], p["id"]): p for o in ONTO["objects"] for p in o["properties"]}
RULES = {r["id"] for r in ONTO["rules"]}
ACTIONS = {a["id"]: a for a in ONTO["actions"]}


def _ref(dotted: str) -> tuple[str, str]:
    obj, prop = dotted.split(".", 1)
    return obj, prop


def test_ids_unique_and_types_resolve():
    assert len(OBJECTS) == len(ONTO["objects"])
    assert {o["id"] for o in ONTO["objects"] if o["core"]} == {"lead", "country", "opportunity", "followup"}
    for o in ONTO["objects"]:
        ids = [p["id"] for p in o["properties"]]
        assert len(ids) == len(set(ids)), o["id"]
        for p in o["properties"]:
            assert p["by"] in ("ai", "human", "derived"), (o["id"], p["id"])
            if p["type"].startswith(("link:", "links:")):
                assert p["type"].split(":", 1)[1] in OBJECTS, (o["id"], p["id"])
            if p["type"] in ("enum", "enums"):
                assert p.get("enum"), (o["id"], p["id"])


def test_links_and_states_point_at_existing_things():
    for lk in ONTO["links"]:
        assert lk["from"] in OBJECTS and lk["to"] in OBJECTS, lk
        assert _ref(lk["via"]) in PROPS, lk
    for o in ONTO["objects"]:
        st = o.get("states")
        if not st:
            continue
        prop = PROPS[(o["id"], st["property"])]
        for t in st["transitions"]:
            assert t["from"] in prop["enum"] and t["to"] in prop["enum"], (o["id"], t)


def test_actions_reference_existing_properties_and_rules():
    for a in ONTO["actions"]:
        assert a["actor"] in ("ai", "human"), a["id"]
        if a.get("creates"):
            assert a["creates"] in OBJECTS, a["id"]
        for ref in list(a.get("writes") or []) + list((a.get("sets_on_create") or {}).keys()):
            assert _ref(ref) in PROPS, (a["id"], ref)
        for r in a.get("rules") or []:
            assert r in RULES, (a["id"], r)
        for obj in a.get("reads") or []:
            assert obj in OBJECTS, (a["id"], obj)
    for e in ONTO["events"]:
        for aid in e["then"]:
            assert aid in ACTIONS, e
    for v in ONTO["views"]:
        assert set(v["of"]) <= set(OBJECTS), v


def test_rule_r2_machine_never_writes_human_properties():
    for a in ONTO["actions"]:
        if a["actor"] != "ai":
            continue
        for ref in a.get("writes") or []:
            assert PROPS[_ref(ref)]["by"] != "human", f"{a['id']} {a['name']} 写了人写的属性 {ref}"


def test_rule_r10_derived_properties_are_never_written():
    for a in ONTO["actions"]:
        for ref in list(a.get("writes") or []) + list((a.get("sets_on_create") or {}).keys()):
            assert PROPS[_ref(ref)]["by"] != "derived", f"{a['id']} 写了派生属性 {ref}"


def test_rule_r5_machine_creates_opportunities_only_as_suggestions():
    opp = OBJECTS["opportunity"]
    assert opp["states"]["initial_by_ai"] == "建议"
    made_by_ai = [a for a in ONTO["actions"] if a["actor"] == "ai" and a.get("creates") == "opportunity"]
    assert made_by_ai, "应有机器建议商机的行为"
    for a in made_by_ai:
        assert a["sets_on_create"]["opportunity.stage"] == "建议", a["id"]
    assert all(t["by"] == "human" for t in opp["states"]["transitions"])


@pytest.mark.parametrize("obj", ["lead", "opportunity", "followup", "country", "milestone", "report", "template", "source", "member", "city", "company", "preference", "supply_plant", "sewage_plant", "term", "award", "contractor"])
def test_every_object_has_its_current_storage_listed(obj):
    assert ONTO["as_is"][obj], obj


KEYS = {"property": {"id", "name", "type", "by", "required", "note", "enum", "default", "taxonomy", "format", "example", "where"},
        "transition": {"from", "to", "by", "note"},
        "link": {"from", "name", "to", "cardinality", "via", "set_by", "kind"},
        "action": {"id", "name", "actor", "channel", "trigger", "reads", "creates", "sets_on_create", "writes", "note", "rules"},
        "rule": {"id", "name", "text", "note", "enforce", "why"},
        "event": {"id", "when", "then", "note"},
        "view": {"id", "name", "of", "channel", "query"},
        "value": {"v", "code", "parent", "def", "boundary", "example"}}


def test_no_unknown_keys():
    """行内写法 {…} 里的值带英文逗号会被截断,后半截变成一个多出来的键:这里查出来(值里有逗号的要加引号)。"""
    found = [(kind, x) for kind, xs in (("property", [p for o in ONTO["objects"] for p in o["properties"]]),
                                        ("transition", [t for o in ONTO["objects"] for t in (o.get("states") or {}).get("transitions", [])]),
                                        ("link", ONTO["links"]), ("action", ONTO["actions"]), ("rule", ONTO["rules"]),
                                        ("event", ONTO["events"]), ("view", ONTO["views"]),
                                        ("value", [v for t in ONTO["taxonomies"].values() for g in t["groups"] for v in g["values"]]))
             for x in xs if set(x) - KEYS[kind]]
    assert not found, [(k, x.get("id") or x.get("name"), sorted(set(x) - KEYS[k])) for k, x in found]


def test_ontology_md_is_generated_from_yaml():
    """本体.md 的表由 docs/ontology/gen_md.py 按 ontology.yaml 生成;改了本体没重跑生成会在这里发现。"""
    spec = importlib.util.spec_from_file_location("gen_md", ROOT / "docs" / "ontology" / "gen_md.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    md = (ROOT / "docs" / "ontology" / "本体.md").read_text(encoding="utf-8")
    assert gen.render(md, ONTO) == md, "本体.md 与 ontology.yaml 不同步:运行 python docs/ontology/gen_md.py"


# ---------------- v2.0:分类体系、口径、代码副本 ----------------
TAX = ONTO["taxonomies"]


def _tax_values(tid: str) -> list[str]:
    return [v["v"] for g in TAX[tid]["groups"] for v in g["values"]]


def test_every_enum_property_has_a_taxonomy_with_the_same_values_in_the_same_order():
    for (oid, pid), p in PROPS.items():
        if p["type"] not in ("enum", "enums"):
            continue
        tid = p.get("taxonomy")
        assert tid in TAX, f"{oid}.{pid} 没有 taxonomy"
        assert f"{oid}.{pid}" in TAX[tid]["used_by"], f"{oid}.{pid} 不在 taxonomies.{tid}.used_by 里"
        assert _tax_values(tid) == list(p["enum"]), f"{oid}.{pid} 的 enum 与 taxonomies.{tid} 不一致"
    for tid, t in TAX.items():
        for ref in t["used_by"]:
            assert _ref(ref) in PROPS and PROPS[_ref(ref)].get("taxonomy") == tid, (tid, ref)
        vals = _tax_values(tid)
        assert len(vals) == len(set(vals)), tid
        for g in t["groups"]:
            for v in g["values"]:
                assert v.get("def"), (tid, v["v"], "每个值都要有定义")
                if "parent" in v:
                    assert v["parent"] in _tax_values("lead_sectors"), (tid, v)


def test_opp_sectors_all_roll_up_to_a_lead_sector():
    for g in TAX["opp_sectors"]["groups"]:
        for v in g["values"]:
            assert v.get("parent"), v


def test_industry_terms_are_well_formed():
    """行业术语(terms):领域取自线索领域;不重名;拉丁相关词不短于 4 个字母(太短的缩写会误中别的词,GIS 会中 logistics)。
    相关词空着的是只作法规概念的通用概念(v2.24,助手不拿它查线索),要有说明。"""
    sectors = set(_tax_values("lead_sectors"))
    names = [w for t in ONTO["terms"] for w in [t["term"], *(t.get("aliases") or [])]]
    assert len(names) == len(set(names)) and ONTO["terms"]
    for t in ONTO["terms"]:
        assert set(t["sectors"]) <= sectors and set(t["covers"]) <= sectors and set(t["sectors"]) <= set(t["covers"]), t["term"]
        assert t["means"] and not [w for w in t["match"] if w.isascii() and len(w) < 4], t["term"]
    assert [t for t in ONTO["terms"] if t["match"]], "应有查线索用的术语"


def test_semantic_layer_is_well_formed():
    """语义层(semantic,v2.21):指标所属对象存在,要用的属性在每个所属对象上都有,算法取自 aggs、第一个是缺省;
    一个说法只归一个指标(否则「评标」到底算哪个说不清);主体工程的说法只对 award_asset 里有的值,词不重名。"""
    sem = ONTO["semantic"]
    objs = {o["id"] for o in ONTO["objects"]}
    aggs = {a["id"] for a in sem["aggs"]}
    ids = [m["id"] for m in sem["measures"]]
    assert len(ids) == len(set(ids)) and "count" in ids
    seen: dict[str, str] = {}
    for m in sem["measures"]:
        assert set(m) <= {"id", "name", "of", "field", "unit", "aggs", "needs", "missing", "words", "better"}, m["id"]
        assert m.get("better") in (None, "high", "low"), m["id"]
        assert m["name"] and set(m["of"]) <= objs and m["aggs"] and set(m["aggs"]) <= aggs, m["id"]
        if m["id"] == "count":
            continue
        assert m["unit"] and m["missing"] and m["words"], m["id"]
        for oid in m["of"]:
            for pid in m["needs"] + [f for f in ([m["field"]] if isinstance(m["field"], str) else m["field"])]:
                assert (oid, pid) in PROPS and PROPS[(oid, pid)]["type"] == "number", (m["id"], oid, pid)
        for w in m["words"]:
            assert seen.setdefault(w, m["id"]) == m["id"], (w, seen[w], m["id"])
    agg_words = [w for a in sem["aggs"] for w in a["words"]]
    assert len(agg_words) == len(set(agg_words))  # 「总额」既说指标(金额)又说算法(合计):两边分开认,可以重
    words = sem["words"]["award_asset"]
    assert set(words) <= set(_tax_values("award_asset")) and all(words.values())
    flat = [w for ws in words.values() for w in ws]
    assert len(flat) == len(set(flat))


def test_glossary_stage_sets_exist_and_tracked_is_the_in_tracking_group():
    stages = _tax_values("stage")
    for key, g in ONTO["glossary"].items():
        for st in g.get("stages") or []:
            assert st in stages, (key, st)
    in_tracking = next(grp for grp in TAX["stage"]["groups"] if grp["name"] == "在跟踪")
    assert ONTO["glossary"]["tracked"]["stages"] == [v["v"] for v in in_tracking["values"]]


def test_portal_ontology_json_is_generated_from_yaml():
    """页面读的 portal/ontology.json 由 docs/ontology/gen_json.py 按 ontology.yaml 生成;改了本体没重跑会在这里发现。"""
    spec = importlib.util.spec_from_file_location("gen_json", ROOT / "docs" / "ontology" / "gen_json.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    js = (ROOT / "portal" / "ontology.json").read_text(encoding="utf-8")
    assert gen.render(ONTO) == js, "portal/ontology.json 与 ontology.yaml 不同步:运行 python docs/ontology/gen_json.py"
    import json
    data = json.loads(js)
    assert data["version"] == ONTO["version"] and [o["id"] for o in data["objects"]] == list(OBJECTS)


def test_runtime_readers_in_core_ontology():
    """core/ontology.py 的运行时读法:值按本体顺序、英文键唯一且与值一一对应、组按组名开头取。"""
    from core import ontology as onto_mod
    for tid in TAX:
        assert onto_mod.values(tid) == tuple(_tax_values(tid)), tid
        codes = onto_mod.codes(tid)
        assert len(codes) == len(_tax_values(tid)) and set(codes.values()) == set(_tax_values(tid)), f"{tid} 的 code 要唯一且每个值都有"
        assert onto_mod.code_of(tid) == {v: c for c, v in codes.items()}
    assert onto_mod.group_values("lead_category", "采购类") == ("招标", "意向书", "资格预审", "总采购公告", "特定采购公告", "授标或短名单")
    assert onto_mod.glossary_stages("tracked") == frozenset(ONTO["glossary"]["tracked"]["stages"])
    with pytest.raises(KeyError):
        onto_mod.group_values("stage", "不存在的组")


def test_code_enums_match_the_ontology():
    """代码里不再抄枚举(v2.2):常量都在运行时从本体算出;这里核对算出来的与本体一致,以及剩下的映射(code_enums)指向存在的值。"""
    from core.jobs import common, suggest
    from core.jobs import reports
    from core.watch.outputs import SECTOR_ZH
    codes = {tid: {v["code"]: v["v"] for g in TAX[tid]["groups"] for v in g["values"]} for tid in TAX}
    assert common.CAT == {**codes["lead_category"], "page": "其他"}
    assert common.OPP_SECTOR == {c: v for c, v in codes["opp_sectors"].items() if c in codes["lead_sectors"]} and len(common.OPP_SECTOR) == 5
    assert common.DEADLINE_KIND == {c: codes["milestone_kind"][c] for c in ("eoi_deadline", "pq_deadline", "bid_deadline", "rfp_deadline")}
    assert set(common.PUBLIC_STAGE_OF.values()) <= set(_tax_values("public_stage")) and set(common.PUBLIC_STAGE_OF) <= set(codes["lead_category"])
    assert set(common.SOURCE_KIND.values()) <= set(_tax_values("source_kind")) and common.SOURCE_STATUS == codes["source_status"]
    assert SECTOR_ZH == codes["lead_sectors"]
    assert suggest.NEW_OPP_CATEGORIES == {c for c, v in codes["lead_category"].items() if v in ONTO["glossary"]["threshold"]["categories"]} and len(suggest.NEW_OPP_CATEGORIES) == 7
    assert suggest.CORE_SECTORS == {c for c, v in codes["lead_sectors"].items() if v in ONTO["glossary"]["core_sectors"]["sectors"]} and len(suggest.CORE_SECTORS) == 5
    assert set(ONTO["glossary"]["threshold"]["categories"]) <= set(_tax_values("lead_category")) and set(ONTO["glossary"]["core_sectors"]["sectors"]) <= set(_tax_values("lead_sectors"))
    assert set(ONTO["glossary"]["threshold"]["tiers"]) <= set(_tax_values("tier"))
    stages = _tax_values("stage")
    tracked = set(ONTO["glossary"]["tracked"]["stages"])
    assert tuple(reports.STAGE_ORDER) == tuple(stages) and reports.TRACKED_STAGES == tracked
    from core.jobs import brief
    assert brief.TRACKED_STAGES == tracked
    assert tuple(brief.PROCUREMENT) == tuple(v["v"] for g in TAX["lead_category"]["groups"] if g["name"].startswith("采购类") for v in g["values"])
    assert tuple(brief.CATEGORY_PRIORITY) == tuple(_tax_values("lead_category"))  # 采购类是第一组,所以优先级顺序就是本体顺序
    # 页面:不再有阶段、资金来源归组的副本,运行时从 ontology.json 取
    js = (ROOT / "portal" / "portal.js").read_text(encoding="utf-8")
    assert 'fetch("ontology.json"' in js and "get STAGES()" in js and 'glossary("tracked")' in js and 'grouping("funders")' in js and 'grouping("regions")' in js
    assert not re.search(r'STAGES:\s*\[', js) and not re.search(r'FUNDERS:\s*\{', js) and '"跟踪中"' not in js and '"世界银行"' not in js
    html = (ROOT / "portal" / "country.html").read_text(encoding="utf-8")
    assert "P.STAGES" in html and not re.search(r'ORDER\s*=\s*\[', html)


def test_ontology_picture_is_current(tmp_path):
    """本体图 本体模型.svg 由 docs/ontology/draw_svg.py 画(版本、日期从 ontology.yaml 读):每个对象都画在图上,
    提交的图与脚本当前画出的一致。加了对象却没改画图脚本、或改了脚本没重画,会在这里发现。"""
    import subprocess
    import sys
    out = tmp_path / "onto.svg"
    subprocess.run([sys.executable, str(ROOT / "docs" / "ontology" / "draw_svg.py"), str(out)], check=True, capture_output=True)
    svg = out.read_text(encoding="utf-8")
    names = [o["name"] for o in yaml.safe_load((ROOT / "docs" / "ontology" / "ontology.yaml").read_text(encoding="utf-8"))["objects"]]
    missing = [n for n in names if f">{n}<" not in svg]
    assert not missing, f"本体图上没有这些对象:{missing}(在 draw_svg.py 里画上)"
    assert svg == (ROOT / "docs" / "ontology" / "本体模型.svg").read_text(encoding="utf-8"), \
        "本体模型.svg 不是最新:python docs/ontology/draw_svg.py docs/ontology/本体模型.svg,再导出 PNG"
