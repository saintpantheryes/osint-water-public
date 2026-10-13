"""研究资料与水务事件(core/jobs/studies.py,A31、A33;scripts/fetch_wb_studies.py):
导入按编号去重、只填空,选项里没有的类型不写;事件日期只到年、月的取区间起点并留原文,城市按英文名对上;
后台译中文标题只填空;抓取脚本只收水务项目、日期转成年月日、重取时留着人改过的中文标题。不联网。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from core import tstore
from core.jobs import studies
from tests.test_jobs import env  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_wb_studies  # noqa: E402


def test_pdate_start():
    assert studies.pdate_start("2023") == "2023-01-01" and studies.pdate_start("2023-07") == "2023-07-01"
    assert studies.pdate_start("2023-07-18") == "2023-07-18" and studies.pdate_start("2023-13") is None and studies.pdate_start("约 2020 年") is None


def test_import_studies_fill_blanks_and_skip_unknown(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    path = tmp_path / "studies.csv.gz"
    studies.write_rows(path, [
        {"key": "WB-1", "iso3": "AZE", "name": "Azerbaijan - National Water Supply and Sanitation Project", "kind": "项目评估", "publisher": "世界银行", "published": "2007-05-01",
         "language": "英文", "url": "https://example.org/1.pdf"},
        {"key": "SUM-AZE", "iso3": "AZE", "name": "阿塞拜疆水务综述", "kind": "综述", "summary": "## 关键数字\n- 巴库漏损 51.5%"},
        {"key": "WB-2", "iso3": "AZE", "name": "Odd", "kind": "不认识的类型"},
        {"key": "WB-3", "iso3": "MAR", "name": "Morocco study"},  # 国别表里没有
    ], studies.STUDY_COLS)
    assert studies.run(b, path, dry_run=True, log=lambda *a: None) == {"新建": 3, "补空": 0, "国别表里没有": 1} and not b.rows("study")
    studies.run(b, path, log=lambda *a: None)
    got = {b.get("study", r, "key"): r for r in b.rows("study")}
    assert set(got) == {"WB-1", "SUM-AZE", "WB-2"} and tstore.link_id(b.get("study", got["WB-1"], "country")) == crid
    assert b.get("study", got["WB-1"], "kind") == "项目评估" and not b.get("study", got["WB-2"], "kind")
    b.update("study", got["WB-1"]["id"], {"name_zh": "人写的中文标题"})
    assert studies.run(b, path, log=lambda *a: None)["新建"] == 0
    assert b.get("study", b.record("study", got["WB-1"]["id"]), "name_zh") == "人写的中文标题"


def test_import_events_dates_and_cities(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    baku = b.create("city", [{"key": "1", "name_zh": "巴库", "name_en": "Baku", "country": crid, "population": 2300000}])[0]
    path = tmp_path / "events.csv.gz"
    studies.write_rows(path, [
        {"key": "EV-AZE-001", "iso3": "AZE", "day": "2004", "kind": "机构改革", "title": "Azersu 成立", "source": "世行文件 第14页"},
        {"key": "EV-AZE-002", "iso3": "AZE", "day": "2016-05-13", "kind": "水价调整", "title": "水价上调", "city_en": "baku", "url": "https://example.org/2.pdf"},
        {"key": "EV-AZE-003", "iso3": "AZE", "day": "近年", "kind": "其他", "title": "日期认不出"},
    ], studies.EVENT_COLS)
    assert studies.run_events(b, path, log=lambda *a: None) == {"新建": 2, "补空": 0, "挂上城市": 1, "国别表里没有": 0, "日期认不出": 1}
    got = {b.get("water_event", r, "key"): r for r in b.rows("water_event")}
    one, two = got["EV-AZE-001"], got["EV-AZE-002"]
    # 区间起点按北京时间零点写(2004-01-01 00:00 +08:00)
    assert str(b.get("water_event", one, "day")).startswith("2003-12-31T16") and b.get("water_event", one, studies.PDATE_RAW) == "2004"
    assert tstore.link_id(b.get("water_event", two, "city")) == baku["id"] and not b.get("water_event", one, "city")
    assert tstore.link_id(b.get("water_event", one, "country")) == crid
    assert studies.run_events(b, path, log=lambda *a: None)["新建"] == 0


class _Clf:
    enabled = True

    def ask(self, system, user, schema, guide):
        return {"items": [{"i": it["i"], "zh": "中文:" + it["t"]} for it in json.loads(user)]}


def test_translate_only_blank_titles(env):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    b.create("study", [{"key": "A", "name": "Water report", "country": crid}, {"key": "B", "name": "Other", "name_zh": "已有", "country": crid}])
    assert studies.translate(b, _Clf(), log=lambda *a: None) == 1 and studies.translate(b, None) == 0
    got = {b.get("study", r, "key"): b.get("study", r, "name_zh") for r in b.rows("study")}
    assert got == {"A": "中文:Water report", "B": "已有"}


def test_fetch_filters_water_projects_and_dates():
    sec = lambda n, p: {"Name": n, "Percent": p}
    w = fetch_wb_studies.is_water_project
    assert w({"project_name": "Rural Water Supply"}) and w({"project_name": "Urban Development Project", "sector1": sec("Water Supply", 40)})
    assert not w({"project_name": "Air Quality Management", "sector1": sec("Water Supply", 12), "sector2": "Other"})  # 水务只占一成多;纯文字的行业不计
    assert not w({"project_name": "IDP Economic Development Support", "sector1": sec("Water Supply", 30)})
    assert fetch_wb_studies.iso_day("12/31/2016 12:00:00 AM") == "2016-12-31" and fetch_wb_studies.iso_day("2007-06-14T00:00:00Z") == "2007-06-14"
    ev = fetch_wb_studies.project_events("AZE", {"id": "P1", "project_name": "X", "boardapprovaldate": "2007-06-14T00:00:00Z", "closingdate": "6/30/2019 12:00:00 AM",
                                                 "status": "Closed", "totalcommamt": "260,000,000"})
    assert [(e["key"], e["day"]) for e in ev] == [("WB-P1-批准", "2007-06-14"), ("WB-P1-关闭", "2019-06-30")] and "2.6 亿美元" in ev[0]["title"]


def test_fetch_merge_keeps_other_rows_and_hand_edits(tmp_path):
    path = tmp_path / "events.csv.gz"
    studies.write_rows(path, [{"key": "WB-P1-批准", "iso3": "AZE", "title": "人改的中文标题"}, {"key": "EV-AZE-001", "iso3": "AZE", "title": "摘自原文"},
                              {"key": "WB-P9-批准", "iso3": "AZE", "title": "这次没取到的"}, {"key": "WB-P5-批准", "iso3": "MAR", "title": "别的国家"}], studies.EVENT_COLS)
    n = fetch_wb_studies.merge(path, studies.EVENT_COLS, {"AZE"}, [{"key": "WB-P1-批准", "iso3": "AZE", "title": "世界银行批准「X」"}])
    got = {r["key"]: r["title"] for r in studies.read_rows(path)}
    assert n == 3 and got == {"WB-P1-批准": "人改的中文标题", "EV-AZE-001": "摘自原文", "WB-P5-批准": "别的国家"}
