"""A28 导入已中标项目:按私有配置仓 data/ 下的 awards*.csv.gz(一个来源一份)把已授出的合同建进「已中标项目」表;只新建、只填空(R2),去重按编号。
数据由 scripts/fetch_wb_awards.py 从世界银行公开的合同授予数据取(awards.csv.gz,那个脚本用这里的 classify / quantity 归类、摘工程量),
scripts/fetch_ma_awards.py 从摩洛哥公共采购网的结果公告取(awards_mar.csv.gz);别的来源整理成同样的列(COLS)也能导入。
城市:合同名称、所属项目名或执行地点(place 列,只用来认城市)里出现了该国某座规模以上城市的名称才挂(不分大小写、不计重音),几座都出现取人口最多的。
供应商(v2.15):winners 列是 JSON 列表 [{name, iso3, country_zh, ext}],每家一行建进「供应商」表(标识 = contractor_key(名称),同名的并成一家),
再把合同的「中标方」关联到这几家;同样只新建、只填空。
v2.21:estimate(招标估价,原币,与中标价同币种;中标价是它的 0.3–2 倍才写,ESTIMATE_RATIO)、eval_days(评标天数)、bidders(投标家数)、
asset(主体工程,没有这一列时按名称认,见 asset)四列有就填空。
后台每个周期另有 translate:给没有中文名的行请模型译中文名(配了模型时,一次 TRANSLATE_BATCH 条)。
命令行:python -m core.jobs.awards [--file 路径] [--dry-run]。库里还没建这张表时跳过。"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path

from core import tstore

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "private" / "data" / "awards.csv.gz"


def data_files() -> list[Path]:
    """私有配置仓 data/ 下的全部数据文件(awards.csv.gz、awards_mar.csv.gz……),按文件名排。"""
    return sorted(DATA.parent.glob("awards*.csv.gz"))
OID = "award"
COLS = ["key", "iso3", "name", "sector", "contract_type", "price", "currency", "awarded", "quantity", "winner", "winner_country",
        "project", "funder", "method", "source", "url", "winners", "buyer", "place", "estimate", "eval_days", "bidders", "asset", "note"]
TEXT = ("name", "sector", "contract_type", "currency", "awarded", "quantity", "winner", "winner_country", "project", "funder", "method", "buyer",
        "source", "url", "note")  # note(备注):手工整理的来源只有这一列能写「待核实」与出处说明,有就填空

# 类别(lead_sectors):按合同名称里的词归,先中先得(海水淡化 → 污水 → 雨水排涝 → 供水 → 水利 → 水环境);英、法、西、葡、越南文都认。
# 只有泛泛的「水」字样的算其他;一个都不中的不是水务合同(fetch 时丢掉)。
SECTOR_WORDS: list[tuple[str, str]] = [
    ("海水淡化", r"desalin|dessal|desalaci|khử mặn"),
    ("污水", r"sewer|sewage|waste ?water|\bw?wtp\b|\bstp\b|\bfstp\b|sanitation|latrine|toilet|septic|f[a]?ecal|sludge|assainissement|eaux us[ée]es|"
            r"alcantarillado|aguas residuales|saneamiento|esgot|nước thải|vệ sinh"),
    ("雨水排涝", r"drain|storm ?water|caniveau|drenaje|pluvial|thoát nước|inundaci[oó]n urbana"),
    ("供水", r"water supply|drinking|potable|\bwtp\b|water treatment|distribution (?:network|pipe|line)|transmission (?:main|pipe|line)|borehole|tube ?wells?|"
            r"water (?:storage|tank|scheme|meter|connection|kiosk|point|pipe|network|system|works|utilit)|piped water|eau potable|adduction|\baep\b|forages?|branchements?|"
            r"agua potable|abastecimiento|acueducto|abastecimento|água|cấp nước|nước sạch|non-revenue|\bnrw\b"),
    ("水利", r"irrigat|\bdams?\b|canal|embankment|levee|\bdykes?\b|\bdikes?\b|\bweirs?\b|barrage|flood|river ?bank|reservoir|hồ chứa|riego|irriga[çc]|"
            r"thủy lợi|đê|hydraulique agricole|périmètre"),
    ("水环境", r"river restoration|lake|wetland|watershed|catchment|water pollution|water quality|basin management|ecolog|rivière|cours d'eau"),
    ("其他", r"\bwater\b|\beau\b|\bagua\b|\bnước\b|hydro"),
]
_SECTOR_RE = [(s, re.compile(p, re.I)) for s, p in SECTOR_WORDS]

# 世行归在农业、防灾、城市等非水务实践的项目里也有水务合同(灌渠、输水管道、防洪堤):这类合同要过更严的词(STRICT_WORDS),
# 而且名称里不能有道路、学校、医院、电力、种子化肥之类明显不是水务工程的字样(NOT_WATER)。「其他」「水环境」两类在这里不收。
STRICT_WORDS: list[tuple[str, str]] = [
    ("海水淡化", r"\bdesalinat|\bdessalement|\bdesalaci[oó]n|\bdessaliniza"),
    ("污水", r"sewer|sewage|waste ?water|\bw?wtp\b|\bfstp\b|fa?ecal sludge|eaux us[ée]es|assainissement liquide|alcantarillado|aguas residuales|esgoto|nước thải"),
    ("雨水排涝", r"storm ?water|eaux pluviales|assainissement pluvial|drenaje pluvial|urban drainage|drainage (?:system|network|master ?plan) of (?:the )?(?:city|town)"),
    ("供水", r"water supply|drinking water|potable|\bwtp\b|water treatment plant|piped water|water (?:storage|tank|kiosk|scheme|network|pipeline|transmission|distribution)|"
            r"\baep\b|adduction d.eau|eau potable|agua potable|abastecimiento de agua|abastecimento de [áa]gua|cấp nước|nước sạch|boreholes?\b|tube ?wells?"),
    ("水利", r"irrigat|irriga[çc]|\briego\b|thủy lợi|canal (?:principal|secondaire|lining|rehabilitation)|(?:main|secondary|tertiary|feeder|irrigation) canal|"
            r"\b(?:earth |small |check )?dams?\b(?!\s+(?:road|route))|barrage|\bweirs?\b|\bdykes?\b|\bdikes?\b|river ?bank protection|river embankment|flood (?:protection|control|defen[cs]e|wall|mitigation)|"
            r"protection contre les (?:crues|inondations)|périmètres? irrigu|reservoir"),
]
_STRICT_RE = [(s, re.compile(p, re.I)) for s, p in STRICT_WORDS]
NOT_WATER = re.compile(r"\broads?\b|\broute\b|voirie|rodaje|pavement|bitum|culvert|\bbridges?\b|\bpont\b|school|classroom|salles? de classe|\b[ée]cole|"
                       r"health|hospital|clinic|\bsant[ée]\b|\bRHCs?\b|abattoir|slaughter|\bmarkets?\b|march[ée] (?:central|municipal)|seeds?\b|semences?|fertili[sz]|engrais|"
                       r"floodlight|\bpark\b|electricity|[ée]lectricit|power (?:line|plant|distribution|supply)|\bkv\b|substation|transmission line|street ?light|"
                       r"fodder|vehicle|v[ée]hicule|trucks?\b|camions?|tractors?|computer|furniture|mobilier|textbook|teacher|airport|runway|housing|logements?|"
                       r"research station|\bsalles?\b|b[âa]timents?|\bbuildings?\b|warehouse|entrep[ôo]t|plomberie", re.I)


def classify_strict(text: str | None) -> str | None:
    """非水务实践项目里的合同 → 类别;拿不准的返回 None(宁缺毋滥)。"""
    t = str(text or "")
    if NOT_WATER.search(t):
        return None
    return next((s for s, rx in _STRICT_RE if rx.search(t)), None)

# 工程量:合同名称里带单位的数量(原文照摘,最多 4 处)
_QTY = re.compile(r"(?<![\w.+/:-])(\d[\d,.]*\s?(?:km|kms|kilom[eè]tres?|kilometers?|m3/d(?:ay)?|m³/d(?:ay)?|m3/j|m3/h|m3\b|m³|cubic met(?:er|re)s?|mld|ml/d|l/s|"
                  r"ha\b|hectares?|tube ?wells?|boreholes?|wells?|forages?|connections?|branchements?|latrines?|villages?|reservoirs?|"
                  r"(?:water )?tanks?|pumping stations?|stations? de pompage|bornes? fontaines?|(?:water )?kiosks?|meters?|m[eè]tres|lm\b|rm\b|m\b))", re.I)

# 合同类型「运营」:工程、非咨询服务类合同里,名称写明是运营维护、委托运营、管理合同、租赁经营的(咨询、货物不算)。
# 法文的「Exploitation de la STEP」「Gestion et exploitation」「Entretien et maintenance」「Entretien courant」也算(摩洛哥采购网的运维合同多这样写;
# 「forages d'exploitation」是生产井、「Travaux d'entretien et réparation」是修理工程,不算);俄文的「эксплуатация и обслуживание」同理
_OPS = re.compile(r"operation(?:s)? (?:and|&) maintenance|\bO ?& ?M\b|\bO&M\b|management contract|affermage|gestion d[ée]l[ée]gu[ée]e|"
                  r"exploitation et (?:de )?maintenance|operaci[oó]n y mantenimiento|opera[çc][ãa]o e manuten[çc][ãa]o|"
                  r"performance[- ]based (?:contract|management|operation)|lease contract|operate and maintain|"
                  r"\bco-?exploitation\b|exploitation(?:,| et)\s*(?:de |d['’]|l['’]|la )?(?:entretien|maintenance|gestion|surveillance)|"
                  r"(?:gestion|surveillance)(?:,| et)\s*(?:de |d['’]|l['’])?(?:entretien|exploitation)|"
                  r"exploitation (?:des|du|de la|de l['’])\s*(?:STEP|stations?|r[ée]seaux?|syst[èe]mes?|ouvrages?|installations?|usines?|unit[ée]s?)|"
                  r"entretien (?:et |& )?(?:de (?:la )?)?maintenance|maintenance (?:et |& )?(?:d['’])?entretien|entretien courant|"
                  r"эксплуатаци\w* и (?:техническ\w* )?обслуживани|техническ\w* обслуживани", re.I)
TYPE_ZH = {"Works": "工程", "Goods": "货物", "Consultant Services": "咨询服务", "Non-consulting Services": "非咨询服务"}
METHOD_ZH = {"Request for Bids": "公开招标(RFB)", "Request for Quotations": "询价(RFQ)", "Direct Selection": "直接选定",
             "Quality And Cost-Based Selection": "质量和费用选择(QCBS)", "Consultant Qualification Selection": "咨询顾问资格选择(CQS)",
             "Least Cost Selection": "最低费用选择(LCS)", "Quality Based Selection": "质量选择(QBS)", "Fixed Budget Selection": "固定预算选择(FBS)",
             "Request for Proposals": "建议书邀请(RFP)", "Community Driven Development": "社区主导", "UN Agencies (Direct)": "联合国机构(直接)",
             "E-Auctions": "电子反拍", "Force Account": "自营工程", "Public Private Partnership": "公私合营(PPP)",
             "Individual Consultant Selection": "个人咨询顾问选择", "Non-Profit Organizations": "非营利组织(直接)",
             # 2019 年前的叫法(scripts/fetch_wb_old_awards.py)
             "International Competitive Bidding": "国际竞争性招标(ICB)", "National Competitive Bidding": "国内竞争性招标(NCB)",
             "Limited International Bidding": "有限国际招标(LIB)", "Shopping": "询价采购", "International Shopping": "国际询价采购",
             "National Shopping": "国内询价采购", "Direct Contracting": "直接签约", "Single Source Selection": "单一来源选择(SSS)",
             "Selection Under a Fixed Budget": "固定预算选择(FBS)", "Commercial Practices": "商业惯例采购"}

# 名称看得出是中国企业的(各来源的地址常写的是它在当地的分公司,注册国按这个改成中国)
CHINESE_FIRM = re.compile(r"\bCHINA\b|\bSINO|\bCHINESE\b|POWERCHINA|HYDROCHINA|\bCSCEC\b|\bCRCC\b|\bCRBC\b|\bCHEC\b|\bCAMCE\b|\bCMEC\b|\bCGGC\b|"
                          r"\bCAMC\b|\bCNEEC\b|\bCITIC\b|\bCREC\b|\bCR\d{1,2}G\b|\bTBEA\b|\(P\.?R\.?C\.?\)|\bPRC\b", re.I)

CITY_MIN_LEN = 5  # 城市英文名短于这个的只在整段文字就是这个名字时才认(Ica、Bo 这类会误中;Osh 由来源写明)


def classify(text: str | None) -> str | None:
    """合同名称 → 类别;不是水务合同返回 None。"""
    t = str(text or "")
    return next((s for s, rx in _SECTOR_RE if rx.search(t)), None)


def quantity(text: str | None, most: int = 4) -> str:
    """合同名称里带单位的数量,「、」隔开;「Lot 1」这类编号、年份不算。"""
    out: list[str] = []
    for m in _QTY.finditer(str(text or "")):
        q = re.sub(r"\s+", " ", m.group(1)).strip()
        num = re.match(r"[\d,.]+", q).group(0).replace(",", "")
        if re.fullmatch(r"\d+\s?m", q, re.I) and len(num) < 3:  # 「5m」多半是宽度、深度,不是长度
            continue
        try:
            if float(num.rstrip(".")) == 0:
                continue
        except ValueError:
            continue
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
        if len(out) >= most:
            break
    return "、".join(out)


def contract_type(category: str | None, name: str | None) -> str:
    """世行采购类别 + 合同名称 → 合同类型(award_contract_type);认不出的类别返回空串。"""
    zh = TYPE_ZH.get(str(category or "").strip(), "")
    t = str(name or "")
    side = re.search(r"(?:includ\w*|with|plus|and)\s+(?:the\s+)?(?:O ?& ?M|operation)|\b(?:design|build|construct|fabricat|install)", t, re.I)  # 设计建设(含运维)仍是工程
    return "运营" if zh in ("工程", "非咨询服务") and _OPS.search(t) and not side else zh


# 承包模式(award_delivery,使用方 2026-10-13 要能单独查 DBO、EPC):合同类型说买的是什么(工程、货物、服务),承包模式说怎么包。
# 按名称(与采购方式)认,先中先得:DBO(一份合同里既建或改造、又负责建成后的运营维护,含 DBOT、施工加若干年运维)→ BOT/PPP(特许经营、混合年金)
# → 设计施工(EPC、交钥匙)→ 按合同类型(工程 = 施工,运营 = 运营)。咨询服务不分,名称是咨询、监理、技术援助的也不分(给 DBO 合同做监理、给 PPP 做顾问的不算);
# 货物、非咨询服务名称里没写明的留空。法文「forages d'exploitation」是生产井,不算运营
DELIVERY_WORDS: list[tuple[str, str]] = [
    ("DBO", r"\bD\.?B\.?O\.?(?:T|M)?\b|design[\s,/&-]*(?:and\s+)?build[\s,/&-]*(?:and\s+)?operat|design,?\s+(?:construct\w*|build\w*),?\s+(?:and\s+)?operat|"
            r"(?:conception|r[ée]alisation|construction|mise [àa] niveau|r[ée]habilitation|extension|renforcement)[\s,-]+(?:et\s+)?(?:l['’]\s*)?exploitation(?!\s+(?:d['’]|de |des )?forages?)|"
            r"(?:build|construct(?:ion)?|rehabilitat(?:e|ion)|upgrad(?:e|ing|ation)|expansion)[\s,]+(?:and\s+)?operat(?:e|ion)\b|"
            r"(?:design|build|construct|rehabilit|upgrad|expan|réalis|realis)\w*\b.{0,200}?(?:includ\w*|with|plus|and|&)\s+(?:(?:the|its|\d+|five|ten|three|seven)\s+)*"
            r"(?:years?\s+(?:of\s+)?)?(?:O ?& ?M|operations?)\b(?!\s+(?:room|cent(?:er|re)|building|office|of (?:the )?(?:gates?|barrage)))|"
            r"проектир\w*[^.;]{0,60}эксплуатац|строительств\w*[^.;]{0,30}эксплуатац|设计.{0,6}(?:建设|建造|施工).{0,6}运营"),
    ("BOT/PPP", r"\bB\.?O\.?O?\.?T\b|\bBOO\b|\bPPP\b|public[- ]private[- ]partnership|partenariat public[- ]priv|hybrid annuity|\bconcession\b|"
                r"build[\s,-]+(?:own[\s,-]+)?operat\w*[\s,-]+(?:and\s+)?transfer|公私合营|特许经营"),
    ("设计施工", r"\bEPC\b|design[\s,/&-]*(?:and\s+)?build|design[\s,&-]+(?:and\s+)?construct|design,?\s+suppl\w*,?\s+(?:and\s+)?install|turn-?key|"
                 r"cl[ée]s? en main|conception[\s,-]+(?:et\s+)?(?:r[ée]alisation|construction)|проектир\w*\s+и\s+строительств|设计施工|工程总承包|交钥匙"),
]
_DELIVERY_RE = [(v, re.compile(p, re.I)) for v, p in DELIVERY_WORDS]
_ADVICE = re.compile(r"^\W*(?:\w+\s+){0,3}?(?:consult(?:ant|ancy|ancies|ing)|supervis|advis|assistance|conseil|[ée]tude|audit|independent (?:engineer|verification))|"
                     r"\b(?:transaction advis|assistance technique|assistance relative|ing[ée]nieur ind[ée]pendant)", re.I)


def delivery(contract_type: str | None, name: str | None, method: str | None = None) -> str:
    """合同类型 + 名称(+ 采购方式)→ 承包模式;咨询服务、认不出的返回空串。"""
    if contract_type == "咨询服务":
        return ""
    t = f"{name or ''} {method or ''}"
    hit = None if _ADVICE.search(str(name or "")) else next((v for v, rx in _DELIVERY_RE if rx.search(t)), None)
    return hit or {"工程": "施工", "运营": "运营"}.get(contract_type or "", "")


# 主体工程(本体 award_asset,v2.21):按名称认。先看名称里最后一个「Lot …:」「Contract title:」后面那段(这一包具体干什么),认不出再看全名。
# 厂:海水淡化厂 → 污水处理厂 → 供水厂(供水厂的词旁边写着污水的算污水处理厂);厂和管网都写了的,管网在前、厂只是修饰(「供某水厂的输水管」)算管网,否则算厂。
# 英、法、俄、西、葡、印尼、越南文都认;大写缩写(STEP、КОС、ВОС、ETA)只认大写。2026-10-13 按 711 份名称里带厂字样的合同人工核过一遍
_R = lambda p: re.compile(p, re.I)  # noqa: E731
_DESAL = _R(r"desalin|dessal|d[ée]salement|опреснен|(?-i:\bSWRO\b|\bBWRO\b)|desalaci|dessaliniza|sea ?water (?:reverse )?osmosis|osmose inverse|d[ée]min[ée]ralisation")
_WWTP = _R(r"waste ?water treatment|\bWWTPs?\b|\bSTPs?\b|\bFSTPs?\b|sewage treatment|sewerage treatment|effluent treatment|"
           r"f(?:a|ae)?ecal[- ]sludge (?:treatment|management (?:plant|facilit))|\bFS treatment|sludge treatment|septage treatment|treatment of (?:waste ?water|sewage)|"
           r"station[s]? d.?[ée]puration|(?-i:\bSTEPs?\b)|[ée]puration des eaux|lagunage|traitement des eaux us[ée]es|traitement des margines|"
           r"очистн\w* сооружен\w*(?! (?:питьев|водопровод|воды питьев))|(?-i:\bКОС\b)|канализационн\w* очистн|"
           r"(?-i:\bETARs?\b|\bPTARs?\b)|planta de tratamiento de aguas residuales|esta[cç][aã]o de tratamento de esgoto|(?-i:\bIPAL\b)|"
           r"treatment plant.{0,40}(?:sewage|wastewater|sanitation)|(?:sewage|wastewater).{0,40}treatment plant|imhoff|oxidation pond|stabili[sz]ation pond|污水处理厂|污水厂")
_WTP = _R(r"water treatment (?:plant|works|facilit|station)|\bS?WTPs?\b|water purification|purification plant|filtration plant|"
          r"station[s]? de traitement|traitement (?:de l.)?eau potable|potabilisation|"
          r"водоочистн|(?-i:\bВОС\b)|станци\w* (?:водо)?очистк|очистн\w* сооружен\w* (?:питьев|водопровод)|"
          r"(?-i:\bETAs?\b|\bPTAPs?\b)|planta (?:de )?potabiliza|esta[cç][aã]o de tratamento de [aá]gua|(?-i:\bIPA\b)|"
          r"treatment plant|nh[aà] m[aá]y n[uư][oớ]c|iron removal|defluorid|arsenic removal|净水厂|自来水厂")
_NET = _R(r"pipe|network|distribution (?:system|line|main|net)|reticulation|transmission (?:main|line)|trunk main|rising main|"
          r"\bmains?\b|sewer|collector|interceptor|house ?connection|service connection|drainage (?:network|system)|storm ?water drain|"
          r"r[ée]seau|conduite|canalisation|adduction|collecteur|intercepteur|branchement|[ée]missaire|"
          r"трубопровод|\bсет(?:ь|и|ей|ям|ях)\b|сетей|водовод|коллектор|"
          r"tuber[ií]a|red de (?:agua|alcantarillado|distribuci)|alcantarillado|rede de|adutora|jaringan|perpipaan|đường ống|"
          r"assainissement liquide|(?:waste ?water|sewage) collection|collection (?:system|network)|distribution works|acueducto|\bredes\b|ramais|\bdrains?\b|drainage|管网|管道")
_PUMP = _R(r"pump(?:ing)? station|station[s]? de (?:pompage|reprise|relevage)|насосн\w* станц|reservoir|water tower|elevated (?:tank|storage)|"
           r"ch[aâ]teau d.eau|r[ée]servoir|резервуар|\bboreholes?\b|\bwells?\b|wellfield|forage|скважин|водозабор|intake")
_SYSTEM = _R(r"water supply (?:system|scheme|project|works|facilit)|alimentation en eau potable|(?-i:\bAEP\b)|систем\w* водоснабжен|водоснабжени|"
             r"sistema de (?:acueducto|abastecimiento|agua)|abastecimento de [aá]gua|sistema de abastecimento|piped water|rural water supply|water supply|potable water system|"
             r"систем\w* питьев\w* вод|points? d.eau")
_SANI = _R(r"toilet|latrine|on-?site sanitation|sanitation facilit|hand ?washing|WASH (?:facilit|block)|blocs? sanitaires?|санузл|туалет|sanitarios")
_IRR = _R(r"irrigat|canal|\bdams?\b|barrage|levee|dyke|dike|embankment|river ?bank|flood|inondation|оросит|ирригац|канал|дамб|селе|паводк|"
          r"берегоукреп|watershed|catchment|khettara|s[ée]guia|périmètre")
_FOCUS = _R(r"(?:\blot\s*(?:n\s*°|no\.?|n°)?\s*[\w\-]{1,4}\s*[:\-–]|contract tit+le\s*:|\bpackage\s*[\w\-]{1,4}\s*[:\-–])")
_HEAD_PIPE = _R(r"^\W*(?:supply|purchas\w*|procurement|fourniture|acquisition|laying|pose|installation)?\W*(?:\w+\W+){0,6}?"
                r"(?:pipes?|pipeline|conduite|canalisation|tuyau\w*|трубопровод|трубы)\b")
_OF = _R(r"(?:pipes?|pipeline|network|conduite|r[ée]seau|canalisation|трубопровод|сет\w*)\b.{0,60}?\b(?:for|of|to|from|de|du|des|pour|vers|à|au|aux|для|к)\b")
_PLANTS = (("海水淡化厂", _DESAL), ("污水处理厂", _WWTP), ("供水厂", _WTP))


def _plant(t: str) -> str | None:
    for k, rx in _PLANTS:
        if rx.search(t):
            return "污水处理厂" if k == "供水厂" and re.search(r"waste ?water|sewage|sewer|eaux us|канализ|margines|f(?:a|ae)?ecal|污水", t, re.I) else k
    return None


def _asset_of(t: str) -> str | None:
    p, net, sysm = _plant(t), _NET.search(t), _SYSTEM.search(t)
    if not p and sysm and (not net or sysm.start() < net.start()):
        return "供水系统(综合)"
    if p and net:
        first = min(rx.search(t).start() for _, rx in _PLANTS if rx.search(t))
        return "管网" if net.start() < first and (_HEAD_PIPE.search(t) or _OF.search(t[net.start():])) else p
    return p or ("管网" if net else None)


def asset(name: str | None, sector: str | None = None) -> str:
    """合同名称(+ 类别)→ 主体工程(本体 award_asset 的值);认不出的算「其他」。"""
    t = str(name or "")
    parts = _FOCUS.split(t)
    focus = parts[-1] if len(parts) > 1 and len(parts[-1].strip()) >= 10 else ""
    got = (_asset_of(focus) if focus else None) or _asset_of(t)
    if got:
        return got
    if _SANI.search(t):
        return "卫生设施"
    if any(src and _PUMP.search(src) for src in (focus, t)):
        return "泵站水池水源"
    if _IRR.search(t) or sector in ("水利", "雨水排涝"):
        return "水利防洪"
    return "其他"


# 供应商标识:名称大写、去标点、去开头的 M/S 与结尾的公司后缀(可连着几个),空白并成一个
_SUFFIX = {"LLC", "LTD", "LIMITED", "CO", "COMPANY", "CORP", "CORPORATION", "INC", "INCORPORATED", "JSC", "OJSC", "CJSC", "PJSC", "OAO", "ZAO",
           "OOO", "TOO", "SA", "SAS", "SARL", "SRL", "SPA", "SAU", "SL", "SAC", "LTDA", "LDA", "CIA", "PVT", "PRIVATE", "PLC", "LLP", "GMBH",
           "AG", "BV", "NV", "AB", "AS", "PTY", "PT", "TBK", "BHD", "SDN", "KG", "EIRL", "SE", "AND", "Ş", "ŞTI", "STI", "AŞ", "A", "S", "L", "C"}
_PREFIX = {"LLC", "OOO", "TOO", "JSC", "OJSC", "CJSC", "PT", "CV", "SARL", "SA", "ETS", "STE"}
_PHRASES = re.compile(r"\b(?:GESELLSCHAFT MIT BESCHR[ÄA]NKTER HAFTUNG|LIMITED LIABILITY (?:COMPANY|PARTNERSHIP)|(?:OPEN |CLOSED |PUBLIC )?JOINT[ -]STOCK COMPANY|"
                      r"ANON[İI]M [ŞS][İI]RKET[İI]|L[İI]M[İI]TED [ŞS][İI]RKET[İI]|SANAY[İI] VE T[İI]CARET|C[ÔO]NG TY (?:C[ỔO] PH[ẦA]N|TNHH(?: MTV)?)|"
                      r"PRIVATE LIMITED|SOCI[ÉE]T[ÉE] ANONYME|SOLE PROPRIETORSHIP)\b")


def contractor_key(name: str | None) -> str:
    t = str(name or "").upper().replace("&", " AND ")
    t = re.sub(r"^\s*(?:M\s*[/.]?\s*S\b\.?|MESSRS\.?)\s*", "", t)  # M/S、M/s.、Messrs
    t = re.sub(r"\b([A-Z])\.(?=[A-Z]\.)", r"\1", t)  # S.A. / P.L.C. → SA / PLC
    t = re.sub(r"(?<=\b[A-Z])\.(?=\s|$)", "", t)
    t = _PHRASES.sub(" ", t)
    words = re.sub(r"[^\w]+", " ", t).split()
    while len(words) > 1 and words[0] in _PREFIX:
        words.pop(0)
    while len(words) > 1 and words[-1] in _SUFFIX:
        words.pop()
    return " ".join(words)


def winners_of(row: dict) -> list[dict]:
    """数据文件一行的 winners 列 → [{name, iso3, country_zh, ext}];没有或读不了时为空。"""
    try:
        xs = json.loads(row.get("winners") or "[]")
    except ValueError:
        return []
    return [x for x in xs if isinstance(x, dict) and str(x.get("name") or "").strip()] if isinstance(xs, list) else []


def read_rows(path: Path) -> list[dict]:
    raw = path.read_bytes()
    text = gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def write_rows(path: Path, rows: list[dict], cols: list[str] | None = None) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols or COLS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == ".gz" else data)


def _num(v: str | None) -> float | None:
    try:
        x = float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None
    return x if x is None or math.isfinite(x) else None


def _fold(text: str) -> str:
    """去掉重音(Kénitra → Kenitra),认城市名时用。"""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


class CityIndex:
    """城市表:国别记录编号 → [(英文名正则, 人口, 城市记录编号)],人口从多到少;不计重音。"""

    def __init__(self, b: tstore.Base):
        self.by_country: dict[str, list[tuple[re.Pattern, float, str]]] = {}
        if "city" not in b.t:
            return
        for r in b.rows("city", props=["name_en", "country", "population"]):
            name, crid = str(b.get("city", r, "name_en") or "").strip(), tstore.link_id(b.get("city", r, "country"))
            if crid and name:
                rx = re.compile((r"(?<!\w)" if len(name) >= CITY_MIN_LEN else r"^\s*") + re.escape(_fold(name))
                                + (r"(?!\w)" if len(name) >= CITY_MIN_LEN else r"\s*$"), re.I)
                self.by_country.setdefault(crid, []).append((rx, float(b.get("city", r, "population") or 0), r["id"]))
        for v in self.by_country.values():
            v.sort(key=lambda x: -x[1])

    def find(self, country_rid: str, *texts: str | None) -> str | None:
        for t in texts:
            if t:
                t = _fold(t)
                hit = next((rid for rx, _, rid in self.by_country.get(country_rid, []) if rx.search(t)), None)
                if hit:
                    return hit
        return None


ESTIMATE_RATIO = (0.3, 2.0)  # 中标价 ÷ 招标估价出了这个范围的,多半是标段或公告没对上:估价不写


def values(row: dict, country_rid: str, city_rid: str | None) -> dict:
    """数据文件的一行 → 本体属性的值(空的不写)。"""
    v: dict = {k: (str(row.get(k) or "").strip() or None) for k in TEXT}
    v["price"], v["estimate"] = _num(row.get("price")), _num(row.get("estimate"))
    v["country"], v["city"] = country_rid, city_rid
    if v["price"] is not None:
        v["price"] = round(v["price"], 2)
    if v["estimate"] is not None:
        lo, hi = ESTIMATE_RATIO
        v["estimate"] = round(v["estimate"], 2) if v["price"] and v["estimate"] > 0 and lo <= v["price"] / v["estimate"] <= hi else None
    days, firms = _num(row.get("eval_days")), _num(row.get("bidders"))
    v["eval_days"] = int(days) if days is not None and 0 <= days <= 900 else None  # 一年多的多半是日期写错,本体 award.eval_days
    v["bidders"] = int(firms) if firms is not None and 1 <= firms <= 200 else None
    v["asset"] = str(row.get("asset") or "").strip() or asset(row.get("name"), v.get("sector"))
    return {k: x for k, x in v.items() if x is not None}


def run(b: tstore.Base, path: Path | list[Path] | None = None, dry_run: bool = False, log=print) -> dict:
    """→ {新建, 补空, 挂上城市, 跳过(国别表里没有)};表没建或没有数据文件时跳过。path 不给时读 data/ 下的全部数据文件。"""
    paths = [path] if isinstance(path, Path) else list(path) if path else data_files()
    if OID not in b.t:
        return {"跳过": "库里还没有「已中标项目」表(先跑建表命令)"}
    paths = [p for p in paths if p.exists()]
    if not paths:
        return {"跳过": f"没有数据文件 {DATA.parent}/awards*.csv.gz"}
    rows = [r for p in paths for r in read_rows(p)]
    countries = {str(b.get("country", r, "key")): r["id"] for r in b.rows("country", props=["key"]) if b.get("country", r, "key")}
    cities = CityIndex(b)
    props = ["key", "country", "city", *[p for p in (*TEXT, "price", "estimate", "eval_days", "bidders", "asset", "winners", "delivery") if b.has(OID, p)]]
    have = {str(b.get(OID, r, "key")): r for r in b.rows(OID, props=props) if b.get(OID, r, "key")}
    allowed = {p: {c["name"] for c in (b.field(OID, p).get("options") or {}).get("choices") or []} for p in ("sector", "contract_type", "currency", "delivery", "asset") if b.has(OID, p)}
    with_firms = b.has(OID, "winners") and "contractor" in b.t
    firms, n_firm_new, n_firm_fill = _contractors(b, rows, countries, dry_run) if with_firms else ({}, 0, 0)
    new, ups, n_city, skipped, seen = [], [], 0, 0, set()
    for row in rows:
        key = str(row.get("key") or "").strip()
        crid = countries.get(str(row.get("iso3") or "").strip())
        if not key or key in seen:
            continue
        seen.add(key)
        if not crid:
            skipped += 1
            continue
        city = cities.find(crid, row.get("name"), (row.get("project") or "").split(" ", 1)[-1], row.get("place"))
        want = {k: x for k, x in values(row, crid, city).items() if k in ("country", "city") or b.has(OID, k)}
        ids = list(dict.fromkeys(firms[k] for k in (contractor_key(w["name"]) for w in winners_of(row)) if firms.get(k)))
        if ids:
            want["winners"] = ids
        rec = have.get(key)
        if b.has(OID, "delivery"):  # 承包模式按库里现在的合同类型认(人改过的以人为准)
            ct = (b.get(OID, rec, "contract_type") if rec else None) or want.get("contract_type")
            want["delivery"] = delivery(ct, row.get("name"), row.get("method")) or None
            if want["delivery"] is None:
                want.pop("delivery")
        for p, ok in allowed.items():  # 选项里没有的值不写(建表时选项来自本体)
            if p in want and want[p] not in ok:
                want.pop(p)
        if rec is None:
            new.append({"key": key, **want})
            n_city += bool(city)
            continue
        cur = {p: b.get(OID, rec, p) for p in want}
        changes, _ = tstore.fill_empty({**cur, "winners": tstore.link_ids(cur.get("winners"))} if "winners" in cur else cur, want)
        if changes:
            ups.append((rec["id"], changes))
            n_city += "city" in changes
    if not dry_run:
        b.create(OID, new)
        b.update_many(OID, ups)
    out = {"新建": len(new), "补空": len(ups), "挂上城市": n_city, "国别表里没有": skipped}
    if with_firms:
        out.update({"新建供应商": n_firm_new, "供应商补空": n_firm_fill})
    log(f"  award:新建 {len(new)},补空 {len(ups)},挂上城市 {n_city},国别表里没有的跳过 {skipped}"
        + (f";供应商新建 {n_firm_new},补空 {n_firm_fill}" if with_firms else "") + ("(只算,没写)" if dry_run else ""))
    return out


def _contractors(b: tstore.Base, rows: list[dict], countries: dict, dry_run: bool) -> tuple[dict[str, str | None], int, int]:
    """把数据文件里出现的供应商建进「供应商」表(国别表里没有的合同也不建它的单位)。
    → ({标识: 记录编号(只算不写时新建的是 None)}, 新建几家, 补空几家)"""
    C = "contractor"
    want: dict[str, dict] = {}
    for row in rows:
        if not countries.get(str(row.get("iso3") or "").strip()):
            continue
        for w in winners_of(row):
            k = contractor_key(w["name"])
            if not k:
                continue
            v = want.setdefault(k, {"name": " ".join(str(w["name"]).split()), "ext": [], "nationality": None, "nationality_code": None})
            v["nationality"] = v["nationality"] or str(w.get("country_zh") or "").strip() or None
            v["nationality_code"] = v["nationality_code"] or str(w.get("iso3") or "").strip() or None
            if w.get("ext") and w["ext"] not in v["ext"]:
                v["ext"].append(str(w["ext"]))
    props = [p for p in ("key", "name", "nationality", "nationality_code", "ext_ids") if b.has(C, p)]
    have = {str(b.get(C, r, "key")): r for r in b.rows(C, props=props) if b.get(C, r, "key")}
    new, ups = [], []
    for k, v in want.items():
        vals = {"name": v["name"], "nationality": v["nationality"], "nationality_code": v["nationality_code"], "ext_ids": ",".join(v["ext"][:20]) or None}
        vals = {p: x for p, x in vals.items() if x and b.has(C, p)}
        rec = have.get(k)
        if rec is None:
            new.append({"key": k, **vals})
            continue
        changes, _ = tstore.fill_empty({p: b.get(C, rec, p) for p in vals}, vals)
        if changes:
            ups.append((rec["id"], changes))
    ids: dict[str, str | None] = {k: r["id"] for k, r in have.items()}
    if dry_run:
        ids.update({x["key"]: None for x in new})
    else:
        ids.update({x["key"]: r["id"] for x, r in zip(new, b.create(C, new))})
        b.update_many(C, ups)
    return ids, len(new), len(ups)


ZH_SYSTEM = ("你是水务与国际工程领域的翻译。每份合同:zh 把名称译成简洁准确的中文(一般 40 字以内),工程内容、地名、标段号照译,"
             "机构用通用中文译名,合同号保留原文,不要解释;qty 写名称里明说了的主要工程量(数量 + 中文单位,如「配水管网 17 km」「水表 5 万只」,"
             "几项用「、」隔开),名称里没写数量的写空串,不要猜。")
ZH_GUIDE = '\n输出格式:只输出一个 JSON 对象 {"items": [{"i": 序号, "zh": "中文名", "qty": "工程量或空串"}, …]},不要任何其他文字。\n'
ZH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "zh": {"type": "string"}, "qty": {"type": "string"}}, "required": ["i", "zh", "qty"],
    "additionalProperties": False}}}, "required": ["items"], "additionalProperties": False}
TRANSLATE_BATCH = 40


def translate(b: tstore.Base, clf, limit: int = TRANSLATE_BATCH, log=print) -> int:
    """没有中文名的行请模型译一批,工程量空着的顺带从名称里摘;只填空。没配模型、表没建或字段没有时什么也不做。→ 写了几条"""
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "ask") or OID not in b.t or not b.has(OID, "name_zh"):
        return 0
    it = b.api.iter_records(b.t[OID], filter=b.filter(b.cond(OID, "name_zh", "isEmpty"), b.cond(OID, "name", "isNotEmpty")),
                            projection=[b.fid(OID, p) for p in ("name", "quantity") if b.has(OID, p)], take=limit)
    rows = [r for _, r in zip(range(limit), it)]  # 只要第一页
    if not rows:
        return 0
    names = [str(b.get(OID, r, "name") or "")[:300] for r in rows]
    out = clf.ask(ZH_SYSTEM, json.dumps([{"i": i, "t": t} for i, t in enumerate(names)], ensure_ascii=False), ZH_SCHEMA, ZH_GUIDE)
    if not out or out.get("_error"):
        log(f"  ! 已中标项目中文名没译成:{(out or {}).get('_error') or '模型未返回'}")
        return 0
    ups = []
    for it in out.get("items") or []:
        try:
            i, zh = int(it.get("i")), str(it.get("zh") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(rows) and zh:
            v = {"name_zh": zh[:200]}
            qty = str(it.get("qty") or "").strip()
            if qty and b.has(OID, "quantity") and not b.get(OID, rows[i], "quantity"):
                v["quantity"] = qty[:200]
            ups.append((rows[i]["id"], v))
    b.update_many(OID, ups)
    return len(ups)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.jobs.awards", description="A28 导入已中标项目(只新建、只填空)")
    ap.add_argument("--file", type=Path, action="append", help=f"数据文件,可给几个(缺省 {DATA.parent}/awards*.csv.gz 全部)")
    ap.add_argument("--dry-run", action="store_true", help="只算要建、要补多少,不写")
    a = ap.parse_args(argv)
    b = tstore.Base(tstore.Client(*tstore.credentials(admin=not os.environ.get("TEABLE_TOKEN"))))  # 机器账号的令牌;只有管理员令牌时用它
    print(run(b, a.file, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
