"""游客只读通道(core/guest.py + 助手接口 /guest/*):只放行本体 guest 节的对象与属性;报告只给定期报告的公开版;
跟进记录、成员、报告模板整表 403;filter / orderBy 里引用了不放行的字段 403;读回来的记录去掉不放行的字段;不带登录标记 403。"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import httpx
import pytest

from core import guest as guest_mod, tstore
from core.assistant_server import Assistant, serve
from tests.test_portal import _fake_with_schema, _seed


def _base_with_reports():
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    today = date.today()
    b.create("report", [{"kind": "周报", "day": today.isoformat(), "period": "2026-W41", "edition": "公开版", "gen_status": "完成", "key": "周报_2026-W41_公开版.docx"},
                        {"kind": "月报", "day": today.isoformat(), "period": "2026-09", "edition": "内部版", "gen_status": "完成", "key": "月报_2026-09_内部版.docx"},
                        {"kind": "项目报告", "day": today.isoformat(), "opportunity": o["id"], "edition": "公开版", "gen_status": "完成", "key": "项目报告_AZE-001_公开版.docx"}])
    b.update("opportunity", o["id"], {"internal_note": "内部:报价底线 4000 万", "priority": "A"})
    bot_base = tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    return fake, b, bot_base, c, o, ld


def test_policy_follows_the_ontology_guest_section():
    fake, b, bot_base, c, o, ld = _base_with_reports()
    p = guest_mod.Policy(bot_base)
    assert set(p.by_oid) == {"lead", "country", "opportunity", "milestone", "source", "city", "report", "supply_plant", "sewage_plant", "award", "contractor", "study", "water_event", "water_area", "law", "law_clause", "local_term", "discharge_limit"}  # 跟进记录、成员、报告模板不在
    e = p.by_oid["opportunity"]
    assert e["id"] == b.t["opportunity"] and b.fid("opportunity", "internal_note") in e["deny"] and b.fid("opportunity", "owner") in e["deny"]
    assert b.fid("opportunity", "code") in e["allowed"] and b.fid("opportunity", "internal_note") not in e["allowed"]
    r = p.by_oid["report"]
    assert r["rows"]["filterSet"] == [b.cond("report", "edition", "is", "公开版"), b.cond("report", "kind", "isAnyOf", ["周报", "月报", "日报", "招标监测"])]
    assert b.fid("report", "requested_by") in r["deny"]
    pub = p.public()
    assert pub["objects"]["report"]["rows"].startswith("版本 = 公开版") and "游客" in pub["note"] and "allowed" not in json.dumps(pub)
    with pytest.raises(guest_mod.Denied):
        p.table(b.t["followup"])
    with pytest.raises(guest_mod.Denied):  # 嵌套条件里引用了负责人
        p.check_fields(e, {"conjunction": "and", "filterSet": [{"conjunction": "or", "filterSet": [b.cond("opportunity", "owner", "isNotEmpty")]}]})
    p.check_fields(e, {"conjunction": "and", "filterSet": [b.cond("opportunity", "stage", "is", "跟踪中")]})


def test_guest_reads_strip_fields_and_keep_only_public_periodic_reports():
    fake, b, bot_base, c, o, ld = _base_with_reports()
    g = guest_mod.Guest(base_factory=lambda: bot_base)
    page = g.records(b.t["opportunity"], {"filter": [json.dumps(b.filter(b.cond("opportunity", "code", "is", "AZE-001")))],
                                          "projection": [b.fid("opportunity", "code"), b.fid("opportunity", "internal_note"), b.fid("opportunity", "priority")]})
    assert len(page["records"]) == 1
    f = page["records"][0]["fields"]
    assert f[b.fid("opportunity", "code")] == "AZE-001" and b.fid("opportunity", "internal_note") not in f and b.fid("opportunity", "priority") not in f
    # 不带 projection 也去掉
    allf = g.records(b.t["opportunity"], {})["records"]
    assert all(b.fid("opportunity", "internal_note") not in r["fields"] and b.fid("opportunity", "owner") not in r["fields"] for r in allf) and len(allf) == 2
    # 报告:只剩定期报告的公开版;页面自己的条件与行规则叠加
    reps = g.records(b.t["report"], {})["records"]
    assert [b.get("report", r, "key") for r in reps] == ["周报_2026-W41_公开版.docx"]
    assert g.count(b.t["report"], {})["rowCount"] == 1
    assert g.count(b.t["report"], {"filter": [json.dumps(b.filter(b.cond("report", "kind", "is", "月报")))]})["rowCount"] == 0
    # 单条:公开周报给,内部月报与项目报告 404(None)
    rows = {b.get("report", r, "key"): r["id"] for r in b.rows("report", props=["key"])}
    assert g.record(b.t["report"], rows["周报_2026-W41_公开版.docx"]) is not None
    assert g.record(b.t["report"], rows["月报_2026-09_内部版.docx"]) is None and g.record(b.t["report"], rows["项目报告_AZE-001_公开版.docx"]) is None
    one = g.record(b.t["opportunity"], o["id"])
    assert one and b.fid("opportunity", "internal_note") not in one["fields"] and b.get("opportunity", one, "code") == "AZE-001"
    # 越界:整表、字段、排序
    with pytest.raises(guest_mod.Denied):
        g.records(b.t["followup"], {})
    with pytest.raises(guest_mod.Denied):
        g.records(b.t["opportunity"], {"filter": [json.dumps(b.filter(b.cond("opportunity", "owner", "isNotEmpty")))]})
    with pytest.raises(guest_mod.Denied):
        g.records(b.t["opportunity"], {"orderBy": [json.dumps([{"fieldId": b.fid("opportunity", "next_due"), "order": "desc"}])]})
    # 不认识的参数不转发;take 封顶 1000
    n = len(fake.calls)
    g.records(b.t["lead"], {"search": ["x"], "take": ["5000"], "skip": ["0"]})
    assert fake.calls[n:] == [("GET", f"/api/table/{b.t['lead']}/record")]


def test_server_guest_routes_need_login_mark(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake, b, bot_base, c, o, ld = _base_with_reports()
    bot = Assistant(None, guest=guest_mod.Guest(base_factory=lambda: bot_base))
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base, H = f"http://127.0.0.1:{srv.server_port}", {"X-Portal-Auth": "1"}
        assert httpx.get(base + "/guest/policy").status_code == 403  # 没有登录标记(没经 Caddy)
        pol = httpx.get(base + "/guest/policy", headers=H).json()
        assert set(pol["objects"]) == {"lead", "country", "opportunity", "milestone", "source", "city", "report", "supply_plant", "sewage_plant", "award", "contractor", "study", "water_event", "water_area", "law", "law_clause", "local_term", "discharge_limit"} and pol["objects"]["opportunity"]["id"] == b.t["opportunity"]
        r = httpx.get(base + f"/guest/table/{b.t['opportunity']}/record", headers=H, params=[("fieldKeyType", "id"), ("projection", b.fid("opportunity", "code")), ("projection", b.fid("opportunity", "internal_note"))])
        assert r.status_code == 200 and len(r.json()["records"]) == 2 and all(b.fid("opportunity", "internal_note") not in x["fields"] for x in r.json()["records"])
        assert httpx.get(base + f"/guest/table/{b.t['followup']}/record", headers=H).status_code == 403
        assert httpx.get(base + f"/guest/table/{b.t['template']}/aggregation/row-count", headers=H).status_code == 403
        assert httpx.get(base + f"/guest/table/{b.t['report']}/aggregation/row-count", headers=H).json() == {"rowCount": 1}
        assert httpx.get(base + f"/guest/table/{b.t['opportunity']}/record/{o['id']}", headers=H).json()["fields"][b.fid("opportunity", "code")] == "AZE-001"
        assert httpx.get(base + f"/guest/table/{b.t['opportunity']}/record/recNOPE", headers=H).status_code in (404, 502)
        assert httpx.get(base + "/guest/nope", headers=H).status_code == 404
        bad = httpx.get(base + f"/guest/table/{b.t['opportunity']}/record", headers=H, params={"filter": json.dumps(b.filter(b.cond("opportunity", "owner", "isNotEmpty")))})
        assert bad.status_code == 403
        # 没启用(单独跑、没 profile)→ 503
        assert httpx.get(f"http://127.0.0.1:{srv.server_port}/guest/policy", headers=H).status_code == 200
    finally:
        srv.shutdown()
    bot2 = Assistant(None)
    assert bot2.guest is None
    srv2 = serve(bot2, 0, host="127.0.0.1")
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        assert httpx.get(f"http://127.0.0.1:{srv2.server_port}/guest/policy", headers={"X-Portal-Auth": "1"}).status_code == 503
    finally:
        srv2.shutdown()


def test_partner_policy_and_tier():
    """伙伴(本体 partner 节):线索去掉处理备注、国别去掉我方机构与备注;商机、节点、跟进、我方资料整表不给;报告只给公开版(不限类型)。
    分档:库里的人 member;库外的按邮箱对伙伴名单(邮箱或 @域名,大小写不分)→ partner / guest;伙伴读真实库,游客读示例库。"""
    fake, b, bot_base, c, o, ld = _base_with_reports()
    p = guest_mod.Policy(bot_base, section="partner")
    assert p.section == "partner" and "opportunity" not in p.by_oid and "milestone" not in p.by_oid and "followup" not in p.by_oid and "company" not in p.by_oid
    assert {"lead", "country", "report", "law", "city", "award"} <= set(p.by_oid)
    assert b.fid("lead", "note") in p.by_oid["lead"]["deny"] and b.fid("country", "presence") in p.by_oid["country"]["deny"] and b.fid("country", "note") in p.by_oid["country"]["deny"]
    assert p.by_oid["report"]["rows"]["filterSet"] == [b.cond("report", "edition", "is", "公开版")]
    assert "伙伴" in p.public()["note"]
    with pytest.raises(guest_mod.Denied):
        p.table(b.t["opportunity"])
    # 名单匹配
    assert guest_mod.is_partner("Chen@BEWG.example", ["@bewg.example"]) and guest_mod.is_partner("a@x.example", ["a@x.example"]) and not guest_mod.is_partner("a@x.example", ["@bewg.example"]) and not guest_mod.is_partner("", ["@bewg.example"]) and not guest_mod.is_partner("a@notbewg.example", ["@bewg.example"])
    # 分档 + 伙伴读真实库:商机一条不给,报告给公开版(两份:周报、项目报告都是公开版)
    sample_base = tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    g = guest_mod.Guest(base_factory=lambda: sample_base, real_factory=lambda: bot_base, partners=["@bewg.example"],
                        outsider=lambda cookie: cookie != "member", whoami=lambda cookie: {"p": "chen@bewg.example", "g": "someone@other.com"}.get(cookie, ""))
    assert g.tier("member") == "member" and g.tier("p") == "partner" and g.tier("g") == "guest" and g.tier(None) == "guest"
    assert g.outsider_name("p") == "伙伴帐号" and g.outsider_name("g") == "游客帐号"
    with pytest.raises(guest_mod.Denied):
        g.records(b.t["opportunity"], {}, tier="partner")
    assert g.records(b.t["opportunity"], {}, tier="guest")["records"]  # 游客那一档照旧放行商机的公开部分
    reps = g.records(b.t["report"], {}, tier="partner")["records"]
    assert len(reps) == 2 and all(r["fields"][b.fid("report", "edition")] == "公开版" for r in reps) and all(b.fid("report", "requested_by") not in r["fields"] for r in reps)
    assert g.count(b.t["report"], {}, tier="partner") == {"rowCount": 2}
    leads = g.records(b.t["lead"], {}, tier="partner")["records"]
    assert leads and all(b.fid("lead", "note") not in r["fields"] for r in leads)
    with pytest.raises(guest_mod.Denied):  # 条件里用了伙伴看不到的字段
        g.records(b.t["country"], {"filter": [json.dumps(b.filter(b.cond("country", "presence", "isNotEmpty")))]}, tier="partner")
    # 没有名单:库外的都是游客
    g2 = guest_mod.Guest(base_factory=lambda: sample_base, outsider=lambda cookie: True, whoami=lambda cookie: "chen@bewg.example")
    assert g2.tier("x") == "guest"


def test_load_partners_reads_private_team_yaml(tmp_path):
    (tmp_path / "team.yaml").write_text("members:\n  - {code: 市场一部-01, teable: LGH}\npartners:\n  - {email: Chen@bewg.example, org: 北控水务}\n  - '@other.example'\n  - {org: 没写邮箱的不算}\n", encoding="utf-8")
    prof = type("P", (), {"path": str(tmp_path / "profile.yaml")})()
    assert guest_mod.load_partners(prof) == ["chen@bewg.example", "@other.example"]
    assert guest_mod.load_partners(type("P", (), {"path": str(tmp_path / "nope" / "profile.yaml")})()) == []
    assert guest_mod.load_partners(type("P", (), {})()) == []


def test_server_guest_routes_answer_by_tier(monkeypatch):
    """接口按登录 cookie 分档:伙伴的 policy 带 tier: partner、读真实库、商机 403;游客照旧;法规问答伙伴可用、游客不行;伙伴不能下载日历。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake, b, bot_base, c, o, ld = _base_with_reports()
    g = guest_mod.Guest(base_factory=lambda: bot_base, real_factory=lambda: bot_base, partners=["@bewg.example"],
                        outsider=lambda cookie: "member" not in str(cookie), whoami=lambda cookie: "chen@bewg.example" if "partner" in str(cookie) else "x@y.example")
    bot = Assistant(None, guest=g)
    srv = serve(bot, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        P, G = {"X-Portal-Auth": "1", "Cookie": "auth_session=partner"}, {"X-Portal-Auth": "1", "Cookie": "auth_session=guest"}
        pp, gp = httpx.get(base + "/guest/policy", headers=P).json(), httpx.get(base + "/guest/policy", headers=G).json()
        assert pp["tier"] == "partner" and "opportunity" not in pp["objects"] and "伙伴" in pp["note"]
        assert gp["tier"] == "guest" and "opportunity" in gp["objects"]
        assert httpx.get(base + f"/guest/table/{b.t['opportunity']}/record", headers=P).status_code == 403
        assert len(httpx.get(base + f"/guest/table/{b.t['opportunity']}/record", headers=G).json()["records"]) == 2
        assert httpx.get(base + f"/guest/table/{b.t['report']}/aggregation/row-count", headers=P).json() == {"rowCount": 2}
        assert httpx.get(base + f"/guest/table/{b.t['report']}/aggregation/row-count", headers=G).json() == {"rowCount": 1}
        assert httpx.get(base + f"/guest/table/{b.t['lead']}/record/{ld['id']}", headers=P).status_code == 200
        assert httpx.get(base + f"/guest/table/{b.t['opportunity']}/record/{o['id']}", headers=P).status_code == 403
        assert httpx.post(base + "/law", json={"q": "排放标准"}, headers=G).status_code == 403
        assert httpx.post(base + "/law", json={"q": "排放标准"}, headers=P).status_code == 200  # 伙伴可以问法规(没连法规库时接口答不出,但不是拒绝)
        r = httpx.post(base + "/link", json={"url": "https://example.org/x"}, headers=P).json()
        assert r["ok"] is False and r["error"].startswith("伙伴帐号")
        assert httpx.get(base + "/feed-link", headers=P).json()["error"].startswith("伙伴帐号")
    finally:
        srv.shutdown()


def test_partner_list_from_table_and_country_scope():
    """伙伴名单从表「伙伴帐号」读(启用的行;邮箱或 @域名;整个邮箱优先于域名),team.yaml 后备;
    写了可看国别的伙伴:带「所属国别」的表只给这些国家的记录,国别表只给这些国家,单条记录越界 404,信源表不限;停用的行不算。"""
    fake, b, bot_base, c, o, ld = _base_with_reports()
    uzb = b.create("country", [{"name_zh": "乌兹别克斯坦", "key": "UZB", "region": "亚洲 · 中亚"}])[0]
    b.create("lead", [{"key": "x-uzb-1", "title": "Tashkent water", "url": "https://example.org/uzb1", "country": uzb["id"], "found_at": "2026-10-01", "relevant": True}])
    b.create("partner_account", [{"email": "Only@bewg.example", "org": "北控", "countries": [uzb["id"]], "enabled": True},
                                 {"email": "@bewg.example", "org": "北控(全域)", "enabled": True},
                                 {"email": "off@bewg.example", "org": "停用的", "enabled": False, "countries": [uzb["id"]]},
                                 {"email": "all@x.example", "org": "不限国别", "enabled": True}])
    g = guest_mod.Guest(base_factory=lambda: bot_base, real_factory=lambda: bot_base, partners=["@fallback.example"],
                        outsider=lambda cookie: cookie != "member", whoami=lambda cookie: str(cookie))
    assert g.lookup("only@bewg.example") == (True, [uzb["id"]])  # 整个邮箱的行优先,大小写不分
    assert g.lookup("someone@bewg.example") == (True, None)  # 域名的行,不限国别
    assert g.lookup("off@bewg.example") == (True, None)  # 停用的那行不算;落到域名的行
    assert g.lookup("all@x.example") == (True, None) and g.lookup("a@fallback.example") == (True, None) and g.lookup("a@other.example") == (False, None)
    assert g.tier("only@bewg.example") == "partner" and g.scope("only@bewg.example") == [uzb["id"]] and g.scope("someone@bewg.example") is None and g.scope("member") is None
    assert g.scope_info([uzb["id"]]) == [{"id": uzb["id"], "iso3": "UZB", "name": "乌兹别克斯坦"}] and g.scope_isos([uzb["id"]]) == ["UZB"]
    sc = [uzb["id"]]
    leads = g.records(b.t["lead"], {}, "partner", sc)["records"]
    assert len(leads) == 1 and b.fid("lead", "title") in leads[0]["fields"] and leads[0]["fields"][b.fid("lead", "title")] == "Tashkent water"
    assert len(g.records(b.t["lead"], {}, "partner", None)["records"]) >= 2  # 不限国别时都有
    cs = g.records(b.t["country"], {}, "partner", sc)["records"]
    assert [r["id"] for r in cs] == [uzb["id"]]
    assert g.count(b.t["country"], {}, "partner", sc) == {"rowCount": 1}
    assert g.record(b.t["country"], c["id"], "partner", sc) is None and g.record(b.t["country"], uzb["id"], "partner", sc) is not None
    assert g.record(b.t["lead"], ld["id"], "partner", sc) is None  # 阿塞拜疆的线索越界
    assert g._scope_cond(g.policy("partner").table(b.t["source"]), sc, "partner") is None  # 信源没有所属国别:不限
    assert g.records(b.t["source"], {}, "partner", sc)["records"] == g.records(b.t["source"], {}, "partner", None)["records"]
    assert g.records(b.t["lead"], {}, "partner", [])["records"] == []  # 范围空(国别都删了)= 什么都看不到
    # 页面自己的条件与范围叠加
    page = g.records(b.t["lead"], {"filter": [json.dumps(b.filter(b.cond("lead", "relevant", "is", True)))]}, "partner", sc)["records"]
    assert len(page) == 1
    # 改了名单:forget_partners 后马上重读
    b.update("partner_account", b.rows("partner_account", filter=b.filter(b.cond("partner_account", "email", "is", "all@x.example")))[0]["id"], {"enabled": False})
    assert g.lookup("all@x.example") == (True, None)  # 还在 5 分钟缓存里
    g.forget_partners()
    assert g.lookup("all@x.example") == (False, None)
