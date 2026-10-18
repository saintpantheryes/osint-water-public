"""投标评估(2026-10-18 使用方:发一个项目链接,助手直接给能不能投的结论并附报告):八项检查 → 三档结论,每条理由带依据;Word 报告。"""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import httpx
import pytest

from core import bid_check as B
from core import link_check as L
from core import schema, tstore
from core.assistant_server import Assistant, serve
from core.markets import load_markets
from core.watch.fetch import Page
from tests.teable_fake import FakeTeable
from tests.test_link_check import HTML, MODEL, URL, _clf
from tests.test_link_check import ONTO, TMAP

TODAY = date(2026, 10, 18)
RECORDS = {"本单位": [{"id": "r1", "name": "尼泊尔Bheri Babai引水隧道项目", "year": 2020, "usd": 107_330_000, "amount": "", "sectors": ["水利"]},
                    {"id": "r2", "name": "摩洛哥西迪斯里玛尼省排水整治项目", "year": 2023, "usd": 650_000, "amount": "", "sectors": ["污水"]}],
           "兄弟单位": [{"id": "r3", "name": "中铁一局某市污水厂", "year": 2023, "usd": 90_000_000, "amount": "", "sectors": ["污水"]}]}
MARKETS = """schema: osint-water/markets@1
name: 测试市场分布
org: 测试公司
regions:
- name: 中亚片区
  countries:
  - {iso3: UZB, name: 乌兹别克斯坦, local_entity: true, projects: true, staff: true, lead: true}
  - {iso3: TJK, name: 塔吉克斯坦, local_entity: false, projects: false, staff: false, lead: false, note: 由乌兹别克国别管理}
  - {iso3: AZE, name: 阿塞拜疆, staff: true}
"""
WB = {"kind": "世行采购公告", "title": "Request for Bids: NRWWP-7 Namangan wastewater networks", "url": "https://projects.worldbank.org/notice/7", "date": "2026-10-01"}


@pytest.fixture
def scope(tmp_path):
    f = tmp_path / "markets.yaml"
    f.write_text(MARKETS, encoding="utf-8")
    return B.Scope.from_markets(load_markets(f))


def _facts(**kw):
    base = {"title": "纳曼干污水管网", "url": "https://projects.worldbank.org/notice/7", "country": "UZB", "sectors": ["污水"], "stage": "招标", "category": "招标",
            "deadline": "2026-11-20", "requirements": {"similar_usd": 500_000, "turnover_usd": None, "financial_usd": None, "years": 5, "specific": None},
            "credibility": {"level": "高", "reason": "世行一手公告"}, "source": {"level": "高", "label": "世界银行", "domain": "worldbank.org"},
            "original": WB, "sure": True, "outside": [], "verify_links": [], "pros": ["资金已落实"], "cons": []}
    return {**base, **kw}


def _by(bid):
    return {c["key"]: c for c in bid["checks"]}


def test_scope_from_markets_knows_lead_unit_and_presence(scope):
    assert scope.known() and scope.org == "测试公司"
    assert scope.get("UZB")["lead"] is True and scope.get("UZB")["any_presence"] and scope.get("TJK")["lead"] is False and not scope.get("TJK")["any_presence"]
    assert scope.get("AZE")["lead"] is None and scope.get("AZE")["any_presence"] and scope.get("KEN") is None
    assert not B.Scope.from_markets(None).known()


def test_all_checks_pass_means_bid(scope):
    bid = B.assess(_facts(), TODAY, RECORDS, scope, {})
    assert bid["verdict"] == "建议投标" and bid["blockers"] == [] and bid["conditions"] == [] and bid["days_left"] == 33
    by = _by(bid)
    assert [c["key"] for c in bid["checks"]] == ["sector", "source", "scope", "stage", "time", "quals", "library", "presence"]
    assert by["source"]["status"] == "通过" and by["source"]["evidence"][0]["url"] == WB["url"]
    assert by["scope"]["status"] == "通过" and "测试公司是主责单位" in by["scope"]["why"]
    assert by["time"]["status"] == "通过" and "还有 33 天" in by["time"]["why"]
    assert by["quals"]["status"] == "通过" and "摩洛哥西迪斯里玛尼省排水整治项目」65 万美元(2023),够" in by["quals"]["why"]
    assert by["library"]["status"] == "不明" and "新线索" in by["library"]["why"]
    assert by["presence"]["status"] == "通过" and "已设当地机构" in by["presence"]["why"]


