"""已中标项目(core/jobs/awards.py,A28;scripts/fetch_wb_awards.py):类别按关键词归、工程量从名称里摘、运营维护合同记「运营」;
供应商按规范化名称并成一家、建行并挂到合同的「中标方」;世行原始行并成一份合同一行;
导入按编号去重、只填空,城市按英文名对上;后台译中文名(顺带摘工程量)只填空;A21 折算中标价;
摩洛哥采购网、EBRD 授标公告、亚行已授合同、吉尔吉斯采购网中标单位表各自整理成同样的行。不联网。"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from core import tstore
from core.jobs import awards, fx
from tests.test_jobs import env  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_adb_awards as adb  # noqa: E402
import fetch_ebrd_awards as ebrd  # noqa: E402
import fetch_kg_awards as kg  # noqa: E402
import fetch_ma_awards as ma  # noqa: E402
import fetch_wb_awards  # noqa: E402
import fetch_wb_old_awards as wb_old  # noqa: E402


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


def test_strict_classify_for_contracts_outside_water_projects():
    s = awards.classify_strict
    assert s("Construction of Main Canal 1 and Main canal 2") == "水利" and s("Rehabilitation of Lugu dam embankment") == "水利"
    assert s("Construction of Yedigoze Water Treatment Plant") == "供水" and s("Travaux d'AEP des douars de Tazarine") == "供水"
    assert s("Rehabilitation of Storm Water Drainage System in the City of Beira") == "雨水排涝"
    assert s("Planta de tratamiento de aguas residuales Los Merinos") == "污水"
    assert s("Traitement des points critiques sur le tronçon Marchand Dessalines") is None  # 地名,不是海水淡化
    assert s("Construction of drainage and road improvement") is None and s("Classrooms and latrines") is None
    assert s("Acquisition de semences pluviales") is None and s("Community park with floodlights") is None
    assert s("Dam road rehabilitation") is None and s("Procurement of 10 tractors for irrigation farmers") is None
    assert s("Water body cleaning") is None  # 泛泛的「水」在这里不收


def test_contract_type_operations_and_contractor_key():
    assert awards.contract_type("Works", "Operation and Maintenance of Kality WWTP") == "运营"
    assert awards.contract_type("Non-consulting Services", "Contrat d'affermage du service de l'eau") == "运营"
    assert awards.contract_type("Works", "Water Loss Reduction Performance Based Contract") == "运营"
    assert awards.contract_type("Works", "Construction of latrines including O & M") == "工程"  # 建设含运维仍是工程
    assert awards.contract_type("Works", "Design, build and operation & maintenance of WTP") == "工程"
    assert awards.contract_type("Consultant Services", "Operation and maintenance support") == "咨询服务"  # 咨询不算
    assert awards.contract_type("Goods", "Pipes") == "货物" and awards.contract_type("Other", "x") == ""
    # 摩洛哥采购网的法文运维合同(2026-10-13:问「运营维护的中标」查不到,这类都记成了工程、非咨询服务)
    assert awards.contract_type("Non-consulting Services", "Exploitation de la station d’épuration des eaux usées de la ville de Fès") == "运营"
    assert awards.contract_type("Non-consulting Services", "Gestion et exploitation des stations monoblocs de dessalement") == "运营"
    assert awards.contract_type("Non-consulting Services", "CO-exploitation de la station d'épuration des eaux usées de la ville d’Essaouira") == "运营"
    assert awards.contract_type("Works", "Travaux d’entretien et de maintenance du réseau d’eau potable au niveau de la province d’Essaouira") == "运营"
    assert awards.contract_type("Works", "Travaux d'entretien et réparation du réseau d'assainissement") == "工程"  # 修理工程
    assert awards.contract_type("Works", "TRAVAUX DE REALISATION DES FORAGES D’EXPLOITATION POUR L’AEP") == "工程"  # 生产井
    assert awards.contract_type("Works", "Fourniture, installation et mise en service de débitmètres d’exploitation") == "工程"
    k = awards.contractor_key
    assert k("China Gezhouba Group Co., Ltd.") == k("CHINA GEZHOUBA GROUP COMPANY LIMITED") == "CHINA GEZHOUBA GROUP"
    assert k("M/S. AIRES INGEGNERIA") == "AIRES INGEGNERIA" and k("Veolia Eau S.A.") == "VEOLIA EAU" and k("Larsen & Toubro Limited") == "LARSEN AND TOUBRO"
    assert k("HYDROPLAN INGENIEUR GESELLSCHAFT MIT BESCHRÄNKTER HAFTUNG") == "HYDROPLAN INGENIEUR" and k("LLC Aqua Build") == "AQUA BUILD"


def test_delivery_model_from_name():
    """承包模式(2026-10-13 使用方要能单独查 DBO、EPC):按名称认,DBO(建了还管运营)→ BOT/PPP → 设计施工 → 按合同类型;咨询、监理不分。"""
    d = awards.delivery
    assert d("工程", "Design, Build and Operate of Eastern Catchment Wastewater Treatment Plants (WWTPs)") == "DBO"
    assert d("工程", "Design, build, operate and transfer (DBOT) Drinking Water Supply System") == "DBO"  # 业主出钱:DBO,不是 BOT
    assert d("货物", "DBO: Comprehensive Water Supply & Sanitation Improvement Works including STP of 2.5 MLD") == "DBO"  # 亚行记成货物的
    assert d("工程", "Construction of Water Distribution Network including house connections with O&M at Rangamati") == "DBO"
    assert d("运营", "MISE A NIVEAU ET EXPLOITATION DE LA STEP DE TAMESNA") == "DBO"  # 改造 + 运营
    assert d("运营", "Exploitation de la station d’épuration des eaux usées de la ville de Fès") == "运营"
    assert d("工程", "I&D and STP works at Saharanpur in Uttar Pradesh State under Hybrid Annuity PPP Mode") == "BOT/PPP"
    assert d("工程", "Concession du service d'assainissement collectif dans le périmètre de Tunis nord") == "BOT/PPP"
    assert d("工程", "STP works at Patna", "公私合营(PPP)") == "BOT/PPP"  # 采购方式写明 PPP
    assert d("工程", "Design and Build of WWTP in Karakol") == "设计施工"
    assert d("工程", "Contrato Turnkey para Sistema de Esgotamento Sanitário") == "设计施工"
    assert d("工程", "Travaux de réalisation de la STEP mutualisée des villes de Larache et Ksar el Kebir") == "施工"
    assert d("工程", "TRAVAUX DE REALISATION DES FORAGES D’EXPLOITATION POUR L’AEP") == "施工"  # 生产井不是运营
    assert d("工程", "Construction, Interior Improvement and Related Works for the Flood Monitoring and Operations Room") == "施工"
    assert d("咨询服务", "Construction Supervision of the Design, Build and Operate (DBO) Contract") == ""  # 给 DBO 做监理的不算
    assert d("非咨询服务", "Assistance relative au volet technique dans le cadre d’un PPP de dessalement") == ""
    assert d("货物", "Supply of water meters") == "" and d(None, "Pipes") == ""


def test_main_works_from_name():
    """主体工程(v2.21,使用方要水厂、管网分开看):海水淡化厂 → 污水处理厂 → 供水厂;厂和管网都写了的,管网在前、厂只是修饰的算管网。"""
    a = awards.asset
    assert a("Design, Build and Operate of Eastern Catchment Wastewater Treatment Plants (WWTPs)") == "污水处理厂"
    assert a("Construction de la STEP de Safi") == "污水处理厂" and a("Реконструкция КОС г. Ош") == "污水处理厂"
    assert a("Construction of 50,000 m3/day Water Treatment Plant") == "供水厂" and a("Station de traitement d'eau potable de Taza") == "供水厂"
    assert a("2.4 MIGD SWRO Desalination Plant DBO") == "海水淡化厂"
    assert a("Supply of pipes for the Ganja WTP") == "管网" and a("Distribution network from the WTP to Ganja districts") == "管网"  # 「供某水厂的管子」:管网
    assert a("Water Distribution Network Rehabilitation") == "管网" and a("Construction of pumping station and reservoir") == "泵站水池水源"
    assert a("Supply of Smart Water Meters") == "其他"


def test_import_keeps_only_plausible_estimates_and_process_numbers():
    """招标估价只在中标价是它的 0.3–2 倍时写(差得多的多半是标段、公告没对上);评标天数 0–900、投标家数 1–200 以外的不写。"""
    v = lambda **kw: awards.values(dict({"key": "K", "name": "Construction de la STEP", "sector": "污水", "price": "900", "currency": "MAD"}, **kw), "rec", None)  # noqa: E731
    assert v(estimate="1000")["estimate"] == 1000 and "estimate" not in v(estimate="100") and "estimate" not in v(estimate="5000") and "estimate" not in v(estimate="")
    assert "estimate" not in v(estimate="1000", price="")
    assert v(eval_days="35")["eval_days"] == 35 and "eval_days" not in v(eval_days="-3") and "eval_days" not in v(eval_days="2000")
    assert v(bidders="4")["bidders"] == 4 and "bidders" not in v(bidders="0")
    assert v()["asset"] == "污水处理厂" and v(asset="管网")["asset"] == "管网"  # 数据文件里写了的以它为准


def _raw(no, desc, amount, supplier, sc="CN", country="KG", practice="Water/Sanit/Waste", cat="Works", method="Request for Bids", sid=""):
    return {"supplier_id": sid, "wb_contract_number": no, "contract_description": desc, "supplier_contract_amount_usd": amount, "supplier": supplier, "supplier_country_code": sc,
            "supplier_country": "", "borrower_country_code": country, "project_global_practice": practice, "procurement_category": cat, "procurement_method": method,
            "contract_signing_date": "19-May-2022", "as_of_date": "12-Oct-2026", "project_id": "P1", "project_name": "Osh Water"}


def test_fetch_pick_merges_joint_ventures_and_drops_non_water():
    raw = [_raw("1", "Water supply network in Osh, 30 km", 2_000_000, "A Co", "CN", sid="7"), _raw("1", "Water supply network in Osh, 30 km", 2_000_000, "B Co", "TR"),  # 联合体:同额只算一次
           _raw("2", "Sewer lots", 300_000, "C Co", "IN"), _raw("2", "Sewer lots", 200_000, "D Co", "IN"),  # 金额不同的相加
           _raw("3", "Office furniture", 900_000, "E"), _raw("4", "Water supply", 50_000, "F"),  # 不是水务、太小
           _raw("5", "Water supply for 12 schools", 900_000, "G", practice="Education"), _raw("6", "Water supply", 900_000, "H", method="Individual Consultant Selection"),
           _raw("8", "Rehabilitation of the main canal of Massa irrigated perimeter", 3_000_000, "J", practice="Agriculture"),  # 农业项目里的灌渠:收
           _raw("9", "Seeds for irrigated wheat", 900_000, "K", practice="Agriculture"), _raw("10", "Office water dispenser", 900_000, "L", practice=None),  # 种子、泛泛的「水」:不收
           _raw("7", "Water supply", 900_000, "I", country="3E")]  # 地区性项目没有国别
    rows = {r["key"]: r for r in fetch_wb_awards.pick(raw)}
    assert set(rows) == {"WB-1", "WB-2", "WB-8"} and rows["WB-8"]["sector"] == "水利"
    one = rows["WB-1"]
    assert one["iso3"] == "KGZ" and one["price"] == "2000000.00" and one["winner"] == "A Co、B Co" and one["winner_country"] == "中国、土耳其"
    assert one["sector"] == "供水" and one["contract_type"] == "工程" and one["awarded"] == "2022-05-19" and one["quantity"] == "30 km" and one["method"] == "公开招标(RFB)"
    assert one["url"].endswith("/P1") and "2026-10-12" in one["source"] and one["funder"] == "世界银行"
    assert rows["WB-2"]["price"] == "500000.00" and rows["WB-2"]["winner_country"] == "印度"
    assert json.loads(one["winners"]) == [{"name": "A Co", "iso3": "CHN", "country_zh": "中国", "ext": "WB:7"},
                                          {"name": "B Co", "iso3": "TUR", "country_zh": "土耳其", "ext": ""}]


def test_fetch_pick_fixes_north_korea_supplier_codes():
    raw = [_raw("1", "Water supply network", 2_000_000, "DOHWA ENGINEERING CO., LTD.", "KP", country="TZ"),  # 世行数据录错:韩国公司
           _raw("2", "Water supply network", 2_000_000, "FPM ASBL", "KP", country="CD")]  # 刚果(金)本地单位
    rows = {r["key"]: r for r in fetch_wb_awards.pick(raw)}
    assert rows["WB-1"]["winner_country"] == "韩国" and json.loads(rows["WB-1"]["winners"])[0]["iso3"] == "KOR"
    assert json.loads(rows["WB-2"]["winners"])[0]["iso3"] == "COD"


def _old(no, desc, amount, firms, group="CW", sectors=("Water Supply", "Sanitation"), project="NATIONAL WATER SUPPLY & SANITATION", signed="25-Nov-2011",
         method="International Competitive Bidding", country="AZ"):
    return {"contr_id": no, "contr_desc": desc, "total_contr_amnt": str(amount), "contr_sgn_date": signed, "countrycode": country, "countryshortname": "Azerbaijan",
            "projectid": "P096213", "project_name": project, "procurement_group": group, "procurement_group_desc": {"CW": "Works", "GO": "Goods", "CS": "Consultant Services"}[group],
            "procu_meth_text": method, "sector": list(sectors), "suppinfo": [{"name": n, "id": str(i), "country": c, "countryshortname": ""} for i, (n, c) in enumerate(firms, 1)]}


def test_old_wb_awards_short_titles_mojibake_and_date_split():
    raw = [_old("1", "Construction of Water Supply and Waste Water Systems in Goychay", 105_753_853.25, [("ESER CONTRACTING", "TR")]),
           _old("2", "Construction of Transmission Main from Kura", 58_784_356, [("AZERSUTECHIZATTIKINTI JSC", "AZ"), ("TTÅž INÅžAAT LLC", "AZ")]),  # 读坏的名称还原
           _old("3", "NETWORK REHABILITATION NASIMI, NIZAMI AND NARIMANOV DISTRICTS", 400_000, [("KHAZAR LTD.", "AZ")]),  # 名称太短:工程按项目名称归
           _old("4", "MILL-MUGAN SIPHON COMPLETION", 5_840_000, [("AZERSU", "AZ")], project="Rehabilitation & Completion of Irrigation & Drainage", sectors=("Irrigation & drainag",)),
           _old("5", "SUPPLY OF POLYETHYLENE (PE) PIPES AND FITTINGS", 3_900_000, [("DERNEGUL BORU", "TR")], group="GO"),  # 管材:收
           _old("6", "Supervision consultant for 4 rayons", 7_840_000, [("P2MBERLIN GMBH", "DE")], group="CS", method="Quality And Cost-Based Selection"),
           _old("7", "SUPPLY OF EARTHMOVING PLANT", 2_690_000, [("X", "TR")], group="GO"),  # 机械、审计、办公楼、个人顾问、太小、2019 年后的:不收
           _old("8", "Audit of the project for 2015", 380_000, [("Y", "AZ")], group="CS"),
           _old("9", "Construction of Office Buildings for 8 WUAs", 250_000, [("Z", "AZ")]),
           _old("10", "Water supply specialist", 150_000, [("INDIVIDUAL CONSULTANT", "AZ")], group="CS", method="Individual Consultant Selection"),
           _old("11", "Water supply network", 90_000, [("W", "AZ")]),
           _old("12", "Water supply network", 900_000, [("V", "AZ")], signed="03-Jul-2019"),
           _old("13", "Construction of 25 km sewer in Rural Investment area", 600_000, [("U", "AZ")], project="Rural Investment Project", sectors=("Rural",)),  # 别的项目:严格词
           _old("14", "Training of PMU staff", 600_000, [("T", "AZ")], project="Rural Investment Project", sectors=("Rural",))]
    rows = {r["key"]: r for r in wb_old.pick(raw, today=date(2026, 10, 14))}
    assert set(rows) == {"WB-1", "WB-2", "WB-3", "WB-4", "WB-5", "WB-6", "WB-13"}
    one = rows["WB-1"]
    assert one["iso3"] == "AZE" and one["price"] == "105753853.25" and one["currency"] == "USD" and one["awarded"] == "2011-11-25"
    assert one["sector"] == "污水" and one["contract_type"] == "工程" and one["method"] == "国际竞争性招标(ICB)" and one["winner_country"] == "土耳其"
    assert one["funder"] == "世界银行" and "2019 年前" in one["source"] and "2026-10-14" in one["source"] and one["url"].endswith("/P096213")
    assert rows["WB-2"]["winner"] == "AZERSUTECHIZATTIKINTI JSC、TTŞ INŞAAT LLC" and rows["WB-2"]["winner_country"] == "阿塞拜疆"
    assert json.loads(rows["WB-2"]["winners"])[1] == {"name": "TTŞ INŞAAT LLC", "iso3": "AZE", "country_zh": "阿塞拜疆", "ext": "WB:2"}
    assert rows["WB-3"]["sector"] == "供水" and rows["WB-4"]["sector"] == "水利" and rows["WB-5"]["contract_type"] == "货物"
    assert rows["WB-6"]["contract_type"] == "咨询服务" and rows["WB-6"]["method"] == "质量和费用选择(QCBS)" and rows["WB-13"]["sector"] == "污水"


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
    assert b.get("award", got["WB-1"], "delivery") == "施工" and not b.get("award", got["WB-2"], "delivery")  # 承包模式按合同类型与名称认;没有合同类型的不分
    # A21:中标价按汇率折人民币
    assert fx.run(ctx).get("已中标项目折算") == 2 and b.get("award", b.record("award", got["WB-2"]["id"]), "price_cny") > 0


def test_import_fills_delivery_on_rows_already_there(env, tmp_path):
    """库里已有的合同(加「承包模式」之前导入的)再跑一次导入就补上;按库里现在的合同类型认(人改过的以人为准);人填过的承包模式不动。"""
    fake, admin, b, ctx = env
    crid = b.rows("country")[0]["id"]
    base = {"iso3": "AZE", "sector": "污水", "contract_type": "工程", "source": "测试"}
    path = tmp_path / "awards.csv.gz"
    awards.write_rows(path, [{**base, "key": "A-1", "name": "Design, Build and Operate of Baku WWTP"}, {**base, "key": "A-2", "name": "Exploitation de la STEP de Baku"},
                             {**base, "key": "A-3", "name": "Sewer network, lot 2"}])
    b.create("award", [{"key": "A-1", "name": "Design, Build and Operate of Baku WWTP", "sector": "污水", "contract_type": "工程", "country": crid, "source": "测试"},
                       {"key": "A-2", "name": "Exploitation de la STEP de Baku", "sector": "污水", "contract_type": "运营", "country": crid, "source": "测试"},  # 人改成了运营
                       {"key": "A-3", "name": "Sewer network, lot 2", "sector": "污水", "contract_type": "工程", "delivery": "设计施工", "country": crid, "source": "测试"}])  # 人填过
    assert awards.run(b, path, log=lambda *a: None)["补空"] == 3  # 三份都补上主体工程(v2.21),前两份还补承包模式
    got = {b.get("award", r, "key"): (b.get("award", r, "delivery"), b.get("award", r, "asset")) for r in b.rows("award")}
    assert got == {"A-1": ("DBO", "污水处理厂"), "A-2": ("运营", "污水处理厂"), "A-3": ("设计施工", "管网")}


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


# ---------- 摩洛哥公共采购网(scripts/fetch_ma_awards.py)----------


def test_ma_classify_water_titles_only():
    c = ma.classify
    assert c("Assainissement liquide du centre TAHANAOUT - Station d'épuration") == "污水"
    assert c("Construction d'un réservoir semi-enterré de capacité 5 000 m3 Ouled Mbarek") == "供水"
    assert c("TRAVAUX D'AMENAGEMENT DE LA SEGUIA JDIDA TAKDIMT") == "水利" and c("Travaux d'aménagement hydro-agricole du périmètre PMH Aghbalou") == "水利"
    assert c("LUTTE CONTRE LES INONDATIONS - SALE: collecteur pluvial avenue Annasr") == "雨水排涝"
    assert c("Travaux de protection du centre de Jorf contre les inondations de l'oued Elbatha") == "水利"
    assert c("Programme de mise à niveau des infrastructures routières (Province de Taroudant)") is None
    assert c("L'acquisition de berce d'intervention contre les inondations pour la Protection Civile") is None  # 救生艇
    assert c("L'exécution des travaux de reboisement dans le périmètre Rouached II") is None
    assert c("TRAVAUX DE CONSTRUCTION DE LA VOIE DE CONTOURNEMENT DU BARRAGE BIN EL OUIDANE") is None  # 路
    assert c("Contrôle technique des travaux pour les opérations AL MANZAH à Oued Zem") is None  # 地名里的 oued 不算


def test_ma_amounts_and_what_is_worth_reading():
    assert ma.amounts("STE F.S IDIVA |2.086.236,00 Dhs TTC") == [2086236.0]
    assert ma.amounts("ART STAM SARL AU | 520 518,00") == [520518.0] and ma.amounts("Montant : 1 772 820 DH TTC") == [1772820.0]
    assert ma.amounts("STE HMM TRAVAUX  I 772 820,00 DH TTC") == [1772820.0]  # 识别把 1 认成了 I
    assert ma.amounts("Estimation (en Dhs TTC) : 3.000.000,00 ; caution provisoire 30.000,00") == []
    assert ma.amounts("Date 08/10/2026, ICE 003659661000046") == []
    body = "Résultat définitif de l'appel d'offres ouvert n° 302/26/EAU. Maître d'ouvrage : SRM-MS. Concurrent retenu : "
    assert ma.worth_reading(body + "UBAC 17 533 473,60 DH TTC")
    assert not ma.worth_reading(body + "ALITFAI SARL 520 518,00 DH TTC")  # 不够 100 万迪拉姆
    assert not ma.worth_reading(body + "Appel d'offres infructueux, néant") and ma.worth_reading(body + "XYZ (montant illisible)")
    assert not ma.worth_reading("illisible")


def _notice(ref, title, buyer="SRM RABAT - SALE - KENITRA SA", category="Travaux", published="07/10/2026", place="KENITRA"):
    return {"ref_cons": ref, "org": "g3h", "title": title, "buyer": buyer, "procedure": "Appel d'offres ouvert", "category": category,
            "place": place, "published": published}


def test_ma_rows_keep_awarded_lots_above_threshold_once():
    notices = [_notice("1", "Construction d'un réservoir 5 000 m3 Ouled Mbarek"), _notice("2", "Construction d'un réservoir 4 000 m3 Ahmed Taleb"),
               _notice("3", "Réservoir 5 000 m3 Ouled Mbarek (même PV)"), _notice("4", "Etude d'AEP des douars de Saada", category="Services"),
               _notice("5", "Assainissement liquide en deux lots"), _notice("6", "Programme routier"), _notice("7", "Entretien du réseau d'eau potable")]
    lot = lambda w, a, n="unique": {"lot": n, "winner": w, "amount": a, "tax": "TTC", "currency": "MAD"}
    reads = {"1": {"status": "awarded", "lots": [lot("UBAC", 17533473.6)]}, "2": {"status": "awarded", "lots": [lot("UBAC", 17698520.4)]},
             "3": {"status": "awarded", "lots": [lot("UBAC", 17533473.6)]},  # 同一张结果表挂在两条公告上
             "4": {"status": "awarded", "lots": [lot("China Gezhouba Group Maroc / CID SARL", 2500000)]},
             "5": {"status": "awarded", "lots": [lot("STE A", 3000000, "1"), lot("STE B", 900000, "2")]},
             "6": {"status": "awarded", "lots": [lot("ROUTES SA", 5000000)]}, "7": {"status": "no_award", "lots": []}}
    rows = {r["key"]: r for r in ma.rows_from(notices, reads, {"4": "Le projet est financé par la KfW."}, today=awards_today())}
    assert set(rows) == {"MA-1", "MA-2", "MA-4", "MA-5-1"}  # 3 重复;5 的标段 2 不够门槛;6 不是水务;7 流标
    r1 = rows["MA-1"]
    assert r1["iso3"] == "MAR" and r1["price"] == "17533473.60" and r1["currency"] == "MAD" and r1["awarded"] == "2026-10-07" and r1["sector"] == "供水"
    assert r1["contract_type"] == "工程" and r1["buyer"] == "SRM RABAT - SALE - KENITRA SA" and r1["place"] == "KENITRA" and r1["method"].startswith("公开招标")
    assert r1["url"].endswith("refConsultation=1&orgAcronyme=g3h") and r1["source"] == "摩洛哥公共采购网结果公告(2026-10-13 取)"
    r4 = rows["MA-4"]
    assert r4["contract_type"] == "咨询服务" and r4["funder"] == "德国复兴信贷银行" and r4["winner"] == "China Gezhouba Group Maroc、CID SARL"
    assert r4["winner_country"] == "中国、摩洛哥" and [w["iso3"] for w in json.loads(r4["winners"])] == ["CHN", "MAR"]
    assert rows["MA-5-1"]["name"] == "Assainissement liquide en deux lots(Lot 1)"


def awards_today():
    from datetime import date
    return date(2026, 10, 13)


class _ReadClf:
    enabled, label = True, "测试模型"

    def __init__(self, out):
        self.out = out

    def ask(self, system, user, schema, guide):
        return self.out


def test_ma_read_drops_amounts_not_in_the_text():
    text = "Concurrent retenu: CAP TP | 18 311 355,58 ; TELSEL 4 170 000,00"
    good = {"lot": "1", "winner": "CAP TP", "amount": 18311355.58, "tax": "TTC", "currency": "MAD"}
    made_up = {"lot": "2", "winner": "TELSEL", "amount": 4710000, "tax": "TTC", "currency": "MAD"}
    r = ma.read_one(_ReadClf({"status": "awarded", "lots": [good, made_up]}), _notice("1", "x"), text)
    assert r["status"] == "awarded" and r["lots"] == [good] and r["by"] == "测试模型"
    assert ma.read_one(_ReadClf({"status": "awarded", "lots": [made_up]}), _notice("1", "x"), text)["status"] == "unclear"
    assert ma.read_one(_ReadClf({"_error": "x"}), _notice("1", "x"), text) is None


def test_import_reads_every_data_file_and_matches_cities_without_accents(env, tmp_path, monkeypatch):
    fake, admin, b, ctx = env
    mar = b.create("country", [{"name_zh": "摩洛哥", "ISO3": "MAR"}])[0]
    ken = b.create("city", [{"key": "2544248", "name_zh": "盖尼特拉", "name_en": "Kenitra", "country": mar["id"], "population": 431282}])[0]
    monkeypatch.setattr(awards, "DATA", tmp_path / "awards.csv.gz")
    awards.write_rows(tmp_path / "awards.csv.gz", [{"key": "WB-1", "iso3": "AZE", "name": "Ganja water", "sector": "供水", "source": "测试"}])
    awards.write_rows(tmp_path / "awards_mar.csv.gz", [
        {"key": "MA-1", "iso3": "MAR", "name": "Réservoir 5 000 m3 à Kénitra", "sector": "供水", "price": "17533473.60", "currency": "MAD", "buyer": "SRM-RSK",
         "source": "测试"},
        {"key": "MA-2", "iso3": "MAR", "name": "Station de pompage", "sector": "供水", "place": "KENITRA", "buyer": "ONEE", "source": "测试"}])
    got = awards.run(b, log=lambda *a: None)
    assert got["新建"] == 3 and got["挂上城市"] == 2
    rows = {b.get("award", r, "key"): r for r in b.rows("award")}
    assert tstore.link_id(b.get("award", rows["MA-1"], "city")) == ken["id"] and tstore.link_id(b.get("award", rows["MA-2"], "city")) == ken["id"]
    assert b.get("award", rows["MA-1"], "buyer") == "SRM-RSK" and b.get("award", rows["MA-1"], "currency") == "MAD"


def test_short_city_names_only_match_a_place_that_names_them(env, tmp_path):
    fake, admin, b, ctx = env
    kgz = b.create("country", [{"name_zh": "吉尔吉斯斯坦", "ISO3": "KGZ"}])[0]
    osh = b.create("city", [{"key": "1527534", "name_zh": "奥什", "name_en": "Osh", "country": kgz["id"], "population": 322164}])[0]
    awards.write_rows(tmp_path / "a.csv.gz", [
        {"key": "KG-1", "iso3": "KGZ", "name": "Канализация в г. Ош", "sector": "污水", "place": kg.city_of("Канализация в г. Ош"), "source": "测试"},
        {"key": "KG-2", "iso3": "KGZ", "name": "Водопровод в Ошской области", "sector": "供水", "place": kg.city_of("Водопровод в Ошской области"), "source": "测试"},
        {"key": "KG-3", "iso3": "KGZ", "name": "Josh water osh", "sector": "供水", "source": "测试"}])  # 短名不在长文字里乱认
    awards.run(b, tmp_path / "a.csv.gz", log=lambda *a: None)
    rows = {b.get("award", r, "key"): r for r in b.rows("award")}
    assert tstore.link_id(b.get("award", rows["KG-1"], "city")) == osh["id"]
    assert not b.get("award", rows["KG-2"], "city") and not b.get("award", rows["KG-3"], "city")


_EBRD_HEAD = ("<b>Project Name:</b><br /> {project} <br /><b>EBRD Project ID:</b><br />{pid}<br /><b>Country:</b><br />{country}<br />"
              "<b>Client Name:</b><br />{client}<br /><b>Type of Procurement:</b><br /> {type}<br /><b>Procurement Method:</b><br /> Open Tender Single Stage<br />"
              "<b>1. Contract Details</b><br /><br /><b>Contract Title:</b> {title}<br /><br /><b>Contract Description:</b> {title}<br /><br />"
              "<b>Date of Contract Signature:</b> 25/02/2025  <br /><br /><b>Signed Contract Value:</b><br />{value}<br /><br /><b>Lot(s):</b><br />")


def _ebrd(body, **kw):
    kw = {"project": "Naryn Water and Wastewater", "pid": "48707", "country": "Kyrgyz Republic", "client": "Naryn Water Company",
          "type": "Works", "title": "Extension of main sewer network", "value": "2,352,875.53 EUR", **kw}
    return _EBRD_HEAD.format(**kw) + body + "<b>3. Other Information</b>"


_EBRD_JV = ("No lots for this contract. <br /><br /><br /><b>2. Contracting Party</b><br />CRBC&amp;TJRT Joint Venture<br /><address>Tianjin, <br />China</address>"
            "<br /><br /><b>JVCA Partners:</b><br /><br /><br />Lead Partner:  No <br />China Road and Bridge Corporation<br /><address>manas 155b, <br />Chui, <br />"
            "720014, <br />Kyrgyz Republic</address><br /><br />Lead Partner:  Yes <br />Tianjin RongTai Water Co.,Ltd<br /><address>No.16 Bixiang Road, <br />"
            "300300, <br />China</address><br /><br /><b>2. Contracting Party</b><br />")
_EBRD_LOTS = ("<span>Lot Number:</span> 2<br /><b>2. Contracting Party</b><br />SNCE - SOGEA MAROC<br /><address>Rabat</address><br /><br />"
              "<b>JVCA Partners:</b><br />x<br /> <br /><b>Lot 1</b><br /><br />SNCE - SOGEA MAROC<br />		<br />Contract Price: MAD 471,913,890<br />"
              "Contract Price (Euro Equivalent): &#8364; 43,746,417.6<br /><br />JVCA? Yes<br /><br />JVCA Details:<br />Lead Partner Details:<br />"
              "	SNCE<br />	Résidence Kays, place Rabia, Agdal<br />	Rabat, Morocco, 10090<br /><br />Other Partner Details:<br />	SOGEA MAROC<br />"
              "	BP 81 Quartier industriel, Had Soualem 26400 Maroc<br />	Témara, Morocco, 12040<br /><br /><b>Lot 2</b><br /><br />	CMGP<br />"
              "	Parc Industriel SAPINO<br />	Nouaceur<br />	20240<br />	Morocco<br />		<br />Contract Price: MAD 345,498,628.8<br />"
              "Contract Price (Euro Equivalent): &#8364; 32,027,722.88<br /><br />JVCA? No<br /><br /><br /><br /><b>2. Contracting Party</b><br />")


class _Rates:
    def to_usd(self, value, currency):
        return {"EUR": 1.1, "USD": 1.0, "MAD": 0.1}.get(currency, 0) * value if value else None


def test_ebrd_detail_reads_jv_partners_and_lots():
    d = ebrd.parse_detail(_ebrd(_EBRD_JV))
    assert d["project_id"] == "48707" and d["client"] == "Naryn Water Company" and d["signed"] == "25/02/2025"
    (lot,) = d["lots"]
    assert lot["price"] == 2352875.53 and lot["currency"] == "EUR"
    assert [(f["name"], f["iso3"]) for f in lot["firms"]] == [("Tianjin RongTai Water Co.,Ltd", "CHN"), ("China Road and Bridge Corporation", "KGZ")]
    d = ebrd.parse_detail(_ebrd(_EBRD_LOTS, project="Saiss III water conservation project", value="393,261,575.00 MAD", title="PP1W"))
    assert [(x["lot"], x["price"], x["currency"], [f["name"] for f in x["firms"]]) for x in d["lots"]] == [
        ("1", 471913890.0, "MAD", ["SNCE", "SOGEA MAROC"]), ("2", 345498628.8, "MAD", ["CMGP"])]
    assert [f["iso3"] for x in d["lots"] for f in x["firms"]] == ["MAR", "MAR", "MAR"]


def test_ebrd_rows_water_only_above_threshold_once():
    notices = [{"id": "37014120", "title": "Kyrgyz Republic: Extension of main sewer network", "pub": "27/02/2025 10:00 UK Time"},
               {"id": "37014121", "title": "Kyrgyz Republic: same contract again", "pub": "28/02/2025 10:00 UK Time"},
               {"id": "40000000", "title": "Kyrgyz Republic: Supply of Special Vehicles", "pub": "01/03/2025 10:00 UK Time"},
               {"id": "40000001", "title": "Ukraine: Trolleybuses", "pub": "01/03/2025 10:00 UK Time"},
               {"id": "40000002", "title": "Kyrgyz Republic: Water specialist", "pub": "01/03/2025 10:00 UK Time"}]
    details = {"37014120": ebrd.parse_detail(_ebrd(_EBRD_JV)), "37014121": ebrd.parse_detail(_ebrd(_EBRD_JV)),
               "40000000": ebrd.parse_detail(_ebrd(_EBRD_JV, title="Supply of Special Vehicles")),
               "40000001": ebrd.parse_detail(_ebrd(_EBRD_JV, project="Kryvyi Rih Trolleybus", client="City Trolleybus", country="Ukraine",
                                                    title="Supply of trolleybuses")),
               "40000002": ebrd.parse_detail(_ebrd(_EBRD_JV, title="Water and Wastewater Technical Specialist", value="25,672.00 EUR"))}
    rows = ebrd.rows_from(notices, details, rates=_Rates(), today=awards_today())
    assert [r["key"] for r in rows] == ["EBRD-37014120"]  # 重登的只收一次;车辆、无轨电车不是水务;个人小合同不够门槛
    r = rows[0]
    assert r["iso3"] == "KGZ" and r["sector"] == "污水" and r["contract_type"] == "工程" and r["currency"] == "EUR" and r["awarded"] == "2025-02-25"
    assert r["winner"] == "Tianjin RongTai Water Co.,Ltd、China Road and Bridge Corporation" and r["winner_country"] == "中国"  # CRBC 按名称认作中国企业
    assert r["funder"] == "欧洲复兴开发银行" and r["buyer"] == "Naryn Water Company" and r["project"] == "48707 Naryn Water and Wastewater"
    assert r["url"].endswith("displayNoticeId=37014120") and r["source"].endswith("(2026-10-13 取)")


def _adb(node, title, project, price, name, country="Kyrgyz Republic", group="goods", loc="BISHKEK, KYRGYZ REPUBLIC", pno="55250-001"):
    return {"id": f"x-entity:node/{node}:en", "tm_X3b_en_title": [title], "tm_X3b_en_project_title": [project], "sm_price": [str(price)],
            "tm_X3b_en_contractor_name": [name], "tm_X3b_en_country": [country], "ss_fct_group": group, "tm_X3b_en_location": [loc],
            "tm_X3b_en_project_number": [pno], "tm_X3b_en_executing_agency": ["Bishkek Vodokanal"], "ds_date_posted": "2025-07-02T12:00:00Z"}


def test_adb_rows_split_joint_ventures_and_skip_individuals():
    assert adb.split_firms("CRBC, TJRT, AND TJMM (JV)") == ["CRBC", "TJRT", "TJMM"]
    assert adb.split_firms("JV of HAYAT GROUP LLC and BIOWORKS Verfahrenstechnik GmbH") == ["HAYAT GROUP LLC", "BIOWORKS Verfahrenstechnik GmbH"]
    assert adb.split_firms("Zhejiang Post, Telecommunication Construction Co., Ltd. (PRC) in association with Egis Eau (FRANCE)") == [
        "Zhejiang Post, Telecommunication Construction Co., Ltd. (PRC)", "Egis Eau (FRANCE)"]
    assert adb.split_firms("China Road and Bridge Corporation") == ["China Road and Bridge Corporation"]
    docs = [_adb(1, "Wastewater Treatment Plant Pumping Stations: Lot 1", "Bishkek Water Supply and Wastewater", 12845224.83, "CRBC, TJRT, AND TJMM (JV)"),
            _adb(2, "Wastewater Treatment Plant Pumping Stations Lot 1 (repost)", "Bishkek Water Supply and Wastewater", 12845224.83, "CRBC & TJRT"),
            _adb(3, "Chief Technical Advisor", "Bishkek Water Supply and Wastewater", 310000, "WOUTER J. LAMBRECHTSEN", group="consulting"),
            _adb(4, "Rehabilitation of school buildings", "Bishkek Water Supply and Wastewater", 900000, "ABC LLC"),
            _adb(5, "Construction of irrigation canal", "Rural Roads Improvement", 900000, "Kanalet Insaat A.S. (TURKEY)", pno="1"),
            _adb(6, "Road works", "Rural Roads Improvement", 900000, "XYZ LLC", pno="1")]
    rows = adb.rows_from(docs, today=awards_today())
    assert [r["key"] for r in rows] == ["ADB-1", "ADB-5"]  # 重登的、个人顾问、学校、道路都不收;道路项目里的灌渠过严筛
    r = rows[0]
    assert r["sector"] == "污水" and r["contract_type"] == "工程" and r["currency"] == "USD" and r["funder"] == "亚洲开发银行"
    assert r["winner"] == "CRBC、TJRT、TJMM" and json.loads(r["winners"])[0]["iso3"] == "CHN" and r["buyer"] == "Bishkek Vodokanal"
    assert r["url"] == "https://www.adb.org/projects/55250-001/main" and r["awarded"] == "2025-07-02"
    assert rows[1]["sector"] == "水利" and rows[1]["winner_country"] == "土耳其"


def test_kg_classify_russian_and_kyrgyz_titles():
    assert kg.classify("Строительство водопроводных сетей в жилом массиве") == "供水"
    assert kg.classify("Канализационный коллектор по ул. Элебесова") == "污水"
    assert kg.classify("Реконструкция ирригационного канала Кара-Кол") == "水利"
    assert kg.classify("Строительство защитной дамбы, селезащитные работы") == "水利"
    assert kg.classify("Асфальтирование в селе Кара-Суу") is None  # 「в селе」是村里,不是泥石流
    assert kg.classify("Ремонт школы в селе Жаңы-Арык") is None
    assert kg.classify("МРТ аппарат 48 канальный") is None
    assert kg.classify("Ичүүчү суу түтүгүн куруу") == "供水"
    assert kg.classify("Услуги по предоставлению каналов передачи данных") is None  # 数据、电视通道
    assert kg.classify("Трансляция телепрограмм 6 каналов") is None


def test_kg_rows_threshold_plausible_amounts_and_buyer():
    def row(no, name, price, plan, winner="Общество с ограниченной ответственностью ”Бекстрой”", contract="12", signed="2019-09-20"):
        return {"notice_no": no, "purchase_name": name, "winner": winner, "contract_no": contract, "signed_date": signed,
                "lots": [{"lot_name": name, "planned": plan, "contract": price}], "planned_total": plan, "contract_total": price}
    rows = [row("190905000000001", "Строительство канал село Кабык", 43801161.0, 45000000.0),
            row("190905000000001", "Строительство канал село Кабык", 43801161.0, 45000000.0),  # 表里重了一行
            row("190905000000002", "Строительство водопровода", 2000000.0, 2100000.0),  # 不够门槛
            row("190905000000003", "Поставка труб для водопровода", 21450.0, 15000000.0),  # 多半是单价或外币
            row("190905000000004", "Строительство дороги", 50000000.0, 50000000.0),
            row("261002000000005", "Бурение скважин", 10000000.0, 11000000.0, winner="China Railway 21st Bureau", signed="1011-01-01")]
    out = kg.rows_from(rows, {"190905000000001": {"buyer": "Айыл окмоту Кабык", "address": "Чуйская область"}}, 8_750_000,
                       today=awards_today())
    assert [r["key"] for r in out] == ["KG-190905000000001-12", "KG-261002000000005-12"]
    r = out[0]
    assert r["iso3"] == "KGZ" and r["sector"] == "水利" and r["contract_type"] == "工程" and r["currency"] == "KGS" and r["price"] == "43801161.00"
    assert r["winner"] == 'ОсОО "Бекстрой"' and r["winner_country"] == "吉尔吉斯斯坦" and r["buyer"] == "Айыл окмоту Кабык"
    assert r["url"] == "https://zakupki.gov.kg/popp/view/order/view.xhtml?id=000000001" and r["awarded"] == "2019-09-20"
    assert out[1]["awarded"] == "2026-10-02" and out[1]["winner_country"] == "中国"  # 签约日期写错的按公告日期
    assert r["estimate"] == "45000000.00"  # 计划金额写进招标估价(v2.21)
