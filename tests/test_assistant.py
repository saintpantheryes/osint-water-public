"""助手(门户「助手」页):一句提问 → 查询计划。规则解析、模型输出校验、进程内接口、后台任务里的线程。"""
from __future__ import annotations

import json
from datetime import date

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