def test_hard_failures_mean_no_bid(scope):
    # 已截止
    bid = B.assess(_facts(deadline="2026-10-10"), TODAY, RECORDS, scope, {})
    assert bid["verdict"] == "不建议投标" and bid["blockers"] == ["时间是否来得及:已于 2026-10-10 截止(过了 8 天)"] and bid["summary"].startswith("不建议投标:时间是否来得及")
    # 忽略国别
    assert _by(B.assess(_facts(country="KEN"), TODAY, RECORDS, scope, {}))["scope"]["status"] == "不通过"
    # 已授标
    by = _by(B.assess(_facts(stage="授标", category="授标或短名单"), TODAY, RECORDS, scope, {}))
    assert by["stage"]["status"] == "不通过" and "不是投标机会" in by["stage"]["why"]
    # 非水务
    assert _by(B.assess(_facts(sectors=["其他"]), TODAY, RECORDS, scope, {}))["sector"]["status"] == "不通过"
    # 业绩不够:兄弟单位的只作参考
    bid = B.assess(_facts(requirements={"similar_usd": 12_000_000, "turnover_usd": None, "financial_usd": None, "years": 5, "specific": None}), TODAY, RECORDS, scope, {})
    assert bid["verdict"] == "不建议投标" and "中铁一局某市污水厂" in _by(bid)["quals"]["why"] and bid["qualify"]["verdict"] == "不满足"


def test_conditions_mean_bid_with_conditions(scope):
    # 媒体转述、没找到原公告、截止日近、门槛没写、营业额要财务部出数
    f = _facts(url="https://www.gazeta.uz/ru/1", source={"level": "中", "label": "Gazeta.uz", "domain": "gazeta.uz"}, credibility={"level": "中", "reason": "转述"},
               original=None, sure=False, deadline="2026-10-30", outside=[{"kind": "新闻(近三个月)", "title": "Namangan sewer works", "url": "https://news.example/1"}],
               verify_links=[{"name": "到世行采购网找", "url": "https://projects.worldbank.org/en/projects-operations/procurement?qterm=NRWWP-7"}],
               requirements={"similar_usd": None, "turnover_usd": 10_000_000, "financial_usd": None, "years": None, "specific": None})
    bid = B.assess(f, TODAY, RECORDS, scope, {})
    by = _by(bid)
    assert bid["verdict"] == "有条件可投" and bid["blockers"] == []
    assert by["source"]["status"] == "有条件" and "只有新闻旁证" in by["source"]["why"] and [e["url"] for e in by["source"]["evidence"]] == ["https://www.gazeta.uz/ru/1", "https://news.example/1", f["verify_links"][0]["url"]]
    assert by["time"]["status"] == "有条件" and "只剩 12 天" in by["time"]["why"] and "四到六周" in by["time"]["why"]
    assert by["quals"]["status"] == "不明" and "没写单个类似合同的金额门槛" in by["quals"]["why"] and "要财务部出数" in by["quals"]["why"]
    assert bid["conditions"][0].startswith("信源是否真实:Gazeta.uz") and any(c.startswith("时间是否来得及:2026-10-30 截止,只剩 12 天") for c in bid["conditions"])
    # 媒体转述但世行渠道里有对得上的项目:信源通过;意向书截止近只算抓紧
    f2 = _facts(source={"level": "中", "label": "Gazeta.uz", "domain": "gazeta.uz"}, original=None, sure=False, category="意向书", deadline="2026-10-28",
                outside=[{"kind": "世行项目(Active)", "title": "Namangan sewerage", "url": "https://projects.worldbank.org/p/P1"}])
    by = _by(B.assess(f2, TODAY, RECORDS, scope, {}))
    assert by["source"]["status"] == "通过" and "世行渠道里有对得上的项目" in by["source"]["why"] and "抓紧能赶上" in by["time"]["why"]
    # 没认出国别、没抽到截止日、还没招标:都是有条件;主责不是本单位也是
    by = _by(B.assess(_facts(country=None, deadline=None, stage="融资", category="融资"), TODAY, RECORDS, scope, {}))
    assert by["scope"]["status"] == "不明" and by["time"]["status"] == "不明" and by["stage"]["status"] == "有条件" and by["presence"]["status"] == "不明"
    by = _by(B.assess(_facts(country="TJK"), TODAY, RECORDS, scope, {}))
    assert by["scope"]["status"] == "有条件" and "不归本单位主责" in by["scope"]["why"] and by["presence"]["status"] == "有条件" and "从总部派" in by["presence"]["why"]
    # 没有公司资料:资格不明;没有市场分布表:国别不明
    by = _by(B.assess(_facts(), TODAY, {"本单位": [], "兄弟单位": []}, B.Scope(None), {}))
    assert by["quals"]["status"] == "不明" and "补业绩" in by["quals"]["why"] and by["scope"]["status"] == "不明" and "市场分布表" in by["scope"]["why"]
    # 库里:已在库 / 相关商机已放弃
    by = _by(B.assess(_facts(), TODAY, RECORDS, scope, {"same_url": [{"title": "x", "url": URL, "opportunity": "纳曼干污水"}]}))
    assert by["library"]["status"] == "通过" and "已归入商机「纳曼干污水」" in by["library"]["why"]
    by = _by(B.assess(_facts(), TODAY, RECORDS, scope, {"related_opps": [{"code": "UZB-003", "name_zh": "纳曼干污水", "stage": "放弃"}]}))
    assert by["library"]["status"] == "有条件" and "之前已放弃或未中标" in by["library"]["why"]


