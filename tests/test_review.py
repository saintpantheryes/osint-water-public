"""复核按钮(core/review.py 与 POST /review、/review/help;2026-10-19 使用方:「需要人工复核的地方,能否增加"已复核""帮我复核""忽略此项"之类按钮?」):
点选记进「助手复核记录」表、同一份材料再看时照旧显示,投标评估按点选重算;「帮我复核」由助手自己查并请模型给结论。用内存版 Teable、假模型,不联网。"""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import httpx
import pytest

from core import bid_check as B
from core import link_check as L
from core import review as R
from core import schema, tstore
from core.assistant_server import Assistant, serve
from core.watch.fetch import Page
from tests.teable_fake import FakeTeable
from tests.test_link_check import HTML, MODEL, ONTO, TMAP, URL, _clf

TODAY = date(2026, 10, 19)
REPLY = {**MODEL, "sectors": ["污水"], "stage": "招标", "category": "意向书", "deadline": None,
         "requirements": {"similar_usd": 500_000, "turnover_usd": None, "financial_usd": None, "years": 5, "specific": None},
         "verify": ["意向书截止日期(公告中可能截断未显示)", "联合体牵头方需满足核心经验的具体定义"]}


def _db(with_review=True):
    fake = FakeTeable()
    admin = tstore.Client("https://water.example", "admin-token", transport=fake.transport, sleep=lambda s: None)
    tmap = TMAP if with_review else {**TMAP, "order": [o for o in TMAP["order"] if o != "review"], "tables": {k: v for k, v in TMAP["tables"].items() if k != "review"},
                                     "views": {k: v for k, v in TMAP.get("views", {}).items() if k != "review"}}
    schema.apply(admin, schema.build_plan(ONTO, tmap, {}), "水务项目机会 新版", log=lambda *a: None)
    b = tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None), tmap=tmap)
    uz = b.create("country", [{"name_zh": "乌兹别克斯坦", "ISO3": "UZB", "tier": "市场", "presence": "已设当地机构、有在建项目"}])[0]
    b.create("company", [{"kind": "业绩", "owner": "本单位", "name": "摩洛哥排水整治", "year": 2023, "amount": "折 65万美元", "sectors": ["污水"]}])
    b.create("lead", [{"title": "Surum WWTP feasibility", "url": "https://other.example/a", "country": uz["id"], "summary_zh": "苏鲁姆污水厂可研"},
                      {"title": "Surum WWTP lot 2 pumping", "url": "https://other.example/b", "country": uz["id"], "summary_zh": "苏鲁姆污水厂二标段泵站"}])
    return b


def _check(db, reply=REPLY, outside=lambda q, iso: ([], [])):
    return L.check(URL, "", TODAY, _clf(reply, []), db, fetch=lambda u: Page(URL, 200, HTML, "text/html"), outside=outside, scope=B.Scope.from_db(db))


def test_keys_are_stable_per_material_and_item():
    assert R.material_of("", "https://a.example/x", None) == "https://a.example/x" and R.material_of("", "", b"pdf") == R.material_of("", "", b"pdf")
    assert R.material_of("", "", b"pdf").startswith("file:") and R.material_of("", "", b"pdf") != R.material_of("", "", b"other")
    k = R.item_key("https://a.example/x", "要核实", "截止日")
    assert k.startswith("R") and len(k) == 17 and k == R.item_key("https://a.example/x", "要核实", " 截止日 ") and k != R.item_key("https://a.example/x", "库里线索", "截止日")


def test_check_result_carries_material_facts_and_buttons_keys():
    db = _db()
    out = _check(db)
    assert out["material"] == URL and isinstance(out["bid_facts"], dict) and out["bid_facts"]["country"] == "UZB"
    keys = R.keys_for(out)
    assert sorted(v["kind"] for v in keys.values()) == ["库里线索", "库里线索", "要核实", "要核实"]
    assert {v["lead_id"] for v in keys.values() if v["kind"] == "库里线索"} == {r["id"] for r in out["related_leads"]}


