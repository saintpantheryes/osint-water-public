"""助手(门户「助手」页):一句提问 → 查询计划。规则解析、模型输出校验、进程内接口、后台任务里的线程。"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import httpx

from core import assistant as A
from core.assistant_server import Assistant, serve
from core.watch.llm import Classifier, DeepSeekClient, LLMConfig

TODAY = date(2026, 10, 8)  # 周四


def _on(p: dict) -> dict:
    return {k: v for k, v in p["filters"].items() if v not in (None, [], False)}


def test_vocab_comes_from_the_ontology():
    v = A.vocab()
    assert "供水" in v.sectors and "招标" in v.categories and "世界银行" in v.funders and v.tracked == ["跟踪中", "准备投标", "已投标", "中标"]
    assert v.sectors_fine["供水"] == ["供水", "输水干管", "水库调蓄", "减漏计量"] and "投标截止" in v.deadline_kinds and "日报" in v.report_kinds
    assert "世行" in v.words["funders"]["世界银行"] and "world bank" in v.words["funders"]["世界银行"] and "海淡" in v.words["sectors"]["海水淡化"]


def test_rules_lead_questions():
    p = A.rules_parse("最近 7 天埃及有哪些新线索", TODAY)
    assert p["object"] == "lead" and p["intent"] == "list" and _on(p) == {"countries": [{"iso3": "EGY", "name": "埃及"}], "days": 7}
    assert ["国别", "埃及"] in p["understood"] and ["时间", "最近 7 天"] in p["understood"] and p["understood"][0] == ["对象", "线索"]
    p = A.rules_parse("世界银行出资的供水项目有多少条", TODAY)
    assert p["intent"] == "count" and _on(p) == {"sectors": ["供水"], "funders": ["世界银行"]}
    p = A.rules_parse("最近 30 天的招标按国家分", TODAY)
    assert p["intent"] == "chart" and p["group_by"] == "country" and _on(p) == {"categories": ["招标"], "days": 30}
    p = A.rules_parse("埃及和摩洛哥上个月的融资线索", TODAY)
    assert [c["iso3"] for c in p["filters"]["countries"]] == ["EGY", "MAR"] and p["filters"]["from"] == "2026-09-01" and p["filters"]["to"] == "2026-09-30"
    p = A.rules_parse("9月 亚行 污水 招标 多少", TODAY)
    assert p["intent"] == "count" and p["filters"]["funders"] == ["亚洲开发银行"] and p["filters"]["from"] == "2026-09-01"
    p = A.rules_parse("待处理的线索有多少", TODAY)
    assert p["intent"] == "count" and p["filters"]["status"] == ["待处理"]
    p = A.rules_parse("“Cairo” 相关的线索", TODAY)
    assert p["filters"]["keywords"] == ["Cairo"] and p["filters"]["relevant"] is True
    p = A.rules_parse("海水淡化的线索按资金来源统计", TODAY)
    assert p["group_by"] == "funder" and p["filters"]["sectors"] == ["海水淡化"]
    p = A.rules_parse("本周新增线索每天多少", TODAY)
    assert p["group_by"] == "day" and p["filters"]["from"] == "2026-10-05" and p["filters"]["to"] == "2026-10-08"
    p = A.rules_parse("前 20 条供水线索", TODAY)
    assert p["intent"] == "list" and p["limit"] == 20


def test_rules_other_objects_and_help():
    p = A.rules_parse("在跟踪的商机有哪些", TODAY)
    assert p["object"] == "opportunity" and p["filters"]["tracked"] is True and p["filters"]["stages"] == []
    p = A.rules_parse("AI推荐的商机", TODAY)
    assert p["object"] == "opportunity" and p["filters"]["stages"] == ["建议"]
    p = A.rules_parse("沙特有哪些供水商机", TODAY)
    assert p["object"] == "opportunity" and p["filters"]["sectors_fine"] == ["供水", "输水干管", "水库调蓄", "减漏计量"]
    p = A.rules_parse("未来 30 天有哪些投标截止", TODAY)
    assert p["object"] == "milestone" and p["filters"]["future_days"] == 30 and p["filters"]["kinds"] == ["投标截止"] and p["filters"]["categories"] == []
    p = A.rules_parse("有哪些截止日", TODAY)  # 没说类型:四种采购截止;没说时间:未来 30 天
    assert p["object"] == "milestone" and set(p["filters"]["kinds"]) == set(A.vocab().deadline_kinds) and p["filters"]["future_days"] == 30
    p = A.rules_parse("最近的周报", TODAY)
    assert p["object"] == "report" and p["filters"]["kinds"] == ["周报"] and p["limit"] == 20
    p = A.rules_parse("生成一份摩洛哥的国别报告", TODAY)
    assert p["intent"] == "report" and p["object"] == "report" and p["filters"]["countries"][0]["iso3"] == "MAR"
    assert A.rules_parse("你好", TODAY)["intent"] == "help"
    assert A.plan("", TODAY)["intent"] == "help"


def test_time_phrases():
    t = lambda s: A.parse_time(s, TODAY)  # noqa: E731
    assert t("最近两周") == ({"days": 14}, "最近 14 天") and t("近三个月") == ({"days": 90}, "最近 90 天") and t("过去半年") == ({"days": 182}, "最近 182 天")
    assert t("未来两周")[0] == {"future_days": 14} and t("30 天内")[0] == {"future_days": 30}
    assert t("上周")[0] == {"from": "2026-09-28", "to": "2026-10-04"} and t("本月")[0] == {"from": "2026-10-01", "to": "2026-10-08"}
    assert t("今年")[0]["from"] == "2026-01-01" and t("12月")[0] == {"from": "2025-12-01", "to": "2025-12-31"}  # 还没到的月份按去年
    assert t("昨天")[0] == {"from": "2026-10-07", "to": "2026-10-07"} and t("没有时间词") == ({}, None)


def test_from_model_validates_every_value():
    data = {"object": "lead", "intent": "chart", "countries": ["Egypt", "火星"], "sectors": ["供水", "核电"], "categories": ["tender"],
            "funders": ["世行", "某基金"], "stages": ["跟踪中"], "tracked": True, "status": ["待处理"], "relevant": True, "days": 30, "from": None,
            "to": None, "future_days": 10, "kinds": ["投标截止"], "keywords": ["Cairo"], "group_by": "country", "limit": 500, "unresolved": [], "_model": "deepseek-flash"}
    p = A.from_model(data, TODAY)
    assert p["parsed_by"] == "model" and p["model"] == "deepseek-flash" and p["intent"] == "chart" and p["group_by"] == "country"
    f = p["filters"]
    assert f["countries"] == [{"iso3": "EGY", "name": "埃及"}] and f["sectors"] == ["供水"] and f["categories"] == ["招标"] and f["funders"] == ["世界银行"]
    assert f["stages"] == [] and f["tracked"] is None and f["kinds"] == [] and f["future_days"] is None  # 线索没有阶段、节点类型、未来窗口
    assert f["status"] == ["待处理"] and f["relevant"] is True and f["days"] == 30 and f["keywords"] == ["Cairo"] and p["limit"] == 200
    assert p["unresolved"] == ["火星", "核电", "某基金"]
    p = A.from_model({"object": "milestone", "intent": "list", "group_by": "stage", "from": "2026-11-01", "to": "2026-11-30"}, TODAY)
    assert p["object"] == "milestone" and p["group_by"] is None and p["filters"]["from"] == "2026-11-01" and p["filters"]["future_days"] is None
    p = A.from_model({"object": "nonsense", "intent": "fly"}, TODAY)
    assert p["object"] == "lead" and p["intent"] == "list" and p["filters"]["days"] is None


def test_industry_terms_from_the_ontology_override_a_single_sector():
    """「智慧水务」跨供水、排水、水利:模型把它判成领域「供水」+ 标题含「智慧水务」时(10-09 用户反映 0 条),按本体 terms 纠正为不限领域、按相关词找。"""
    q = "最近一个月国内智慧水务招投标项目"
    data = {"object": "lead", "intent": "list", "countries": ["中国"], "sectors": ["供水"], "categories": ["招标"], "keywords": ["智慧水务"], "days": 30}
    p = A.from_model(data, TODAY, q)
    f = p["filters"]
    assert f["sectors"] == [] and f["keywords"] == [] and [t["term"] for t in f["terms"]] == ["智慧水务"]
    assert {"SCADA", "smart", "智慧水利", "non-revenue"} <= set(f["terms"][0]["match"]) and f["categories"] == ["招标"] and f["days"] == 30
    chip = dict(p["understood"])
    assert "领域" not in chip and "标题或摘要含" not in chip and chip["术语"].startswith("智慧水务(跨供水、污水、雨水排涝、水利,不限领域")
    # 提问里另外明说的领域照留;术语自带领域的(漏损控制 → 供水)按术语筛
    assert A.from_model(dict(data, keywords=[]), TODAY, "智慧水务的污水项目")["filters"]["sectors"] == ["污水"]
    p = A.rules_parse(q, TODAY)
    assert p["filters"]["sectors"] == [] and p["filters"]["terms"][0]["term"] == "智慧水务" and p["filters"]["categories"] == ["招标"] and p["intent"] == "list"
    assert A.rules_parse("埃及的漏损控制项目", TODAY)["filters"]["sectors"] == ["供水"]
    assert A.rules_parse("Smart Water 的线索", TODAY)["filters"]["terms"][0]["term"] == "智慧水务"
    assert A.rules_parse("沙特的供水项目", TODAY)["filters"]["terms"] == []
    assert "智慧水务(跨供水、污水、雨水排涝、水利,不限领域)" in A.system_prompt(TODAY)


def test_terms_come_from_the_teable_table_when_the_worker_has_read_it():
    """人在 Teable「行业术语」表里加的词:后台每分钟重读(Assistant.refresh_terms),下一个提问就用上;勾「停用」的不用;读表出错沿用上一次的。"""
    class FakeBase:
        t = {"term": "tblT"}
        def __init__(self, rows): self.r = rows
        def has(self, oid, p): return True
        def rows(self, oid): return self.r
        def get(self, oid, r, p): return r.get(p)
    rows = [{"term": "管网改造", "aliases": "管网更新\n旧网改造", "covers": ["供水", "污水"], "sectors": [], "match": "管网、pipe network、GIS", "means": ""},
            {"term": "智慧水务", "match": "smart", "disabled": True}]
    bot = Assistant(None, use_llm=False)
    bot.terms_base = lambda: FakeBase(rows)
    try:
        bot.refresh_terms()
        f = A.rules_parse("埃及的管网更新项目", TODAY)["filters"]
        assert f["terms"][0]["term"] == "管网改造" and f["terms"][0]["match"] == ["管网", "pipe network"] and f["sectors"] == []
        assert A.rules_parse("智慧水务的线索", TODAY)["filters"]["terms"] == []  # 表里停用了,本体初始词也不再用
        bot._terms_at, bot.terms_base, bot._terms_db = None, lambda: (_ for _ in ()).throw(RuntimeError("down")), None
        bot.refresh_terms()
        assert A.rules_parse("埃及的管网更新项目", TODAY)["filters"]["terms"]  # 读不到:沿用上一次
    finally:
        A.set_terms(None)
    assert A.rules_parse("智慧水务的线索", TODAY)["filters"]["terms"][0]["term"] == "智慧水务"


def test_data_scope_note_when_the_question_is_about_china():
    """中国国内的招标采购数据不在采集范围(本体 data_scope):问到中国或「国内」时计划带上说明,页面显示在答案上方。"""
    p = A.rules_parse("最近一个月国内智慧水务招投标项目", TODAY)
    assert p["filters"]["countries"] == [{"iso3": "CHN", "name": "中国"}] and p["scope_note"] == "数据范围:本系统面向海外,不包含中国国内(大陆、港澳台)数据。"
    assert A.from_model({"object": "lead", "intent": "list", "countries": ["中国"]}, TODAY, "中国的线索")["scope_note"]
    assert A.rules_parse("埃及的线索", TODAY)["scope_note"] == "" and A.rules_parse("国内外的供水线索", TODAY)["filters"]["countries"] == []
    assert A.rules_parse("我国企业在埃及的线索", TODAY)["filters"]["countries"] == [{"iso3": "EGY", "name": "埃及"}]
    assert "plan.scope_note" in (A.ROOT / "portal" / "index.html").read_text(encoding="utf-8")


def test_portal_searches_keywords_and_terms_in_title_and_chinese_summary():
    html = (A.ROOT / "portal" / "index.html").read_text(encoding="utf-8")
    assert 'P.cond("lead", "summary_zh", "contains", k)' in html and "(f.terms || []).forEach((t) => c.push(text(t.match)))" in html


def test_award_questions_go_to_the_award_object():
    """「近三年摩洛哥地区运营维护的项目中标信息,规模在5000m3/d以上」(10-13 使用方问,模型理解成商机 + 我方阶段「中标」+ 关键词,0 条):
    问中标记录的是已中标项目,不是商机;合同类型、中标时间、规模按词表认,规模交给页面按工程量与名称里写明的数比。"""
    q = "近三年摩洛哥地区运营维护的项目中标信息，规模在5000m3/d以上"
    p = A.rules_parse(q, TODAY)
    assert p["object"] == "award" and p["intent"] == "list" and p["parsed_by"] == "rules"
    assert _on(p) == {"countries": [{"iso3": "MAR", "name": "摩洛哥"}], "contract_types": ["运营"], "days": 1095, "capacity": {"min": 5000.0, "max": None}}
    chip = dict(p["understood"])
    assert chip["对象"] == "已中标项目" and chip["合同类型"] == "运营" and chip["中标时间"] == "最近 3 年(1095 天)" and chip["规模"].startswith("≥ 5000 m³/日")
    assert A.plan(q, TODAY)["object"] == "award"  # 没配模型:只用词表
    # 我方的「中标」仍是商机阶段;线索的「中标公告」仍是线索类别
    for q2 in ("我们中标的商机", "中标的商机有哪些", "我们去年在摩洛哥中标的项目"):
        p = A.rules_parse(q2, TODAY)
        assert p["object"] == "opportunity" and p["filters"]["stages"] == ["中标"], q2
    p = A.rules_parse("最近 30 天的中标公告", TODAY)
    assert p["object"] == "lead" and p["filters"]["categories"] == ["授标或短名单"]
    assert A.rules_parse("帮我们查一下摩洛哥的中标信息", TODAY)["object"] == "award" and A.rules_parse("awards in Kenya", TODAY)["object"] == "award"
    # 中标单位国别:「中国企业」「中资」不是项目所在国;「国内」的才是(并带数据范围说明)
    p = A.rules_parse("中国企业在摩洛哥中标的项目", TODAY)
    assert p["filters"]["countries"] == [{"iso3": "MAR", "name": "摩洛哥"}] and p["filters"]["winner_countries"] == ["中国"] and p["scope_note"] == ""
    assert A.rules_parse("中资企业在埃及的中标记录", TODAY)["filters"]["winner_countries"] == ["中国"]
    assert A.rules_parse("国内企业在埃及中标", TODAY)["filters"]["countries"] == [{"iso3": "EGY", "name": "埃及"}]
    p = A.rules_parse("国内的中标信息", TODAY)
    assert p["filters"]["countries"][0]["iso3"] == "CHN" and p["scope_note"]
    assert A.rules_parse("中标单位是POWERCHINA的项目", TODAY)["filters"]["winners"] == ["POWERCHINA"]
    # 合同类型、分组、时间、资金来源
    assert A.rules_parse("规模不超过 2 万吨/日的 EPC 中标", TODAY)["filters"]["contract_types"] == ["工程"]
    assert A.rules_parse("摩洛哥的设计监理中标", TODAY)["filters"]["contract_types"] == ["咨询服务"]
    assert A.rules_parse("摩洛哥非咨询服务的中标", TODAY)["filters"]["contract_types"] == ["非咨询服务"]
    assert A.rules_parse("摩洛哥设备采购的中标", TODAY)["filters"]["contract_types"] == ["货物"]
    assert A.rules_parse("中标项目按工程量排", TODAY)["filters"]["contract_types"] == []  # 「工程量」不是工程
    p = A.rules_parse("2023年以来埃及的中标记录按合同类型分", TODAY)
    assert p["intent"] == "chart" and p["group_by"] == "contract_type" and (p["filters"]["from"], p["filters"]["to"]) == ("2023-01-01", "2026-10-08")
    assert A.rules_parse("按中标单位国别统计阿塞拜疆的中标", TODAY)["group_by"] == "winner_country"
    assert A.rules_parse("摩洛哥每年的中标金额", TODAY)["group_by"] == "year"
    assert A.rules_parse("亚行资金的中标按类别统计", TODAY)["group_by"] == "sector"
    p = A.rules_parse("世界银行出资的供水中标有多少份", TODAY)
    assert p["intent"] == "count" and p["filters"]["funders"] == ["世界银行"] and p["filters"]["sectors"] == ["供水"] and dict(p["understood"])["类别"] == "供水"
    assert A.rules_parse("三年内吉尔吉斯斯坦的中标", TODAY)["filters"]["days"] == 1095  # 「三年内」对中标记录说的是过去


def test_capacity_phrases():
    c = A.parse_capacity
    assert c("规模在5000m3/d以上") == {"min": 5000.0, "max": None} and c("日处理 1 万吨以上") == {"min": 10000.0, "max": None}
    assert c("超过 50 MLD") == {"min": 50000.0, "max": None} and c("不低于200 l/s") == {"min": 17280.0, "max": None}
    assert c("5万立方米/日以下") == {"min": None, "max": 50000.0} and c("不超过 2 万吨/日") == {"min": None, "max": 20000.0}
    assert c("3000~8000 m³/d") == {"min": 3000.0, "max": 8000.0} and c("日处理 3-5 万吨的污水厂") == {"min": 30000.0, "max": 50000.0}
    assert c("五千吨/日") == {"min": 5000.0, "max": None} and c("1,500 m3/h") == {"min": 36000.0, "max": None}
    assert c("规模最大的") is None and c("近三年") is None and c("水池 5000 m3") is None  # 不带「/日」的立方米是池容
    assert A.cap_label({"min": 30000.0, "max": 50000.0}) == "3 万–5 万 m³/日" and A.cap_label({"min": None, "max": 5000.0}) == "≤ 5000 m³/日"


def test_from_model_validates_award_fields():
    """模型给的已中标项目字段按词表校验:合同类型认同义说法、中标单位国别要是国名、规模以提问里写的为准;认不出的进 unresolved。
    模型把「中标信息」理解成商机 + 我方阶段「中标」的,按提问纠正成已中标项目,合同类型、规模从提问补,不当关键词。"""
    q = "近三年摩洛哥地区运营维护的项目中标信息，规模在5000m3/d以上"
    bad = {"object": "opportunity", "intent": "list", "countries": ["摩洛哥"], "stages": ["中标"], "keywords": ["运营维护", "5000m3/d"], "days": 1095, "unresolved": ["规模5000m3/d以上"]}
    p = A.from_model(bad, TODAY, q)
    assert p["object"] == "award" and p["parsed_by"] == "model" and p["unresolved"] == []
    assert _on(p) == {"countries": [{"iso3": "MAR", "name": "摩洛哥"}], "contract_types": ["运营"], "days": 1095, "capacity": {"min": 5000.0, "max": None}}
    assert dict(p["understood"])["中标时间"] == "最近 3 年(1095 天)"
    data = {"object": "award", "intent": "chart", "countries": ["摩洛哥", "中国"], "sectors": ["供水", "核电"], "categories": ["招标"], "stages": ["中标"], "funders": ["亚行"],
            "contract_types": ["O&M", "工程", "外包"], "winner_countries": ["China", "火星"], "winners": ["POWERCHINA", "中国企业"], "capacity_min": 5000, "capacity_max": "大",
            "keywords": ["运营维护", "Casablanca", "中标信息"], "group_by": "sectors", "days": 365, "future_days": 30, "limit": 20}
    p = A.from_model(data, TODAY, "摩洛哥中国企业中标的供水项目按类别")
    f = p["filters"]
    assert p["object"] == "award" and p["intent"] == "chart" and p["group_by"] == "sector" and p["limit"] == 20
    assert f["countries"] == [{"iso3": "MAR", "name": "摩洛哥"}] and f["winner_countries"] == ["中国"] and f["winners"] == ["POWERCHINA"]  # 中国是中标单位的国别
    assert f["sectors"] == ["供水"] and f["contract_types"] == ["运营", "工程"] and f["funders"] == ["亚洲开发银行"] and f["capacity"] == {"min": 5000.0, "max": None}
    assert f["categories"] == [] and f["stages"] == [] and f["future_days"] is None and f["days"] == 365 and f["keywords"] == ["Casablanca"]
    assert p["unresolved"] == ["外包", "火星", "核电"]
    assert A.from_model({"object": "award", "group_by": "stage"}, TODAY)["group_by"] is None  # 不是它的分组维度
    s = A.system_prompt(TODAY)
    assert "award 已中标项目" in s and "工程、货物、运营、咨询服务、非咨询服务" in s and "capacity_min" in A.GUIDE and "winner_country / funder / year" in s
    assert {"contract_types", "winner_countries", "winners", "capacity_min", "capacity_max"} <= set(A.SCHEMA["required"]) and "award" in A.SCHEMA["properties"]["object"]["enum"]


def test_portal_reads_award_capacity_from_quantity_and_name():
    """规模表格筛不了:页面(index.html 的 capM3d)从工程量、中文名、原名里读带单位的数折成 m³/日,几处取最大;读不出是 null(另列,不丢)。"""
    import shutil
    import subprocess
    if shutil.which("node") is None:
        import pytest
        pytest.skip("没有 node")
    run = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8").match(/\n  function capM3d\(text\) \{[\s\S]*?\n  \}\n/)[0];
const capM3d = new Function(src + "; return capM3d;")();
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(capM3d)));
"""
    cases = {"配水管网 17 km、净水厂 5 万吨/日": 50000, "Construction of 10,000 m3/day WTP": 10000, "station de traitement 5 000 m3/j": 5000, "20 MLD STP": 20000,
             "200 l/s pumping": 17280, "capacidad 10.000 m3/día": 10000, "мощностью 10 тыс. м3/сут": 10000, "日处理 2 万立方米": 20000, "水厂规模 5000 吨": 5000,
             "120 m3/h": 2880, "两座水厂 3 万吨/日、2 万吨/日": 30000, "reservoir 5000 m3": None, "Management, Operation and Maintenance of Water Supply System": None, "": None}
    out = subprocess.run(["node", "-e", run, str(A.ROOT / "portal" / "index.html"), json.dumps(list(cases), ensure_ascii=False)], capture_output=True, text=True, check=True).stdout
    assert dict(zip(cases, json.loads(out))) == cases