def test_money_parsing_and_conversion(monkeypatch):
    from core import assistant
    monkeypatch.setattr(assistant, "_rate", lambda cur: {"USD": 7.0, "EUR": 8.0, "CNY": 1.0, "UZS": 0.0005}.get(cur, 1.0))
    assert B.parse_money("12.3 million USD") == (12_300_000, "USD") and B.parse_money("USD 200 million") == (200_000_000, "USD")
    assert B.parse_money("EUR 4.2m") == (4_200_000, "EUR") and B.parse_money("1.5 亿美元") == (150_000_000, "USD") and B.parse_money("5,000 万元") == (50_000_000, "CNY")
    assert B.parse_money("1.5 million m3/day") is None and B.parse_money("") is None and B.parse_money("约 300 公里管网") is None
    assert B.money_line("12.3 million USD") == "12.3 million USD(≈ 8,610 万元人民币)"
    assert B.money_line("EUR 4.2m") == "EUR 4.2m(≈ 480 万美元,≈ 3,360 万元人民币)"
    assert B.money_line("1.5 million m3/day") == "1.5 million m3/day"


def _result(bid, **kw):
    base = {"ok": True, "url": URL, "final_url": URL, "title": "Surum WWTP tender", "published": "2026-10-01", "country": {"iso3": "UZB", "name": "乌兹别克斯坦"},
            "source": {"level": "高", "label": "世界银行", "domain": "worldbank.org"},
            "analysis": {**MODEL, "title_zh": "塔什干苏鲁姆污水厂招标", "amount": "USD 200 million", "deadline": "2026-11-20", "jv": "允许", "pros": ["资金已落实"], "cons": ["工期紧"],
                         "requirements": {"similar_usd": 500_000, "turnover_usd": 10_000_000, "financial_usd": None, "years": 5, "specific": "日处理 10 万吨以上"}, "model": "deepseek-flash"},
            "same_url": [], "related_leads": [{"title": "Surum WWTP feasibility", "url": "https://other.example/a", "country": "乌兹别克斯坦", "found_at": "2026-09-01", "status": "待处理", "project_name": "苏鲁姆污水厂可研"}],
            "related_opps": [{"code": "UZB-001", "name_zh": "塔什干 Surum 污水厂", "stage": "跟踪中"}], "outside": [], "bid": bid}
    return {**base, **kw}