def test_rebid_counts_confirmed_leads_as_in_library_and_takes_facts_the_assistant_found():
    db = _db()
    out = _check(db)
    by = {c["key"]: c for c in out["bid"]["checks"]}
    assert out["bid"]["verdict"] == "有条件可投" and by["time"]["status"] == "不明" and by["library"]["status"] == "不明" and "2 条可能相关" in by["library"]["why"]
    a_id, b_id = [r["id"] for r in out["related_leads"]]
    mat = out["material"]
    reviews = {R.item_key(mat, "库里线索", a_id): {"kind": "库里线索", "lead_id": a_id, "status": "是同一项目"},
               R.item_key(mat, "库里线索", b_id): {"kind": "库里线索", "lead_id": b_id, "status": "不是同一项目"},
               R.item_key(mat, "要核实", REPLY["verify"][0]): {"kind": "要核实", "status": "助手已核", "facts": {"deadline": "2026-12-15"}}}
    bid = R.rebid(out, reviews, TODAY, L.our_records(db), B.Scope.from_db(db))
    by = {c["key"]: c for c in bid["checks"]}
    assert by["library"]["status"] == "通过" and "库里已有这一条" in by["library"]["why"] and by["time"]["status"] == "通过" and "2026-12-15 截止" in by["time"]["why"]
    assert bid["verdict"] == "有条件可投" and bid["conditions"] == ["信源是否真实:企业、展会、行业协会或转载站,可信度中"]  # 只剩信源(展会网站转载)
    # 助手核出了原公告:信源也过了 → 建议投标
    reviews[R.item_key(mat, "要核实", REPLY["verify"][0])]["facts"].update(original={"kind": "世行采购公告", "title": "UZ Surum EOI", "url": "https://projects.worldbank.org/n/1"}, sure=True)
    bid = R.rebid(out, reviews, TODAY, L.our_records(db), B.Scope.from_db(db))
    assert bid["verdict"] == "建议投标" and {c["key"]: c for c in bid["checks"]}["source"]["evidence"][0]["url"] == "https://projects.worldbank.org/n/1"
    # 只否定、不确认:相关的去掉后是新线索;人点的「已复核」不改评估
    bid2 = R.rebid(out, {R.item_key(mat, "库里线索", a_id): {"kind": "库里线索", "lead_id": a_id, "status": "不是同一项目"},
                          R.item_key(mat, "库里线索", b_id): {"kind": "库里线索", "lead_id": b_id, "status": "已忽略"},
                          R.item_key(mat, "要核实", REPLY["verify"][1]): {"kind": "要核实", "status": "已复核"}}, TODAY, L.our_records(db), B.Scope.from_db(db))
    assert {c["key"]: c for c in bid2["checks"]}["library"]["why"] == "库里没有同一项目,是新线索。" and bid2["verdict"] == "有条件可投"
    assert R.rebid({"related_leads": []}, reviews, TODAY, None, None) is None  # 旧结果没有评估依据


def test_save_load_and_apply_round_trip():
    db = _db()
    out = _check(db)
    mat, a_id = out["material"], out["related_leads"][0]["id"]
    rec = R.save(db, {"key": R.item_key(mat, "库里线索", a_id), "material": mat, "project": "塔什干苏鲁姆污水处理厂", "kind": "库里线索", "item": "Surum WWTP feasibility",
                      "lead_id": a_id, "status": "是同一项目"}, who="陈宝 <c@x>")
    R.save(db, {"key": R.item_key(mat, "要核实", REPLY["verify"][0]), "material": mat, "kind": "要核实", "item": REPLY["verify"][0], "status": "助手已核",
                "conclusion": "世行公告写明 2026-12-15 截止。", "confidence": "高", "facts": {"deadline": "2026-12-15", "stage": None},
                "evidence": [{"name": "世行公告", "url": "https://projects.worldbank.org/n/1"}]}, who="助手")
    R.save(db, {"key": rec["key"], "material": mat, "kind": "库里线索", "item": "Surum WWTP feasibility", "lead_id": a_id, "status": "不是同一项目"}, who="李四")  # 同编号改这一行
    rows = db.rows("review")
    assert len(rows) == 2
    got = R.load(db, mat)
    assert got[rec["key"]]["status"] == "不是同一项目" and got[rec["key"]]["who"] == "李四" and got[rec["key"]]["project"] == "塔什干苏鲁姆污水处理厂"
    vk = R.item_key(mat, "要核实", REPLY["verify"][0])
    assert got[vk]["facts"] == {"deadline": "2026-12-15"} and got[vk]["evidence"] == [{"name": "世行公告", "url": "https://projects.worldbank.org/n/1"}] and got[vk]["who"] == "助手"
    assert R.load(db, "https://nobody.example/") == {}
    applied = R.apply(out, got, TODAY, L.our_records(db), B.Scope.from_db(db), True)
    assert set(applied["reviews"]) == {rec["key"], vk} and applied["review_saved"] is True and applied["reviews"][vk]["conclusion"].startswith("世行公告")
    assert {c["key"]: c for c in applied["bid"]["checks"]}["time"]["status"] == "通过"
    # 别的材料的记录不挂上来
    other = R.apply({**out, "material": "https://x.example/"}, got, TODAY, None, None, True)
    assert other["reviews"] == {} and other["bid"] == out["bid"]


