import json

from core import zh_name as z


def test_key_points_english_and_ledger_prefix():
    assert z.key_points("台账出处(World Bank):Procurement Regulations for IPF Borrowers") == "采购规定、投资项目融资借款人"
    assert z.key_points("Sumgait WWTP tender notice") == "污水处理厂、招标公告"
    assert z.key_points("Request for Expressions of Interest – Consulting Services for Water Supply and Sanitation Project") == "征集意向书、咨询服务、供水与污水处理、项目"
    assert z.key_points("Rehabilitation of Dam and Irrigation Canal, Lot 2") == "修复改造、大坝、灌溉、渠道、标段"
    assert z.key_points("Damage report on roads") == ""  # dam 不配 damage;认不得的词不乱给
    assert z.key_points("") == ""


def test_key_points_other_languages_fold_accents_and_case():
    assert z.key_points("AVIS D'APPEL D'OFFRES : STATION D’EPURATION DE DAKAR") == "招标公告、污水处理厂"
    assert z.key_points("Licitación pública: planta de tratamiento de aguas residuales") == "公开招标、处理厂、污水"
    assert z.key_points("Тендер: реконструкция водопровода и канализации") == "招标、改造、供水管网、排水"
    assert z.key_points("Sumqayıt şəhərində kanalizasiya kollektorunun tikintisi") == "排水、干管、建设"


def test_acronyms_whole_word_case_sensitive():
    assert z.key_points("EOI for WWTP supervision") == "意向书、污水处理厂、监理"
    assert "意向书" not in z.key_points("Geoid study")  # eoi 不是缩写 EOI
    assert z.key_points("ADB approved loan for Baku water") == "亚洲开发银行、批准、贷款"


def test_zh_name_order_and_limit():
    assert z.zh_name("Sumgait WWTP tender notice", summary_zh="苏姆盖特新污水厂主体工程招标\n第二行", project_name="") == ("苏姆盖特新污水厂主体工程招标", "摘要")
    assert z.zh_name("x", summary_zh="摘要", project_name="哈卡里恰伊输水干管") == ("哈卡里恰伊输水干管", "项目名")
    assert z.zh_name("Sumgait WWTP tender notice") == ("污水处理厂、招标公告", "要点")
    assert z.zh_name("Baku city news") == ("", "")
    assert len(z.zh_name("x", summary_zh="甲" * 100)[0]) == 60


def test_terms_file_sane():
    raw = json.loads(z.TERMS_PATH.read_text(encoding="utf-8"))
    assert raw["schema"].startswith("osint-water/zh-terms@") and len(raw["terms"]) > 300
    assert all(isinstance(k, str) and k.strip() == k and v.strip() for k, v in raw["terms"].items())