def test_single_report_has_conclusion_first_and_superscript_links(scope, tmp_path, monkeypatch):
    from core import assistant
    monkeypatch.setattr(assistant, "_rate", lambda cur: {"USD": 7.0}.get(cur, 1.0))
    bid = B.assess(_facts(url=URL, requirements=_result(None)["analysis"]["requirements"]), TODAY, RECORDS, scope, {"related_opps": [{"code": "UZB-001", "name_zh": "塔什干 Surum 污水厂", "stage": "跟踪中"}]})
    md = B.report_markdown(_result(bid), TODAY)
    assert md.startswith("# 投标评估:塔什干苏鲁姆污水厂招标\n") and "## 核心结论\n\n**建议投标。**" in md
    assert md.index("## 核心结论") < md.index("## 1. 项目基本情况") < md.index("## 2. 八项检查") < md.index("## 3. 招标要求与我方资格对照") < md.index("## 7. 信源与依据")
    assert "USD 200 million(≈ 140,000 万元人民币)" in md and "2026-11-20(剩 33 天)" in md and "| 联合体 | 允许 |" in md
    assert f"^[1](<{URL}>)^" in md and f"1. [塔什干苏鲁姆污水厂招标](<{URL}>)  expo.example.uz" in md and "2. [Request for Bids: NRWWP-7 Namangan wastewater networks](<https://projects.worldbank.org/notice/7>)  projects.worldbank.org" in md  # 角标 + 文末依据
    assert "| 单个类似合同金额 | 50 万美元 |" in md and "年均营业额要 1,000 万美元:系统里没有营业额数据,要财务部出数" in md and "日处理 10 万吨以上" in md
    assert "- 资金已落实" in md and "- 工期紧" in md and "| UZB-001 | 塔什干 Surum 污水厂 | 跟踪中 |" in md
    # 不建议的:结论里写原因,下一步是不投
    bad = B.assess(_facts(url=URL, deadline="2026-10-01"), TODAY, RECORDS, scope, {})
    md2 = B.report_markdown(_result(bad), TODAY)
    assert "**不建议投标。**不建议投标:时间是否来得及:已于 2026-10-01 截止(过了 17 天)。" in md2 and "1. 不投;" in md2 and "投标前要落实的" not in md2
    # 真出一份 Word:角标成了超链接
    out = tmp_path / "r.docx"
    B.to_docx(md, out)
    from docx import Document
    doc = Document(str(out))
    assert out.stat().st_size > 5000 and any("核心结论" in p.text for p in doc.paragraphs)
    rels = [r.target_ref for r in doc.part.rels.values() if "hyperlink" in r.reltype]
    assert URL in rels


def test_list_report_tabulates_every_item(scope):
    items = [{"ref": "NRWWP-7", "title": "NRWWP-7 wastewater", "title_zh": "纳曼干污水管网", "country": {"iso3": "UZB", "name": "乌兹别克斯坦"}, "deadline": "2026-11-20", "amount": "12.3 million USD",
              "funder": "World Bank", "stage": "招标", "sectors": ["污水"], "status": "建议入库", "original": WB, "sure": True, "why": "找到原公告", "requirements": None,
              "bid": B.assess(_facts(), TODAY, RECORDS, scope, {})},
             {"ref": "SLSDP/RFB/16", "title": "Veterinary clinic", "title_zh": "兽医诊所", "country": {"iso3": "UZB", "name": "乌兹别克斯坦"}, "deadline": None, "amount": None, "funder": "IDA",
              "stage": None, "sectors": ["其他"], "status": "非水务", "original": None, "sure": False, "why": "不是水务项目", "requirements": None,
              "bid": B.assess(_facts(sectors=["其他"], deadline=None), TODAY, RECORDS, scope, {})}]
    md = B.list_report_markdown({"ok": True, "title": "Tender monitoring", "analysis": {"title_zh": "招标监测清单", "model": "deepseek-flash"}, "list": {"items": items, "n": 2}}, TODAY)
    assert md.startswith("# 投标评估(清单):招标监测清单\n") and "**建议投标 1 条、有条件可投 0 条、不建议投标 1 条**(其中非水务 1 条)" in md
    assert "| 1 | 纳曼干污水管网^[1](<https://projects.worldbank.org/notice/7>)^ | 乌兹别克斯坦 | 2026-11-20 | 建议投标 |" in md
    assert "| 2 | 兽医诊所 | 乌兹别克斯坦 | — | 不建议投标 | 是不是水务项目:不是水务项目(领域:其他) |" in md
    assert "## 2. 1. 纳曼干污水管网" in md and "## 3. 2. 兽医诊所" in md and "## 4. 信源与依据" in md


# ---------- 接口:看链接的结果带 bid,报告按任务编号下载 ----------