def test_help_verify_searches_then_asks_the_model_and_keeps_only_dated_facts():
    seen: list[dict] = []
    clf = _clf({"found": True, "conclusion": "世行同一项目的公告写明意向书 2026-12-15 截止。", "confidence": "高",
                "facts": {"deadline": "2026-12-15", "stage": "招标", "category": "意向书", "amount": None, "requirements": None}, "evidence": [1]}, seen)
    verify = lambda item, iso: {"original": {"kind": "世行采购公告", "title": "UZ Surum WWTP EOI", "url": "https://projects.worldbank.org/n/1", "date": "2026-10-01", "deadline": "2026-12-15", "text": "Deadline 15 Dec 2026"},  # noqa: E731
                                "sure": True, "candidates": [], "news": [{"kind": "新闻", "title": "Gazeta: Surum plant", "url": "https://gazeta.example/1"}], "links": [], "notes": ["整国核对了 12 条"]}
    ctx = {"title": "塔什干苏鲁姆污水处理厂", "country": "UZB", "funder": "ADSSC", "summary": "两期日处理150万立方米", "names": ["Surum"], "news_query": '"Surum" Tashkent'}
    got = R.help_verify("意向书截止日期(公告中可能截断未显示)", ctx, TODAY, clf, verify=verify, lib_rows=[{"title": "Surum WWTP feasibility", "url": "https://other.example/a", "found_at": "2026-09-01"}])
    assert got["status"] == "助手已核" and got["confidence"] == "高"
    assert got["facts"] == {"deadline": "2026-12-15", "stage": "招标", "category": "意向书", "original": {"kind": "世行采购公告", "title": "UZ Surum WWTP EOI", "url": "https://projects.worldbank.org/n/1"}, "sure": True}
    assert got["evidence"] == [{"name": "UZ Surum WWTP EOI", "url": "https://projects.worldbank.org/n/1"}] and got["notes"] == ["整国核对了 12 条"]
    user = seen[0]["messages"][-1]["content"]
    assert "要核实的点:意向书截止日期" in user and "[1] 世行采购公告 2026-10-01 UZ Surum WWTP EOI 截止 2026-12-15" in user and "[2] 新闻" in user and "[3] 2026-09-01 Surum WWTP feasibility" in user
    assert "乌兹别克斯坦" in user and "出资方:ADSSC" in user
    # 没查到:模型的判断照给,写明是推断;坏日期不进事实;没配模型时只给检索结果
    clf2 = _clf({"found": False, "conclusion": "公告没写截止日;欧佩克基金意向书一般给三周,建议按 11 月上旬准备。", "confidence": "低",
                 "facts": {"deadline": "三周后", "stage": None, "category": None, "amount": None, "requirements": None}, "evidence": []}, [])
    none = lambda item, iso: {"original": None, "sure": False, "candidates": [], "news": [], "links": [{"name": "到世行采购网找", "url": "https://projects.worldbank.org/search?q=Surum"}], "notes": []}  # noqa: E731
    got2 = R.help_verify("意向书截止日期", ctx, TODAY, clf2, verify=none)
    assert got2["conclusion"].startswith("公开渠道里没查到,公告没写截止日") and got2["facts"] == {} and got2["confidence"] == "低" and got2["evidence"][0]["name"] == "到世行采购网找"
    got3 = R.help_verify("意向书截止日期", ctx, TODAY, None, verify=none)
    assert got3["status"] == "助手已核" and got3["conclusion"].startswith("没配模型") and got3["evidence"][0]["url"].startswith("https://projects.worldbank.org/search")
    boom = lambda item, iso: (_ for _ in ()).throw(RuntimeError("no net"))  # noqa: E731
    got4 = R.help_verify("意向书截止日期", ctx, TODAY, None, verify=boom)
    assert got4["notes"] == ["公开渠道没查成(RuntimeError)"]


