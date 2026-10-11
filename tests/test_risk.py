"""水风险与经济数据(core/jobs/risk.py,A26):城市按 GeoNames 编号、国别按 ISO3 填 Aqueduct 分数、人均 GDP、世行涉水项目;
只填空,人改过的不动;空值与文件里没有的城市跳过;字段没建时跳过。不联网。"""
from __future__ import annotations

import gzip

from core import tstore
from core.jobs import risk
from tests.test_jobs import env  # noqa: F401

CITY = ("geonames,water_risk,water_stress,water_stress_2030,water_stress_2050,drought,flood,no_drinking,no_sanitation,untreated_ww,gdp_pc\n"
        "587084,4.24,5.0,5.0,5.0,3.17,2.47,5.6,2.8,98.8,53797.0\n"
        "586523,2.1,,,,,,,,,9000.4\n")
COUNTRY = "iso3,water_stress,drought,flood,water_stress_2030,water_stress_2050,gdp_pc,wb_water_musd,wb_water_n\nAZE,2.47,3.13,2.55,2.58,2.85,25089,0,0\nKEN,0.79,3.02,4.03,1.43,1.78,6619,1650,3\n"


def _dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "city_risk.csv.gz").write_bytes(gzip.compress(CITY.encode()))
    (d / "country_risk.csv").write_text(COUNTRY, encoding="utf-8")
    return d


def test_fills_city_and_country_blanks_only(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    baku, ganja, other = b.create("city", [{"key": "587084", "name_zh": "巴库", "country": crid}, {"key": "586523", "name_zh": "甘贾", "country": crid},
                                           {"key": "999", "name_zh": "别处", "country": crid}])
    d = _dir(tmp_path)
    assert risk.run(b, d, dry_run=True, log=lambda *a: None) == {"city": 2, "country": 1}
    assert b.get("city", b.record("city", baku["id"]), "water_risk") in (None, "")
    b.update("city", ganja["id"], {"gdp_pc": 12000})  # 人填过的
    assert risk.run(b, d, log=lambda *a: None) == {"city": 2, "country": 1}
    r = b.record("city", baku["id"])
    assert b.get("city", r, "water_risk") == 4.24 and b.get("city", r, "untreated_ww") == 98.8 and b.get("city", r, "gdp_pc") == 53797
    assert "Aqueduct" in b.get("city", r, "risk_source")
    g = b.record("city", ganja["id"])
    assert b.get("city", g, "gdp_pc") == 12000 and b.get("city", g, "water_risk") == 2.1 and b.get("city", g, "drought") in (None, "")
    assert not b.get("city", b.record("city", other["id"]), "risk_source")
    c = b.record("country", crid)
    assert b.get("country", c, "water_stress") == 2.47 and b.get("country", c, "gdp_pc") == 25089 and b.get("country", c, "wb_water_n") == 0
    assert risk.run(b, d, log=lambda *a: None) == {"city": 0, "country": 0}  # 再跑不重写


def test_missing_fields_or_files_skip(env, tmp_path):
    fake, admin, b, ctx = env
    assert risk.run(b, tmp_path, log=lambda *a: None) == {}