@pytest.fixture
def db():
    fake = FakeTeable()
    admin = tstore.Client("https://water.example", "admin-token", transport=fake.transport, sleep=lambda s: None)
    schema.apply(admin, schema.build_plan(ONTO, TMAP, {}), "水务项目机会 新版", log=lambda *a: None)
    b = tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    b.create("country", [{"name_zh": "乌兹别克斯坦", "ISO3": "UZB", "tier": "市场", "presence": "已设当地机构、有在建项目"}, {"name_zh": "肯尼亚", "ISO3": "KEN", "tier": "其他"}])
    b.create("company", [{"kind": "业绩", "owner": "本单位", "name": "摩洛哥排水整治", "year": 2023, "amount": "折 65万美元", "sectors": ["污水"]}])
    return b


def test_check_link_returns_bid_and_the_scope_falls_back_to_the_country_table(db):
    reply = {**MODEL, "sectors": ["污水"], "stage": "招标", "category": "招标", "deadline": "2026-12-01", "pros": ["资金已落实"], "cons": [], "jv": "允许",
             "requirements": {"similar_usd": 500_000, "turnover_usd": None, "financial_usd": None, "years": 5, "specific": None}}
    out = L.check("https://projects.worldbank.org/notice/7", "", TODAY, _clf(reply, []), db, fetch=lambda u: Page("https://projects.worldbank.org/notice/7", 200, HTML, "text/html"),
                  outside=lambda q, iso: ([], []), scope=B.Scope.from_db(db))
    bid = out["bid"]
    assert bid["verdict"] == "建议投标"  # 没有市场分布表时按国别表:UZB 是市场、有当地机构;库里只有相关线索不算条件
    by = _by(bid)
    assert by["scope"]["status"] == "通过" and by["presence"]["status"] == "通过" and "已设当地机构" in by["presence"]["why"] and by["source"]["status"] == "通过"
    assert by["quals"]["status"] == "通过" and by["time"]["status"] == "通过" and by["library"]["status"] == "不明" and out["analysis"]["deadline"] == "2026-12-01"
    assert bid["conditions"] == [] and bid["blockers"] == []
    assert "deadline" in L.SCHEMA["properties"] and "pros" in L.SCHEMA["properties"] and "jv" in L.GUIDE
    # 忽略国别的项目:不建议
    out = L.check(URL, "", TODAY, _clf({**reply, "country": "KEN"}, []), db, fetch=lambda u: Page(URL, 200, HTML, "text/html"), outside=lambda q, iso: ([], []), scope=B.Scope.from_db(db))
    assert out["bid"]["verdict"] == "不建议投标" and "肯尼亚不在市场国别内" in out["bid"]["blockers"][0]


def test_link_job_makes_a_report_the_page_downloads(db, monkeypatch):
    reply = {**MODEL, "sectors": ["污水"], "stage": "招标", "category": "招标", "deadline": "2026-12-01", "requirements": None}
    bot = Assistant(None)
    bot.terms_base = lambda: db
    bot.clf = _clf(reply, [])
    bot.link_kw = {"fetch": lambda u: Page(URL, 200, HTML, "text/html"), "outside": lambda q, iso: ([], [])}
    made: list[str] = []
    bot.to_docx = lambda md, path: (made.append(md), path.write_bytes(b"PK-docx " + md[:40].encode()))
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, h = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1"}
        r = httpx.post(base + "/link", json={"url": URL, "job": True}, headers=h).json()
        for _ in range(100):
            st = httpx.get(base + "/link/" + r["job"], headers=h).json()
            if st["done"]:
                break
            time.sleep(0.05)
        assert st["ok"] and st["bid"]["verdict"] in B.VERDICTS and st["report"] == {"name": st["report"]["name"], "url": f"link/{r['job']}/report.docx"}
        assert st["report"]["name"].startswith("投标评估_") and st["report"]["name"].endswith(".docx") and made and made[0].startswith("# 投标评估:")
        assert httpx.get(base + f"/link/{r['job']}/report.docx").status_code == 403  # 没登录
        d = httpx.get(base + f"/link/{r['job']}/report.docx", headers=h)
        assert d.status_code == 200 and d.content.startswith(b"PK-docx") and "wordprocessingml" in d.headers["content-type"] and "filename*=UTF-8''" in d.headers["content-disposition"]
        assert httpx.get(base + "/link/nope/report.docx", headers=h).status_code == 404
    finally:
        srv.shutdown()
    assert json.dumps(st, ensure_ascii=False)  # 结果可序列化
