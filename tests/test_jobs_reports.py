"""阶段 D 第二步:生成报告(A14)与国别信息(A11)。用 test_jobs 的内存版 Teable 环境;Word 转换与世行、IMF 抓取换成假的,不联网。"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core import tstore
from core.jobs import countries, reports
from tests.test_jobs import TODAY, env  # noqa: F401  fixture

TZ = ZoneInfo("Asia/Shanghai")


def _rows(base, oid):
    return base.rows(oid)


def _val(base, oid, rec, prop):
    return base.get(oid, rec, prop)


def _fake_docx(md_path: Path, docx_path: Path, template=None) -> Path:
    docx_path.write_bytes(b"PK fake docx " + md_path.read_bytes()[:200])
    return docx_path


def _seed(base):
    """一个国别、两条线索、一个跟踪中的商机(带所属线索、时间节点、跟进)。"""
    c = base.rows("country")[0]
    o = base.create("opportunity", [{"code": "AZE-001", "name_zh": "甘贾污水厂", "country": c["id"], "stage": "跟踪中", "public_stage": "采购进行中",
                                     "financing": "亚行 · 50,000,000 USD [aaaaaaaa]"}])[0]
    l1, l2 = base.create("lead", [
        {"key": "aaaaaaaa", "title": "Ganja WWTP EOI", "url": "https://example.org/a", "relevant": True, "category": "意向书", "country": c["id"],
         "found_at": "2026-10-01T02:00:00.000Z", "opportunity": o["id"], "summary_zh": "甘贾污水厂意向书", "origin": "机器采集"},
        {"key": "bbbbbbbb", "title": "Baku water supply tender", "url": "https://example.org/b", "relevant": True, "category": "意向书",
         "country": c["id"], "found_at": "2026-10-03T02:00:00.000Z", "summary_zh": "巴库供水招标", "origin": "机器采集"},
    ])
    base.create("milestone", [{"opportunity": o["id"], "lead": l1["id"], "kind": "意向书截止", "day": "2026-10-20", "日期原文": "2026-10-20", "status": "官方计划"}])
    base.create("followup", [{"opportunity": o["id"], "day": "2026-10-02", "content": "阶段从「建议」改为「跟踪中」", "stage_change": "跟踪中"}])
    return c, o, l1, l2


def test_weekly_report_is_generated_uploaded_and_cited(env, monkeypatch):  # noqa: F811
    fake, admin, abase, ctx = env
    monkeypatch.setattr(reports, "_to_docx", _fake_docx)
    c, o, l1, l2 = _seed(abase)
    # 周一 09:00 自建上周周报;再跑一次不重复建
    now = datetime(2026, 10, 5, 9, 1, tzinfo=TZ)  # 2026-10-05 是周一
    assert reports.ensure_periodic(ctx, now) == {"自建报告": 1}
    assert reports.ensure_periodic(ctx, now) == {}
    r = _rows(abase, "report")[0]
    assert _val(abase, "report", r, "kind") == "周报" and _val(abase, "report", r, "period") == "2026-W40" and _val(abase, "report", r, "gen_status") == "待生成"
    assert reports.run(ctx) == {"报告完成": 1, "报告失败": 0}
    r = abase.record("report", r["id"])
    assert _val(abase, "report", r, "gen_status") == "完成"
    name = _val(abase, "report", r, "key")
    assert name.startswith("周报_") and name.endswith(".docx")
    files = _val(abase, "report", r, "file")
    assert files and files[0]["name"] == name
    # 引用的线索 = 正文列出的(本期两条)∪ 写进的商机的所属线索;写进的商机 = 阶段有变化的
    assert {x["id"] for x in _val(abase, "report", r, "cites_leads")} == {l1["id"], l2["id"]}
    assert [x["id"] for x in _val(abase, "report", r, "cites_opps")] == [o["id"]]
    md = (ctx.cache.root.parent / "reports" / name.replace(".docx", ".md")).read_text(encoding="utf-8")
    assert "aaaaaaaa" in md and "bbbbbbbb" in md and "AZE-001" in md and "意向书截止" in md and "本报告未使用模型" in md
    # 线索的派生属性「写进的报告」跟着有了
    assert _val(abase, "lead", abase.record("lead", l1["id"]), "in_reports")


def test_country_and_project_reports_and_failures(env, monkeypatch):  # noqa: F811
    fake, admin, abase, ctx = env
    monkeypatch.setattr(reports, "_to_docx", _fake_docx)
    c, o, l1, l2 = _seed(abase)
    abase.create("report", [{"kind": "国别报告", "country": c["id"], "day": "2026-10-05", "gen_status": "待生成"},
                            {"kind": "项目报告", "opportunity": o["id"], "day": "2026-10-05", "gen_status": "待生成"},
                            {"kind": "国别报告", "day": "2026-10-05", "gen_status": "待生成"}])  # 没选国别:失败
    assert reports.run(ctx) == {"报告完成": 2, "报告失败": 1}
    by = {_val(abase, "report", r, "kind") + str(_val(abase, "report", r, "gen_status")): r for r in _rows(abase, "report")}
    cr, pr = by["国别报告完成"], by["项目报告完成"]
    assert "国别报告失败" in by
    assert _val(abase, "report", cr, "key").startswith("国别报告_AZE_")
    assert {x["id"] for x in _val(abase, "report", cr, "cites_leads")} == {l1["id"]}  # 国别报告不列线索,只记商机的出处线索
    assert {x["id"] for x in _val(abase, "report", pr, "cites_leads")} == {l1["id"]}
    assert [x["id"] for x in _val(abase, "report", pr, "cites_opps")] == [o["id"]]
    md = (ctx.cache.root.parent / "reports" / _val(abase, "report", pr, "key").replace(".docx", ".md")).read_text(encoding="utf-8")
    assert "甘贾污水厂" in md and "一、核心结论" in md and "七、信息来源" in md and "Ganja WWTP EOI" in md  # 按《项目分析报告》模板
    # 重新生成:把完成的改回待生成,同一行再出一版(文件名不变、不新建行)
    abase.update("report", pr["id"], {"gen_status": "待生成"})
    assert reports.run(ctx) == {"报告完成": 1, "报告失败": 0}
    assert len(_rows(abase, "report")) == 3 and len(_val(abase, "report", abase.record("report", pr["id"]), "file")) == 2


def test_stale_generating_reports_are_reset(env):  # noqa: F811
    fake, admin, abase, ctx = env
    r = abase.create("report", [{"kind": "周报", "day": "2026-10-05", "gen_status": "生成中"}])[0]
    fake.records[abase.t["report"]][0]["lastModifiedTime"] = "2026-01-01T00:00:00.000Z"
    assert reports.reset_stale(ctx) == 1
    assert _val(abase, "report", abase.record("report", r["id"]), "gen_status") == "待生成"


class _V:
    def __init__(self, label, text, source, url):
        self.metric = type("M", (), {"label": label})()
        self._t, self.source, self.url = text, source, url

    def text(self, currency=""):
        return self._t


class _F:
    """load_facts 的最小替身。"""
    def __init__(self, iso):
        self.iso3 = iso
        self.values = {"gdp": _V("GDP", "720 亿美元(2024)", "WB-WDI", "https://data.worldbank.org/x")}
        self.meta = {"income": "Upper middle income"}
        self.market = None
        self.water = {"institutions": [{"name_zh": "阿塞拜疆水务局", "url": "https://water.gov.az"}], "enterprises": [], "laws": []}
        self.currency_code = "AZN"

    def income(self):
        return "中高收入"

    def lending(self):
        return "IBRD"

    def basics_rows(self, internal=True):
        return [("首都", "巴库", "维基", "")]


def test_country_info_fills_replaces_and_appends(env, monkeypatch):  # noqa: F811
    fake, admin, abase, ctx = env
    from core.country_facts import METRICS
    monkeypatch.setattr(countries, "_fetch", lambda ctx, d: False)
    monkeypatch.setattr(countries, "_facts", lambda d, m, isos: {i: _F(i) for i in isos})
    monkeypatch.setattr(countries, "METRICS", tuple(m for m in METRICS if m.key == "gdp"))
    c = abase.rows("country")[0]
    abase.update("country", c["id"], {"basics": "首都:巴库(维基)", "water_agencies": "旧机构 · https://old.example"})
    out = countries.run(ctx, refresh=True)
    assert out["国别"] == 1 and out["替换"] == 2 and out["追加"] == 1  # 指标与收入组替换;机构追加;基础信息已有同一条不重复
    c = abase.record("country", c["id"])
    assert _val(abase, "country", c, "region") and _val(abase, "country", c, "tier") == "重点"  # 人管的不动
    assert "GDP:720 亿美元(2024)(世行 WDI https://data.worldbank.org/x)" in _val(abase, "country", c, "indicators")
    assert _val(abase, "country", c, "income_group") == "中高收入;IBRD"
    assert _val(abase, "country", c, "water_agencies") == "旧机构 · https://old.example\n阿塞拜疆水务局 · https://water.gov.az"
    assert _val(abase, "country", c, "basics") == "首都:巴库(维基)"
    # 只补空着的:都填了就什么也不做
    assert countries.run(ctx, only_missing=True) == {}
    # 新国别出现(线索建了国别行)→ only_missing 补它
    ctx.country_rid("MAR")
    out = countries.run(ctx, only_missing=True)
    assert out["国别"] == 1
    m = next(r for r in abase.rows("country") if _val(abase, "country", r, "key") == "MAR")
    assert _val(abase, "country", m, "name_zh") == "摩洛哥" and _val(abase, "country", m, "indicators")


def test_report_uses_the_default_template_from_the_template_table(env, monkeypatch):  # noqa: F811
    """「报告模板」表里适用类型含该类型且勾了缺省的模板 → 下载到本机、转 Word 时套它;报告行记下实际用的模板。没有合适的 → 程序自带模板。"""
    fake, admin, abase, ctx = env
    used: list = []

    def fake_docx(md_path: Path, docx_path: Path, template=None) -> Path:
        used.append(template)
        return _fake_docx(md_path, docx_path)

    monkeypatch.setattr(reports, "_to_docx", fake_docx)
    monkeypatch.setattr(reports, "_download", lambda ctx, att: b"PK template " + att["name"].encode())
    _seed(abase)
    t_other = abase.create("template", [{"name": "只给月报", "kinds": ["月报"], "is_default": True}])[0]
    admin.upload_attachment(abase.t["template"], t_other["id"], abase.fid("template", "file"), "monthly.docx", b"PK m")
    t1 = abase.create("template", [{"name": "集团版式", "kinds": ["周报", "国别报告"], "is_default": True}])[0]
    admin.upload_attachment(abase.t["template"], t1["id"], abase.fid("template", "file"), "group.docx", b"PK g")
    # 没有文件的缺省模板不算
    abase.create("template", [{"name": "空的", "kinds": ["周报"], "is_default": True}])
    r = abase.create("report", [{"kind": "周报", "period": "2026-W40", "day": "2026-10-05", "edition": "内部版", "gen_status": "待生成"}])[0]
    assert reports.run(ctx) == {"报告完成": 1, "报告失败": 0}
    assert len(used) == 1 and used[0] is not None and used[0].suffix == ".docx" and used[0].read_bytes() == b"PK template group.docx"
    r = abase.record("report", r["id"])
    assert _val(abase, "report", r, "gen_status") == "完成" and tstore.link_id(_val(abase, "report", r, "template")) == t1["id"]
    # 报告行自己选了模板 → 用选的;项目报告没有合适的模板 → 自带(template=None)
    o = abase.rows("opportunity")[0]
    r2 = abase.create("report", [{"kind": "月报", "period": "2026-09", "day": "2026-10-05", "edition": "内部版", "gen_status": "待生成", "template": t1["id"]},
                                 {"kind": "项目报告", "opportunity": o["id"], "day": "2026-10-05", "edition": "内部版", "gen_status": "待生成"}])
    assert reports.run(ctx) == {"报告完成": 2, "报告失败": 0}
    assert len(used) == 2 and used[1].read_bytes() == b"PK template group.docx"  # 项目报告不走 pandoc,填自带的项目分析报告模板
    assert tstore.link_id(_val(abase, "report", abase.record("report", r2[0]["id"]), "template")) == t1["id"]
    assert _val(abase, "report", abase.record("report", r2[1]["id"]), "template") is None


def test_daily_brief_is_built_after_collection_and_shown_as_summary(env, monkeypatch):  # noqa: F811
    """日报(E10):采集完成后自建当天一行(同一天只建一次);生成时正文写进「简报正文」并传 Word;口径与首页一致。"""
    from core.jobs import brief
    fake, admin, abase, ctx = env
    monkeypatch.setattr(reports, "_to_docx", _fake_docx)
    c, o, l1, l2 = _seed(abase)
    today = TODAY.isoformat()
    abase.create("lead", [
        {"key": "cccccccc", "title": "Sumgait desalination tender", "url": "https://example.org/c", "relevant": True, "category": "招标", "sectors": ["海水淡化"],
         "country": c["id"], "found_at": f"{today}T01:00:00.000Z", "funder": "World Bank (IDA)", "amount": "50,000,000 USD", "status": "待处理", "origin": "机器采集", "summary_zh": "苏姆盖特海水淡化厂招标"},
        {"key": "dddddddd", "title": "Water policy news", "url": "https://example.org/d", "relevant": True, "category": "政策", "sectors": ["供水"],
         "country": c["id"], "found_at": f"{today}T01:10:00.000Z", "status": "待处理", "origin": "机器采集", "suggested_opp": o["id"]},
        {"key": "eeeeeeee", "title": "Unrelated page", "url": "https://example.org/e", "relevant": False, "found_at": f"{today}T01:20:00.000Z", "status": "待处理", "origin": "机器采集"},
    ])
    ctx.source_rid("fake_src", name="假信源", kind="接口")
    ctx.source_status("fake_src", "ok", f"{today}T00:40:00.000Z")
    assert reports.ensure_daily(ctx, TODAY) == {"自建报告": 1}
    assert reports.ensure_daily(ctx, TODAY) == {}  # 同一天不重复建
    rows = [r for r in abase.rows("report") if _val(abase, "report", r, "kind") == "日报"]
    assert len(rows) == 1 and _val(abase, "report", rows[0], "period") == today and _val(abase, "report", rows[0], "gen_status") == "待生成"
    assert reports.run(ctx) == {"报告完成": 1, "报告失败": 0}
    r = abase.record("report", rows[0]["id"])
    assert _val(abase, "report", r, "gen_status") == "完成" and _val(abase, "report", r, "key").startswith("日报_") and _val(abase, "report", r, "key").endswith(".docx")
    md = _val(abase, "report", r, "summary")
    assert md.startswith(f"# 线索日报 {today}")
    # 今日新增 3(相关 2、采购类 1);累计 5;待处理 = 处理状态为待处理的(与首页卡片同一条件);待确认归属 1(dddddddd 有建议所属)
    assert "线索累计 5 条;今日新增 3 条(相关 2 条,其中采购类 1 条)" in md
    assert "待处理 5 条,待确认归属 1 条;跟踪中的商机 1 个" in md  # 处理状态缺省待处理,5 条都是
    assert "资金来源:世界银行 1、未填 1" in md and "领域:海水淡化 1、供水 1" in md
    assert "阿塞拜疆 · 招标 · 苏姆盖特海水淡化厂招标 · 50,000,000 USD [cccccccc]" in md  # 采购类排在政策前;线索号在句末(页面与 Word 里成角标)
    assert md.index("[cccccccc]") < md.index("[dddddddd]")
    assert "阿塞拜疆 · 政策 · 政策(原题:Water policy news) [dddddddd]" in md  # 没有中文摘要的:从原题挑词成「要点」,原题跟在后面
    assert "今日采集信源 1 个:正常 1" in md and "未使用模型" in md and "模型研判" not in md
    files = _val(abase, "report", r, "file")
    assert [f["name"] for f in files] == [_val(abase, "report", r, "key")]  # 有「简报正文」字段时只传 Word
    cited = {_val(abase, "lead", abase.record("lead", x["id"]), "key") for x in _val(abase, "report", r, "cites_leads")}
    assert cited == {"cccccccc", "dddddddd"}
    # 配了模型:多一段研判,编造的编号标「未核到」
    class W(type(ctx.clf)):
        label = "测试模型"
        def write(self, system, user, fields):
            return {"insight": "海水淡化厂招标值得跟 [cccccccc];另有一条编号不存在 [12345678]。"}
    ctx.clf = W()
    md2, _, _, _ = brief.build_daily(ctx, TODAY)
    assert "## 模型研判" in md2 and "[cccccccc]" in md2 and "12345678(未核到)" in md2 and "1 个编号在今日数据里没找到" in md2


def test_daily_brief_tells_sections_apart_shows_rmb_and_ai_suggested_opps(env, monkeypatch):  # noqa: F811
    """同一项目的不同标段只有结尾不同:截短时留尾;金额带折合人民币;机器推荐的商机和人立项的分开说。"""
    from datetime import date as _date
    from core.jobs import brief
    fake, admin, abase, ctx = env
    c, o, l1, l2 = _seed(abase)
    opp = abase.create("opportunity", [{"code": "MAR-099", "name_zh": "推荐的", "stage": "建议"}])[0]
    today = _date.fromisoformat(tstore.day(abase.record("opportunity", opp["id"])["createdTime"], abase.tz))  # 新建商机按创建时间算
    head = "TRAVAUX D'ELARGISSEMENT ET MISE A NIVEAU DE LA ROUTE D'OURIKA (RP 2017) - TRAVAUX DE VOIRIE, ASSAINISSEMENT ET GENIE CIVIL"
    abase.create("lead", [{"key": "ffffffff", "title": head + " - TRONÇON 2", "url": "https://example.org/f", "relevant": True, "category": "招标",
                           "country": c["id"], "found_at": f"{today.isoformat()}T01:00:00.000Z", "amount": "51,953,580 MAD", "amount_cny": 3700,
                           "status": "待处理", "origin": "机器采集"}])
    md, _, _, _ = brief.build_daily(ctx, today)
    line = next(x for x in md.splitlines() if "[ffffffff]" in x)
    assert "TRONÇON 2" in line and "…" in line and "51,953,580 MAD(约 3,700 万元)" in line
    assert "今日 AI 新推荐商机 1 个(MAR-099 推荐的);新立项跟踪 " in md and "今日新建商机" not in md


def test_daily_brief_compares_with_median_of_past_week(env, monkeypatch):  # noqa: F811
    """一次批量导入(新信源第一次跑进来上千条)不该让平常的一天看着像「明显低于」:基准是前 7 天的中位数,不含今天。"""
    from datetime import timedelta
    from core.jobs import brief
    fake, admin, abase, ctx = env
    per_day = {1: 20, 2: 13, 3: 3, 4: 300, 5: 9, 6: 28, 7: 20}  # 前 7 天;中位数 20
    rows = [{"key": f"k{i}-{j:03d}", "title": f"t{i}-{j}", "url": f"https://example.org/{i}/{j}", "found_at": f"{(TODAY - timedelta(days=i)).isoformat()}T01:00:00.000Z",
             "status": "待处理", "origin": "机器采集"} for i, n in per_day.items() for j in range(n)]
    rows += [{"key": f"t-{j:03d}", "title": f"today {j}", "url": f"https://example.org/t/{j}", "found_at": f"{TODAY.isoformat()}T01:00:00.000Z",
              "status": "待处理", "origin": "机器采集"} for j in range(16)]
    for i in range(0, len(rows), 100):
        abase.create("lead", rows[i:i + 100])
    md, _, _, _ = brief.build_daily(ctx, TODAY)
    assert "今日新增 16 条" in md and "低于前 7 天的一般水平(每天 20 条,取中位数)" in md and "明显低于" not in md


def test_daily_brief_requested_for_a_past_day_uses_that_day(env, monkeypatch):  # noqa: F811
    fake, admin, abase, ctx = env
    monkeypatch.setattr(reports, "_to_docx", _fake_docx)
    _seed(abase)
    r = abase.create("report", [{"kind": "日报", "period": "2026-10-03", "day": TODAY.isoformat(), "gen_status": "待生成"}])[0]
    assert reports.run(ctx) == {"报告完成": 1, "报告失败": 0}
    r = abase.record("report", r["id"])
    assert "# 线索日报 2026-10-03" in _val(abase, "report", r, "summary") and "今日新增 1 条" in _val(abase, "report", r, "summary")  # bbbbbbbb 是 10-03 发现的
    bad = abase.create("report", [{"kind": "日报", "period": "上周", "day": TODAY.isoformat(), "gen_status": "待生成"}])[0]
    assert reports.run(ctx) == {"报告完成": 0, "报告失败": 1}
    assert _val(abase, "report", abase.record("report", bad["id"]), "gen_status") == "失败"


def test_internal_country_and_project_reports_append_company_profile(env, monkeypatch):  # noqa: F811
    """A23:勾了「写进报告」的公司资料按适用国别与领域附在国别 / 项目报告的内部版后;公开版不附;没勾的不附。"""
    fake, admin, abase, ctx = env
    monkeypatch.setattr(reports, "_to_docx", _fake_docx)
    c, o, l1, l2 = _seed(abase)
    abase.update("opportunity", o["id"], {"sectors": ["污水"]})
    other = abase.create("country", [{"name_zh": "格鲁吉亚", "ISO3": "GEO"}])[0]
    abase.create("company", [
        {"kind": "资质", "name": "市政公用工程施工总承包特级", "year": 2020, "for_report": True},                      # 全球通用、各领域
        {"kind": "业绩", "name": "巴库净水厂扩建", "countries": [c["id"]], "sectors": ["供水"], "year": 2023, "amount": "1.2 亿美元", "for_report": True},
        {"kind": "业绩", "name": "第比利斯污水厂", "countries": [other["id"]], "sectors": ["污水"], "year": 2022, "for_report": True},  # 别国
        {"kind": "其他", "name": "公司简介", "content": "中英文简介", "for_report": False},                               # 没勾
    ])
    abase.create("report", [{"kind": "国别报告", "country": c["id"], "day": "2026-10-05", "edition": "内部版", "gen_status": "待生成"},
                            {"kind": "国别报告", "country": c["id"], "day": "2026-10-05", "edition": "公开版", "gen_status": "待生成"},
                            {"kind": "项目报告", "opportunity": o["id"], "day": "2026-10-05", "gen_status": "待生成"}])
    assert reports.run(ctx) == {"报告完成": 3, "报告失败": 0}
    mds = {}
    for r in _rows(abase, "report"):
        key = _val(abase, "report", r, "key")
        mds[_val(abase, "report", r, "kind") + str(_val(abase, "report", r, "edition") or "")] = (ctx.cache.root.parent / "reports" / key.replace(".docx", ".md")).read_text(encoding="utf-8")
    internal = mds["国别报告内部版"]
    assert "## 附录 C 我方资质与相关业绩" in internal and "市政公用工程施工总承包特级" in internal and "巴库净水厂扩建" in internal
    assert "第比利斯污水厂" not in internal and "公司简介" not in internal
    assert "我方资质" not in mds["国别报告公开版"]
    project = mds["项目报告"]  # 商机领域污水:供水的业绩不附,全球通用的资质附
    assert "市政公用工程施工总承包特级" in project and "巴库净水厂扩建" not in project and "第比利斯污水厂" not in project


class _FakeTranslator:
    enabled, label = True, "fake"

    def __init__(self):
        self.calls = 0

    def ask(self, system, user, schema, guide):
        import json
        self.calls += 1
        return {"items": [{"i": x["i"], "zh": "译:" + x["t"]} for x in json.loads(user)]}

    def write(self, system, user, fields):
        return None


def test_country_report_has_clickable_sources_chinese_titles_and_bank_names(env, monkeypatch):  # noqa: F811
    fake, admin, abase, ctx = env
    monkeypatch.setattr(reports, "_to_docx", _fake_docx)
    c, o, l1, l2 = _seed(abase)
    l3 = abase.create("lead", [{"key": "cccccccc", "title": "Construction of pumping station [Lot 2]", "url": "https://example.org/c", "relevant": True,
                                "category": "意向书", "country": c["id"], "found_at": "2026-10-04T02:00:00.000Z", "origin": "机器采集"}])[0]
    abase.update("country", c["id"], {"basics": "首都:巴库(维基百科 https://zh.wikipedia.org/zh-cn/%E5%B7%B4)\n世行区域 / 贷款类别:欧洲与中亚 / IBRD",
                                      "income_group": "中等偏上收入;IBRD", "water_agencies": "水务公司 · Azersu · https://www.azersu.az/en"})
    tr = _FakeTranslator()
    ctx.clf = tr
    md, *_ = reports.build_country(ctx, abase.get("country", abase.record("country", c["id"]), "key"))
    md = reports.expand_acronyms(md)
    # 基本情况成表,来源名是可点的链接,网址不再印成正文;机构成表,网址变成可点的网站名
    assert "| 首都 | 巴库 [维基百科](<https://zh.wikipedia.org/zh-cn/%E5%B7%B4>) |" in md
    assert "| 水务公司 | Azersu |  | [azersu.az](<https://www.azersu.az/en>) |" in md
    # 国别报告不列线索(使用方 2026-10-10),线索的标题与网址都不出现;出处线索号也去掉
    assert "example.org/a" not in md and "example.org/c" not in md and "[aaaaaaaa]" not in md and tr.calls == 0
    # 线索清单(周报等)标题中文在前、点到原文;没有中文的请模型译(只译一次,存本机缓存)
    rows = str(reports._lead_rows(ctx, [abase.record("lead", l1["id"]), abase.record("lead", l3["id"])]))
    assert "[甘贾污水厂意向书](<https://example.org/a>)(原文:Ganja WWTP EOI)" in rows
    assert r"[译:Construction of pumping station \\[Lot 2\\]](<https://example.org/c>)" in rows and tr.calls == 1
    reports._lead_rows(ctx, [abase.record("lead", l3["id"])])
    assert tr.calls == 1
    # 银行缩写第一次出现时补中文全称,后面不重复
    assert md.count("IBRD(国际复兴开发银行)") == 1 and "IBRD" in md.split("IBRD(国际复兴开发银行)", 1)[1]
    assert l3


def test_word_links_are_real_hyperlinks(tmp_path):
    import pytest
    pytest.importorskip("pypandoc")
    docx = pytest.importorskip("docx")
    md = tmp_path / "r.md"
    md.write_text("# 报告\n\n- 首都:巴库([维基百科](<https://zh.wikipedia.org/wiki/x>))\n\n| 标题 |\n|---|\n| [中文](<https://example.org/a>) |\n", encoding="utf-8")
    from core.watch import reporting
    out = reporting.to_docx_styled(md, tmp_path / "r.docx", template=tmp_path / "none.docx")
    d = docx.Document(str(out))
    rels = [r.target_ref for r in d.part.rels.values() if r.reltype.endswith("/hyperlink")]
    assert "https://zh.wikipedia.org/wiki/x" in rels and "https://example.org/a" in rels
    assert "https://" not in "\n".join(p.text for p in d.paragraphs)


def test_fact_rows_parse_value_year_source_and_tone():
    ind = ("GDP(现价):1,470 亿美元(2025)(世行 WDI https://data.worldbank.org/x)\nGDP 实际增速:-0.3%(2025)(世行 WDI https://data.worldbank.org/y)\n"
           "淡水压力(取水/可再生资源):123.0%(2022)(世行 WDI https://data.worldbank.org/w)")
    rows = reports.fact_rows(ind)
    assert rows[0] == {"k": "GDP(现价)", "v": "1,470 亿美元", "year": "2025", "src": "世行 WDI", "url": "https://data.worldbank.org/x", "bad": None}
    assert rows[1]["bad"] == "负增长" and rows[2]["bad"] == "取水 ≥ 75% 可再生量"
    md = reports.kpi_table(reports._kpis(rows))
    assert '| 1,470 [亿美元]{custom-style="KPI Unit"} | -0.3% |' in md and "Table: 关键指标" in md
    assert "GDP 实际增速(2025) 超警戒线:负增长" in md and "▼" not in md  # 写明越过了哪条线,不用看不懂的符号


class _FakeWriter:
    enabled, label = True, "fake"

    def write(self, system, user, fields):
        return {k: f"〔{k}〕据数据分析 [aaaaaaaa]" for k in fields}

    def ask(self, *a):
        return None


def test_country_report_follows_the_azerbaijan_outline(env, monkeypatch):  # noqa: F811
    from core.jobs import country_report
    fake, admin, abase, ctx = env
    c, o, l1, l2 = _seed(abase)
    abase.update("country", c["id"], {"indicators": "GDP(现价):759 亿美元(2025)(世行 WDI https://data.worldbank.org/x)\n"
                                                    "GDP 实际增速:-0.3%(2025)(世行 WDI https://data.worldbank.org/y)",
                                      "basics": "首都:巴库(维基百科 https://zh.wikipedia.org/x)", "income_group": "中等偏上收入;IBRD"})
    iso = abase.get("country", abase.record("country", c["id"]), "key")
    md, cl, co, scope = reports.build_country(ctx, iso)  # 没配模型:只出数据,分析从略
    assert "本报告未使用模型" in md and "〔" not in md
    titles = [t for t, _ in country_report.OUTLINE]
    heads = re.findall(r"^# 第 \d+ 章 (.+)$", md, re.MULTILINE)
    assert heads and all(h in titles for h in heads) and heads == [t for t in titles if t in heads]  # 章的顺序与成稿一致
    assert md.index("# 摘要") < md.index("# 第 1 章") and "# 附录" in md and "**目\u3000录**" in md
    assert '1,470' not in md and '759 [亿美元]{custom-style="KPI Unit"}' in md
    assert "B-READY 2025 这一期没有覆盖" in md  # 营商环境(v2.23):没覆盖的写明
    abase.update("country", c["id"], {"bready_total": 66, "bready_water": 79.3, "bready_procure": 45.7, "water_tariff": 2, "water_tariff_cur": "AZN", "water_tariff_usd": 1.18,
                                      "foreign_bid": "部分招标有限制", "award_days": 365, "gov_pay_days": 7, "bready_edition": "B-READY 2025(数据版本 December 10, 2025)"})
    md, *_ = reports.build_country(ctx, iso)
    sec = md[md.index("营商环境(世界银行 B-READY)"):]
    assert "总分 66.0 分;需要留意的是公共采购得分、外企参标限制、大型工程授标天数" in sec and "约 1.18 美元/m³(2 AZN/m³)" in sec
    assert "45.7 分(超警戒线:低于 50 分,偏弱)" in sec and "365 天(超警戒线:超过 180 天,授标慢)" in sec and "7 天 |" in sec
    ctx.clf = _FakeWriter()
    md, *_ = reports.build_country(ctx, iso)
    assert "〔summary〕" in md and "〔risks〕" in md and "〔conclusion〕" in md and "## 2.3 宏观经济" in md
    assert scope == iso and cl == {l1["id"]} and co == {o["id"]}  # 只记商机的出处线索
    assert "已立项跟踪的商机" in md and "AZE-001" in md and "example.org/b" not in md and "巴库供水招标" not in md


def test_template_outline_lists_every_chapter():
    from core.jobs import country_report
    md = country_report.outline_md()
    for n, (t, secs) in enumerate(country_report.OUTLINE, 1):
        assert f"# 第 {n} 章 {t}" in md and f"## {n}.1 {secs[0][0]}" in md


def test_negative_thresholds_match_the_country_page():
    import re
    from core.country_facts import TONES
    js = (Path(__file__).resolve().parents[1] / "portal" / "portal.js").read_text(encoding="utf-8")
    block = js[js.index("TONES: ["):js.index("\n    ],", js.index("TONES: ["))]
    assert re.findall(r'"([^"]+)"\]', block) == [why for _, _, why in TONES]


def test_kpi_cards_and_red_marks_in_word(tmp_path):
    import pytest
    pytest.importorskip("pypandoc")
    docx = pytest.importorskip("docx")
    from core.watch import reporting
    md = tmp_path / "r.md"
    md.write_text("# 报告\n\n" + reports.kpi_table([("GDP", {"v": "1,470 亿美元", "year": "2025", "bad": None}),
                                                     ("增速", {"v": "-0.3%", "year": "2025", "bad": "负增长"})])
                  + "\n| 指标 | 数值 |\n|---|---|\n| 外债/GNI | 60.5%(超警戒线:外债 ≥ 60% GNI) |\n", encoding="utf-8")
    d = docx.Document(str(reporting.to_docx_styled(md, tmp_path / "r.docx", template=tmp_path / "none.docx")))
    kpi, other = d.tables
    assert "关键指标" not in "\n".join(p.text for p in d.paragraphs)  # 表格标题段落删掉了
    big = [r for p in kpi.rows[1].cells[0].paragraphs for r in p.runs]
    assert big[0].font.size.pt == 18 and big[0].font.bold
    red = lambda c: {str(r.font.color.rgb) for p in c.paragraphs for r in p.runs}  # noqa: E731
    assert red(kpi.rows[1].cells[1]) == {"C00000"} and red(other.rows[1].cells[1]) == {"C00000"}


class _Capture:
    enabled, label = True, "fake"

    def __init__(self, answers=None):
        self.data, self.answers = [], answers or {}

    def write(self, system, user, fields):
        self.data.append(user)
        return {k: self.answers.get(k, "") for k in fields}


def test_project_report_internal_chinese_and_public_english(env):  # noqa: F811
    """项目报告按《项目分析报告》模板:内部版中文报领导;公开版英文发伙伴,不给模型人写的内容(R9),没有「请领导决策」。"""
    from core.jobs import opp_report
    fake, admin, abase, ctx = env
    c, o, l1, l2 = _seed(abase)
    abase.update("opportunity", o["id"], {"internal_note": "建议动作:约见业主(到期 2026-10-31)", "name_local": "Ganja Wastewater Treatment Plant",
                                          "sectors": ["污水"]})
    cap = _Capture({"project": "甘贾新建污水厂,亚行贷款 5,000 万美元 [1]。", "phases": "\n".join(f"{p} | 未开始 | — | —" for p in opp_report.PR.PHASES)})
    ctx.clf = cap
    zh, cited, code = opp_report.build(ctx, o["id"], internal=True)
    assert zh["lang"] == "zh" and zh["to"] == "公司领导" and code == "AZE-001" and cited == {l1["id"]}
    assert zh["core"][0][1] == "甘贾新建污水厂,亚行贷款 5,000 万美元 [1]。" and zh["sources"][0][1]["url"] == "https://example.org/a"
    assert zh["actions"][0][:3] == ("约见业主", "【待定】", "2026-10-31") and zh["leader"]  # 人写的下一步进内部版
    assert "内部备注" in cap.data[0] and "[aaaaaaaa]" not in zh["basics"][4][1][0]  # 线索号换成来源编号
    cap.data.clear()
    en, _, _ = opp_report.build(ctx, o["id"], internal=False)
    assert en["lang"] == "en" and en["to"] == "Partners in Azerbaijan" and en["field"] == "Wastewater" and en["leader"] == []
    assert en["basics"][0][0] == "Name and ID" and en["phases"][0][0] == "Preparation"
    assert all("内部" not in d and "约见业主" not in d for d in cap.data)  # 公开版不给模型人写的内容
    tpl = opp_report.TEMPLATES["zh"]
    if tpl.exists():  # 私有仓的模板在时连 Word 一起出(中英文各一份)
        import docx
        from scripts.make_project_template_en import main as make_en
        en_tpl = Path(ctx.cache.root.parent / "en.docx")
        make_en(str(tpl), str(en_tpl))
        for content, t, want in ((zh, tpl, "一、核心结论"), (en, en_tpl, "1. Key Findings")):
            out = opp_report.PR.to_docx(content, ctx.cache.root.parent / f"{content['lang']}.out.docx", t)
            text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)
            assert want in text and "【" not in text.split(want)[1]
