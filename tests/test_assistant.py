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
    f = A.rules_parse("规模不超过 2 万吨/日的 EPC 中标", TODAY)["filters"]
    assert f["contract_types"] == [] and f["deliveries"] == ["设计施工"]  # EPC 是承包模式(10-13 起),不再算成合同类型「工程」
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


def test_delivery_and_price_conditions():
    """10-13 使用方问「摩洛哥近三年的 DBO 项目或者运营项目的招标结果」(只按运营查了)、「污水处理厂 DBO 项目,中标金额大于 5000 万迪拉姆的」(DBO、金额都没认):
    DBO、EPC、PPP 这类是承包模式;同一句里还说了运营的,两样按「或」查(都进承包模式);金额按原币记,页面同币种比原币、别的折人民币比。"""
    p = A.rules_parse("摩洛哥近三年的DBO项目或者运营项目的招标结果", TODAY)
    assert p["object"] == "award" and _on(p) == {"countries": [{"iso3": "MAR", "name": "摩洛哥"}], "deliveries": ["DBO", "运营"], "days": 1095}
    assert dict(p["understood"])["承包模式"] == "DBO、运营(几样都算)"
    p = A.rules_parse("摩洛哥近三年的污水处理厂DBO项目招标中，中标金额大于5000万迪拉姆的", TODAY)
    f = p["filters"]
    assert p["object"] == "award" and f["deliveries"] == ["DBO"] and f["contract_types"] == [] and f["sectors"] == ["污水"] and f["days"] == 1095
    assert f["price"]["min"] == 5e7 and f["price"]["max"] is None and f["price"]["currency"] == "MAD" and f["price"]["rate"] > 0
    assert dict(p["understood"])["中标价"].startswith("≥ 5,000 万迪拉姆(约 ")
    assert A.rules_parse("近三年摩洛哥运营维护的中标", TODAY)["filters"]["contract_types"] == ["运营"]  # 只说运营的仍按合同类型
    assert A.rules_parse("印度的 PPP 和 BOT 中标", TODAY)["filters"]["deliveries"] == ["BOT/PPP"]
    assert A.rules_parse("设计建设运营一体的中标", TODAY)["filters"]["deliveries"] == ["DBO"]  # 不再算成设计施工
    pr = A.parse_price
    assert pr("中标价在1000到5000万元之间") == {"min": 1e7, "max": 5e7, "currency": "CNY", "rate": 1.0}
    assert pr("100 万美元以下的")["max"] == 1e6 and pr("100 万美元以下的")["currency"] == "USD" and pr("合同额 2-3 亿美元")["min"] == 2e8
    assert pr("超过 1 亿元")["min"] == 1e8 and pr("金额不到 300 万")["max"] == 3e6
    assert pr("规模在5000m3/d以上") is None and pr("2025年以来") is None and pr("近三年 50 份") is None  # 不是金额
    old, A.RATES_FILE = A.RATES_FILE, None
    try:
        assert pr("5000万迪拉姆以上")["rate"] == A.fx.STATIC["MAD"]  # 没有后台的汇率缓存时用近似表
    finally:
        A.RATES_FILE = old
    data = {"object": "award", "intent": "list", "countries": ["摩洛哥"], "deliveries": ["DBO", "运营"], "contract_types": ["工程"], "keywords": ["DBO", "5000万迪拉姆"]}
    p = A.from_model(data, TODAY, "摩洛哥 DBO 或运营项目,中标金额大于5000万迪拉姆的")
    f = p["filters"]
    assert f["deliveries"] == ["DBO", "运营"] and f["contract_types"] == [] and f["keywords"] == [] and f["price"]["min"] == 5e7
    s = A.system_prompt(TODAY)
    assert "deliveries(承包模式)施工、设计施工、DBO、BOT/PPP、运营" in s and "deliveries" in A.SCHEMA["required"] and "contract_type / delivery" in s
    assert A.rules_parse("摩洛哥的中标按承包模式分", TODAY)["group_by"] == "delivery"


