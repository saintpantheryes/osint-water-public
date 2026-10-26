"""规模以上城市(core/jobs/cities.py,A22):按 GeoNames 数据建城市行(人口 ≥ 门槛、去重按城市码、只填空)、中文名取中文别名、行政区名、
密度 = 人口 ÷ 面积;只补没有城市的国别;数据取不到用缓存、没缓存就跳过;库里没建「城市」表时整个跳过(tstore.Base 把它记为 missing)。
中文名:先查简体名表,没有的把纯汉字别名转简体;旧规则自动填的(繁体、日文、英文)换掉,人改的不动。不联网。"""
from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from core import tstore
from core.jobs import cities
from tests.test_jobs import env  # noqa: F401

CITIES = "\n".join([
    "587084\tBaku\tBaku\tBakou,Baku,巴库\t40.37767\t49.89201\tP\tPPLC\tAZ\t\t09\t\t\t\t2300000\t\t-28\tAsia/Baku\t2024-03-01",
    "586523\tGanja\tGanja\tGandzha,甘贾\t40.68278\t46.36056\tP\tPPLA\tAZ\t\t20\t\t\t\t335600\t\t400\tAsia/Baku\t2023-05-12",
    "585128\tSmalltown\tSmalltown\t\t40.1\t47.1\tP\tPPL\tAZ\t\t20\t\t\t\t16000\t\t200\tAsia/Baku\t2023-05-12",
    "2538475\tRabat\tRabat\t拉巴特\t34.01325\t-6.83255\tP\tPPLC\tMA\t\t07\t\t\t\t1655753\t\t45\tAfrica/Casablanca\t2024-01-10",
])
DAR = "160263\tDar es Salaam\tDar es Salaam\tDar es Salaam,三蘭港\t-6.82349\t39.26951\tP\tPPLA\tTZ\t\t23\t\t\t\t2698652\t\t55\tAfrica/Dar_es_Salaam\t2024-01-01"
ADMIN1 = "AZ.09\tBaki\tBaki\t585955\nAZ.20\tGanja\tGanja\t585954\nMA.07\tRabat-Sale-Kenitra\tRabat-Salé-Kénitra\t7577882\n"


