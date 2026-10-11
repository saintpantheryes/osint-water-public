"""存量水厂导入(core/jobs/plants.py,A25):数据文件的自来水厂、污水厂按编号去重建行、只填空;城市按 GeoNames 编号挂,
没有就挂同一国家 25 公里内最近的城市,再没有只挂国别;国别表里没有的国家跳过;库里没建两张表时整个跳过。不联网。"""
from __future__ import annotations

import csv
import gzip
import io

from core import tstore
from core.jobs import plants
from tests.test_jobs import env  # noqa: F401

COLS = ["key", "group", "name", "status", "iso3", "geonames", "lat", "lon", "capacity", "pop_served", "built", "level", "operator", "source", "kind"]
ROWS = [
    ["P-1", "sewage", "Baku Central WWTP", "运行", "AZE", "587084", "40.38", "49.89", "64", "1200000.4", "1985", "二级", "Azersu", "测试", "污水厂"],
    ["P-2", "sewage", "Sumgait WWTP", "在建", "AZE", "", "40.59", "49.65", "5", "", "", "", "", "测试", "污水厂"],   # 离巴库约 30 公里
    ["P-3", "supply", "Jeyranbatan WTP", "运行", "AZE", "", "40.40", "49.80", "52", "", "", "", "Azersu", "测试", "自来水厂"],  # 巴库 10 公里内
    ["P-4", "supply", "Rabat desal", "运行", "MAR", "", "34.0", "-6.8", "10", "", "", "", "", "测试", "海水淡化厂"],  # 国别表里没有摩洛哥
]


def _file(tmp_path, rows=ROWS):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLS)
    w.writerows(rows)
    p = tmp_path / "plants.csv.gz"
    p.write_bytes(gzip.compress(buf.getvalue().encode("utf-8")))
    return p


def test_import_links_cities_fills_blanks_and_skips_unknown_countries(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    baku = b.create("city", [{"key": "587084", "name_zh": "巴库", "country": crid, "coords": "40.3777,49.8920", "population": 2300000}])[0]
    path = _file(tmp_path)
    assert plants.run(b, path, dry_run=True, log=lambda *a: None) == {"supply_plant": {"新建": 1, "补空": 0, "挂上城市": 1}, "sewage_plant": {"新建": 2, "补空": 0, "挂上城市": 1}}
    assert not b.rows("supply_plant") and not b.rows("sewage_plant")  # 只算不写
    plants.run(b, path, log=lambda *a: None)
    sew = {b.get("sewage_plant", r, "key"): r for r in b.rows("sewage_plant")}
    assert tstore.link_id(b.get("sewage_plant", sew["P-1"], "city")) == baku["id"] and b.get("sewage_plant", sew["P-1"], "pop_served") == 1200000
    assert b.get("sewage_plant", sew["P-1"], "level") == "二级" and b.get("sewage_plant", sew["P-1"], "built") == "1985"
    assert not b.get("sewage_plant", sew["P-2"], "city") and tstore.link_id(b.get("sewage_plant", sew["P-2"], "country")) == crid  # 太远:只挂国别
    sup = b.rows("supply_plant")
    assert len(sup) == 1 and tstore.link_id(b.get("supply_plant", sup[0], "city")) == baku["id"] and b.get("supply_plant", sup[0], "kind") == "自来水厂"
    # 人改过的不覆盖、人填的改造扩建保留;再跑不重建,只补空
    b.update("sewage_plant", sew["P-1"]["id"], {"name": "巴库中心污水厂", "upgrades": "2012 扩建"})
    tid = b.t["sewage_plant"]
    next(r for r in fake.records[tid] if r["id"] == sew["P-2"]["id"])["fields"].pop(b.fid("sewage_plant", "status"))  # 人清掉了状态
    assert plants.run(b, path, log=lambda *a: None)["sewage_plant"] == {"新建": 0, "补空": 1, "挂上城市": 0}
    p1 = b.record("sewage_plant", sew["P-1"]["id"])
    assert b.get("sewage_plant", p1, "name") == "巴库中心污水厂" and b.get("sewage_plant", p1, "upgrades") == "2012 扩建"
    assert b.get("sewage_plant", b.record("sewage_plant", sew["P-2"]["id"]), "status") == "在建" and len(b.rows("sewage_plant")) == 2


def test_missing_tables_or_file_skip(env, tmp_path):
    fake, admin, b, ctx = env
    assert "跳过" in plants.run(b, tmp_path / "nope.csv.gz", log=lambda *a: None)
    for oid in ("supply_plant", "sewage_plant"):
        fake.tables.pop(b.t[oid])
    b2 = tstore.Base(admin)
    assert "supply_plant" in b2.missing and plants.run(b2, _file(tmp_path), log=lambda *a: None) == {"跳过": "库里还没有自来水厂、污水厂表(先跑建表命令)"}
