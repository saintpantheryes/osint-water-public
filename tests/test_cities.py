"""规模以上城市(core/jobs/cities.py,A22):按 GeoNames 数据建城市行(人口 ≥ 门槛、去重按城市码、只填空)、中文名取中文别名、行政区名、
密度 = 人口 ÷ 面积;只补没有城市的国别;数据取不到用缓存、没缓存就跳过;库里没建「城市」表时整个跳过(tstore.Base 把它记为 missing)。不联网。"""
from __future__ import annotations

import io
import zipfile

import httpx

from core import tstore
from core.jobs import cities
from tests.test_jobs import env  # noqa: F401

CITIES = "\n".join([
    "587084\tBaku\tBaku\tBakou,Baku,巴库\t40.37767\t49.89201\tP\tPPLC\tAZ\t\t09\t\t\t\t2300000\t\t-28\tAsia/Baku\t2024-03-01",
    "586523\tGanja\tGanja\tGandzha,甘贾\t40.68278\t46.36056\tP\tPPLA\tAZ\t\t20\t\t\t\t335600\t\t400\tAsia/Baku\t2023-05-12",
    "585128\tSmalltown\tSmalltown\t\t40.1\t47.1\tP\tPPL\tAZ\t\t20\t\t\t\t16000\t\t200\tAsia/Baku\t2023-05-12",
    "2538475\tRabat\tRabat\t拉巴特\t34.01325\t-6.83255\tP\tPPLC\tMA\t\t07\t\t\t\t1655753\t\t45\tAfrica/Casablanca\t2024-01-10",
])
ADMIN1 = "AZ.09\tBaki\tBaki\t585955\nAZ.20\tGanja\tGanja\t585954\nMA.07\tRabat-Sale-Kenitra\tRabat-Salé-Kénitra\t7577882\n"


def _client(ok=True, hits=None):
    def handle(req: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(req.url.path)
        if not ok:
            return httpx.Response(503)
        if req.url.path.endswith("cities15000.zip"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("cities15000.txt", CITIES)
            return httpx.Response(200, content=buf.getvalue())
        if req.url.path.endswith("admin1CodesASCII.txt"):
            return httpx.Response(200, content=ADMIN1.encode())
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_parse_cities_filters_by_country_threshold_and_names():
    admin = cities.parse_admin1(ADMIN1)
    rows = cities.parse_cities(CITIES, "AZ", 100000, admin)
    assert [r["name_zh"] for r in rows] == ["巴库", "甘贾"] and rows[0]["key"] == "587084" and rows[0]["admin"] == "Baki"
    assert rows[0]["coords"] == "40.3777,49.8920" and rows[0]["population"] == 2300000 and rows[0]["pop_year"] == 2024 and rows[0]["source"] == "GeoNames cities15000(2024-03)"
    assert [r["name_zh"] for r in cities.parse_cities(CITIES, "AZ", 10000, admin)] == ["巴库", "甘贾", "Smalltown"]  # 没中文别名用英文名


def test_job_creates_fills_and_computes_density(env, tmp_path):
    fake, admin, abase, ctx = env
    b = abase
    hits: list[str] = []
    out = cities.run(ctx, refresh=True, client=_client(hits=hits))
    assert out == {"新建": 2} and len(hits) == 2
    rows = {b.get("city", r, "key"): r for r in b.rows("city")}
    baku = rows["587084"]
    assert b.get("city", baku, "name_zh") == "巴库" and b.get("city", baku, "country")["title"] == "阿塞拜疆" and b.get("city", baku, "population") == 2300000
    # 人补了面积 → 密度算出来;人改过的中文名不被覆盖;再跑不重建
    b.update("city", baku["id"], {"area_km2": 2130, "name_zh": "巴库市"})
    out = cities.run(ctx, client=_client(hits=hits))
    assert out == {"密度": 1} and len(hits) == 2  # 缓存还新鲜:不再取
    baku = b.record("city", baku["id"])
    assert b.get("city", baku, "density") == round(2300000 / 2130) and b.get("city", baku, "name_zh") == "巴库市" and len(b.rows("city")) == 2
    # 只补没有城市的国别:新国别摩洛哥出现 → 只给它建;阿塞拜疆不再碰
    ctx.country_rid("MAR")
    out = cities.run(ctx, only_missing=True, client=_client(hits=hits))
    assert out == {"新建": 1} and {b.get("city", r, "name_zh") for r in b.rows("city")} == {"巴库市", "甘贾", "拉巴特"}
    assert cities.run(ctx, only_missing=True, client=_client(hits=hits)) == {}
    # 数据取不到:有缓存照用;没缓存就跳过
    assert cities.run(ctx, refresh=True, client=_client(ok=False)) == {}
    import shutil
    shutil.rmtree(ctx.cache.root.parent / "geonames")
    assert cities.run(ctx, refresh=True, client=_client(ok=False)) == {"跳过": "没有 GeoNames 数据"}


def test_base_without_city_table_is_optional(env):
    fake, admin, abase, ctx = env
    tid = abase.t["city"]
    fake.tables.pop(tid)
    b = tstore.Base(ctx.api)
    assert "city" in b.missing and "city" not in b.t and not b.has("city", "name_zh")
    ctx.base = b
    assert cities.run(ctx, refresh=True, client=_client()) == {}
    from core import guest as guest_mod
    assert "city" not in guest_mod.Policy(b).by_oid  # 游客策略也跳过