def test_semantic_layer_measures_and_assets():
    """语义层(本体 semantic,2026-10-13 使用方:「没认出来:招标金额」「平均评标时间」「5000万人民币以上」,第二句还被当成线索):
    指标与算法按本体的说法认(长词先认),问了只有已中标项目才有的指标就查已中标项目;主体工程按 award_asset 的说法认,「污水厂」不算供水。"""
    q1 = "摩洛哥近3年来的污水项目中标金额与招标金额什么比例"
    p = A.plan(q1, TODAY)
    assert p["object"] == "award" and p["measures"] == [{"id": "price_ratio", "agg": "median"}] and p["unresolved"] == []
    assert _on(p) == {"countries": [{"iso3": "MAR", "name": "摩洛哥"}], "sectors": ["污水"], "days": 1095}
    assert dict(p["understood"])["指标"] == "中标价 / 招标估价(中位数)" and dict(p["understood"])["呈现"] == "指标卡片"
    q2 = "摩洛哥近3年来5000万人民币以上的污水厂项目，平均评标时间多长?"
    p = A.plan(q2, TODAY)  # 没说「中标」也查已中标项目:评标天数只有它有
    f = p["filters"]
    assert p["object"] == "award" and p["measures"] == [{"id": "eval_days", "agg": "mean"}] and p["unresolved"] == []
    assert f["assets"] == ["污水处理厂"] and f["sectors"] == ["污水"] and f["price"]["min"] == 5e7 and f["price"]["currency"] == "CNY" and f["keywords"] == []
    assert dict(p["understood"])["主体工程"] == "污水处理厂" and dict(p["understood"])["中标价"] == "≥ 5,000 万元"
    pm = A.parse_measures
    assert pm("平均中标价") == [{"id": "amount", "agg": "mean"}] and pm("中标总额") == [{"id": "amount", "agg": "sum"}]  # 长词先认,不再拆成「中标」+「价」
    assert pm("评标一般要多久") == [{"id": "eval_days", "agg": "median"}] and pm("下浮率") == [{"id": "price_ratio", "agg": "median"}]
    assert pm("平均几家投标") == [{"id": "bidders", "agg": "mean"}] and pm("平均投标家数") == [{"id": "bidders", "agg": "mean"}] and pm("最近 7 天埃及的线索") == []
    assert pm("平均评标时间和投标家数") == [{"id": "eval_days", "agg": "mean"}, {"id": "bidders", "agg": "mean"}]
    assert A.assets("污水处理厂扩建") == ["污水处理厂"] and A.assets("净水厂和管网") == ["供水厂", "管网"] and A.assets("水处理厂") == ["供水厂"] and A.assets("海水淡化厂") == ["海水淡化厂"]
    assert A.rules_parse("摩洛哥的污水厂中标", TODAY)["filters"]["sectors"] == ["污水"]  # 「污水厂」里的「水厂」不算供水
    assert A.rules_parse("各国污水处理厂的中标按主体工程分", TODAY)["group_by"] == "asset"
    # 线索没有评标天数:明说查线索的,指标进「没认出来」,对象不改
    p = A.plan("最近 30 天埃及线索的平均评标时间", TODAY)
    assert p["object"] == "lead" and p["measures"] == [] and "评标天数" in p["unresolved"]


