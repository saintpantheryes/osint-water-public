"""按本体建表(core/schema.py):字段与本体的类型映射一致;关联的反向字段改成映射里的名字;派生字段是计算字段、写不进去(R10);
主字段换成公式;视图带上筛选;重复运行不重复建;机器账号建不了表;只动新库。用内存版的 Teable,不联网。"""
from __future__ import annotations

import json

import pytest

from core import ontology, schema, tstore
from tests.teable_fake import FakeTeable

ONTO, TMAP = ontology.load(), ontology.load_map()
LEAD_TIMES = {"bid_deadline": {"准备": 56, "审批": 21, "保函": 7}, "eoi_deadline": {"准备": 14, "审批": 7}, "local_registration": 90}
NEW = "水务项目机会 新版"


def _api(fake: FakeTeable, token: str = "admin-token") -> tstore.Client:
    return tstore.Client("https://water.example", token, transport=fake.transport, sleep=lambda s: None)


def _built(fake: FakeTeable | None = None):
    fake = fake or FakeTeable()
    plans = schema.build_plan(ONTO, TMAP, LEAD_TIMES)
    res = schema.apply(_api(fake), plans, NEW, log=lambda *a: None)
    return fake, plans, res


def test_plan_covers_every_property_once_with_the_right_types():
    plans = {tp.obj: tp for tp in schema.build_plan(ONTO, TMAP, LEAD_TIMES)}
    assert set(plans) == set(ONTO.objects) - {"member"}  # 成员不建表
    for oid, tp in plans.items():
        names = [tp.primary["name"]] + [f["name"] for f in tp.fields] + [lp.prop.name for lp in tp.links] + [p.name for p, _ in tp.derived]
        names += [h["name"] for h in tp.helpers] + [s["name"] for s in tp.system]
        assert len(names) == len(set(names)), (oid, names)
        for p in ONTO.objects[oid].props:
            assert p.name in names, p.ref
        assert all(p.by == "derived" for p, _ in tp.derived)
        assert not {f["name"] for f in tp.fields} & {p.name for p in ONTO.objects[oid].props if p.by == "derived"}
    types = {f["name"]: f for f in plans["milestone"].fields}
    assert types["日期"]["type"] == "date" and types["日期原文"]["type"] == "singleLineText"  # pdate 两个字段
    lead = {f["name"]: f for f in plans["lead"].fields}
    assert lead["原文网址"]["options"] == {"showAs": {"type": "url"}} and lead["是否相关"]["type"] == "checkbox"
    assert lead["处理状态"]["options"]["preventAutoNewOptions"] is True and lead["处理状态"]["description"].startswith("人写")
    assert lead["中文摘要"]["description"].startswith("机器写") and lead["线索号"]["description"].startswith("机器写:标识")
    opp = {f["name"]: f for f in plans["opportunity"].fields}
    assert opp["负责人"]["options"]["shouldNotify"] is True and opp["资金"]["type"] == "longText"
    helper = next(h for h in plans["milestone"].helpers if h["name"] == "最晚启动(辅助)")["formula"]
    assert 'DATE_ADD({日期}, -84, "day")' in helper and 'DATE_ADD({日期}, -21, "day")' in helper  # 投标截止 56+21+7、意向书截止 14+7
    assert helper.startswith('IF(OR({状态} = "已发生", IS_BEFORE({日期}, TODAY())), BLANK()')  # 未来 = 状态不是已发生且日期不早于今天(审查三-A)
    assert 'NOT(IS_BEFORE({日期}, TODAY()))' in next(h for h in plans["milestone"].helpers if h["name"] == "未过截止")["formula"]