def test_portal_answers_award_questions():
    """首页问已中标项目:按国别、合同类型、中标时间向表格要行,规模由页面读工程量与名称再筛;没写规模的折在下面另列;
    中标价写原币并给约合人民币;只问一国的给到国别概况「已中标项目」页签的链接。只读。"""
    import pytest
    pytest.importorskip("playwright.sync_api", reason="没有 Playwright")
    from urllib.parse import quote
    from playwright.sync_api import Error as PWError, sync_playwright
    from core import schema, tstore
    from tests.test_portal import NEW, ONTO, _fake_with_schema, _seed, _serve, _today
    fake, admin = _fake_with_schema()
    c, _, _ = _seed(admin, fake)
    b = tstore.Base(admin)
    mar = b.create("country", [{"name_zh": "摩洛哥", "ISO3": "MAR"}])[0]
    y = _today().year
    row = lambda key, name, **kw: dict({"key": key, "name": name, "sector": "供水", "contract_type": "运营", "currency": "MAD", "awarded": f"{y - 1}-06-01", "country": mar["id"], "funder": "世界银行"}, **kw)  # noqa: E731
    b.create("award", [
        row("MA-1", "Gestion déléguée du service d'eau potable de Rabat", name_zh="拉巴特供水委托运营", quantity="净水厂 2 万吨/日", price=120_000_000, price_cny=8_500,
            winner="China Sample Water Co、Société Exemple", winner_country="中国、法国", url="https://example.org/ma1"),
        row("MA-2", "Management, Operation and Maintenance of Water Supply System in Fes", name_zh="非斯供水系统运营维护", price=30_000_000, price_cny=2_100, winner="Sample O&M SARL", winner_country="摩洛哥"),
        row("MA-3", "Exploitation de la station de traitement 3 000 m3/j de Taza", name_zh="塔扎水厂运营", price=5_000_000, price_cny=350, winner="Petite SARL", winner_country="摩洛哥"),
        row("MA-4", "Construction de la station 100 000 m3/j", contract_type="工程", price=900_000_000, price_cny=63_000, winner="Big Builder", winner_country="西班牙"),
        row("MA-5", "Operation of WTP 50,000 m3/day (old)", awarded=f"{y - 5}-01-01", price=1_000_000, price_cny=70, winner="Old Co", winner_country="法国"),
        row("AZ-1", "Operation of Baku WTP 50,000 m3/day", country=c["id"], currency="USD", price=2_000_000, price_cny=1_420, winner="Az Co", winner_country="阿塞拜疆"),
    ])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/index.html?q=" + quote("近三年摩洛哥地区运营维护的项目中标信息，规模在5000m3/d以上"))
            page.wait_for_selector(".answer table", timeout=15000)
            ans = page.locator(".answer").first
            und = ans.locator(".understood").inner_text()
            assert "对象:已中标项目" in und and "国别:摩洛哥" in und and "合同类型:运营" in und and "规模:≥ 5000 m³/日" in und and "中标时间:最近 3 年" in und
            rows = ans.locator(".a-body > .panel table tbody tr").all_inner_texts()
            assert len(rows) == 1 and "拉巴特供水委托运营" in rows[0] and "Gestion déléguée" in rows[0] and "读出规模约 2 万 m³/日" in rows[0]
            assert "1.20 亿 MAD" in rows[0] and "约 8,500 万元" in rows[0] and "中国、法国" in rows[0] and "世界银行" in rows[0]  # 本币金额旁边给约合人民币
            note = ans.locator(".a-body").inner_text()
            assert "符合的 1 份" in note and "不在范围的 1 份" in note and "另有 1 份没写规模" in note  # 塔扎 3000 m³/日 不够;非斯没写规模
            assert ans.locator("details summary").inner_text().startswith("没写规模的 1 份") and ans.locator("details").get_attribute("open") is None  # 有符合的:没写的先折着
            ans.locator("details summary").click()
            assert "非斯供水系统运营维护" in ans.locator("details tbody").inner_text()
            assert ans.locator("a[href='country.html?iso=MAR&t=awards']").count() >= 1
            if os.environ.get("PORTAL_SHOTS"):
                ans.screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "assistant_awards.png"))
            # 数一下 / 分组统计
            page.goto(base + "/index.html?q=" + quote("摩洛哥近三年的中标有多少份"))
            page.wait_for_selector(".answer .card .n", timeout=15000)
            assert page.locator(".answer .card .n").inner_text() == "4" and "份已中标项目" in page.inner_text(".answer")
            page.goto(base + "/index.html?q=" + quote("摩洛哥近三年的中标按合同类型统计"))
            page.wait_for_selector(".answer .chart canvas", timeout=15000)
            assert "共 4 份已中标项目" in page.inner_text(".answer") and "按合同类型分" in page.inner_text(".answer")
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "助手只读"