def test_follow_up_questions_inherit_the_previous_plan():
    """接着问:页面把上一问的计划带来(prev)。这一句没明说查什么的沿用它的对象;像追问的(「那……呢」、很短的「……呢」)再沿用没说的条件、指标与分组,
    「理解为」写明沿用了哪几项。外面传来的 prev 逐项校验,认不得的丢掉;明说查别的对象的不沿用。"""
    p1 = A.plan("摩洛哥近3年来的污水项目中标金额与招标金额什么比例", TODAY)
    prev = dict(p1, q="摩洛哥近3年来的污水项目中标金额与招标金额什么比例")
    p = A.plan("那吉尔吉斯斯坦呢", TODAY, prev=prev)
    assert p["object"] == "award" and p["filters"]["countries"] == [{"iso3": "KGZ", "name": "吉尔吉斯斯坦"}] and p["filters"]["sectors"] == ["污水"] and p["filters"]["days"] == 1095
    assert p["measures"] == [{"id": "price_ratio", "agg": "median"}] and "类别或领域、时间、指标" in dict(p["understood"])["沿用上一问"]
    p = A.plan("按年份呢", TODAY, prev=prev)
    assert p["group_by"] == "year" and p["filters"]["countries"][0]["iso3"] == "MAR" and p["measures"] == [{"id": "price_ratio", "agg": "median"}]
    p = A.plan("平均评标时间呢", TODAY, prev=prev)  # 换个指标:条件照旧
    assert p["object"] == "award" and p["measures"] == [{"id": "eval_days", "agg": "mean"}] and p["filters"]["countries"][0]["iso3"] == "MAR"
    p = A.plan("最近 7 天埃及有哪些新线索", TODAY, prev=prev)  # 明说查线索:不沿用
    assert p["object"] == "lead" and _on(p) == {"countries": [{"iso3": "EGY", "name": "埃及"}], "days": 7} and "沿用上一问" not in dict(p["understood"])
    p = A.plan("其中供水的有多少", TODAY, prev=prev)
    assert p["object"] == "award" and p["filters"]["sectors"] == ["供水"] and p["filters"]["countries"][0]["iso3"] == "MAR"
    # 不像追问的:只沿用对象(「理解为」写明),条件不沿用
    p = A.plan("阿塞拜疆近五年的供水中标价中位数", TODAY, prev=prev)
    assert p["object"] == "award" and p["filters"]["countries"][0]["iso3"] == "AZE" and p["filters"]["sectors"] == ["供水"]
    # 外面传来的 prev 逐项校验
    junk = {"object": "award", "q": "x" * 9999, "filters": {"countries": [{"iso3": "XXX"}, "MAR", {"iso3": "MAR", "name": "伪"}], "sectors": ["供水", "核电"], "days": -5,
                                                       "price": {"min": "多", "currency": "MAD"}, "assets": ["污水处理厂", "宫殿"]},
            "measures": [{"id": "eval_days", "agg": "sum"}, {"id": "bidders", "agg": "median"}, {"id": "drop table"}], "group_by": "stage"}
    clean = A._prev(junk)
    assert clean["filters"]["countries"] == [{"iso3": "MAR", "name": "摩洛哥"}] and clean["filters"]["sectors"] == ["供水"] and clean["filters"]["assets"] == ["污水处理厂"]
    assert clean["filters"]["days"] is None and clean["filters"]["price"] is None and clean["measures"] == [{"id": "bidders", "agg": "median"}] and clean["group_by"] is None
    assert len(clean["q"]) == A.MAX_Q and A._prev({"object": "nope"}) is None and A._prev("x") is None
    assert A.plan("那吉尔吉斯斯坦呢", TODAY, prev={"object": "nope"})["object"] == "lead"