def test_help_lead_maps_the_models_relation_to_a_status():
    lead = {"id": "rec1", "title": "Surum WWTP lot 2 pumping", "summary_zh": "苏鲁姆污水厂二标段泵站", "country": "乌兹别克斯坦", "found_at": "2026-09-01", "url": "https://other.example/b"}
    ctx = {"title": "塔什干苏鲁姆污水处理厂", "country": "UZB"}
    same = R.help_lead(lead, ctx, TODAY, _clf({"relation": "同一项目", "why": "项目名、地点一致"}, []))
    assert same["status"] == "是同一项目" and same["conclusion"] == "同一项目:项目名、地点一致" and same["evidence"] == [{"name": "Surum WWTP lot 2 pumping", "url": "https://other.example/b"}]
    assert R.help_lead(lead, ctx, TODAY, _clf({"relation": "不同项目", "why": "另一座城市"}, []))["status"] == "不是同一项目"
    lot = R.help_lead(lead, ctx, TODAY, _clf({"relation": "同项目别的标段", "why": "二标段泵站"}, []))
    assert lot["status"] == "助手已核" and lot["conclusion"].startswith("同项目别的标段:")
    assert R.help_lead(lead, ctx, TODAY, None)["conclusion"].startswith("没配模型")


# ---------- 接口 ----------

def _bot(db, reply=REPLY):
    bot = Assistant(None)
    bot.terms_base = lambda: db
    bot.clf = _clf(reply, [])
    bot.link_kw = {"fetch": lambda u: Page(URL, 200, HTML, "text/html"), "outside": lambda q, iso: ([], [])}
    bot.to_docx = lambda md, path: path.write_bytes(b"PK")
    bot.who = lambda cookie: "陈宝 <c@x>" if cookie else ""
    return bot


def _poll(base, h, jid):
    for _ in range(200):
        st = httpx.get(base + "/link/" + jid, headers=h).json()
        if st["done"]:
            return st
        time.sleep(0.03)
    raise AssertionError("任务没完")


