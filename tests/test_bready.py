"""营商环境数据(core/jobs/bready.py,A36;scripts/build_bready.py):国别按 ISO3 填世行 B-READY 的得分、商业水价、外企参标限制等;
同一期只填空(人改过的不动),数据版本变了整组替换;字段没建或没有数据文件时跳过。不联网。"""
from __future__ import annotations

from core import tstore
from core.jobs import bready
from tests.test_jobs import env  # noqa: F401

HEAD = ",".join(bready.COLS) + "\n"
ROW25 = "AZE,66,79.3,45.7,65.3,2,AZN,1.18,97.1,5,部分招标有限制,365,7,\"[供水] 法规设了降低漏损的目标:是\n[采购] 外企须与本地企业联合才能投标:否\",B-READY 2025\n"
ROW26 = "AZE,68,81,50.2,66,2.2,AZN,1.29,96,,无限制,300,9,[供水] 法规设了降低漏损的目标:是,B-READY 2026\n"


def _file(tmp_path, body):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    (d / bready.FILE).write_text(HEAD + body, encoding="utf-8")
    return d


def test_fills_blanks_then_replaces_on_new_edition(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    d = _file(tmp_path, ROW25 + "EGY,50,,,,,,,,,,,,,B-READY 2025\n")  # 表里没有的国家不建
    assert bready.run(b, d, dry_run=True, log=lambda *a: None) == {"country": 1}
    assert tstore.is_empty(b.get("country", b.record("country", crid), "bready_total"))
    assert bready.run(b, d, log=lambda *a: None) == {"country": 1}
    c = b.record("country", crid)
    assert b.get("country", c, "bready_total") == 66 and b.get("country", c, "water_tariff_usd") == 1.18 and b.get("country", c, "foreign_bid") == "部分招标有限制"
    assert "漏损" in b.get("country", c, "bready_items") and b.get("country", c, "bready_edition") == "B-READY 2025"
    assert len(b.rows("country")) == 1
    b.update("country", crid, {"award_days": 200})  # 人核过改了的:同一期不覆盖
    assert bready.run(b, d, log=lambda *a: None) == {"country": 0}
    assert b.get("country", b.record("country", crid), "award_days") == 200
    d = _file(tmp_path, ROW26)  # 新一期:整组替换,新一期没有的数清掉
    assert bready.run(b, d, log=lambda *a: None) == {"country": 1}
    c = b.record("country", crid)
    assert b.get("country", c, "award_days") == 300 and b.get("country", c, "foreign_bid") == "无限制" and b.get("country", c, "bready_edition") == "B-READY 2026"
    assert tstore.is_empty(b.get("country", c, "water_conn_days")) and "联合" not in b.get("country", c, "bready_items")


def test_missing_file_skips(env, tmp_path):
    fake, admin, b, ctx = env
    assert bready.run(b, tmp_path, log=lambda *a: None) == {}


def test_build_answers_and_scores():
    """整理脚本的几处换算:问卷答案转中文、「半数专家未答」的数、-777 = 没数据、国名写法不同(Côte d'Ivoire)也对得上。"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("build_bready", Path(__file__).resolve().parents[1] / "scripts" / "build_bready.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.yes_no("Yes, in some public tenders") == "部分招标有" and m.yes_no("No") == "否" and m.yes_no("Skip Logic") is None
    assert m.yes_no("Multiple modes: No. Yes, in some public tenders. ") == "说法不一" and m.yes_no("Yes; 50% Skip Logic") == "是(半数专家未答)"
    assert m.yes_no("Yes, fully digitized") == "是(全程线上)"
    assert m._num("Calculate median; 50% Skip Logic: 120") == 120 and m._num(-777) is None and m._num(7.17) == 7.17 and m.half("Calculate median; 50% Skip Logic: 120")
    assert m.norm("Côte d’Ivoire") == m.norm("Cote d'Ivoire") and m.FOREIGN["部分招标有"] == "部分招标有限制"
    assert set(m.CUR) >= {"AZE", "MAR", "KGZ", "UZB"} and m.CUR["MAR"] == "MAD"
