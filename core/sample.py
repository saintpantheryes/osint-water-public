"""游客看的示例数据:编出来的线索、商机、国别、城市、信源、时间节点、日报周报,真实库的记录一条也不给游客。

为什么:用户定(2026-10-09)游客只看测试数据。页面不用改:游客的读表请求本来就走 core/guest.py 的只读通道,这里给它换一个
「示例客户端」——表、字段、视图这些结构照旧从真实库读(页面用的表编号、字段编号与真实库一致),记录则全部来自本文件编的数据,
按表格接口的样子回(关联是 {id, title},日期是北京时间当天零点的 UTC 时间,多选是列表),条件、排序、分页、计数在内存里算。
日期都相对「今天」编(最近 30 天的线索、未来两个月的截止),每天第一次读时重编,演示时总是新鲜的。

只读:除 GET 以外的请求一律拒绝;读记录以外的 GET(结构)才转给真实库。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from core import tstore
from core.jobs.awards import asset, delivery

TZ = ZoneInfo("Asia/Shanghai")
USD_CNY, EUR_CNY = 7.1, 7.7  # 示例里折合人民币用的固定汇率

_REC = re.compile(r"^/table/([^/]+)/record$")
_ONE = re.compile(r"^/table/([^/]+)/record/([^/]+)$")
_CNT = re.compile(r"^/table/([^/]+)/aggregation/row-count$")


def _h(*parts: object) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


# ---------- 编数据(属性编号 → 值;关联写对方的 _k) ----------

COUNTRIES = [  # ISO3, 国名, 大洲与区域, 收入组, 市场分级, 首府城市(中文, 英文, 坐标, 人口)
    ("UZB", "乌兹别克斯坦", "亚洲 · 中亚", "中低收入 · IDA/IBRD 混合", "重点", ("塔什干", "Tashkent", "41.31,69.28", 2_900_000)),
    ("VNM", "越南", "亚洲 · 东南亚", "中低收入 · IBRD", "重点", ("岘港", "Da Nang", "16.05,108.20", 1_200_000)),
    ("KEN", "肯尼亚", "非洲 · 东非", "中低收入 · IDA/IBRD 混合", "市场", ("蒙巴萨", "Mombasa", "-4.04,39.67", 1_300_000)),
    ("EGY", "埃及", "非洲 · 北非", "中低收入 · IBRD", "市场", ("亚历山大", "Alexandria", "31.20,29.92", 5_400_000)),
    ("PER", "秘鲁", "美洲 · 南美", "中高收入 · IBRD", "其他", ("阿雷基帕", "Arequipa", "-16.41,-71.54", 1_100_000)),
    ("IDN", "印度尼西亚", "亚洲 · 东南亚", "中高收入 · IBRD", "市场", ("泗水", "Surabaya", "-7.25,112.75", 2_900_000)),
]

SOURCES = [("S901", "示例 · 国际采购公告门户", "采购门户"), ("S902", "示例 · 开发银行项目库", "接口"),
           ("S903", "示例 · 行业新闻订阅", "RSS"), ("S904", "示例 · 人工上传", "人工上传")]

# 商机:编号, 国别, 名称, 原文名称, 领域, 业主, 公开进度, 投标资格, 核实, 规模, 币种, 我方阶段, 优先级
OPPS = [
    ("UZB-901", "UZB", "塔什干北区供水管网改造", "Tashkent North Water Network Rehabilitation", ["供水", "减漏计量"], "塔什干市供水公司(示例)", "采购进行中", "可直接投标", "已核实", 86_000_000, "USD", "准备投标", "A"),
    ("UZB-902", "UZB", "撒马尔罕污水处理厂扩建", "Samarkand WWTP Expansion", ["污水", "污泥"], "国家住房与公用事业部(示例)", "融资已落实", "推定可投", "部分核实", 54_000_000, "USD", "跟踪中", "B"),
    ("VNM-901", "VNM", "岘港雨水排涝与水环境整治", "Da Nang Drainage and Water Environment Improvement", ["雨水排涝", "水环境治理"], "岘港市人民委员会(示例)", "前期", "有条件", "部分核实", 120_000_000, "USD", "跟踪中", "A"),
    ("VNM-902", "VNM", "湄公河三角洲农村供水", "Mekong Delta Rural Water Supply", ["供水"], "农业与农村发展部(示例)", "规划", "待核实", "未核实", 35_000_000, "USD", "建议", ""),
    ("KEN-901", "KEN", "蒙巴萨海水淡化一期", "Mombasa Desalination Phase I", ["海水淡化"], "海岸水务局(示例)", "采购进行中", "可直接投标", "已核实", 210_000_000, "USD", "已投标", "A"),
    ("KEN-902", "KEN", "内罗毕输水干管", "Nairobi Bulk Water Transmission", ["输水干管"], "水务部(示例)", "已定标", "推定可投", "已核实", 48_000_000, "EUR", "未中标", ""),
    ("EGY-901", "EGY", "亚历山大再生水回用", "Alexandria Water Reuse Programme", ["再生水", "污水"], "建设与清洁水务控股公司(示例)", "融资已落实", "推定可投", "部分核实", 95_000_000, "EUR", "中标", "A"),
    ("PER-901", "PER", "阿雷基帕水库调蓄与灌溉", "Arequipa Reservoir and Irrigation", ["水库调蓄", "灌溉"], "地区政府(示例)", "前期", "受限", "来源冲突", 64_000_000, "USD", "不跟进", ""),
    ("IDN-901", "IDN", "泗水污泥处理与资源化", "Surabaya Sludge Treatment and Recovery", ["污泥"], "泗水市政府(示例)", "规划", "待核实", "未核实", 28_000_000, "USD", "建议", ""),
]

LEAD_KINDS = [("招标", "Invitation for Bids"), ("意向书", "Request for Expressions of Interest"), ("资格预审", "Prequalification Notice"),
              ("融资", "Loan Approved"), ("立项或规划", "Project Concept Approved"), ("授标或短名单", "Contract Award"),
              ("项目动态", "Project Update"), ("总采购公告", "General Procurement Notice")]
FUNDERS = ["多边开发银行(示例)", "区域开发银行(示例)", "双边援助机构(示例)", "本国财政(示例)"]


def _cny_wan(value: float, cur: str) -> float:
    return round(value * (USD_CNY if cur == "USD" else EUR_CNY) / 10_000, 1)


def build(today: date) -> dict[str, list[dict]]:
    """→ {对象: [{"_k": 示例键, 属性: 值}]};关联写对方的 _k(或其列表);日期写 date。"""
    d = timedelta
    countries = [{"_k": iso, "key": iso, "name_zh": zh, "region": reg, "income_group": inc, "tier": tier, "market": reg.split(" · ")[1] + "区域",
                  "basics": f"首都或主要城市:{city[0]}(示例)\n官方语言:示例\n货币:示例", "indicators": "城镇供水覆盖率:约 80%(示例数据)\n污水处理率:约 40%(示例数据)",
                  "water_agencies": f"{zh}国家水务主管部门(示例)", "water_soes": f"{zh}国家水务公司(示例)",
                  "access": "外资可参与国际竞争性招标;部分项目要求本地合作方(示例)"}
                 for iso, zh, reg, inc, tier, city in COUNTRIES]
    cities = [{"_k": f"C-{iso}", "key": str(9_000_000 + i), "name_zh": city[0], "name_en": city[1], "country": iso, "admin": "示例行政区",
               "coords": city[2], "population": city[3], "pop_year": today.year - 1, "source": "示例数据"}
              for i, (iso, zh, reg, inc, tier, city) in enumerate(COUNTRIES)]
    sources = [{"_k": k, "key": k, "name": n, "kind": kind, "url": "https://example.org/" + k.lower(), "scope": "示例", "enabled": True,
                "last_run": datetime.combine(today, datetime.min.time(), TZ).replace(hour=8, minute=30), "last_status": "正常"}
               for k, n, kind in SOURCES]
    opps = []
    for i, (code, iso, name, local, sectors, owner, pstage, elig, ver, size, cur, stage, pri) in enumerate(OPPS):
        opps.append({"_k": code, "code": code, "name_zh": name, "name_local": local, "country": iso, "city": f"C-{iso}", "location": "示例地点",
                     "sectors": sectors, "owner_org": owner, "public_stage": pstage, "eligibility": elig, "verified": ver,
                     "scope": f"{name}:新建与改造工程若干,详见招标文件(示例)", "size_value": size, "currency": cur,
                     "size_usd": size if cur == "USD" else round(size * EUR_CNY / USD_CNY), "size_cny": _cny_wan(size, cur),
                     "financing": f"{FUNDERS[i % 3]} · 主权贷款 · {size // 1_000_000} 百万 {cur} · 已批准", "ai_reason": "领域与我方业务一致,资金来源明确(示例)",
                     "parties": f"{owner}(业主)", "stage": stage, "priority": pri})
    leads = []
    for n in range(36):
        o = OPPS[n % len(OPPS)]
        code, iso = o[0], o[1]
        cat, en = LEAD_KINDS[_h("cat", n) % len(LEAD_KINDS)]
        found = today - d(days=_h("day", n) % 30)
        attached = n % 4 != 3
        size = o[9] // (1 + _h("amt", n) % 3)
        leads.append({"_k": f"L{n:02d}", "key": f"{_h('lead', n):08x}", "title": f"{o[3]} - {en}", "url": f"https://example.org/notice/{9000 + n}",
                      "source": SOURCES[n % len(SOURCES)][0], "origin": "用户上传" if n % len(SOURCES) == 3 else "机器采集",
                      "published": found - d(days=1), "found_at": datetime.combine(found, datetime.min.time(), TZ).replace(hour=8, minute=30 + n % 20),
                      "excerpt": "示例原文摘录:本公告为演示用的编造数据。", "relevant": n % 9 != 8, "category": cat, "sectors": [o[4][0] if o[4][0] in ("供水", "污水", "海水淡化", "雨水排涝") else "其他"],
                      "country": iso, "city": f"C-{iso}" if attached else None, "summary_zh": f"{o[2]}:{cat}(示例)", "project_name": o[2], "funder": FUNDERS[n % len(FUNDERS)],
                      "amount": f"{size:,} {o[10]}", "amount_value": size, "currency": o[10], "amount_cny": _cny_wan(size, o[10]),
                      "opportunity": code if attached else None, "suggested_opp": None if attached else code, "suggest_reliable": not attached and n % 2 == 0,
                      "judged_by": "模型" if n % 5 else "规则", "status": "已处理" if attached and found < today - d(days=3) else "待处理"})
    ms, kinds = [], [("资格预审截止", 9), ("投标截止", 24), ("开标", 25), ("授标", 70)]
    for i, o in enumerate(OPPS):
        if o[11] in ("建议", "不跟进", "未中标"):
            continue
        for j, (kind, off) in enumerate(kinds):
            day = today + d(days=off + 5 * i - 20)
            ms.append({"_k": f"M{i}{j}", "opportunity": o[0], "country": o[1], "lead": next(x["_k"] for x in leads if x["country"] == o[1]),
                       "kind": kind, "day": day, "日期原文": day.isoformat(), "status": "已发生" if day < today else ("官方计划" if j < 2 else "推断"),
                       "label": f"{o[2]} {kind}(示例)"})
    ms.append({"_k": "M-UZB", "country": "UZB", "kind": "其他", "day": today + d(days=15), "日期原文": (today + d(days=15)).isoformat(),
               "status": "官方计划", "label": "国家水务投资推介会(示例)"})
    y, w, _ = (today - d(days=7)).isocalendar()
    reports = [
        {"_k": "R-daily", "key": f"日报_{today.isoformat()}_公开版.docx", "kind": "日报", "day": today, "period": today.isoformat(), "edition": "公开版",
         "gen_status": "完成", "summary": _brief(today, leads, opps)},
        {"_k": "R-weekly", "key": f"周报_{y}-W{w:02d}_公开版.docx", "kind": "周报", "day": today - d(days=today.weekday()), "period": f"{y}-W{w:02d}",
         "edition": "公开版", "gen_status": "完成", "summary": "## 摘要\n\n本周示例数据:新增线索若干,两个商机进入采购阶段。\n"},
    ]
    # 每座示例城市一座自来水厂、一座污水厂(规模、服务人口按城市人口编;第一座城市的污水厂带一条改造扩建)
    supply = [{"_k": f"WS-{c['country']}", "key": f"sample-ws-{i}", "name": f"{c['name_zh']}第一水厂(示例)", "kind": "海水淡化厂" if i == 2 else "自来水厂", "status": "运行",
               "country": c["country"], "city": c["_k"], "capacity": round(c["population"] * 0.25 / 1e4, 1), "pop_served": round(c["population"] * 0.6, -3), "built": str(1975 + 3 * i),
               "process": "常规处理(混凝、沉淀、过滤、消毒)", "coords": c["coords"], "source": "示例数据"} for i, c in enumerate(cities)]
    sewage = [{"_k": f"WW-{c['country']}", "key": f"sample-ww-{i}", "name": f"{c['name_zh']}中心污水处理厂(示例)", "kind": "污水厂", "status": "在建" if i == 4 else "运行",
               "country": c["country"], "city": c["_k"], "capacity": round(c["population"] * 0.15 / 1e4, 1), "pop_served": round(c["population"] * 0.4, -3), "built": str(1990 + 4 * i),
               "level": "二级" if i % 2 else "深度", "process": "A2/O 生物处理", "upgrades": "2019 | 提标改造 | 增加深度过滤(示例)" if i == 0 else None, "coords": c["coords"], "source": "示例数据"}
              for i, c in enumerate(cities)]
    # 已中标项目:每个示例国家 4 份(三份在示例城市,一份只挂国别),类别、年份错开,好演示筛选;
    # 工程合同里一半写了招标估价(中标价是估价的 80%–114%),工程合同都写评标天数与投标家数(演示助手的「中标价是招标估价的几成」「平均评标多久」)
    kinds = [("供水", "工程", "配水管网改造", "Water Distribution Network Rehabilitation", "配水管网 42 km"), ("污水", "工程", "污水处理厂扩建并运营", "Design, Build and Operate of WWTP Expansion", "污水厂 8 万吨/日"),
             ("水利", "工程", "灌渠衬砌", "Irrigation Canal Lining", "渠道 65 km"), ("供水", "货物", "智能水表采购", "Supply of Smart Water Meters", "水表 3 万只"),
             ("雨水排涝", "工程", "城区排水改造", "Urban Drainage Improvement", "排水管涵 18 km"), ("污水", "咨询服务", "污水管网设计与监理", "Design and Supervision of Sewer Network", ""),
             ("供水", "运营", "供水系统委托运营", "Management, Operation and Maintenance of Water Supply System", "")]
    firms = [{"_k": "F-CN1", "key": "SAMPLE CONSTRUCTION GROUP", "name": "示例建设集团(中国)", "nationality": "中国", "nationality_code": "CHN", "kind": "国有企业",
              "sectors": ["供水", "污水"], "profile": "示例单位:在中亚、南高加索承建供水与污水工程(示例)。"},
             {"_k": "F-IN", "key": "SAMPLE INFRA", "name": "Sample Infra Ltd(示例)", "nationality": "印度", "nationality_code": "IND"},
             {"_k": "F-ES", "key": "EJEMPLO CONSTRUCCIONES", "name": "Ejemplo Construcciones(示例)", "nationality": "西班牙", "nationality_code": "ESP"},
             {"_k": "F-CN2", "key": "SAMPLE WATER ENGINEERING", "name": "示例水务工程公司", "nationality": "中国", "nationality_code": "CHN"},
             {"_k": "F-TR", "key": "SAMPLE JV", "name": "Sample JV(示例)", "nationality": "土耳其", "nationality_code": "TUR"}]
    winners = [["F-CN1"], ["F-IN"], ["F-ES"], ["F-CN2", "F-TR"]]  # 第四种是联合体
    name_of = {f["_k"]: f for f in firms}
    awards = []
    for i, c in enumerate(cities):
        for j in range(4):
            sec, ctype, zh, en, qty = kinds[(i + j) % len(kinds)]
            price = (2 + _h("aw", i, j) % 40) * 1_000_000 if ctype != "咨询服务" else (3 + _h("aw", i, j) % 9) * 100_000
            ws = winners[(i + j) % len(winners)]
            win, wc = "、".join(name_of[k]["name"] for k in ws), "、".join(name_of[k]["nationality"] for k in ws)
            awards.append({"_k": f"AW-{c['country']}-{j}", "key": f"sample-aw-{i}-{j}", "name": f"{en} in {c['name_en']} (sample)", "name_zh": f"{c['name_zh']}{zh}(示例)",
                           "sector": sec, "contract_type": ctype, "delivery": delivery(ctype, en) or None, "price": price, "currency": "USD", "price_cny": _cny_wan(price, "USD"),
                           "awarded": today.replace(month=1, day=1) - d(days=365 * (j % 3) + 40 * i + 15 * j), "quantity": qty or None, "winner": win, "winner_country": wc, "winners": ws,
                           "asset": asset(en, sec), "estimate": round(price / (0.8 + _h("est", i, j) % 35 / 100)) if ctype == "工程" and (i + j) % 2 == 0 else None,
                           "eval_days": 20 + _h("ev", i, j) % 70 if ctype == "工程" else None, "bidders": 2 + _h("bd", i, j) % 7 if ctype == "工程" else None,
                           "project": f"P9{i}{j}000 {c['name_en']} Water Security Project (sample)", "funder": FUNDERS[j % 2], "method": "公开招标(RFB)",
                           "country": c["country"], "city": c["_k"] if j < 3 else None, "source": "示例数据", "url": f"https://example.org/award/{i}{j}"})
    # 研究资料与水务事件(国别概况「水务历史资料」、城市大事记):每个示例国家一份综述、一份评估文件、五件大事(两件在示例城市)
    studies, events = [], []
    for i, c in enumerate(cities):
        y = today.year - 12 + i
        studies += [{"_k": f"ST-{c['country']}-0", "key": f"SUM-sample-{i}", "name": f"{c['name_zh']}所在国水务综述(示例)", "name_zh": f"{c['name_zh']}所在国水务综述(示例)",
                     "kind": "综述", "publisher": "本系统整理", "published": today.replace(month=1, day=1), "language": "中文", "country": c["country"],
                     "summary": f"## 关键数字\n- 城市供水覆盖率约 {80 + i}%,漏损率约 {35 + i}%(示例)\n- 主要资金来自开发银行贷款与国家预算(示例)\n\n## 主要机构\n- 国家水务公司负责{c['name_zh']}供排水(示例)"},
                    {"_k": f"ST-{c['country']}-1", "key": f"WB-sample-{i}", "name": f"{c['name_en']} Water Security Project: Project Appraisal Document (sample)",
                     "name_zh": f"{c['name_zh']}水安全项目评估文件(示例)", "kind": "项目评估", "publisher": "世界银行", "published": date(y, 5, 20), "language": "英文",
                     "project": f"P9{i}0000 {c['name_en']} Water Security Project (sample)", "country": c["country"], "url": f"https://example.org/study/{i}"}]
        for j, (kind, title, city) in enumerate([("项目批准", f"世界银行批准「{c['name_zh']}水安全项目」,1.2 亿美元(示例)", True),
                                                  ("机构改革", "国家水务公司改组,城市供排水统一经营(示例)", False),
                                                  ("规划与政策", "国家水战略发布,2030 年漏损率目标 20%(示例)", False),
                                                  ("水价调整", "居民水价上调 10%(示例)", False),
                                                  ("工程投运", f"{c['name_zh']}第二水厂投运,10 万吨/日(示例)", True)]):
            day = date(y + j, 1, 1) if j % 2 else date(y + j, 3 + j, 1)  # 只到年的写区间起点
            events.append({"_k": f"EV-{c['country']}-{j}", "key": f"EV-sample-{i}-{j}", "day": day, "日期原文": str(day.year) if j % 2 else day.isoformat(),
                           "title": title, "kind": kind, "country": c["country"], "city": c["_k"] if city else None, "source": "示例数据", "url": f"https://example.org/event/{i}{j}"})
    data = {"country": countries, "city": cities, "source": sources, "opportunity": opps, "lead": leads, "milestone": ms, "report": reports,
            "supply_plant": supply, "sewage_plant": sewage, "award": awards, "contractor": firms, "study": studies, "water_event": events,
            "water_area": [{"_k": f"WA-{c['country']}", "name": f"大{c['name_zh']}(示例)", "name_en": f"Greater {c['name_en']} (sample)", "country": c["country"], "cities": [c["_k"]],
                            "utility": "国家水务公司(示例)", "sources": "示例水库\n示例输水干管", "scope": f"{c['name_zh']}市区及周边郊区居民点(示例)",
                            "basis": "同一家水司、同一批水源供水(示例)"} for c in cities[:2]]}
    _derive(data, today)
    return data


def _brief(today: date, leads: list[dict], opps: list[dict]) -> str:
    new = [x for x in leads if x["found_at"].date() == today] or leads[:3]
    lines = [f"# 每日简报 {today.isoformat()}", "", "## 摘要", "", f"今天是演示用的示例数据:新到 {len(new)} 条线索,下面列出要点。", "", "## 新线索", ""]
    lines += [f"- [{x['key']}] {x['summary_zh']}" for x in new[:5]]
    lines += ["", "## 说明", "", "本简报由示例数据生成,内容为编造,仅供演示。"]
    return "\n".join(lines)


def _derive(data: dict[str, list[dict]], today: date) -> None:
    """汇总字段(线索数、商机数、所属线索数、下一时间节点、最近截止)按编好的数据算。"""
    by_country = lambda oid, iso: sum(1 for r in data[oid] if r.get("country") == iso)  # noqa: E731
    for c in data["country"]:
        c["lead_count"], c["opp_count"] = by_country("lead", c["_k"]), by_country("opportunity", c["_k"])
    for c in data["city"]:
        c["lead_count"], c["opp_count"], c["supply_count"], c["sewage_count"] = (sum(1 for r in data[oid] if r.get("city") == c["_k"]) for oid in ("lead", "opportunity", "supply_plant", "sewage_plant"))
    for s in data["source"]:
        s["lead_count"] = sum(1 for r in data["lead"] if r.get("source") == s["_k"])
    for o in data["opportunity"]:
        o["lead_count"] = sum(1 for r in data["lead"] if r.get("opportunity") == o["_k"])
        o["sources"] = [r["_k"] for r in data["lead"] if r.get("opportunity") == o["_k"]][:5]
        up = sorted((m for m in data["milestone"] if m.get("opportunity") == o["_k"] and m["day"] >= today), key=lambda m: m["day"])
        if up:
            o["next_milestone"] = f"{up[0]['kind']} · {up[0]['日期原文']}"
    for f in data.get("contractor", []):
        mine = [a for a in data["award"] if f["_k"] in (a.get("winners") or [])]
        f["award_count"], f["award_cny"] = len(mine), round(sum(a.get("price_cny") or 0 for a in mine), 2)
    for ld in data["lead"]:
        due = sorted(m["day"] for m in data["milestone"] if m.get("opportunity") and m["opportunity"] in (ld.get("opportunity"), ld.get("suggested_opp")) and m["day"] >= today)
        if due:
            ld["nearest_deadline"] = due[0]


# ---------- 按表格接口的样子回 ----------

def _date_out(v: date | datetime) -> str:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return datetime.combine(v, datetime.min.time(), TZ).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _date_in(v: Any) -> date | None:
    if not v:
        return None
    s = str(v)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return date.fromisoformat(s[:10])
    return dt.date() if dt.tzinfo is None else dt.astimezone(TZ).date()


class SampleStore:
    """把 build() 的数据按真实库的表、字段编号摆好;match / select 做条件、排序。"""

    def __init__(self, meta: tstore.Base, today: date):
        self.today = today
        self.b = meta
        data = build(today)
        self.rid = {(oid, r["_k"]): "rec" + hashlib.sha256(f"{oid}:{r['_k']}".encode()).hexdigest()[:14] for oid, rows in data.items() for r in rows}
        self.tables: dict[str, list[dict]] = {}
        self.fields: dict[str, dict[str, dict]] = {}
        stamp = _date_out(datetime.combine(today, datetime.min.time(), TZ))
        titles: dict[tuple[str, str], str] = {}
        for oid, rows in data.items():  # 先算每行的标题(主字段),关联要用
            for r in rows:
                titles[(oid, r["_k"])] = self._title(oid, r)
        for oid, rows in data.items():
            if oid in meta.missing or oid not in meta.t:
                continue
            tid = meta.t[oid]
            self.fields[tid] = {f["id"]: f for f in meta.f[oid].values()}
            out = []
            for r in rows:
                fields: dict[str, Any] = {}
                for prop, v in r.items():
                    if prop == "_k" or v is None or v == "" or v == [] or v is False or not meta.has(oid, prop):
                        continue
                    f = meta.field(oid, prop)
                    fields[f["id"]] = self._value(oid, prop, f, v, titles)
                prim = next((f for f in meta.f[oid].values() if f.get("isPrimary")), None)
                if prim is not None and prim["id"] not in fields:
                    fields[prim["id"]] = titles[(oid, r["_k"])]
                out.append({"id": self.rid[(oid, r["_k"])], "fields": fields, "createdTime": stamp, "lastModifiedTime": stamp})
            self.tables[tid] = out

    def _title(self, oid: str, r: dict) -> str:
        if oid == "milestone":
            return f"{r['kind']} · {r['日期原文']}"
        if oid == "report":
            return f"{r['kind']} · {r['day'].isoformat()} · {r['edition']}"
        prim = (self.b.tmap["tables"].get(oid) or {}).get("primary")
        return str(r.get(prim) if isinstance(prim, str) else r.get("key") or r["_k"])

    def _value(self, oid: str, prop: str, f: dict, v: Any, titles: dict) -> Any:
        t = f["type"]
        if t == "link":
            target = self._target(oid, prop)
            keys = v if isinstance(v, list) else [v]
            vals = [{"id": self.rid[(target, k)], "title": titles[(target, k)]} for k in keys if (target, k) in self.rid]
            multi = (f.get("options") or {}).get("relationship") in ("manyMany", "oneMany")
            return vals if multi else (vals[0] if vals else None)
        if isinstance(v, date) and not isinstance(v, datetime) and t in ("rollup", "formula"):
            return v.isoformat()  # 汇总出来的日期(最近截止)页面原样显示,给成「年-月-日」
        if isinstance(v, (date, datetime)):
            return _date_out(v)
        if f.get("isLookup"):
            return v if isinstance(v, list) else [v]
        if t == "multipleSelect" and not isinstance(v, list):
            return [v]
        if t == "number":
            return float(v)
        if t == "checkbox":
            return bool(v)
        return v

    def _target(self, oid: str, prop: str) -> str:
        p = self.b.onto.objects[oid].props_by_id[prop]
        return str(p.type).split(":", 1)[1]

    # ---------- 条件 ----------
    def match(self, tid: str, rec: dict, flt: dict | None) -> bool:
        if not flt:
            return True
        res = []
        for item in flt.get("filterSet") or []:
            if "filterSet" in item:
                res.append(self.match(tid, rec, item))
                continue
            v, op, want = rec["fields"].get(item.get("fieldId")), item.get("operator"), item.get("value")
            titles = [x.get("title") for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict)]
            if isinstance(v, dict):
                v = v.get("id")
            if isinstance(v, list):
                v = [x.get("id") if isinstance(x, dict) else x for x in v]
            empty = v is None or v == [] or v == ""
            if op == "isEmpty":
                res.append(empty)
            elif op == "isNotEmpty":
                res.append(not empty)
            elif op == "is":
                res.append(v == want or (want is not None and want in titles) or (isinstance(v, list) and want in v))
            elif op == "isNot":
                res.append(not (v == want or (isinstance(v, list) and want in v)))
            elif op in ("isAnyOf", "hasAnyOf"):
                res.append(any(x in (want or []) for x in (v if isinstance(v, list) else [v])))
            elif op in ("isNoneOf", "hasNoneOf"):
                res.append(not any(x in (want or []) for x in (v if isinstance(v, list) else [v])))
            elif op == "hasAllOf":
                res.append(all(x in (v if isinstance(v, list) else [v]) for x in (want or [])))
            elif op == "contains":
                res.append(want is not None and str(want).casefold() in json.dumps(v, ensure_ascii=False).casefold() + " ".join(map(str, titles)).casefold())
            elif op == "doesNotContain":
                res.append(want is None or str(want).casefold() not in json.dumps(v, ensure_ascii=False).casefold())
            elif op in ("isBefore", "isAfter", "isOnOrBefore", "isOnOrAfter", "isWithIn"):
                res.append(self._date_ok(v, op, want))
            elif op in ("isGreater", "isGreaterEqual", "isLess", "isLessEqual"):
                try:
                    a, w = float(v), float(want)
                    res.append({"isGreater": a > w, "isGreaterEqual": a >= w, "isLess": a < w, "isLessEqual": a <= w}[op])
                except (TypeError, ValueError):
                    res.append(False)
            else:
                res.append(True)
        if not res:
            return True
        return all(res) if flt.get("conjunction", "and") == "and" else any(res)

    def _date_ok(self, v: Any, op: str, want: Any) -> bool:
        d = _date_in(v)
        if d is None or not isinstance(want, dict):
            return False
        mode = want.get("mode")
        anchor = {"today": self.today, "tomorrow": self.today + timedelta(days=1), "yesterday": self.today - timedelta(days=1)}.get(mode)
        if mode == "exactDate" and want.get("exactDate"):
            anchor = _date_in(want["exactDate"]) if "T" not in str(want["exactDate"]) else date.fromisoformat(str(want["exactDate"])[:10])
        if anchor is None:
            return True
        return {"isBefore": d < anchor, "isAfter": d > anchor, "isOnOrBefore": d <= anchor, "isOnOrAfter": d >= anchor, "isWithIn": d == anchor}[op]

    def select(self, tid: str, params: list[tuple[str, Any]]) -> list[dict]:
        if tid not in self.tables:  # 跟进记录、成员这类没编示例的表:空表(游客通道本来就不放行它们;日历会读跟进的到期日)
            return []
        p = dict(params)
        recs = [r for r in self.tables[tid] if self.match(tid, r, json.loads(p["filter"]) if p.get("filter") else None)]
        for ob in reversed(json.loads(p["orderBy"]) if p.get("orderBy") else []):
            fid, desc = ob.get("fieldId"), ob.get("order") == "desc"
            def key(r: dict, fid=fid) -> tuple:
                v = r["fields"].get(fid)
                v = v.get("title") if isinstance(v, dict) else v
                return (v is None, v if isinstance(v, (int, float)) else str(v or ""))
            present = sorted([r for r in recs if r["fields"].get(fid) is not None], key=key, reverse=desc)
            recs = present + [r for r in recs if r["fields"].get(fid) is None]
        return recs

    @staticmethod
    def project(rec: dict, projection: list[str]) -> dict:
        if not projection:
            return rec
        return {**rec, "fields": {k: v for k, v in rec["fields"].items() if k in projection}}


class SampleClient(tstore.Client):
    """给游客通道用的表格客户端:结构(表、字段、视图、库)转给真实库,记录从示例数据里出;只读。"""

    def __init__(self, real: tstore.Client, today: Callable[[], date] | None = None, base_factory: Callable[[tstore.Client], tstore.Base] | None = None):
        self.real = real
        self.url = real.url
        self._today = today or (lambda: datetime.now(TZ).date())
        self._factory = base_factory or (lambda c: tstore.Base(c))
        self._store: SampleStore | None = None

    def store(self) -> SampleStore:
        today = self._today()
        if self._store is None or self._store.today != today:
            self._store = SampleStore(self._factory(self.real), today)
        return self._store

    def req(self, method: str, path: str, *, params: Any = None, json_body: Any = None, files: Any = None) -> Any:
        if method != "GET":
            raise tstore.TStoreError(f"{method} {path}: 示例数据只读", 403)
        path = path.split("?")[0]
        plist = list(params.items()) if isinstance(params, dict) else list(params or [])
        if m := _REC.match(path):
            s = self.store()
            recs = s.select(m[1], plist)
            p = dict(plist)
            skip, take = int(p.get("skip") or 0), int(p.get("take") or 100)
            proj = [v for k, v in plist if k == "projection"]
            return {"records": [s.project(r, proj) for r in recs[skip:skip + take]]}
        if m := _CNT.match(path):
            return {"rowCount": len(self.store().select(m[1], plist))}
        if m := _ONE.match(path):
            s = self.store()
            rec = next((r for r in s.tables.get(m[1], []) if r["id"] == m[2]), None)
            if rec is None:
                raise tstore.TStoreError(f"GET {path} -> 404: 没有这条示例记录", 404)
            return rec
        if "/record" in path or "/history" in path or "/comment" in path:
            raise tstore.TStoreError(f"GET {path}: 示例数据不提供", 403)
        return self.real.req(method, path, params=params)