def test_review_endpoints_save_recompute_and_show_up_next_time(monkeypatch):
    db = _db()
    bot = _bot(db)
    monkeypatch.setattr(type(bot), "today", property(lambda self: TODAY))
    verify_calls: list[dict] = []
    bot.link_kw["verify"] = lambda item, iso: (verify_calls.append(item), {"original": {"kind": "世行采购公告", "title": "UZ Surum EOI", "url": "https://projects.worldbank.org/n/1", "deadline": "2026-12-15"},
                                                                          "sure": True, "candidates": [], "news": [], "links": [], "notes": []})[1]
    help_reply = {"found": True, "conclusion": "世行公告写明 2026-12-15 截止。", "confidence": "高", "facts": {"deadline": "2026-12-15", "stage": None, "category": None, "amount": None, "requirements": None}, "evidence": [1]}
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, h = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1", "Cookie": "auth_session=t"}
        first = _poll(base, h, httpx.post(base + "/link", json={"url": URL, "job": True}, headers=h).json()["job"])
        assert first["reviews"] == {} and first["review_saved"] is True and first["bid"]["verdict"] == "有条件可投" and first["material"] == URL
        a = first["analysis"]
        lib = {k: first[k] for k in ("same_url", "related_leads", "related_opps")}
        common = {"material": first["material"], "project": a["project_name"], "bid_facts": first["bid_facts"], "lib": lib}
        lead = first["related_leads"][0]
        # 没登录 403;类型、处理不对 → 说明
        assert httpx.post(base + "/review", json={}).status_code == 403
        bad = httpx.post(base + "/review", json={**common, "kind": "要核实", "item": "x", "status": "是同一项目"}, headers=h).json()
        assert bad["ok"] is False and "不能点" in bad["error"]
        # 库里线索点「是同一项目」:记下、库里检查变通过
        r1 = httpx.post(base + "/review", json={**common, "kind": "库里线索", "item": lead["title"], "lead_id": lead["id"], "status": "是同一项目", "reviews": {}}, headers=h).json()
        assert r1["ok"] and r1["saved"] and r1["review"]["status"] == "是同一项目" and r1["review"]["who"] == "陈宝 <c@x>" and r1["review"]["key"] == R.item_key(URL, "库里线索", lead["id"])
        by = {c["key"]: c for c in r1["bid"]["checks"]}
        assert by["library"]["status"] == "通过" and r1["bid"]["verdict"] == "有条件可投"
        # 「帮我复核」要核实的第一条:后台任务,助手查了世行、模型给了截止日 → 时间检查变通过、结论变建议投标
        bot.clf = _clf(help_reply, [])
        reviews = {r1["review"]["key"]: r1["review"]}
        job = httpx.post(base + "/review/help", json={**common, "kind": "要核实", "item": REPLY["verify"][0], "reviews": reviews,
                                                     "ctx": {"title": a["title_zh"], "country": "UZB", "funder": a["funder"], "summary": a["summary_zh"], "names": a["names"], "news_query": a["news_query"]}}, headers=h).json()
        assert job["ok"] and job["job"] and job["stage"] in ("查公开渠道", "记下结论", "")  # 假模型快,可能已经做完
        r2 = _poll(base, h, job["job"])
        assert r2["ok"] and r2["saved"] and r2["review"]["status"] == "助手已核" and r2["review"]["confidence"] == "高" and r2["review"]["facts"]["deadline"] == "2026-12-15" and r2["review"]["facts"]["sure"] is True
        assert r2["review"]["evidence"] == [{"name": "UZ Surum EOI", "url": "https://projects.worldbank.org/n/1"}] and verify_calls[0]["names"] == a["names"]
        by = {c["key"]: c for c in r2["bid"]["checks"]}
        assert by["time"]["status"] == "通过" and by["library"]["status"] == "通过" and by["source"]["status"] == "通过" and r2["bid"]["verdict"] == "建议投标"
        # 表里两行;同一份材料再看:点过的都在,评估按它们算
        assert len(db.rows("review")) == 2
        bot.clf = _clf(REPLY, [])
        again = _poll(base, h, httpx.post(base + "/link", json={"url": URL, "job": True}, headers=h).json()["job"])
        assert set(again["reviews"]) == {r1["review"]["key"], r2["review"]["key"]} and again["bid"]["verdict"] == "建议投标" and again["reviews"][r2["review"]["key"]]["who"] == "助手"
        # 人改主意:同一条点「不是同一项目」改同一行,库里检查回到「新线索」
        r3 = httpx.post(base + "/review", json={**common, "kind": "库里线索", "item": lead["title"], "lead_id": lead["id"], "status": "不是同一项目", "reviews": again["reviews"]}, headers=h).json()
        assert r3["ok"] and len(db.rows("review")) == 2 and {c["key"]: c for c in r3["bid"]["checks"]}["library"]["status"] == "不明"
        # 「帮我复核」库里线索:模型判同一项目 → 直接记「是同一项目」
        bot.clf = _clf({"relation": "同一项目", "why": "项目名、地点一致"}, [])
        job = httpx.post(base + "/review/help", json={**common, "kind": "库里线索", "item": lead["title"], "lead_id": lead["id"], "reviews": r3.get("reviews") or again["reviews"], "lead": lead, "ctx": {"title": a["title_zh"], "country": "UZB"}}, headers=h).json()
        r4 = _poll(base, h, job["job"])
        assert r4["ok"] and r4["review"]["status"] == "是同一项目" and r4["review"]["conclusion"] == "同一项目:项目名、地点一致" and r4["review"]["who"] == "助手"
        assert json.dumps(r4, ensure_ascii=False)
    finally:
        srv.shutdown()


def test_without_the_table_clicks_still_recompute_but_are_not_saved(monkeypatch):
    db = _db(with_review=False)
    bot = _bot(db)
    monkeypatch.setattr(type(bot), "today", property(lambda self: TODAY))
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, h = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1"}
        first = _poll(base, h, httpx.post(base + "/link", json={"url": URL, "job": True}, headers=h).json()["job"])
        assert first["review_saved"] is False and first["reviews"] == {}
        lead = first["related_leads"][0]
        r1 = httpx.post(base + "/review", json={"material": URL, "bid_facts": first["bid_facts"], "lib": {k: first[k] for k in ("same_url", "related_leads", "related_opps")},
                                                "kind": "库里线索", "item": lead["title"], "lead_id": lead["id"], "status": "是同一项目"}, headers=h).json()
        assert r1["ok"] and r1["saved"] is False and {c["key"]: c for c in r1["bid"]["checks"]}["library"]["status"] == "通过"
        assert "review" not in db.t
    finally:
        srv.shutdown()


@pytest.mark.parametrize("status", R.STATUS)
def test_every_status_is_in_the_ontology(status):
    tax = next(t for t in ONTO.raw["taxonomies"] if t == "review_status") if isinstance(ONTO.raw.get("taxonomies"), dict) else None
    values = [v["v"] for g in ONTO.raw["taxonomies"]["review_status"]["groups"] for v in g["values"]] if tax else []
    assert status in values
