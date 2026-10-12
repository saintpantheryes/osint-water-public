"""已中标项目(core/jobs/awards.py,A28;scripts/fetch_wb_awards.py):类别按关键词归、工程量从名称里摘、运营维护合同记「运营」;
供应商按规范化名称并成一家、建行并挂到合同的「中标方」;世行原始行并成一份合同一行;
导入按编号去重、只填空,城市按英文名对上;后台译中文名(顺带摘工程量)只填空;A21 折算中标价。不联网。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from core import tstore
from core.jobs import awards, fx
from tests.test_jobs import env  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_wb_awards  # noqa: E402


def test_classify_and_quantity():
    assert awards.classify("Construction of 25 km sewer network in Osh") == "污水"
    assert awards.classify("Lot 2: drilling of 120 boreholes for rural water supply") == "供水"
    assert awards.classify("Rehabilitation of Feeder Canal from 8+000 to 33+288 Km") == "水利"
    assert awards.classify("Travaux d'assainissement pluvial de Dakar") == "污水"  # 先中先得:assainissement 在雨水排涝前
    assert awards.classify("Construction de caniveaux à Bamako") == "雨水排涝"
    assert awards.classify("Desalination plant EPC") == "海水淡化"
    assert awards.classify("Procurement of office furniture") is None
    assert awards.quantity("drilling of 120 boreholes and 35 water kiosks") == "120 boreholes、35 water kiosks"
    assert awards.quantity("Feeder Canal from 8+000 to 33+288 Km, Lot 5m wide") == ""  # 桩号、宽度不算
    assert awards.quantity("pipe network about 17071m and 2 tanks of 6,000m3") == "17071m、2 tanks、6,000m3"


def test_contract_type_operations_and_contractor_key():
    assert awards.contract_type("Works", "Operation and Maintenance of Kality WWTP") == "运营"
    assert awards.contract_type("Non-consulting Services", "Contrat d'affermage du service de l'eau") == "运营"
    assert awards.contract_type("Works", "Water Loss Reduction Performance Based Contract") == "运营"
    assert awards.contract_type("Works", "Construction of latrines including O & M") == "工程"  # 建设含运维仍是工程
    assert awards.contract_type("Works", "Design, build and operation & maintenance of WTP") == "工程"
    assert awards.contract_type("Consultant Services", "Operation and maintenance support") == "咨询服务"  # 咨询不算
    assert awards.contract_type("Goods", "Pipes") == "货物" and awards.contract_type("Other", "x") == ""
    k = awards.contractor_key
    assert k("China Gezhouba Group Co., Ltd.") == k("CHINA GEZHOUBA GROUP COMPANY LIMITED") == "CHINA GEZHOUBA GROUP"
    assert k("M/S. AIRES INGEGNERIA") == "AIRES INGEGNERIA" and k("Veolia Eau S.A.") == "VEOLIA EAU" and k("Larsen & Toubro Limited") == "LARSEN AND TOUBRO"
    assert k("HYDROPLAN INGENIEUR GESELLSCHAFT MIT BESCHRÄNKTER HAFTUNG") == "HYDROPLAN INGENIEUR" and k("LLC Aqua Build") == "AQUA BUILD"


def _raw(no, desc, amount, supplier, sc="CN", country="KG", practice="Water/Sanit/Waste", cat="Works", method="Request for Bids", sid=""):
    return {"supplier_id": sid, "wb_contract_number": no, "contract_description": desc, "supplier_contract_amount_usd": amount, "supplier": supplier, "supplier_country_code": sc,
            "supplier_country": "", "borrower_country_code": country, "project_global_practice": practice, "procurement_category": cat, "procurement_method": method,
            "contract_signing_date": "19-May-2022", "as_of_date": "12-Oct-2026", "project_id": "P1", "project_name": "Osh Water"}


def test_fetch_pick_merges_joint_ventures_and_drops_non_water():
    raw = [_raw("1", "Water supply network in Osh, 30 km", 2_000_000, "A Co", "CN", sid="7"), _raw("1", "Water supply network in Osh, 30 km", 2_000_000, "B Co", "TR"),  # 联合体:同额只算一次
           _raw("2", "Sewer lots", 300_000, "C Co", "IN"), _raw("2", "Sewer lots", 200_000, "D Co", "IN"),  # 金额不同的相加
           _raw("3", "Office furniture", 900_000, "E"), _raw("4", "Water supply", 50_000, "F"),  # 不是水务、太小
           _raw("5", "Water supply", 900_000, "G", practice="Education"), _raw("6", "Water supply", 900_000, "H", method="Individual Consultant Selection"),
           _raw("7", "Water supply", 900_000, "I", country="3E")]  # 地区性项目没有国别
    rows = {r["key"]: r for r in fetch_wb_awards.pick(raw)}
    assert set(rows) == {"WB-1", "WB-2"}
    one = rows["WB-1"]
    assert one["iso3"] == "KGZ" and one["price"] == "2000000.00" and one["winner"] == "A Co、B Co" and one["winner_country"] == "中国、土耳其"
    assert one["sector"] == "供水" and one["contract_type"] == "工程" and one["awarded"] == "2022-05-19" and one["quantity"] == "30 km" and one["method"] == "公开招标(RFB)"
    assert one["url"].endswith("/P1") and "2026-10-12" in one["source"] and one["funder"] == "世界银行"
    assert rows["WB-2"]["price"] == "500000.00" and rows["WB-2"]["winner_country"] == "印度"
    assert json.loads(one["winners"]) == [{"name": "A Co", "iso3": "CHN", "country_zh": "中国", "ext": "WB:7"},
                                          {"name": "B Co", "iso3": "TUR", "country_zh": "土耳其", "ext": ""}]


def test_import_links_cities_fills_blanks_and_skips_unknown_countries(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    ganja = b.create("city", [{"key": "586523", "name_zh": "甘贾", "name_en": "Ganja", "country": crid, "population": 335600}])[0]
    b.create("city", [{"key": "1", "name_zh": "巴库", "name_en": "Baku", "country": crid, "population": 2300000}])  # 4 个字母:太短,不认
    path = tmp_path / "awards.csv.gz"
    awards.write_rows(path, [
        {"key": "WB-1", "iso3": "AZE", "name": "Ganja water supply, 42 km", "sector": "供水", "contract_type": "工程", "price": "25000000", "currency": "USD", "awarded": "2024-05-10",
         "quantity": "42 km", "winner": "A Co", "winner_country": "中国", "project": "P1 Water", "funder": "世界银行", "source": "测试"},
        {"key": "WB-2", "iso3": "AZE", "name": "Baku WWTP", "sector": "污水", "price": "8000000", "currency": "USD", "awarded": "2022-03-01", "source": "测试"},
        {"key": "WB-3", "iso3": "MAR", "name": "Rabat desal", "sector": "海水淡化", "source": "测试"},  # 国别表里没有
    ])
    assert awards.run(b, path, dry_run=True, log=lambda *a: None) == {"新建": 2, "补空": 0, "挂上城市": 1, "国别表里没有": 1, "新建供应商": 0, "供应商补空": 0}
    assert not b.rows("award")
    awards.run(b, path, log=lambda *a: None)
    got = {b.get("award", r, "key"): r for r in b.rows("award")}
    assert tstore.link_id(b.get("award", got["WB-1"], "city")) == ganja["id"] and b.get("award", got["WB-1"], "price") == 25000000
    assert not b.get("award", got["WB-2"], "city") and tstore.link_id(b.get("award", got["WB-2"], "country")) == crid
    # 人改过的不覆盖;再跑不重建
    b.update("award", got["WB-1"]["id"], {"quantity": "配水管网 42 公里"})
    assert awards.run(b, path, log=lambda *a: None) == {"新建": 0, "补空": 0, "挂上城市": 0, "国别表里没有": 1, "新建供应商": 0, "供应商补空": 0}
    assert b.get("award", b.record("award", got["WB-1"]["id"]), "quantity") == "配水管网 42 公里"
    # A21:中标价按汇率折人民币
    assert fx.run(ctx).get("已中标项目折算") == 2 and b.get("award", b.record("award", got["WB-2"]["id"]), "price_cny") > 0


def test_import_creates_contractors_and_links_winners(env, tmp_path):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    old = b.create("award", [{"key": "WB-1", "name": "Old row", "sector": "供水", "country": crid}])[0]  # 上一版导入的:没有中标方
    b.create("contractor", [{"key": "VEOLIA EAU", "name": "Veolia Eau", "name_zh": "威立雅水务"}])  # 人建过、补过的
    w = lambda *xs: json.dumps([{"name": n, "iso3": i, "country_zh": z, "ext": e} for n, i, z, e in xs], ensure_ascii=False)
    path = tmp_path / "awards.csv.gz"
    awards.write_rows(path, [
        {"key": "WB-1", "iso3": "AZE", "name": "Ganja water supply", "sector": "供水", "source": "测试",
         "winners": w(("China Gezhouba Group Co., Ltd.", "CHN", "中国", "WB:1"), ("Veolia Eau S.A.", "FRA", "法国", "WB:2"))},
        {"key": "WB-2", "iso3": "AZE", "name": "Baku WWTP O&M", "sector": "污水", "contract_type": "运营", "source": "测试",
         "winners": w(("CHINA GEZHOUBA GROUP COMPANY LIMITED", "CHN", "中国", "WB:3"))},
        {"key": "WB-3", "iso3": "MAR", "name": "Rabat", "sector": "供水", "source": "测试", "winners": w(("Only Morocco Co", "MAR", "摩洛哥", ""))},
    ])
    assert awards.run(b, path, dry_run=True, log=lambda *a: None)["新建供应商"] == 1 and not b.rows("contractor")[1:]
    got = awards.run(b, path, log=lambda *a: None)
    assert got["新建"] == 1 and got["补空"] == 1 and got["新建供应商"] == 1 and got["供应商补空"] == 1
    firms = {b.get("contractor", r, "key"): r for r in b.rows("contractor")}
    assert set(firms) == {"CHINA GEZHOUBA GROUP", "VEOLIA EAU"}  # 国别表里没有的合同不建它的单位
    cg, ve = firms["CHINA GEZHOUBA GROUP"], firms["VEOLIA EAU"]
    assert b.get("contractor", cg, "name") == "China Gezhouba Group Co., Ltd." and b.get("contractor", cg, "nationality") == "中国"
    assert b.get("contractor", cg, "nationality_code") == "CHN" and b.get("contractor", cg, "ext_ids") == "WB:1,WB:3"
    assert b.get("contractor", ve, "name") == "Veolia Eau" and b.get("contractor", ve, "nationality") == "法国"  # 只补空
    rows = {b.get("award", r, "key"): r for r in b.rows("award")}
    assert set(tstore.link_ids(b.get("award", rows["WB-1"], "winners"))) == {cg["id"], ve["id"]} and rows["WB-1"]["id"] == old["id"]
    assert tstore.link_ids(b.get("award", rows["WB-2"], "winners")) == [cg["id"]] and b.get("award", rows["WB-2"], "contract_type") == "运营"
    again = awards.run(b, path, log=lambda *a: None)
    assert again["新建"] == again["补空"] == again["新建供应商"] == again["供应商补空"] == 0


class _Clf:
    enabled = True

    def __init__(self):
        self.calls = []

    def ask(self, system, user, schema, guide):
        items = json.loads(user)
        self.calls.append(items)
        return {"items": [{"i": it["i"], "zh": "中文:" + it["t"], "qty": "管网 9 km" if "Baku" in it["t"] else ""} for it in items]}


def test_translate_fills_chinese_name_and_quantity_only_when_blank(env):
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    b.create("award", [{"key": "A", "name": "Baku pipes", "sector": "供水", "country": crid},
                       {"key": "B", "name": "Ganja WWTP", "sector": "污水", "country": crid, "quantity": "6 万吨/日"},
                       {"key": "C", "name": "Done", "name_zh": "已译", "sector": "其他", "country": crid}])
    clf = _Clf()
    assert awards.translate(b, clf, log=lambda *a: None) == 2 and len(clf.calls[0]) == 2
    got = {b.get("award", r, "key"): r for r in b.rows("award")}
    assert b.get("award", got["A"], "name_zh") == "中文:Baku pipes" and b.get("award", got["A"], "quantity") == "管网 9 km"
    assert b.get("award", got["B"], "quantity") == "6 万吨/日" and b.get("award", got["C"], "name_zh") == "已译"
    assert awards.translate(b, clf, log=lambda *a: None) == 0 and awards.translate(b, None) == 0