def test_model_path_uses_the_semantic_layer_and_previous_question():
    """配了模型时:模型把第二句当成线索、把「平均评标时间」「5000万人民币以上」放进关键词与没认出来的,按语义层纠正;
    接着问时模型看到上一句(只是提问文字,不是数据)。"""
    q2 = "摩洛哥近3年来5000万人民币以上的污水厂项目，平均评标时间多长?"
    bad = {"object": "lead", "intent": "list", "countries": ["摩洛哥"], "sectors": ["污水", "供水"], "keywords": ["平均评标时间", "5000万人民币"], "days": 1095,
           "unresolved": ["平均评标时间", "5000万人民币以上"]}
    p = A.from_model(bad, TODAY, q2)
    assert p["object"] == "award" and p["measures"] == [{"id": "eval_days", "agg": "mean"}] and p["unresolved"] == [] and p["filters"]["keywords"] == []
    assert p["filters"]["sectors"] == ["污水"] and p["filters"]["assets"] == ["污水处理厂"] and p["filters"]["price"]["min"] == 5e7
    p = A.from_model({"object": "award", "intent": "list", "measure": "bidders", "agg": "mean"}, TODAY, "摩洛哥污水厂一般几家来投")
    assert p["measures"] == [{"id": "bidders", "agg": "mean"}]  # 提问里没有语义层的说法:用模型给的
    p = A.from_model({"object": "award", "intent": "list", "measure": "eval_days", "agg": "sum"}, TODAY, "评标多久")
    assert p["measures"] == [{"id": "eval_days", "agg": "median"}]  # 提问里的说法为准;算法不合这个指标的用缺省
    seen: list = []
    clf = _deepseek(bad, seen)
    prev = dict(A.plan("摩洛哥近3年来的污水项目中标金额与招标金额什么比例", TODAY), q="摩洛哥近3年来的污水项目中标金额与招标金额什么比例")
    p = A.plan(q2, TODAY, clf, prev)
    assert p["parsed_by"] == "model" and p["object"] == "award" and p["measures"] == [{"id": "eval_days", "agg": "mean"}]
    assert seen[-1]["messages"][1]["content"] == "上一个问题(查的是已中标项目):摩洛哥近3年来的污水项目中标金额与招标金额什么比例\n这一个问题:" + q2
    s = A.system_prompt(TODAY)
    assert "price_ratio 中标价 / 招标估价" in s and "eval_days 评标天数" in s and {"measure", "agg", "assets"} <= set(A.SCHEMA["required"])


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
    row = lambda key, name, **kw: dict({"key": key, "name": name, "sector": "供水", "contract_type": "运营", "delivery": "运营", "currency": "MAD", "awarded": f"{y - 1}-06-01", "country": mar["id"], "funder": "世界银行"}, **kw)  # noqa: E731
    b.create("award", [
        row("MA-1", "Gestion déléguée du service d'eau potable de Rabat", name_zh="拉巴特供水委托运营", quantity="净水厂 2 万吨/日", price=120_000_000, price_cny=8_500,
            winner="China Sample Water Co、Société Exemple", winner_country="中国、法国", url="https://example.org/ma1"),
        row("MA-2", "Management, Operation and Maintenance of Water Supply System in Fes", name_zh="非斯供水系统运营维护", price=30_000_000, price_cny=2_100, winner="Sample O&M SARL", winner_country="摩洛哥"),
        row("MA-3", "Exploitation de la station de traitement 3 000 m3/j de Taza", name_zh="塔扎水厂运营", price=5_000_000, price_cny=350, winner="Petite SARL", winner_country="摩洛哥"),
        row("MA-4", "Construction de la station 100 000 m3/j", contract_type="工程", delivery="施工", price=900_000_000, price_cny=63_000, winner="Big Builder", winner_country="西班牙"),
        row("MA-5", "Operation of WTP 50,000 m3/day (old)", awarded=f"{y - 5}-01-01", price=1_000_000, price_cny=70, winner="Old Co", winner_country="法国"),
        row("MA-6", "Conception, réalisation et exploitation de la STEP de Tanger", sector="污水", contract_type="工程", delivery="DBO", price=60_000_000, price_cny=4_100,
            winner="Waterleau Sample", winner_country="比利时"),
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
            # 一份都对不上规模:不摆空表,先直说 0 份,没写规模的直接摊开
            page.goto(base + "/index.html?q=" + quote("近三年摩洛哥运营维护的中标，规模在10万m3/d以上"))
            page.wait_for_selector(".answer details[open] table", timeout=15000)
            body = page.locator(".answer .a-body").first.inner_text()
            assert "写明规模且 ≥ 10 万 m³/日 的:0 份(写了规模但不在范围的 2 份没列)。另有 1 份没写规模" in body and "(无)" not in body
            assert "非斯供水系统运营维护" in page.locator(".answer details tbody").inner_text()
            # 数一下 / 分组统计
            page.goto(base + "/index.html?q=" + quote("摩洛哥近三年的中标有多少份"))
            page.wait_for_selector(".answer .kpi .kn", timeout=15000)
            assert page.locator(".answer .kpi .kn").inner_text() == "5" and page.locator(".answer .kpi .ku").inner_text() == "份" and "已中标项目" in page.inner_text(".answer .kpi")
            page.goto(base + "/index.html?q=" + quote("摩洛哥近三年的中标按合同类型统计"))
            page.wait_for_selector(".answer .chart canvas", timeout=15000)
            assert "共 5 份已中标项目" in page.inner_text(".answer") and "按合同类型分,柱旁是份数" in page.inner_text(".answer")
            # 承包模式与金额(10-13 使用方问):DBO 或运营两样都算,5000 万迪拉姆以上按原币比;合同类型列下面标出 DBO
            page.goto(base + "/index.html?q=" + quote("摩洛哥近三年的DBO项目或者运营项目，中标金额大于5000万迪拉姆的"))
            page.wait_for_selector(".answer table", timeout=15000)
            und = page.locator(".answer .understood").first.inner_text()
            assert "承包模式:DBO、运营(几样都算)" in und and "中标价:≥ 5,000 万迪拉姆(约 " in und
            rows = page.locator(".answer .a-body > .panel table tbody tr").all_inner_texts()
            assert len(rows) == 2 and "Rabat" in rows[0] + rows[1] and "Tanger" in rows[0] + rows[1] and "DBO" in next(r for r in rows if "Tanger" in r)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "助手只读"


def test_portal_answers_measures_follow_ups_and_keeps_history():
    """2026-10-13 使用方在首页问的几句(语义层):中标价与招标估价的比例、5000 万人民币以上污水厂的平均评标时间、「那吉尔吉斯斯坦呢」。
    页面按条件取行自己算:卡片写问的算法(中位数、平均)与单位,写明按几份算、其余几份没有这项数据又各来自哪里;接着问的沿用上一问;
    长清单先列 20 行、能再列、能回到这一问;按年份分的柱图写单位并说明头一年、今年不是整年;问过的话记在浏览器里,点一句跳回或重问。只读。"""
    import pytest
    pytest.importorskip("playwright.sync_api", reason="没有 Playwright")
    from urllib.parse import quote
    from playwright.sync_api import Error as PWError, sync_playwright
    from core import schema, tstore
    from tests.test_portal import NEW, ONTO, _fake_with_schema, _seed, _serve, _today
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    b = tstore.Base(admin)
    mar, kgz = b.create("country", [{"name_zh": "摩洛哥", "ISO3": "MAR"}, {"name_zh": "吉尔吉斯斯坦", "ISO3": "KGZ"}])
    y = _today().year
    ma = "摩洛哥公共采购网结果公告(2026-10-13 取)"
    row = lambda key, name, **kw: dict({"key": key, "name": name, "sector": "污水", "contract_type": "工程", "currency": "MAD", "awarded": f"{y - 1}-06-01", "country": mar["id"],  # noqa: E731
                                        "source": ma, "winner": "Sample SARL", "winner_country": "摩洛哥"}, **kw)
    b.create("award", [
        row("MA-A", "Construction de la STEP de Safi", asset="污水处理厂", price=90_000_000, estimate=100_000_000, price_cny=6_300, eval_days=30, bidders=4),
        row("MA-B", "Réseau d'assainissement de Safi", asset="管网", price=40_000_000, estimate=50_000_000, price_cny=2_800, eval_days=50, bidders=6, awarded=f"{y - 2}-03-01"),
        row("MA-C", "Design, Build and Operate of Agadir WWTP", asset="污水处理厂", delivery="DBO", currency="USD", price=8_000_000, price_cny=5_600, eval_days=120, bidders=3,
            source="世界银行合同授予数据(STEP,2026-10-12 取)"),
        row("MA-D", "Extension de la STEP de Taza", asset="污水处理厂", price=20_000_000, estimate=18_000_000, price_cny=1_400, eval_days=40, bidders=2),
        row("MA-E", "Old STEP", asset="污水处理厂", price=30_000_000, estimate=10_000_000, price_cny=2_100, awarded=f"{y - 6}-01-01"),  # 三年前的:不算
        row("KG-1", "Реконструкция КОС г. Ош", country=kgz["id"], currency="KGS", asset="污水处理厂", price=9_000_000, estimate=10_000_000, price_cny=74, eval_days=20,
            source="吉尔吉斯斯坦国家采购门户中标单位表(2026-10-13 取)"),
    ] + [row(f"MA-W{i:02d}", f"Réseau d'eau potable lot {i}", sector="供水", asset="管网", price=2_000_000 + i, price_cny=140) for i in range(25)])
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

            def ask(q):
                page.fill("#q", q)
                page.press("#q", "Enter")
                page.wait_for_function("(q) => { const all = document.querySelectorAll('.answer'); const a = all[all.length - 1]; return a && a.querySelector('.a-q').textContent === q && !a.innerText.includes('解析中') && !a.innerText.includes('查表中') && !a.innerText.includes('读取中'); }", arg=q, timeout=15000)
                return page.locator(".answer").last

            q1 = "摩洛哥近3年来的污水项目中标金额与招标金额什么比例"
            page.goto(base + "/index.html?q=" + quote(q1))
            page.wait_for_selector(".answer .kpi .kn", timeout=15000)
            a = page.locator(".answer").last
            und = a.locator(".understood").inner_text()
            assert "对象:已中标项目" in und and "指标:中标价 / 招标估价(中位数)" in und and "没认出来" not in und
            kn, ku, kl = a.locator(".kpi .kn").all_inner_texts(), a.locator(".kpi .ku").all_inner_texts(), a.locator(".kpi .kl").all_inner_texts()
            assert kn[0] == "90" and ku[0] == "%" and kl[0] == "中标价 / 招标估价(中位数)"  # 90%、80%、111% 的中位数
            assert kn[1] == "94" and kl[1] == "平均" and kl[3] == "低于招标估价" and kn[3] == "2"
            body = a.locator(".a-body").inner_text()
            assert "按有招标估价的 3 份算(符合条件的共 4 份);其余 1 份没有(世界银行合同授予数据 1 份)" in body  # 没有估价的说清来自哪里
            a.locator("details.mlist summary").click()
            a.locator("details.mlist tbody tr").first.wait_for(timeout=15000)  # 展开时才画表(toggle 事件),等它画完再数
            assert a.locator("details.mlist tbody tr").count() == 3 and "招标估价的 80%" in a.locator("details.mlist tbody tr").first.inner_text()  # 从低到高
            # 接着问:「那吉尔吉斯斯坦呢」沿用类别、时间、指标
            a = ask("那吉尔吉斯斯坦呢")
            und = a.locator(".understood").inner_text()
            assert "国别:吉尔吉斯斯坦" in und and "沿用上一问" in und and "指标:中标价 / 招标估价(中位数)" in und
            assert a.locator(".kpi .kn").first.inner_text() == "90"
            # 第二句:主体工程「污水厂」、5000 万人民币以上、平均评标时间;没说查什么也认作已中标项目
            a = ask("摩洛哥近3年来5000万人民币以上的污水厂项目，平均评标时间多长?")
            und = a.locator(".understood").inner_text()
            assert "对象:已中标项目" in und and "主体工程:污水处理厂" in und and "中标价:≥ 5,000 万元" in und and "指标:评标天数(平均)" in und and "没认出来" not in und
            assert a.locator(".kpi .kn").first.inner_text() == "75" and a.locator(".kpi .ku").first.inner_text() == "天"  # 30 与 120 天
            assert "按有评标天数的 2 份算(符合条件的共 2 份)" in a.locator(".a-body").inner_text()
            # 按年份分的指标柱图:写单位与按几份算;头一年、今年不是整年要说
            a = ask("摩洛哥近3年污水项目的评标天数按年份")
            page.wait_for_selector(".answer >> nth=-1 >> .mchart canvas", timeout=15000)
            assert "柱高是每组的中位数" in a.inner_text() and "不是整年" in a.inner_text()
            # 长清单:先列 20 行,再列、全部列出、回到这一问
            a = ask("摩洛哥近三年供水的中标")
            rows = a.locator(".a-body table tbody tr")
            assert rows.count() == 25 and a.locator(".a-body table tbody tr:visible").count() == 20
            assert "再列 5 份(已列 20 / 25)" in a.locator(".fold-bar").inner_text() and a.locator(".fold-bar .back-q").count() == 1
            a.locator(".fold-bar button[data-a=more]").click()
            assert a.locator(".a-body table tbody tr:visible").count() == 25
            # 问过的话:钟形按钮打开,这一页问过的点了跳回(不重问);重开首页,示例下面列最近几句
            n = page.locator(".answer").count()
            page.click("#histBtn")
            items = page.locator("#histPop li button").all_inner_texts()
            assert len(items) == 5 and items[0].startswith("摩洛哥近三年供水的中标") and any(x.startswith(q1) for x in items)
            assert any(x.startswith("那吉尔吉斯斯坦呢") and "接着「" + q1 + "」" in x for x in items)  # 接着问的连同上一问记下,单独点也能接上
            page.locator("#histPop li button").last.click()  # 最早问的那句(第一问)
            assert page.locator("#histPop").count() == 0 and page.locator(".answer").count() == n
            page.goto(base + "/index.html")
            page.wait_for_selector("#recent .chip-btn", timeout=15000)
            assert page.locator("#recent .chip-btn").count() == 4 and "最近问过" in page.inner_text("#recent")
            if os.environ.get("PORTAL_SHOTS"):
                page.screenshot(path=str(Path(os.environ["PORTAL_SHOTS"]) / "assistant_recent.png"))
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


def test_country_business_environment_questions():
    """营商环境(世界银行 B-READY,v2.23;使用方 2026-10-14 的问法):对象是国别,门槛、市场分级、外企参标、排序按提问读;说线索的「水价」不改查国别。"""
    p = A.plan("供水得分低于 50 的市场国别", TODAY)
    assert p["object"] == "country" and p["filters"]["tiers"] == ["重点", "市场"] and p["filters"]["metric"] == {"id": "bready_water", "min": None, "max": 50.0}
    assert ["门槛", "供水得分低于 50 分"] in p["understood"] and p["measures"] == [{"id": "bready_water", "agg": "median"}]
    p = A.plan("哪些国家不限制外企参标", TODAY)
    assert p["object"] == "country" and p["filters"]["foreign"] == ["无限制"] and p["measures"][0]["id"] == "bready_procure"
    assert A.plan("哪些国家限制外企参标", TODAY)["filters"]["foreign"] == ["部分招标有限制", "全部招标有限制"]
    p = A.plan("摩洛哥商业水价多少", TODAY)
    assert p["object"] == "country" and [c["iso3"] for c in p["filters"]["countries"]] == ["MAR"] and p["measures"][0]["id"] == "water_tariff_usd" and p["intent"] == "list"
    p = A.plan("哪些国家政府回款最慢", TODAY)
    assert p["order"] == {"id": "gov_pay_days", "dir": "desc"} and p["limit"] == A.MAX_LIMIT
    assert A.plan("公共采购得分最差的国家", TODAY)["order"] == {"id": "bready_procure", "dir": "asc"}
    assert A.plan("哪些国家水价最低", TODAY)["order"] == {"id": "water_tariff_usd", "dir": "asc"}
    p = A.plan("营商环境最好的前5个国家", TODAY)
    assert p["order"] == {"id": "bready_total", "dir": "desc"} and p["limit"] == 5
    p = A.plan("授标天数超过180天的国家有几个", TODAY)
    assert p["intent"] == "count" and p["filters"]["metric"] == {"id": "award_days", "min": 180.0, "max": None}
    p = A.plan("阿塞拜疆外企参标有限制吗", TODAY)  # 点了国名:答这一国,不按外企参标把它筛掉
    assert p["object"] == "country" and not p["filters"]["foreign"] and p["measures"][0]["id"] == "bready_procure"
    # 接着问
    p1 = A.plan("摩洛哥商业水价多少", TODAY)
    prev = {"q": "摩洛哥商业水价多少", "object": "country", "filters": p1["filters"], "measures": p1["measures"], "group_by": None, "order": None}
    p2 = A.plan("那吉尔吉斯斯坦呢", TODAY, prev=prev)
    assert p2["object"] == "country" and [c["iso3"] for c in p2["filters"]["countries"]] == ["KGZ"] and p2["measures"][0]["id"] == "water_tariff_usd"
    p1 = A.plan("哪些国家政府回款最慢", TODAY)
    prev = {"q": "x", "object": "country", "filters": p1["filters"], "measures": p1["measures"], "group_by": None, "order": p1["order"]}
    p2 = A.plan("那市场国别呢", TODAY, prev=prev)
    assert p2["measures"][0]["id"] == "gov_pay_days" and p2["order"]["dir"] == "desc" and p2["filters"]["tiers"] == ["重点", "市场"]
    assert A.plan("那评标天数呢", TODAY, prev=prev)["object"] == "award"  # 换成已中标项目的指标就查已中标项目
    p = A.plan("哪些国家不限制外企参标", TODAY, prev={"q": "平均评标时间", "object": "award", "filters": {}, "measures": [{"id": "eval_days", "agg": "mean"}]})
    assert p["object"] == "country"  # 上一问是已中标项目,这一问明说营商环境的不沿用
    # 不误伤
    for q, obj in (("水价调整的线索", "lead"), ("埃及水价改革", "lead"), ("平均评标时间", "award"), ("最近 30 天的招标按国家分", "lead"), ("在跟踪的商机有哪些", "opportunity")):
        p = A.plan(q, TODAY)
        assert p["object"] == obj and not p["unresolved"], (q, p["object"], p["unresolved"])


def test_country_plan_from_model_uses_the_question():
    p = A.from_model({"object": "country", "intent": "list", "countries": ["摩洛哥"], "measure": "water_tariff_usd"}, TODAY, "摩洛哥的水费贵不贵")
    assert p["object"] == "country" and p["parsed_by"] == "model" and [c["iso3"] for c in p["filters"]["countries"]] == ["MAR"]
    p = A.from_model({"object": "lead", "intent": "list", "countries": []}, TODAY, "供水得分低于 50 的市场国别")  # 模型当成线索的,按提问改成国别
    assert p["object"] == "country" and p["filters"]["metric"]["max"] == 50.0
    assert "country" in A.SCHEMA["properties"]["object"]["enum"] and "country 国别" in A.system_prompt(TODAY)