def test_builds_the_eight_tables_with_links_derived_fields_and_views():
    fake, _, res = _built()
    base = next(b for b in fake.bases.values() if b["name"] == NEW)
    assert {t["name"] for t in fake.tables.values() if t["baseId"] == base["id"]} == {"线索", "国别", "城市", "商机", "跟进记录", "信源", "时间节点", "报告模板", "报告", "公司资料", "个人设置", "自来水厂", "污水厂", "行业术语", "已中标项目", "供应商", "研究资料", "水务事件", "水务片区"}
    assert not [t for t in fake.tables.values() if t["baseId"] == "bseOLD"]  # 旧库没动
    f = {oid: res.fields[oid] for oid in res.fields}
    assert f["lead"]["所属商机"]["options"]["relationship"] == "manyOne" and f["lead"]["建议所属商机"]["options"].get("isOneWay")
    assert {"所属线索", "时间节点", "跟进记录", "范围报告", "写进的报告", "所属线索数", "当前判断", "阶段修改人"} <= set(f["opportunity"])
    assert {"写进的报告", "作为出处的商机", "时间节点", "最近截止"} <= set(f["lead"])
    assert f["opportunity"]["所属线索数"]["type"] == "rollup" and f["opportunity"]["当前判断"].get("isLookup")
    lo = f["opportunity"]["当前判断"]["lookupOptions"]  # 条件查找:不经关联字段,filter 里"跟进记录.商机 = 本行",按日期倒序取 1 条
    assert f["opportunity"]["当前判断"].get("isConditionalLookup") and "linkFieldId" not in lo
    assert lo["filter"]["filterSet"][0] == {"fieldId": f["followup"]["商机"]["id"], "operator": "is",
                                            "value": {"type": "field", "fieldId": f["opportunity"]["名称"]["id"]}}
    assert lo["sort"] == {"fieldId": f["followup"]["日期"]["id"], "order": "desc"} and lo["limit"] == 1
    nxt = f["opportunity"]["下一时间节点"]  # 先换主字段公式再建查找(审查二-9):类型跟着是公式,不照搬表达式
    assert nxt["type"] == "formula" and "expression" not in (nxt.get("options") or {}) and f["milestone"]["节点"]["type"] == "formula"
    assert f["opportunity"]["阶段修改人"]["options"]["trackedFieldIds"] == [f["opportunity"]["我方阶段"]["id"]]
    nm = f["opportunity"]["下一时间节点"]["lookupOptions"]["filter"]["filterSet"]  # 本行 ∧ 状态不是已发生 ∧ 日期 ≥ 今天
    assert nm[2] == {"fieldId": f["milestone"]["日期"]["id"], "operator": "isOnOrAfter", "value": {"mode": "today", "timeZone": TMAP["timezone"]}}
    for oid, name in (("milestone", "节点"), ("followup", "摘要"), ("report", "报告号")):
        assert f[oid][name]["type"] == "formula" and f[oid][name]["isPrimary"], (oid, name)
    assert f["lead"]["处理状态"]["options"]["defaultValue"] == "待处理"
    views = {v["name"]: v for v in fake.views[res.tables["lead"]["id"]]}
    assert views["待处理"]["filter"]["filterSet"][1] == {"fieldId": f["lead"]["处理状态"]["id"], "operator": "is", "value": "待处理"}
    kan = next(v for v in fake.views[res.tables["opportunity"]["id"]] if v["name"] == "跟踪看板")
    assert kan["type"] == "kanban" and kan["options"]["stackFieldId"] == f["opportunity"]["我方阶段"]["id"]
    assert res.warnings == []
    cfg = schema.config(res, ONTO)
    assert cfg["tables"]["lead"]["props"]["opportunity"] == f["lead"]["所属商机"]["id"]
    assert cfg["tables"]["opportunity"]["views"]["跟踪看板"] == kan["id"] and json.dumps(cfg, ensure_ascii=False)


def test_second_run_creates_nothing():
    fake, plans, res = _built()
    n = len(fake.calls)
    again = schema.apply(_api(fake), plans, NEW, log=lambda *a: None)
    assert again.created == [] and again.warnings == []
    assert not [c for c in fake.calls[n:] if c[0] in ("POST", "PUT", "PATCH")]
    assert schema.config(again, ONTO) == schema.config(res, ONTO)


def test_derived_fields_cannot_be_written_and_the_bot_cannot_build():
    fake, _, res = _built()
    bot = _api(fake, "bot-token")
    opp = res.fields["opportunity"]
    rec = bot.create_records(res.tables["opportunity"]["id"], [{opp["名称"]["id"]: "苏姆盖特", opp["我方阶段"]["id"]: "建议"}])[0]
    with pytest.raises(tstore.TStoreError, match="计算出来的"):
        bot.update_records(res.tables["opportunity"]["id"], [(rec["id"], {opp["所属线索数"]["id"]: 3})])
    with pytest.raises(tstore.TStoreError) as e:
        schema.apply(bot, schema.build_plan(ONTO, TMAP), "另一个 新版", log=lambda *a: None)
    assert e.value.status == 403
    with pytest.raises(tstore.TStoreError, match="只动新库"):
        schema.apply(_api(fake), schema.build_plan(ONTO, TMAP), "水务项目机会", log=lambda *a: None)


