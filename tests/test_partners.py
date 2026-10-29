"""潜在合作伙伴(core/jobs/partners.py,A51):按私有 data/partners.csv 建进供应商表并勾「潜在合作伙伴」;标识照 A28 规范化,中过标的同一家只补空;
国别表里没有的跳过;选项里没有的性质、领域不写;人去掉的勾(推荐排序已有值)不打回。A28 导入中标记录时给供应商对上「所在国别」。不联网。"""
from __future__ import annotations

import json
from pathlib import Path

from core import tstore
from core.jobs import awards, partners
from tests.test_jobs import env  # noqa: F401


def _write(path: Path):
    partners.write_rows(path, [
        {"key": "AZE-1", "iso3": "AZE", "rank": "1", "name": "TTŞ İnşaat LLC", "name_zh": "TTŞ 建筑公司", "kind": "民营企业", "website": "https://ttshinshaat.az",
         "sectors": "供水;污水", "profile": "1997 年起家", "reason": "Azersu 项目中标最多", "url": "https://ttshinshaat.az/en/", "note": "公开资料,待核实"},
        {"key": "AZE-2", "iso3": "AZE", "rank": "2", "name": "Evrascon OJSC", "name_zh": "欧亚建设公司", "kind": "不认识的性质", "sectors": "水利;不认识的领域;供水", "reason": "大型水工业绩"},
        {"key": "MAR-1", "iso3": "MAR", "rank": "1", "name": "SGTM", "name_zh": "摩洛哥工程总公司"},  # 国别表里没有
        {"key": "AZE-3", "iso3": "AZE", "rank": "3", "name": "   "},  # 没有名称
    ], partners.COLS)


def test_import_partners_creates_and_fills_blanks_only(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    had = b.create("contractor", [{"key": awards.contractor_key("TTŞ İnşaat LLC"), "name": "TTS INSAAT LLC", "nationality": "阿塞拜疆", "nationality_code": "AZE", "profile": "人写的简介"}])[0]  # 中过标、人补过简介
    path = tmp_path / "partners.csv"
    _write(path)
    assert partners.run(b, path, dry_run=True, log=lambda *a: None) == {"新建": 1, "补空": 1, "国别表里没有": 1, "没有名称": 1} and len(b.rows("contractor")) == 1
    assert partners.run(b, path, log=lambda *a: None)["新建"] == 1
    got = {b.get("contractor", r, "key"): r for r in b.rows("contractor")}
    assert set(got) == {awards.contractor_key("TTŞ İnşaat LLC"), "EVRASCON"}
    t = got[awards.contractor_key("TTŞ İnşaat LLC")]
    assert t["id"] == had["id"] and b.get("contractor", t, "profile") == "人写的简介" and b.get("contractor", t, "name") == "TTS INSAAT LLC"  # 只补空
    assert b.get("contractor", t, "partner") is True and b.get("contractor", t, "partner_rank") == 1 and b.get("contractor", t, "name_zh") == "TTŞ 建筑公司"
    assert tstore.link_id(b.get("contractor", t, "country")) == crid and b.get("contractor", t, "source_url") == "https://ttshinshaat.az/en/" and b.get("contractor", t, "kind") == "民营企业"
    assert b.get("contractor", t, "sectors") == ["供水", "污水"] and not b.get("contractor", t, "note")  # 备注归人写,不导
    e = got["EVRASCON"]
    assert b.get("contractor", e, "partner_rank") == 2 and not b.get("contractor", e, "kind") and b.get("contractor", e, "sectors") == ["水利", "供水"]  # 选项里没有的不写
    assert b.get("contractor", e, "nationality") == "阿塞拜疆" and b.get("contractor", e, "nationality_code") == "AZE"
    # 人去掉了勾(推荐排序已有值):再跑不打回;人改了理由也不动
    b.update("contractor", e["id"], {"partner": False, "partner_reason": "人改的理由"})
    assert partners.run(b, path, log=lambda *a: None) == {"新建": 0, "补空": 0, "国别表里没有": 1, "没有名称": 1}
    e2 = b.record("contractor", e["id"])
    assert not b.get("contractor", e2, "partner") and b.get("contractor", e2, "partner_reason") == "人改的理由"


def test_partners_skip_when_table_or_field_missing(env, tmp_path):
    fake, admin, b, ctx = env
    assert "跳过" in partners.run(b, tmp_path / "none.csv", log=lambda *a: None)


def test_award_import_links_contractor_country(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    w = lambda *xs: json.dumps([{"name": n, "iso3": i, "country_zh": z, "ext": e} for n, i, z, e in xs], ensure_ascii=False)
    path = tmp_path / "awards.csv.gz"
    awards.write_rows(path, [{"key": "WB-1", "iso3": "AZE", "name": "Ganja water supply", "sector": "供水", "source": "测试",
                              "winners": w(("TTS Insaat LLC", "AZE", "阿塞拜疆", "WB:1"), ("Veolia Eau S.A.", "FRA", "法国", "WB:2"))}])
    awards.run(b, path, log=lambda *a: None)
    firms = {b.get("contractor", r, "key"): r for r in b.rows("contractor")}
    assert tstore.link_id(b.get("contractor", firms["TTS INSAAT"], "country")) == crid  # 注册国在国别表里:挂上所在国别
    assert not b.get("contractor", firms["VEOLIA EAU"], "country")  # 法国不在国别表里:空着