def _client(ok=True, hits=None, text=CITIES):
    def handle(req: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(req.url.path)
        if not ok:
            return httpx.Response(503)
        if req.url.path.endswith("cities15000.zip"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("cities15000.txt", text)
            return httpx.Response(200, content=buf.getvalue())
        if req.url.path.endswith("admin1CodesASCII.txt"):
            return httpx.Response(200, content=ADMIN1.encode())
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def no_table(monkeypatch):
    monkeypatch.setattr(cities, "zh_table", lambda: {})  # 测试只看规则,不用 reference/city_zh.tsv
    monkeypatch.setattr(cities, "wiki_table", lambda: {})  # 也不用 reference/city_wiki.tsv


def test_wiki_table_parses_area_and_links(tmp_path):
    """reference/city_wiki.tsv:编号、维基数据号、面积、中文条目、英文条目;链接按条目名拼(中文 zh-cn 变体、空格变下划线),没面积、没中文条目的键不出现。"""
    f = tmp_path / "w.tsv"
    f.write_text("# 头\n587084\tQ9248\t2140\t巴库\tBaku\tBaku\n586523\tQ131315\t\t\tGanja, Azerbaijan\tGanja\n9\tQ1\t0\t\t\tx\n", encoding="utf-8")
    t = cities.load_wiki_table(f)
    assert t["587084"] == {"area_km2": 2140.0, "wiki_zh": "https://zh.wikipedia.org/zh-cn/%E5%B7%B4%E5%BA%93", "wiki_en": "https://en.wikipedia.org/wiki/Baku"}
    assert t["586523"] == {"wiki_en": "https://en.wikipedia.org/wiki/Ganja%2C_Azerbaijan"} and "9" not in t
    shipped = cities.load_wiki_table(cities.WIKI_TABLE)  # 随程序发布的表:巴库在里面,带面积与中英文条目
    assert shipped["587084"]["area_km2"] > 2000 and shipped["587084"]["wiki_zh"].endswith("%E5%B7%B4%E5%BA%93") and shipped["587084"]["wiki_en"].endswith("/Baku")


def test_area_and_wiki_links_fill_blanks_only(env, monkeypatch):
    """面积与维基百科链接(使用方 2026-10-26「巴库市的维基百科就有面积数据,为什么现在的城市页上反而没有」):按维基表只填空,面积这次填上的才记来源;
    人补过的面积不动、也不给它记维基来源;每次运行都补(不只每月 1 日);密度随后算出来。"""
    fake, admin, abase, ctx = env
    b = abase
    cities.run(ctx, refresh=True, client=_client())  # 先按老样子建了两座城市,都没面积
    rows = {b.get("city", r, "key"): r for r in b.rows("city")}
    b.update("city", rows["586523"]["id"], {"area_km2": 110, "area_source": "市政府网站"})  # 甘贾的面积是人补的
    monkeypatch.setattr(cities, "wiki_table", lambda: {"587084": {"area_km2": 2140, "wiki_zh": "https://zh.wikipedia.org/zh-cn/%E5%B7%B4%E5%BA%93", "wiki_en": "https://en.wikipedia.org/wiki/Baku"},
                                                       "586523": {"area_km2": 1700, "wiki_en": "https://en.wikipedia.org/wiki/Ganja%2C_Azerbaijan"}})
    out = cities.run(ctx, only_missing=True, client=_client())
    assert out == {"面积与维基": 5, "密度": 2}  # 巴库:面积、来源、中、英;甘贾:只有英文链接
    baku, ganja = b.record("city", rows["587084"]["id"]), b.record("city", rows["586523"]["id"])
    assert b.get("city", baku, "area_km2") == 2140 and b.get("city", baku, "area_source") == cities.AREA_SOURCE and b.get("city", baku, "density") == round(2300000 / 2140)
    assert b.get("city", baku, "wiki_zh").endswith("/zh-cn/%E5%B7%B4%E5%BA%93") and b.get("city", baku, "wiki_en") == "https://en.wikipedia.org/wiki/Baku"
    assert b.get("city", ganja, "area_km2") == 110 and b.get("city", ganja, "area_source") == "市政府网站" and b.get("city", ganja, "wiki_en").endswith("Ganja%2C_Azerbaijan") and not b.get("city", ganja, "wiki_zh")
    assert cities.run(ctx, only_missing=True, client=_client()) == {}  # 再跑没东西可填
    # 新建的城市一并带上:摩洛哥出现 → 拉巴特建行时就有面积与链接
    monkeypatch.setattr(cities, "wiki_table", lambda: {"2538475": {"area_km2": 117, "wiki_zh": "https://zh.wikipedia.org/zh-cn/%E6%8B%89%E5%B7%B4%E7%89%B9", "wiki_en": "https://en.wikipedia.org/wiki/Rabat"}})
    ctx.country_rid("MAR")
    assert cities.run(ctx, only_missing=True, client=_client()) == {"新建": 1, "密度": 1}
    rabat = next(r for r in b.rows("city") if b.get("city", r, "key") == "2538475")
    assert b.get("city", rabat, "area_km2") == 117 and b.get("city", rabat, "area_source") == cities.AREA_SOURCE and b.get("city", rabat, "wiki_zh").endswith("%E7%89%B9")


def test_zh_name_prefers_table_then_simplified_hanzi(monkeypatch):
    assert cities.zh_of("Nairobi,內羅比,奈洛比") == "内罗比"  # 第一个纯汉字别名转简体
    assert cities.zh_of("Zibo,シ博市,淄博") == "淄博" and cities.zh_of("Seoul,서울") is None  # 日文、韩文不算
    monkeypatch.setattr(cities, "zh_table", lambda: {"184745": "内罗毕"})
    assert cities.zh_of("Nairobi,內羅比", "184745") == "内罗毕" and cities.zh_of("Lusaka,卢萨卡", "909137") == "卢萨卡"


def test_shipped_table_is_simplified():
    table = cities.load_zh_table(cities.ZH_TABLE)
    assert len(table) > 4000 and table["160263"] == "达累斯萨拉姆" and table["184745"] == "内罗毕" and table["587084"] == "巴库"
    assert all(cities.to_simplified(v) == v for v in table.values())


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


def test_old_auto_names_are_replaced_hand_edits_kept(env, monkeypatch):
    fake, admin, abase, ctx = env
    b = abase
    ctx.country_rid("TZA")
    text = CITIES.replace("Bakou,Baku,巴库", "Bakou,Baku") + "\n" + DAR
    cities.run(ctx, refresh=True, client=_client(text=text))
    rec = {b.get("city", r, "key"): r for r in b.rows("city")}
    assert b.get("city", rec["160263"], "name_zh") == "三兰港" and b.get("city", rec["587084"], "name_zh") == "Baku"
    # 库里是旧规则填的繁体名(三蘭港)和没中文时的英文名(Baku);人把甘贾改成了「占贾」
    b.update("city", rec["160263"]["id"], {"name_zh": "三蘭港"})
    b.update("city", rec["586523"]["id"], {"name_zh": "占贾"})
    monkeypatch.setattr(cities, "zh_table", lambda: {"160263": "达累斯萨拉姆", "587084": "巴库", "586523": "甘贾"})
    out = cities.run(ctx, refresh=True, client=_client(text=text))
    names = {b.get("city", r, "key"): b.get("city", r, "name_zh") for r in b.rows("city")}
    assert out["改简体名"] == 2 and names["160263"] == "达累斯萨拉姆" and names["587084"] == "巴库" and names["586523"] == "占贾"
    assert "改简体名" not in cities.run(ctx, refresh=True, client=_client(text=text))  # 再跑不再改
