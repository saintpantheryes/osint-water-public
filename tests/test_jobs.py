"""阶段 D 后台任务(core/jobs、core/tstore 的写入规则):只填空、列表只追加、待核更新去重;采集 → 判定 → 入库 → 时间节点 → 挂接与建议商机,
重复运行不重复建;阶段变更记跟进只记人改的;上传的线索补全但不判相关性。用内存版的 Teable(先按本体建表),假采集器与假模型,不联网。"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest

from core import ontology, schema, tstore
from core.jobs import collect, common, enrich, stage_log
from core.profile import load_profile
from core.watch.collectors import KINDS
from core.watch.store import Item
from tests.teable_fake import FakeTeable

ONTO, TMAP = ontology.load(), ontology.load_map()
NEW = "水务项目机会 新版"
TODAY = date(2026, 10, 5)
ITEMS: dict[str, list[Item]] = {}  # 假采集器按信源编号出条目
LLM: dict[str, dict] = {}  # 假模型按网址出结果


def _fake_collector(src: dict, ctx) -> list[Item]:
    return list(ITEMS.get(src["id"], []))


KINDS["fake"] = _fake_collector


class StubClassifier:
    enabled, calls, note, errors = True, 0, "", []

    def system(self, country, bidder, led):
        return f"sys {country}"

    def classify(self, system, sig, text):
        self.calls += 1
        return LLM.get(sig["url"])

    def classify_many(self, system, jobs):
        return [self.classify(system, s, t) for s, t in jobs]


def _llm(relevant=True, category="eoi", new=False, deadlines=(), project=None, funder="虚构银行", amount=None, links=()):
    return {"relevant": relevant, "category": category, "sectors": ["wastewater"], "country": "AZE", "summary_zh": "虚构摘要",
            "project": project, "funder": funder, "amount": amount, "evidence": ["原文片段"], "eligibility": None,
            "deadlines": [{"type": "eoi_deadline", "date": d, "evidence": "…"} for d in deadlines],
            "ledger_links": [{"item": i, "confidence": c, "reason": "读了原文"} for i, c in links], "new_opportunity": new}


@pytest.fixture
def env(tmp_path):
    ITEMS.clear()
    LLM.clear()
    fake = FakeTeable()
    admin = tstore.Client("https://water.example", "admin-token", transport=fake.transport, sleep=lambda s: None)
    schema.apply(admin, schema.build_plan(ONTO, TMAP, {"bid_deadline": {"准备": 56}}), NEW, log=lambda *a: None)
    bot = tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None)
    base = tstore.Base(bot)
    base.create("country", [{"name_zh": "阿塞拜疆", "ISO3": "AZE", "tier": "重点"}])
    prof_dir = tmp_path / "prof"
    (prof_dir / "AZE").mkdir(parents=True)
    (prof_dir / "profile.yaml").write_text(
        "name: t\ncountries:\n  - {iso2: AZ, iso3: AZE, name_en: Azerbaijan, name_zh: 阿塞拜疆, tier: focus, watch: AZE/watch.yaml}\n"
        "bidder: {nationality: CHN}\nsector_scope: {water_supply: true, wastewater_sanitation: true}\n", encoding="utf-8")
    (prof_dir / "AZE" / "watch.yaml").write_text(
        "schema: osint-water/watch@1\ncountry: AZE\nnames: [Azerbaijan]\nsources:\n"
        "  - {id: fake_src, name: 假信源, kind: fake, topic: water, url: https://example.org}\n", encoding="utf-8")
    prof = load_profile(prof_dir / "profile.yaml")
    ctx = common.connect(prof, TODAY, use_llm=False, api=bot, base=base, cache_dir=tmp_path / "cache", log=lambda *a: None)
    ctx.clf = StubClassifier()
    return fake, admin, tstore.Base(admin), ctx


def _rows(base: tstore.Base, oid: str) -> list[dict]:
    return base.rows(oid)


def _val(base, oid, rec, prop):
    return base.get(oid, rec, prop)


# ---------- 写入层的规则 ----------

def test_fill_empty_only_fills_blank_and_reports_differences():
    have = {"a": None, "b": "", "c": "旧值", "d": ["x"], "e": False, "f": "Same Value"}
    changes, diffs = tstore.fill_empty(have, {"a": "甲", "b": "乙", "c": "新值", "d": ["x"], "e": True, "f": "same  value", "g": None})
    assert changes == {"a": "甲", "b": "乙", "e": True}
    assert diffs == [("c", "新值", "旧值")]  # d 一样、f 只差空白与大小写、g 想写的本身为空:都不算不同


def test_append_lines_adds_only_new_keys():
    cur = "世行 · 1,000 USD [a1a1a1a1]\n亚行 · 金额待核实 [b2b2b2b2]"
    assert tstore.append_lines(cur, ["世行 · 1,000 USD [a1a1a1a1]", " 亚行 ·  金额待核实 [b2b2b2b2] "]) is None  # 都已有
    out = tstore.append_lines(cur, ["世行 · 1,000 USD [c3c3c3c3]", "欧投行 · 2 EUR [c3c3c3c3]"])
    assert out.splitlines()[:2] == cur.splitlines() and out.splitlines()[2:] == ["世行 · 1,000 USD [c3c3c3c3]", "欧投行 · 2 EUR [c3c3c3c3]"]
    assert tstore.append_lines(None, ["", "  ", "甲"]) == "甲"


def test_append_review_respects_rejections():
    cur = "✗ [a1a1a1a1] 公开进度:已定标(现值:采购进行中)\n[b2b2b2b2] 资格依据:仅限成员国(现值:无)"
    assert tstore.append_review(cur, [("a1a1a1a1", "公开进度", "已定标", "采购进行中"), ("b2b2b2b2", "资格依据", "x", "y")]) is None
    out = tstore.append_review(cur, [("a1a1a1a1", "进度说明", "签约", None), ("c3c3c3c3", "公开进度", ["x"], "y")])
    assert out.splitlines()[2:] == ["[a1a1a1a1] 进度说明:签约(现值:None)", "[c3c3c3c3] 公开进度:x(现值:y)"]


# ---------- 采集、判定、建议 ----------

def test_collect_judges_once_and_builds_suggested_opportunity(env):
    fake, admin, abase, ctx = env
    ITEMS["fake_src"] = [
        Item(key="a", url="https://example.org/a", title="EOI: Ganja wastewater treatment plant", published="2026-10-01",
             text="Expression of interest for the wastewater plant", fields={"project": "P123456"}),
        Item(key="b", url="https://example.org/b", title="Road rehabilitation", published="2026-10-02", text="water supply mention"),
    ]
    LLM["https://example.org/a"] = _llm(new=True, deadlines=("2026-10-20",), project="Ganja WWTP", amount={"value": 5e6, "currency": "USD"})
    LLM["https://example.org/b"] = _llm(relevant=False)
    res = collect.run(ctx)
    assert res.leads == 2 and res.judged == 2 and res.milestones == 1 and res.suggest == {"新建商机": 1}
    leads = {_val(abase, "lead", r, "url"): r for r in _rows(abase, "lead")}
    a, b = leads["https://example.org/a"], leads["https://example.org/b"]
    assert _val(abase, "lead", a, "judged_by") == "模型" and _val(abase, "lead", a, "relevant") is True
    assert _val(abase, "lead", a, "category") == "意向书" and _val(abase, "lead", a, "sectors") == ["污水"]
    assert _val(abase, "lead", a, "origin") == "机器采集" and _val(abase, "lead", a, "status") == "待处理"
    assert _val(abase, "lead", a, "amount") == "5,000,000 USD" and _val(abase, "lead", a, "key") == a["fields"][abase.fid("lead", "key")]
    assert _val(abase, "lead", b, "judged_by") == "模型" and not _val(abase, "lead", b, "relevant")
    assert _val(abase, "lead", a, "source")["title"] == "假信源" and _val(abase, "lead", a, "country")["title"] == "阿塞拜疆"
    opps = _rows(abase, "opportunity")
    assert len(opps) == 1
    o = opps[0]
    assert _val(abase, "opportunity", o, "code") == "AZE-001" and _val(abase, "opportunity", o, "stage") == "建议"  # R5:只能是建议
    assert _val(abase, "opportunity", o, "name_zh") == "Ganja WWTP" and _val(abase, "opportunity", o, "public_stage") == "采购进行中"
    no = _val(abase, "lead", a, "key")
    assert _val(abase, "opportunity", o, "financing") == f"虚构银行 · 5,000,000 USD [{no}]"
    assert _val(abase, "opportunity", o, "external_ids") == f"project: P123456 [{no}]"
    assert [x["id"] for x in _val(abase, "opportunity", o, "sources")] == [a["id"]]
    assert _val(abase, "lead", a, "opportunity")["id"] == o["id"] and _val(abase, "lead", a, "maybe_new") is True
    ms = _rows(abase, "milestone")
    assert len(ms) == 1 and _val(abase, "milestone", ms[0], "kind") == "意向书截止" and _val(abase, "milestone", ms[0], "lead")["id"] == a["id"]
    assert _val(abase, "milestone", ms[0], "opportunity")["id"] == o["id"]  # 建议商机建好后节点也挂上商机
    src = ctx.sources()["fake_src"]
    assert res.sources["fake_src"]["status"] == "ok" and _val(abase, "source", src, "last_status") == "正常"

    # 再跑一次:同样的条目不再建;模型不再调用(判过的永不回头)
    calls = ctx.clf.calls
    res2 = collect.run(ctx)
    assert res2.leads == 0 and ctx.clf.calls == calls and len(_rows(abase, "lead")) == 2 and len(_rows(abase, "milestone")) == 1

    # 第二天:新公告里出现 P123456 → 编号吻合,可靠归属到 AZE-001;事实只填空、不同的进待核更新、列表追加、出处线索追加
    ITEMS["fake_src"].append(Item(key="c", url="https://example.org/c", title="Contract award P123456 Ganja WWTP", published="2026-10-04",
                                  text="awarded wastewater contract P123456", fields={"project": "P123456"}))
    LLM["https://example.org/c"] = _llm(category="award", funder="亚行", deadlines=("2026-10-20",))
    abase.update("opportunity", o["id"], {"verified": "已核实", "name_local": "Ganja WWTP (phase 1)"})  # 人核实过、人写的原文名称:机器不再提「未核实」、不拿线索的项目名来争(2026-10-16)
    ctx2 = common.connect(ctx.profile, TODAY, use_llm=False, api=ctx.api, base=ctx.base, cache_dir=ctx.cache.root.parent, log=lambda *a: None)
    ctx2.clf = StubClassifier()
    res3 = collect.run(ctx2)
    assert res3.leads == 1 and res3.suggest["归属"] == 1
    c = next(r for r in _rows(abase, "lead") if _val(abase, "lead", r, "url") == "https://example.org/c")
    assert _val(abase, "lead", c, "opportunity")["id"] == o["id"] and _val(abase, "lead", c, "suggest_reliable") is True
    o2 = abase.record("opportunity", o["id"])
    no_c = _val(abase, "lead", c, "key")
    assert _val(abase, "opportunity", o2, "public_stage") == "采购进行中"  # 已有值不改
    assert _val(abase, "opportunity", o2, "review_updates") == f"[{no_c}] 公开进度:已定标(现值:采购进行中)"  # 只有真要人核的;核实状态、原文名称不进来
    assert _val(abase, "opportunity", o2, "verified") == "已核实" and _val(abase, "opportunity", o2, "name_local") == "Ganja WWTP (phase 1)"
    assert _val(abase, "opportunity", o2, "financing").splitlines() == [f"虚构银行 · 5,000,000 USD [{no}]", f"亚行 · 金额待核实 [{no_c}]"]
    assert sorted(x["id"] for x in _val(abase, "opportunity", o2, "sources")) == sorted([a["id"], c["id"]])
    ms = _rows(abase, "milestone")
    assert len(ms) == 2 and all(_val(abase, "milestone", m, "opportunity")["id"] == o["id"] for m in ms)  # 同日期、不同线索:各一条
    assert len(_rows(abase, "opportunity")) == 1  # 没有重复建商机


def test_rule_only_and_unjudged_leads(env):
    fake, admin, abase, ctx = env
    ctx.clf = None  # 没配模型:候选线索入库、判定方式留空;非候选的记「规则」
    ITEMS["fake_src"] = [Item(key="a", url="https://example.org/a", title="EOI: water supply", text="water", published="2026-10-01")]
    res = collect.run(ctx)
    a = _rows(abase, "lead")[0]
    assert res.leads == 1 and _val(abase, "lead", a, "judged_by") is None and _val(abase, "lead", a, "category") is None
    # 之后模型配好了:按原文摘录补判(E1),判成后做建议
    LLM["https://example.org/a"] = _llm(new=True, project="Baku water")
    ctx.clf = StubClassifier()
    res2 = collect.run(ctx)
    assert res2.rejudged == 1 and res2.leads == 0 and res2.suggest == {"新建商机": 1}
    a = abase.record("lead", a["id"])
    assert _val(abase, "lead", a, "judged_by") == "模型" and _val(abase, "lead", a, "opportunity")["title"] == "Baku water"


def test_disabled_source_is_skipped(env):
    fake, admin, abase, ctx = env
    ITEMS["fake_src"] = [Item(key="a", url="https://example.org/a", title="EOI: water supply", text="water")]
    collect.run(ctx)
    src = ctx.sources()["fake_src"]
    abase.update("source", src["id"], {"enabled": False})  # 管理员在表里停用
    ITEMS["fake_src"].append(Item(key="b", url="https://example.org/b", title="EOI: sewer", text="water"))
    ctx2 = common.connect(ctx.profile, TODAY, use_llm=False, api=ctx.api, base=ctx.base, cache_dir=ctx.cache.root.parent, log=lambda *a: None)
    res = collect.run(ctx2)
    assert res.sources["fake_src"]["status"] == "skip" and res.leads == 0


def test_ignored_country_is_not_collected(env, monkeypatch):
    fake, admin, abase, ctx = env
    abase.create("preference", [{"name": "某人", "ignored": "aze, kaz"}])  # 任何一个账号忽略了阿塞拜疆(码不分大小写)
    ITEMS["fake_src"] = [Item(key="a", url="https://example.org/a", title="EOI: water supply", text="water")]
    res = collect.run(ctx)
    assert res.ignored == ["AZE"] and res.leads == 0 and "fake_src" not in res.sources and "忽略国别不采:AZE" in res.text()
    # 全球信源:判到忽略国别的条目不入库,别的国别照常
    gl = collect.Scope(collect.GLOBAL, None, "全球", {"sources": [{"id": "fake_gl", "name": "全球假信源", "kind": "fake", "topic": "water", "store": "all"}]})
    monkeypatch.setattr(collect, "scopes", lambda c, only=None: [gl])
    ITEMS["fake_gl"] = [Item(key="x", url="https://example.org/x", title="EOI: water supply", text="water", published="2026-10-04", fields={"country": "AZE"}),
                        Item(key="y", url="https://example.org/y", title="EOI: water supply", text="water", published="2026-10-04", fields={"country": "GEO"})]
    ctx2 = common.connect(ctx.profile, TODAY, use_llm=False, api=ctx.api, base=ctx.base, cache_dir=ctx.cache.root.parent, log=lambda *a: None)
    res2 = collect.run(ctx2)
    assert res2.dropped == 1 and res2.leads == 1
    assert [_val(abase, "lead", r, "url") for r in _rows(abase, "lead")] == ["https://example.org/y"]


def test_ignored_parsing(env):
    fake, admin, abase, ctx = env
    assert ctx.ignored() == {"CHN"}  # 还没人设过:默认忽略中国
    abase.create("preference", [{"name": "甲"}, {"name": "乙", "ignored": "-"}])
    ctx._ignored = None
    assert ctx.ignored() == {"CHN"}  # 甲没设过,按默认
    abase.update("preference", abase.rows("preference")[0]["id"], {"ignored": "-"})
    ctx._ignored = None
    assert ctx.ignored() == set()  # 都明确不忽略
    # 「默认」行(名称「默认」、账号空着):没设过的账号按它算;它本身不算一个账号(甲明确恢复了的国别,默认不拦)
    abase.create("preference", [{"name": "默认", "ignored": "usa,can"}, {"name": "丙"}])
    ctx._ignored = None
    assert ctx.ignored() == {"USA", "CAN"}  # 甲、乙都是「-」,丙没设过 → 按默认
    abase.update("preference", [r for r in abase.rows("preference") if abase.get("preference", r, "name") == "丙"][0]["id"], {"ignored": "-"})
    ctx._ignored = None
    assert ctx.ignored() == set()  # 都明确不忽略了,「默认」行不参与
    assert common.parse_isos("USA,CAN; mex、 bra\nxx 1234") == {"USA", "CAN", "MEX", "BRA"}


# ---------- 阶段变更记跟进 ----------

def test_stage_log_records_only_human_changes(env):
    fake, admin, abase, ctx = env
    cty = ctx.country_rid("AZE")
    bot_opp = ctx.base.create("opportunity", [{"code": "AZE-001", "name_zh": "机器建的", "country": cty}])[0]
    human_opp = abase.create("opportunity", [{"code": "AZE-002", "name_zh": "人建的", "country": cty, "stage": "跟踪中"}])[0]
    assert stage_log.run(ctx) == {"阶段变更记跟进": 1}
    fu = _rows(abase, "followup")
    assert len(fu) == 1 and _val(abase, "followup", fu[0], "content") == "立项为「跟踪中」"
    assert _val(abase, "followup", fu[0], "opportunity")["id"] == human_opp["id"] and _val(abase, "followup", fu[0], "author")["id"] == "usrADM"
    assert _val(abase, "followup", fu[0], "stage_change") == "跟踪中"
    assert stage_log.run(ctx) == {}  # 不重复记
    abase.update("opportunity", human_opp["id"], {"stage": "准备投标"})
    abase.update("opportunity", bot_opp["id"], {"internal_note": "只改备注"})
    assert stage_log.run(ctx) == {"阶段变更记跟进": 1}
    fu = sorted(_rows(abase, "followup"), key=lambda r: r["createdTime"])
    assert _val(abase, "followup", fu[-1], "content") == "阶段从「跟踪中」改为「准备投标」"
    ctx.base.update("opportunity", bot_opp["id"], {"name_local": "机器改的别的字段"})
    assert stage_log.run(ctx) == {}  # 机器建的建议商机、机器改的不记


# ---------- 补全上传的线索 ----------

def test_enrich_fills_uploaded_leads_without_judging_relevance(env, monkeypatch):
    fake, admin, abase, ctx = env
    up = abase.create("lead", [{"url": "https://example.org/up", "origin": "用户上传"}])[0]
    monkeypatch.setattr(enrich, "_from_url", lambda url, store: ("Sumgait WWTP tender", "Invitation for bids wastewater Azerbaijan", []))
    LLM["https://example.org/up"] = _llm(relevant=False, category="tender", new=True, deadlines=("2026-11-01",))  # 模型说不相关也不算
    assert enrich.run(ctx) == {"补全": 1, "抓取失败": 0, "新建商机": 1}
    r = abase.record("lead", up["id"])
    assert _val(abase, "lead", r, "title") == "Sumgait WWTP tender" and _val(abase, "lead", r, "relevant") is True
    assert _val(abase, "lead", r, "judged_by") == "不判定" and _val(abase, "lead", r, "source")["title"] == "人工提交"
    assert _val(abase, "lead", r, "country")["title"] == "阿塞拜疆" and _val(abase, "lead", r, "key")
    assert len(_rows(abase, "milestone")) == 1 and _val(abase, "lead", r, "opportunity")
    assert enrich.run(ctx) == {}  # 补过的不再补
    # 抓不到的:也写判定方式,不反复抓
    bad = abase.create("lead", [{"url": "https://example.org/bad", "origin": "用户上传", "title": "同事填的标题"}])[0]
    from core.watch import fetch

    def boom(url, store):
        raise fetch.FetchError("404")
    monkeypatch.setattr(enrich, "_from_url", boom)
    assert enrich.run(ctx) == {"补全": 0, "抓取失败": 1}
    r = abase.record("lead", bad["id"])
    assert _val(abase, "lead", r, "title") == "同事填的标题" and _val(abase, "lead", r, "judged_by") == "不判定"
    assert str(_val(abase, "lead", r, "excerpt")).startswith("抓取失败")


def test_cache_dir_is_only_a_cache(env):
    _, _, _, ctx = env
    assert Path(ctx.cache.root).name == "watch" and "cache" in str(ctx.cache.root)


def test_translation_model_is_the_cheap_no_thinking_one(env):
    """译名每两分钟一批,用判定线索的思考模式太贵:同一模型关思考、浅深度,输出够一批 40 个名称。"""
    _, _, _, ctx = env
    prof = dataclasses.replace(ctx.profile, watch={"llm": {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "high", "max_calls": 150}})
    c = common.connect(prof, TODAY, api=ctx.api, base=ctx.base, cache_dir=ctx.cache.root.parent, log=lambda *a: None)
    assert c.clf.cfg.thinking and c.clf.cfg.effort == "high"
    light = c.clf_light.cfg
    assert (light.provider, light.model, light.thinking, light.effort) == ("deepseek", "deepseek-v4-pro", False, "low")
    assert light.max_tokens == common.TRANSLATE_MAX_TOKENS >= 8_000
    assert common.connect(prof, TODAY, use_llm=False, api=ctx.api, base=ctx.base, cache_dir=ctx.cache.root.parent,
                          log=lambda *a: None).clf_light is None


def test_expire_marks_past_milestones_done_and_leaves_today_and_future(env):
    """A 复核的后备:Teable 含 TODAY 的公式隔天不重算,后台每天把 日期 < 今天 的节点改成「已发生」;今天、以后、已发生的都不动。"""
    from core.jobs import expire
    fake, admin, b, ctx = env
    opp = b.create("opportunity", [{"code": "AZE-001", "name_zh": "甘贾污水厂", "stage": "跟踪中"}])[0]
    rows = b.create("milestone", [
        {"opportunity": opp["id"], "kind": "投标截止", "day": "2026-09-20", "status": "官方计划"},
        {"opportunity": opp["id"], "kind": "意向书截止", "day": "2026-10-02", "status": "推断"},
        {"opportunity": opp["id"], "kind": "招标发布", "day": "2026-09-01", "status": "已发生"},
        {"opportunity": opp["id"], "kind": "投标截止", "day": TODAY.isoformat(), "status": "官方计划"},
        {"opportunity": opp["id"], "kind": "贷款签署", "day": "2027-01-15", "status": "推断"},
    ])
    assert expire.run(ctx) == {"过期节点改已发生": 2}
    got = {b.get("milestone", r, "kind") + str(tstore.day(b.get("milestone", r, "day"), b.tz)): b.get("milestone", r, "status")
           for r in (b.record("milestone", x["id"]) for x in rows)}
    assert got["投标截止2026-09-20"] == "已发生" and got["意向书截止2026-10-02"] == "已发生"
    assert got["投标截止" + TODAY.isoformat()] == "官方计划" and got["贷款签署2027-01-15"] == "推断"
    assert expire.run(ctx) == {}  # 第二遍没得改


def test_archive_leads_marks_expired_and_stale_unassigned_only_once(env):
    """A49:截止已过的、或发布超过一年且没归入商机的线索标「已归档」;归入商机的旧线索、一年内的、人取消过归档的都不动;第二遍没得改。"""
    from core.jobs import expire
    fake, admin, b, ctx = env
    opp = b.create("opportunity", [{"code": "AZE-001", "name_zh": "甘贾污水厂", "stage": "跟踪中"}])[0]
    old = (TODAY - timedelta(days=400)).isoformat()
    rows = b.create("lead", [
        {"key": "l-old-free", "title": "old unassigned", "url": "https://example.org/1", "published": old},
        {"key": "l-old-opp", "title": "old in opp", "url": "https://example.org/2", "published": old, "opportunity": opp["id"]},
        {"key": "l-old-found", "title": "old by found_at", "url": "https://example.org/3", "found_at": old + "T01:00:00.000Z"},
        {"key": "l-new", "title": "recent", "url": "https://example.org/4", "published": (TODAY - timedelta(days=30)).isoformat()},
        {"key": "l-dead", "title": "deadline passed", "url": "https://example.org/5", "published": TODAY.isoformat(), "opportunity": opp["id"]},
        {"key": "l-undone", "title": "human un-archived", "url": "https://example.org/6", "published": old, "archived_at": (TODAY - timedelta(days=2)).isoformat()},
    ])
    b.create("milestone", [{"lead": rows[4]["id"], "opportunity": opp["id"], "kind": "投标截止", "day": (TODAY - timedelta(days=1)).isoformat(), "status": "官方计划"}])
    out = expire.archive_leads(ctx)
    assert out == {"过期线索归档": 3, "超过一年": 2, "截止已过": 1}, out
    got = {b.get("lead", r, "key"): (b.get("lead", r, "archived"), b.get("lead", r, "archive_reason")) for r in b.rows("lead", props=["key", "archived", "archive_reason", "archived_at"])}
    assert got["l-old-free"] == (True, "超过一年") and got["l-old-found"] == (True, "超过一年") and got["l-dead"] == (True, "截止已过")
    assert not got["l-old-opp"][0] and not got["l-new"][0] and not got["l-undone"][0]
    assert expire.archive_leads(ctx) == {}