def test_unsupported_capabilities_become_warnings(monkeypatch):
    """条件查找、最后修改人只跟踪一个字段等(阶段 A 核验的)建不成时:记警告、跳过,其余照建。"""
    fake = FakeTeable()
    real = fake.create_field

    def picky(table_id, spec):
        if spec.get("isLookup") and spec.get("lookupOptions", {}).get("limit"):
            from tests.teable_fake import HTTPError
            raise HTTPError(400, "lookupOptions.limit 不认识")
        return real(table_id, spec)
    monkeypatch.setattr(fake, "create_field", picky)
    _, _, res = _built(fake)
    assert {w.split("「")[1].split("」")[0] for w in res.warnings} == {"下一时间节点", "当前判断", "下一步", "下一步到期"}
    assert "所属线索数" in res.fields["opportunity"] and "当前判断" not in res.fields["opportunity"]


def test_an_old_instance_that_drops_sort_and_limit_gets_no_wrong_lookup(monkeypatch):
    """旧版本的接口静默丢掉 isConditionalLookup、sort、limit(字段建成了,但返回的是全部值):schema 查回显,删掉字段、记警告、走后备。"""
    fake = FakeTeable()
    real = fake.create_field

    def old_server(table_id, spec):
        f = real(table_id, spec)
        if f.get("isConditionalLookup"):  # 像不认识这些键的旧版本:建成了经关联的普通查找
            f.pop("isConditionalLookup")
            f["lookupOptions"] = {k: v for k, v in f["lookupOptions"].items() if k not in ("sort", "limit")}
        return f
    monkeypatch.setattr(fake, "create_field", old_server)
    _, _, res = _built(fake)
    gone = {"下一时间节点", "当前判断", "下一步", "下一步到期"}
    assert {w.split("「")[1].split("」")[0] for w in res.warnings} == gone and all("没收下" in w for w in res.warnings)
    assert not gone & set(res.fields["opportunity"]) and not [x for x in res.created if x.split(".")[-1] in gone]
    assert "所属线索数" in res.fields["opportunity"]


def test_rerun_adds_new_enum_choices_to_existing_select_fields():
    """本体给已有的单选 / 多选加了选项(如报告类型加「日报」):重跑建表命令只补缺的选项,已有选项与记录不动;表格锁了自动加选项,写记录时加不上。"""
    from core import ontology as onto_mod
    fake = FakeTeable()
    admin = tstore.Client("https://water.example", "admin-token", transport=fake.transport, sleep=lambda s: None)
    onto, tmap = onto_mod.load(), onto_mod.load_map()
    schema.apply(admin, schema.build_plan(onto, tmap, None), NEW, log=lambda *a: None)
    b = tstore.Base(admin)
    f = b.field("report", "kind")
    choices = [c["name"] for c in f["options"]["choices"]]
    assert "日报" in choices
    # 退回只有四个选项的旧表,再按本体重跑:补上「日报」,顺序接在后面
    admin.convert_field(b.t["report"], f["id"], {"type": "singleSelect", "options": {**f["options"], "choices": [c for c in f["options"]["choices"] if c["name"] != "日报"]}})
    bot = tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None)
    with pytest.raises(tstore.TStoreError):
        tstore.Base(bot).create("report", [{"kind": "日报", "day": "2026-10-07", "gen_status": "待生成"}])  # 锁了自动加选项
    res = schema.apply(admin, schema.build_plan(onto, tmap, None), NEW, log=lambda *a: None)
    assert any("报告.类型 选项 日报" in x for x in res.created)
    assert [c["name"] for c in tstore.Base(admin).field("report", "kind")["options"]["choices"]] == choices
    tstore.Base(bot).create("report", [{"kind": "日报", "day": "2026-10-07", "gen_status": "待生成"}])
