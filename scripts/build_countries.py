"""生成 reference/countries.yaml:国别代码、中英文名、大洲与区域(联合国 M49 标准),以及识别国名用的多语种别名。

数据来源:联合国统计司 M49 标准 https://unstats.un.org/unsd/methodology/m49/overview/(页面内含中、英、俄、法、西文表)。
用法:python scripts/build_countries.py [已下载的 m49.html]   不给文件则联网下载一次。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
URL = "https://unstats.un.org/unsd/methodology/m49/overview/"
UA = os.environ.get("OW_USER_AGENT") or "osint-water/0.1 (+https://github.com/saintpantheryes/osint-water-public)"

# 常用简称(显示用;正式名留在别名里)
SHORT_ZH = {
    "BOL": "玻利维亚", "IRN": "伊朗", "GBR": "英国", "USA": "美国", "RUS": "俄罗斯", "KOR": "韩国", "PRK": "朝鲜",
    "LAO": "老挝", "VNM": "越南", "SYR": "叙利亚", "TZA": "坦桑尼亚", "COD": "刚果(金)", "COG": "刚果(布)", "MDA": "摩尔多瓦",
    "VEN": "委内瑞拉", "PSE": "巴勒斯坦", "MKD": "北马其顿", "CZE": "捷克", "FSM": "密克罗尼西亚", "VAT": "梵蒂冈",
    "BRN": "文莱", "CPV": "佛得角", "SWZ": "斯威士兰", "TUR": "土耳其", "NLD": "荷兰", "HKG": "中国香港", "MAC": "中国澳门",
    "CIV": "科特迪瓦", "GMB": "冈比亚", "BHS": "巴哈马",
}
SHORT_EN = {
    "BOL": "Bolivia", "IRN": "Iran", "GBR": "United Kingdom", "USA": "United States", "RUS": "Russia", "KOR": "South Korea",
    "PRK": "North Korea", "LAO": "Laos", "VNM": "Vietnam", "SYR": "Syria", "TZA": "Tanzania", "COD": "DR Congo",
    "COG": "Congo", "MDA": "Moldova", "VEN": "Venezuela", "PSE": "Palestine", "FSM": "Micronesia", "BRN": "Brunei",
    "CPV": "Cabo Verde", "SWZ": "Eswatini", "TUR": "Türkiye", "CZE": "Czechia", "HKG": "Hong Kong", "MAC": "Macao",
    "CIV": "Côte d'Ivoire", "NLD": "Netherlands", "VAT": "Holy See",
}
# 识别国名用的其他常见写法(含世界银行的写法)
ALIASES = {
    "BOL": ["Bolivia"], "IRN": ["Iran", "Iran, Islamic Rep."], "GBR": ["UK", "Britain"], "USA": ["United States of America"],
    "RUS": ["Russia", "Россия"], "KOR": ["Korea, Rep.", "Republic of Korea", "Korea, Republic of"],
    "PRK": ["Korea, Dem. People's Rep.", "Korea, Democratic People's Republic of"],
    "LAO": ["Laos", "Lao PDR", "Lao People's Democratic Republic"], "VNM": ["Vietnam", "Viet Nam"], "SYR": ["Syria"],
    "TZA": ["Tanzania"],
    "COD": ["Congo, Dem. Rep.", "DR Congo", "Democratic Republic of Congo", "DRC", "Congo, Democratic Republic of",
            "Congo, Democratic Republic of the", "Congo (Kinshasa)", "RDC"],
    "COG": ["Congo, Rep.", "Republic of Congo", "Congo-Brazzaville", "Congo, Republic of", "Congo (Brazzaville)"],
    "MDA": ["Moldova"], "VEN": ["Venezuela", "Venezuela, RB"],
    "PSE": ["Palestine", "West Bank and Gaza", "West Bank & Gaza"], "MKD": ["North Macedonia", "Macedonia"],
    "CZE": ["Czech Republic", "Czechia"], "TUR": ["Turkey", "Türkiye", "Turkiye"], "KGZ": ["Kyrgyz Republic", "Kyrgyzstan", "Кыргызская Республика", "Киргизия"],
    "EGY": ["Egypt", "Egypt, Arab Rep."], "AZE": ["Azərbaycan", "Азербайджанская Республика"], "YEM": ["Yemen", "Yemen, Rep."], "GMB": ["Gambia", "The Gambia", "Gambia, The"],
    "BHS": ["Bahamas", "Bahamas, The"], "CIV": ["Ivory Coast", "Cote d'Ivoire", "Côte d’Ivoire"], "CPV": ["Cape Verde"],
    "SWZ": ["Swaziland", "Eswatini"], "FSM": ["Micronesia, Fed. Sts.", "Micronesia, Federated States of"], "SVK": ["Slovak Republic", "Slovakia"],
    "STP": ["Sao Tome and Principe", "São Tomé and Príncipe"], "TLS": ["East Timor", "Timor Leste"], "MMR": ["Burma"],
    "BRN": ["Brunei Darussalam"], "KNA": ["St. Kitts and Nevis"], "LCA": ["St. Lucia"],
    "NGA": ["Niger Delta"],  # 尼日尔三角洲在尼日利亚,免得配成尼日尔
    "VCT": ["St. Vincent and the Grenadines"], "HKG": ["Hong Kong SAR, China"], "MAC": ["Macao SAR, China"],
}
# M49 表里没有、但开发银行常用的国别
EXTRA = [
    {"iso3": "XKX", "iso2": "XK", "m49": "", "name_zh": "科索沃", "name_en": "Kosovo", "continent_zh": "欧洲", "continent_en": "Europe",
     "region_zh": "南欧", "region_en": "Southern Europe", "aliases": ["Kosovo", "Косово"]},
]
# 世界银行系接口的国别码与 ISO2 不同的(见 README"已知的实施级陷阱")
WB_CODES = {"ZR": "COD", "TP": "TLS", "YF": "SRB", "XK": "XKX", "KV": "XKX", "WG": "PSE", "GZ": "PSE", "RY": "YEM"}


def table(soup, lang: str) -> list[dict]:
    rows = soup.select(f"#downloadTable{lang} tr")
    head = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    return [dict(zip(head, (c.get_text(strip=True) for c in r.find_all(["th", "td"])))) for r in rows[1:]]


def main(src: str | None) -> None:
    html = Path(src).read_text(encoding="utf-8") if src else httpx.get(URL, headers={"User-Agent": UA}, timeout=90).text
    soup = BeautifulSoup(html, "lxml")
    langs = {lang: {r["ISO-alpha3 Code"]: r for r in table(soup, lang) if r.get("ISO-alpha3 Code")}
             for lang in ("EN", "ZH", "RU", "FR", "ES")}
    out = []
    for iso3, en in sorted(langs["EN"].items()):
        zh = langs["ZH"][iso3]
        inter_en, inter_zh = en.get("Intermediate Region Name"), zh.get("Intermediate Region Name")
        use_inter = bool(inter_en) and inter_en != "Channel Islands"
        names = [en["Country or Area"], zh["Country or Area"], langs["RU"][iso3]["Country or Area"],
                 langs["FR"][iso3]["Country or Area"], langs["ES"][iso3]["Country or Area"],
                 SHORT_EN.get(iso3), SHORT_ZH.get(iso3), *ALIASES.get(iso3, [])]
        out.append({
            "iso3": iso3, "iso2": en["ISO-alpha2 Code"], "m49": en["M49 Code"],
            "name_zh": SHORT_ZH.get(iso3, zh["Country or Area"]), "name_en": SHORT_EN.get(iso3, en["Country or Area"]),
            "continent_zh": zh["Region Name"] or "其他", "continent_en": en["Region Name"] or "Other",
            "region_zh": (inter_zh if use_inter else zh["Sub-region Name"]) or "其他",
            "region_en": (inter_en if use_inter else en["Sub-region Name"]) or "Other",
            "aliases": sorted({n for n in names if n}),
        })
    out += EXTRA
    doc = {"schema": "osint-water/countries@1",
           "source": f"联合国统计司 M49 标准({URL});简称、别名与科索沃为本库补充",
           "wb_codes": WB_CODES, "countries": out}
    fp = ROOT / "reference" / "countries.yaml"
    fp.write_text("# 自动生成:python scripts/build_countries.py。国别代码、中英文名、大洲与区域、识别用别名。\n"
                  + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"{len(out)} 个国别 → {fp}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
