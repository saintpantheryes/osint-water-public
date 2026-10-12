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
    assert set(p.by_oid) == {"lead", "country", "opportunity", "milestone", "source", "city", "report", "supply_plant", "sewage_plant", "award", "contractor"}  # 跟进记录、成员、报告模板不在
    e = p.by_oid["opportunity"]
    assert e["id"] == b.t["opportunity"] and b.fid("opportunity", "internal_note") in e["deny"] and b.fid("opportunity", "owner") in e["deny"]
    assert b.fid("opportunity", "code") in e["allowed"] and b.fid("opportunity", "internal_note") not in e["allowed"]
    r = p.by_oid["report"]
    assert r["rows"]["filterSet"] == [b.cond("report", "edition", "is", "公开版"), b.cond("report", "kind", "isAnyOf", ["周报", "月报", "日报"])]
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
        assert set(pol["objects"]) == {"lead", "country", "opportunity", "milestone", "source", "city", "report", "supply_plant", "sewage_plant", "award", "contractor"} and pol["objects"]["opportunity"]["id"] == b.t["opportunity"]
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