def _deepseek(reply: dict | str, seen: list):
    def handler(req: httpx.Request):
        seen.append(json.loads(req.content))
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return httpx.Response(200, json={"model": "deepseek-flash", "usage": {"prompt_tokens": 900, "completion_tokens": 80},
                                         "choices": [{"finish_reason": "stop", "message": {"content": content}}]})
    cfg = LLMConfig.for_assistant({"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "high", "max_calls": 150}, {"model": "deepseek-flash"})
    return Classifier(cfg, client=DeepSeekClient("sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None))


def test_assistant_llm_config_and_model_parse():
    seen: list = []
    clf = _deepseek({"object": "lead", "intent": "count", "countries": ["EGY"], "sectors": ["供水"], "categories": [], "funders": ["世界银行"], "stages": [],
                     "tracked": None, "status": [], "relevant": None, "days": 90, "from": None, "to": None, "future_days": None, "kinds": [], "keywords": [],
                     "group_by": None, "limit": None, "unresolved": []}, seen)
    assert clf.cfg.model == "deepseek-flash" and clf.cfg.thinking is False and clf.cfg.max_calls >= 10 ** 6 and clf.cfg.effort == "low"
    p = A.plan("埃及这个季度世行的供水有多少", TODAY, clf)
    body = seen[0]
    assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body and body["max_tokens"] == 2000
    assert "供水、污水、水利" in body["messages"][0]["content"] and "今天是 2026-10-08" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "埃及这个季度世行的供水有多少"
    assert p["parsed_by"] == "model" and p["intent"] == "count" and _on(p) == {"countries": [{"iso3": "EGY", "name": "埃及"}], "sectors": ["供水"], "funders": ["世界银行"], "days": 90}
    # 判定线索的接入照旧带思考模式
    assert LLMConfig(provider="deepseek").thinking is True
    # 模型给的不是 JSON:退回词表
    clf = _deepseek("我不知道", seen)
    p = A.plan("最近 7 天埃及有哪些新线索", TODAY, clf)
    assert p["parsed_by"] == "rules" and "按词表解析" in p["note"] and p["filters"]["days"] == 7
    # 模型说 help 而词表能认:用词表的
    clf = _deepseek({"object": "lead", "intent": "help"}, seen)
    assert A.plan("最近 7 天埃及有哪些新线索", TODAY, clf)["parsed_by"] == "rules"
    # 没有凭据:只用词表,note 说明
    p = A.plan("最近 7 天埃及有哪些新线索", TODAY, Classifier(LLMConfig(provider="deepseek")))
    assert p["parsed_by"] == "rules" and "DEEPSEEK_API_KEY" in p["note"]


def test_anthropic_config_for_assistant_keeps_effort_valid():
    cfg = LLMConfig.for_assistant({"provider": "anthropic", "model": "claude-opus-5", "effort": "medium"}, None)
    assert cfg.provider == "anthropic" and cfg.model == "claude-opus-5" and cfg.effort == "low" and cfg.max_tokens == 2000
    cfg = LLMConfig.for_assistant({"provider": "deepseek"}, {"provider": "anthropic", "model": "claude-haiku-5-5"})
    assert cfg.provider == "anthropic" and cfg.model == "claude-haiku-5-5"


def test_server_endpoints(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bot = Assistant(None)  # 没有 profile:不接模型
    srv = serve(bot, 0, host="127.0.0.1")
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        h = httpx.get(base + "/health").json()
        assert h["ok"] is True and h["llm"] is None and "词表" in h["note"] and h["examples"]
        r = httpx.post(base + "/ask", json={"q": "最近 7 天埃及有哪些新线索"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        p = r.json()
        assert p["ok"] and p["object"] == "lead" and p["filters"]["countries"][0]["iso3"] == "EGY" and p["examples"] and p["note"]
        assert httpx.post(base + "/ask", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
        assert httpx.post(base + "/nope", json={}).status_code == 404 and httpx.get(base + "/x").status_code == 404
        assert httpx.post(base + "/ask", json={"q": ""}).json()["intent"] == "help"
    finally:
        srv.shutdown()


def test_assistant_forgives_transient_errors(monkeypatch):
    seen: list = []
    clf = _deepseek("我不知道", seen)
    bot = Assistant(None)
    bot.clf = clf
    clf.errors.extend(["x"] * 5)
    assert not clf.enabled
    bot.ask("最近 7 天的线索")
    assert not clf.enabled and bot._disabled_at is not None  # 刚停用:还不到再试的时候
    monkeypatch.setattr("core.assistant_server.time.monotonic", lambda: bot._disabled_at + 10_000)
    bot.ask("最近 7 天的线索")
    assert clf.enabled and bot._disabled_at is None  # 过了 10 分钟:清掉错误再试
    clf.fatal = "余额不足"
    clf.errors.extend(["x"] * 5)
    bot._maybe_forgive()
    assert not clf.enabled and "余额不足" in bot.note()


def test_worker_starts_assistant_thread(monkeypatch, tmp_path):
    from core import worker
    started = {}
    monkeypatch.setattr(worker.assistant_server, "start_in_thread", lambda profile, port, **kw: started.update(port=port, llm=kw.get("use_llm")) or None)
    monkeypatch.setattr(worker.reports, "reset_stale", lambda ctx: 0)
    monkeypatch.setattr(worker.common, "connect", lambda *a, **k: None)
    monkeypatch.setattr(worker, "tick", lambda *a, **k: None)
    (tmp_path / "profile.yaml").write_text("name: t\ncountries: []\n", encoding="utf-8")
    monkeypatch.setenv("ASSISTANT_PORT", "9123")
    assert worker.main(["--profile", str(tmp_path / "profile.yaml"), "--once"]) == 0 and started == {"port": 9123, "llm": True}
    started.clear()
    assert worker.main(["--profile", str(tmp_path / "profile.yaml"), "--once", "--no-assistant"]) == 0 and started == {}


def test_profile_assistant_section(tmp_path):
    from core.profile import load_profile
    (tmp_path / "profile.yaml").write_text("name: t\ncountries: []\nwatch:\n  llm: {provider: deepseek, model: deepseek-v4-pro}\nassistant:\n  llm: {model: deepseek-flash}\n", encoding="utf-8")
    prof = load_profile(tmp_path / "profile.yaml")
    assert prof.assistant == {"llm": {"model": "deepseek-flash"}}
    cfg = LLMConfig.for_assistant(prof.watch.get("llm"), prof.assistant.get("llm"))
    assert cfg.provider == "deepseek" and cfg.model == "deepseek-flash"


def test_caddy_routes_assistant_api_to_the_worker():
    from pathlib import Path
    c = (Path(__file__).resolve().parent.parent / "deploy" / "teable" / "portal.caddy").read_text(encoding="utf-8")
    assert "handle_path /api/assistant/*" in c and "reverse_proxy worker:8765" in c
    assert c.index("forward_auth teable:3000") < c.index("handle_path /api/assistant/*") < c.rindex("file_server")  # 登录检查在前,静态文件兜底(前面登录页那段也有 file_server)
    assert "@ow_noslash path /portal\n" in c and "redir * /portal/ 301" in c and c.index("@ow_noslash") < c.index("@ow_portal path")  # /portal 没斜杠转到 /portal/
    assert "header_up X-Portal-Auth 1" in c  # 登录后来的请求打上标记,后台据此放行日历下载
    # 日历订阅路由不做登录检查、只转这一个文件、去掉外来的标记
    assert c.index("@ow_feed path /portal/feed/calendar.ics") < c.index("@ow_portal path") and "header_up -X-Portal-Auth" in c
