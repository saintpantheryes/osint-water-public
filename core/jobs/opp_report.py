"""项目报告(报告表里类型为「项目报告」、选了范围商机的一行):按《项目分析报告》模板给一个商机出 Word。

两个版本(使用方 2026-10-10,以一个已立项项目的中英文成稿为样):
- 内部版 = 中文,报公司领导:七个部分齐全,判断与计划可用商机上人写的内部备注、下一步与跟进记录;
- 公开版 = 英文,发前方伙伴:不读人写的属性与跟进记录(R9),第六部分只有「请前方伙伴跟进」,模型把事实译成英文。
版式、章节与表格都在模板里(private/templates/项目分析报告模板.docx 与 _英文.docx,也可以在「报告模板」表上传同样结构的文件);
core/project_report.to_docx 认出模板里的各张表往里填。
事实取自商机表各字段,来源是商机的出处线索:字段文字里的 [线索号] 换成第七部分的编号。判断、分析与建议由模型据这些数据起草;
没配模型时只填事实,判断部分写"待研判"。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from core import geo, tstore
from core import project_report as PR

from . import reports as R
from .common import Ctx

TEMPLATES = {"zh": PR.template_file("项目分析报告模板.docx"), "en": PR.template_file("项目分析报告模板_英文.docx")}
_REF = re.compile(r"\s*\[([0-9a-f]{8})\]")
_LEDGER_REF = re.compile(r"\s*〔R\d+〕")
BASICS = (("项目名称与编号", "Name and ID"), ("项目地点", "Location"), ("业主与执行机构", "Owner and implementing agency"), ("借款人", "Borrower"),
          ("总投资与资金来源", "Cost and funding"), ("建设内容", "Scope"), ("采购规则", "Procurement rules"), ("资格要求", "Qualification"),
          ("设计与监理", "Design and supervision"), ("工期", "Duration"), ("环境与社会", "Environmental and social"),
          ("建成后运营", "Operator after completion"), ("业主联系人", "Owner contact"))
PHASES_EN = dict(zip(PR.PHASES, ("Preparation", "Funding approval", "Consultant procurement", "Loan signing", "Detailed design", "Works tenders",
                                 "Construction")))
# 时间节点类型 → 进展表的阶段(说明里有咨询字样的招标算咨询招标)
MS_PHASE = {"董事会批准": "融资批准", "贷款签署": "融资签约", "生效": "融资签约", "可研": "前期研究", "环评": "前期研究", "开工": "施工建设", "完工": "施工建设",
            "投运": "施工建设", "招标发布": "施工招标", "投标截止": "施工招标", "资格预审截止": "施工招标", "意向书截止": "施工招标", "授标": "施工招标",
            "合同签署": "施工招标"}
OFFICIAL = ("ebrd.com", "worldbank.org", "adb.org", "aiib.org", "isdb.org", "eib.org", "afdb.org", "opecfund.org", "e5p.eu", "ungm.org", "iadb.org",
            "president.az", "mofcom.gov.cn")
PLATFORM = ("developmentaid.org", "rightsindevelopment.org", "devex.com", "dgmarket.com", "tendersinfo.com", "globaltenders.com")
PUBLISHER = {"ebrd.com": ("欧洲复兴开发银行", "EBRD"), "worldbank.org": ("世界银行", "World Bank"), "adb.org": ("亚洲开发银行", "ADB"),
             "aiib.org": ("亚洲基础设施投资银行", "AIIB"), "isdb.org": ("伊斯兰开发银行", "IsDB"), "eib.org": ("欧洲投资银行", "EIB"),
             "afdb.org": ("非洲开发银行", "AfDB"), "opecfund.org": ("欧佩克国际发展基金", "OPEC Fund"),
             "e5p.eu": ("东欧能效与环境伙伴关系基金", "E5P"), "developmentaid.org": ("DevelopmentAid", "DevelopmentAid"),
             "rightsindevelopment.org": ("Early Warning System", "Early Warning System"), "mofcom.gov.cn": ("商务部", "MOFCOM (China)")}
SECTOR_EN = {"供水": "water supply", "输水干管": "transmission mains", "水库调蓄": "reservoirs", "减漏计量": "NRW and metering", "污水": "wastewater",
             "污泥": "sludge", "再生水": "water reuse", "雨水排涝": "stormwater", "海水淡化": "desalination", "灌溉": "irrigation", "防洪": "flood control",
             "水环境治理": "water environment", "水利": "hydraulic works", "水环境": "water environment", "其他": "other"}
_CJK = re.compile("[\u4e00-\u9fff]")
GRADES = {"official": ("官方一手", "Official"), "platform": ("第三方平台", "Third-party platform"), "media": ("媒体与其他", "Media and other")}
QUALS_EN = {
    "合格性与注册": "Eligibility and registration", "财务稳健": "Financial soundness", "平均年营业额": "Average annual turnover",
    "融资能力": "Financial resources", "一般经验": "General experience", "管理经验": "Management experience", "类似工程经验": "Specific experience",
    "履约与诉讼": "Performance and litigation", "环境健康安全": "Environment, health and safety", "关键人员": "Key personnel", "本地许可": "Local licensing",
}
QUALS_EN_TEXT = {
    "财务稳健": "usually audited financial statements for the last 3–5 years; no bankruptcy, liquidation or major tax arrears",
    "平均年营业额": "usually a minimum average annual construction turnover over 3–5 years; the JV meets it combined, with a minimum share for the lead",
    "融资能力": "usually liquid assets or credit lines covering several months of contract cash flow",
    "一般经验": "usually a number of works contracts completed in the last 5–10 years as contractor, subcontractor or JV member",
    "管理经验": "usually experience as main contractor or JV lead",
    "类似工程经验": "usually similar contracts of comparable size and type completed in the last 5–10 years, with key quantities",
    "履约与诉讼": "usually no contract terminations for contractor default; pending litigation capped as a share of net worth",
    "环境健康安全": "usually certified EHS management systems and E&S management experience on similar projects",
    "关键人员": "usually degrees and years of similar experience for the project manager, discipline leads and the EHS manager",
}

SYSTEM = {
    "zh": """你是投标人国籍为 {bidder} 的水务工程承包与投资企业的项目分析员,按公司《项目分析报告》模板为管理层写一个项目的分析。
只根据用户给出的数据写:不编造数字、机构、日期与竞争对手;引用事实时在句末用方括号写数据里给出的来源编号,如 [3];
数据里的"内部"内容(我方阶段、内部备注、跟进记录)可以用来写判断与建议,但不能当来源引用;编制方自己的判断注明"推断"或"估算";
数据不足时写"待核实"。用简洁的中文书面语,不写客套话。""",
    "en": """You are a project analyst at a water engineering contractor and investor whose bidder nationality is {bidder}. You write one project's
analysis for our partners in the project country, following our Project Analysis Report template. Write in clear, plain British English,
in full sentences, and translate the Chinese data faithfully (keep official English names, IDs and amounts as given).
Use only the data provided: never invent numbers, agencies, dates or competitors. End each fact with the source number in square brackets,
as given in the data, e.g. [3]. Mark our own judgements "(our estimate)" or "(our inference)". Where the data is insufficient, write
"to be confirmed". This version goes outside the company: do not mention internal approvals, budgets or management decisions.""",
}
FIELDS = {
    "zh": {
        **{k: v for k, v in PR.FIELDS.items()},
        "actions": "计划动作:3–6 条,每行一条「动作 | 责任方 | 时间」,不编号;责任方写总部市场团队、总部投标团队、前方伙伴等",
    },
    "en": {
        "project": "Project: one or two sentences on scope, total cost and funding, with source numbers",
        "progress": "Progress: current stage, latest milestone and next milestone, with source numbers",
        "judgement": "Assessment: whether the project is worth pursuing, the main strengths and difficulties; mark our inferences",
        "advice": "Next steps: what we will do next and what we ask our partners to do",
        "background": "Background: how the project started and what earlier phases achieved, two to four sentences, with source numbers",
        **{k: f"Strength – {en}: one or two sentences" for k, en in zip(PR.PROS, ("Funding", "Procurement", "Fit", "Information and access"))},
        **{k: f"Weakness or risk – {en}: one or two sentences" for k, en in zip(PR.CONS, ("Technical and construction", "Contract and interfaces",
                                                                                       "Uncertainties", "Our gaps"))},
        "首选": "First choice: the lot or contract that suits us best, and why; one or two sentences",
        "次选": "Second choice: an alternative way in; one sentence",
        "补充": "Supply: equipment and material supply opportunities; one sentence",
        "潜在竞争者": "Potential competitors: known or likely competitors and their strengths; cite sources",
        "partner": "Requested follow-up from partners: 3–5 items, one per line, not numbered (facts to confirm, agencies to meet, information to collect)",
        "actions": "Action plan: 3–6 items, one per line as 'action | responsible | timing', not numbered; responsible is e.g. Head office BD team, "
                   "Head office bidding team, Partners in the country",
    },
}
FIELDS_FACTS = {  # 第二次:进展表与基本情况表(英文版即译文)
    "basics": {"zh": "基本情况表:把「基本情况表(数据原文)」逐行改写成通顺、简洁的中文,每行「行名 | 内容」,行名照抄;内容只用原文里的事实,"
                     "去掉「·」分隔与英文缩写堆砌,多条之间用全角分号;不写来源编号(来源另列)",
               "en": "Project overview table: translate each line of the 基本情况表 into clear English as 'label | text', keeping the labels exactly as "
                     "given; use only the facts in the line; separate items with semicolons; no source numbers (they are listed separately)"},
    "phases": {"zh": "项目进展表:按下面 7 个阶段各写一行「阶段 | 状态 | 时间 | 事项」,阶段依次为 " + "、".join(PR.PHASES)
                     + ";状态只能是 已完成、进行中、待核实、未开始;尚未发生的时间注明「估算」;事项一句话,句末标来源编号",
               "en": "Progress table: one line per stage as 'stage | status | date | event', stages in this order: " + ", ".join(PHASES_EN.values())
                     + "; status is one of Completed, In progress, Unconfirmed, Not started; mark future dates '(est.)'; event is one sentence "
                       "ending with source numbers"},
}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _match(host: str, names: tuple[str, ...] | dict) -> str | None:
    return next((n for n in names if host == n or host.endswith("." + n)), None)


def source_row(ctx: Ctx, r: dict, lang: str) -> dict:
    """一条线索 → 第七部分的一行:标题、网址、发布方、日期、等级。"""
    b = ctx.base
    g = lambda p: b.get("lead", r, p) if b.has("lead", p) else None  # noqa: E731
    url = str(g("url") or "")
    host = _host(url)
    i = 0 if lang == "zh" else 1
    pub = PUBLISHER.get(_match(host, PUBLISHER) or "", (None, None))[i]
    src_name = R._cell(g("source"))
    if not pub:
        pub = src_name if src_name and src_name not in ("台账出处", "人工录入", "manual") else (host or "—")
    gov = bool(re.search(r"(^|\.)gov(\.[a-z]{2})?$|\.gov\.", host))
    grade = "official" if (_match(host, OFFICIAL) or gov) else "platform" if _match(host, PLATFORM) else "media"
    title = re.sub(r"^台账出处[((][^))]*[))][::]", "", str(g("title") or g("key") or ""))
    return {"title": title[:160], "url": url, "publisher": pub, "date": tstore.day(g("published") or g("found_at"), b.tz) or "—",
            "grade": GRADES[grade][i]}


def _refs(text: str, src: PR.Sources) -> tuple[str, str]:
    """「…… [4d3f6f4e][9f9fca5a]」→ (去掉线索号的文字, ⟦来源⟧ 标记);台账来源号〔R110〕不是公开来源,去掉。"""
    keys = [f"lead:{k}" for k in _REF.findall(text)]
    clean = _LEDGER_REF.sub("", _REF.sub("", text)).strip()
    return clean, "".join(f"⟦{k}⟧" for k in dict.fromkeys(keys) if k in src.rows)


def build(ctx: Ctx, opp_rid: str, internal: bool = True) -> tuple[dict, set[str], str]:
    """→ (报告内容(project_report.to_docx 用的结构), 引用的线索记录编号, 商机编号)。内部版中文,公开版英文。"""
    b = ctx.base
    o = b.record("opportunity", opp_rid)
    if not o:
        raise R.ReportError("范围商机不存在")
    lang = "zh" if internal else "en"
    g = lambda p: b.get("opportunity", o, p) if b.has("opportunity", p) else None  # noqa: E731
    lines = lambda p: [x.strip() for x in str(g(p) or "").split("\n") if x.strip()]  # noqa: E731
    code = str(g("code") or opp_rid)
    iso = ctx.iso_of(tstore.link_id(g("country"))) or code.split("-")[0]
    cinfo = geo.countries().get(iso, {})
    country = (R._cell(g("country")) or cinfo.get("name_zh") or iso) if lang == "zh" else (cinfo.get("name_en") or iso)

    # ---- 来源:商机的出处线索与挂到它的线索
    rids = [x.get("id") for x in (g("sources") or []) if isinstance(x, dict)]
    leads = b.rows("lead", filter=b.filter(b.cond("lead", "opportunity", "is", opp_rid))) if b.has("lead", "opportunity") else []
    have = {r["id"] for r in leads}
    leads += [r for r in (b.record("lead", x) for x in rids if x and x not in have) if r]
    leads.sort(key=lambda r: tstore.day(b.get("lead", r, "published") or b.get("lead", r, "found_at"), b.tz) or "", reverse=True)
    src = PR.Sources()
    for r in leads:
        k = str(b.get("lead", r, "key") or r["id"])
        row = source_row(ctx, r, lang)
        src.add(f"lead:{k}", row["title"], row["url"], row["publisher"], row["date"], row["grade"])

    def field(p: str, n: int = 8) -> tuple[list[str], str]:
        out, marks = [], []
        for x in lines(p)[:n]:
            t, m = _refs(x, src)
            if t:
                out.append(t)
            marks += PR._MARK.findall(m)
        return out, "".join(f"⟦{k}⟧" for k in dict.fromkeys(marks))

    # ---- 二、项目基本情况(数据):每行 (行名, 几行文字, 来源标记)
    fin, fin_m = field("financing")
    scope, scope_m = field("scope", 12)
    total = [x for x in scope if x.startswith("总投资")]
    scope = [x for x in scope if not x.startswith("总投资")]
    owner, owner_m = field("owner_org")
    parties, parties_m = field("parties")
    pk, pk_m = field("packages")
    notes, notes_m = field("ai_reason", 12)
    borrower = next((re.sub(r"^.*?借款人[::]", "", x).split(";")[0] for x in owner if "借款人" in x), "")
    if not borrower and any("主权贷款" in x for x in fin):
        borrower = f"{country}政府(主权贷款)(推断)"
    elig = " ".join(x for x in (str(g("eligibility") or ""), str(g("eligibility_basis") or "")) if x)
    design = [x for x in parties + pk if re.search("咨询|设计|监理|实施支持", x)]
    env = [x for x in notes if re.search("环境|环评|遗产|征地|移民|类别", x)]
    contact = [x for x in parties if re.search("联系|@|电话", x)]
    ms = R._milestones(ctx, date(2000, 1, 1), date(2100, 1, 1), {opp_rid})
    basics_zh = [
        ("项目名称与编号", [x for x in (str(g("name_zh") or ""), str(g("name_local") or ""), ";".join(lines("external_ids"))) if x], ""),
        ("项目地点", [str(g("location") or R._cell(g("city")) or "待核实")], ""),
        ("业主与执行机构", owner or ["待核实"], owner_m),
        ("借款人", [borrower or "待核实"], fin_m if borrower else ""),
        ("总投资与资金来源", total + fin or ["待核实"], fin_m + scope_m),
        ("建设内容", scope or ["待核实"], scope_m),
        ("采购规则", [elig or "待核实"], ""),
        ("资格要求", ["施工合同的资格条件尚未公布,以招标文件为准;对照框架见第四部分(二)"] + [x for x in pk if "工程" in x][:2], pk_m),
        ("设计与监理", design or ["待核实"], parties_m + pk_m if design else ""),
        ("工期", ["待核实"], ""),
        ("环境与社会", env or ["待核实"], notes_m if env else ""),
        ("建成后运营", ["待核实"], ""),
        ("业主联系人", contact or ["待核实"], parties_m if contact else ""),
    ]

    # ---- 三、项目进展(数据):每个阶段取最近的时间节点
    by_phase: dict[str, list] = {}
    for m in ms:
        kind, label = str(b.get("milestone", m, "kind") or ""), str(b.get("milestone", m, "label") or "")
        ph = "咨询招标" if MS_PHASE.get(kind) == "施工招标" and re.search("咨询|监理|设计|实施支持", label) else MS_PHASE.get(kind)
        if ph:
            by_phase.setdefault(ph, []).append(m)
    phases_zh = []
    for ph in PR.PHASES:
        got = by_phase.get(ph) or []
        if not got:
            phases_zh.append((ph, "未开始", "—", "—", ""))
            continue
        m = got[-1]
        d = tstore.day(b.get("milestone", m, "day"), b.tz) or ""
        st = str(b.get("milestone", m, "status") or "")
        state = "待核实" if st == "推断" or "推断" in str(b.get("milestone", m, "label") or "") else (
            "已完成" if st == "已发生" and d <= ctx.today.isoformat() else "未开始")
        lead = b.get("milestone", m, "source") or b.get("milestone", m, "lead")
        lk = b.get("lead", b.record("lead", tstore.link_id(lead)), "key") if tstore.link_id(lead) else None
        what = f"{b.get('milestone', m, 'kind')}" + (f":{b.get('milestone', m, 'label')}" if b.get("milestone", m, "label") else "")
        phases_zh.append((ph, state, d + ("(估算)" if state == "未开始" else ""), what, f"⟦lead:{lk}⟧" if lk and f"lead:{lk}" in src.rows else ""))
    if g("public_stage"):
        phases_zh = [p if p[1] != "未开始" or p[0] != "施工招标" or str(g("public_stage")) != "采购进行中" else
                     (p[0], "进行中", "—", str(g("public_stage_note") or g("public_stage")), "") for p in phases_zh]

    # ---- 给模型的数据(临时编号 [n] → 来源键)
    data = [f"项目:{code} {g('name_zh') or ''}({g('name_local') or ''});国别 {R._cell(g('country'))};领域 {R._cell(g('sectors'))};"
            f"公开进度 {g('public_stage') or ''}({g('public_stage_note') or ''});投标资格 {elig}"]
    for p_, h in (("owner_org", "业主与执行机构"), ("financing", "资金"), ("scope", "建设内容与规模"), ("packages", "标段"), ("parties", "参与方"),
                  ("ai_reason", "要点"), ("pending", "待核实"), ("external_ids", "外部编号")):
        for x in lines(p_)[:12]:
            t, m = _refs(x, src)
            data.append(f"{h}:{t}{m}")
    for m in ms:
        lk = tstore.link_id(b.get("milestone", m, "source") or b.get("milestone", m, "lead"))
        key = b.get("lead", b.record("lead", lk), "key") if lk else None
        data.append(f"时间节点:{tstore.day(b.get('milestone', m, 'day'), b.tz)} {b.get('milestone', m, 'kind')} {b.get('milestone', m, 'status')} "
                    f"{b.get('milestone', m, 'label') or ''}" + (f"⟦lead:{key}⟧" if key and f"lead:{key}" in src.rows else ""))
    for r in leads[:15]:
        k = str(b.get("lead", r, "key") or "")
        data.append(f"来源线索:{src.rows.get(f'lead:{k}', {}).get('date')} {str(b.get('lead', r, 'title') or '')[:100]} | "
                    f"{str(b.get('lead', r, 'summary_zh') or '')[:200]}⟦lead:{k}⟧")
    if internal:  # 人写的只进内部版(R9)
        data.append(f"内部(不能当来源):我方阶段 {g('stage') or ''};优先级 {g('priority') or ''};负责人 {R._cell(g('owner'))};"
                    f"当前判断 {g('judgment') or ''};下一步 {g('next_step') or ''}(到期 {tstore.day(g('next_due'), b.tz) or '—'})")
        data += [f"内部备注(不能当来源):{x}" for x in lines("internal_note")[:10]]
        fus = R._followups_between(ctx, date(2000, 1, 1), date(2100, 1, 1), {opp_rid})[-10:]
        data += [f"跟进记录(不能当来源):{tstore.day(b.get('followup', f, 'day'), b.tz)} {str(b.get('followup', f, 'content') or '')[:200]}"
                 for f in fus]
        firm = R.company_chapter(ctx, "项目报告", code)  # A23:我方资质与业绩,写判断、短板与资格对照时用
        data += [f"我方资质与业绩(内部,不能当来源):{x.strip('|- ')}" for x in firm.splitlines()[2:] if x.strip() and not x.startswith("|---")][:20]
    text = "\n".join(data)
    num: dict[str, int] = {}
    for k in PR._MARK.findall(text):
        num.setdefault(k, len(num) + 1)
    text = PR._MARK.sub(lambda m: f"[{num[m.group(1)]}]", text)
    text += "\n来源编号:" + ";".join(f"[{n}] {src.rows[k]['publisher']}《{src.rows[k]['title'][:50]}》" for k, n in num.items())
    back = {str(n): k for k, n in num.items()}
    text += "\n\n基本情况表(数据原文):\n" + "\n".join(f"{zh if lang == 'zh' else en}: " + " / ".join(ls)
                                                      for (zh, en), (_, ls, _) in zip(BASICS, basics_zh))

    ana, note = _ask(ctx, lang, text, back)

    # ---- 合成
    def t(k: str, fb: str) -> str:
        return ana.get(k) or fb

    pend = lines("pending")
    tbd = "待研判" if lang == "zh" else "to be assessed"
    phases = phases_zh if lang == "zh" else [(PHASES_EN[p], PR.STATES_EN.get(s, s), d.replace("(估算)", " (est.)"), w, m)
                                              for p, s, d, w, m in phases_zh]
    rows = _rows(ana.get("phases", ""), 4)
    states = PR.STATES if lang == "zh" else tuple(PR.STATES_EN.values())
    if len(rows) >= len(phases):
        phases = [(p[0], r[1] if r[1] in states else p[1], r[2] or p[2], r[3] or p[3], p[4]) for p, r in zip(phases, rows)]
    got = {r[0].strip().lower(): r[1] for r in _rows(ana.get("basics", ""), 2)}  # 模型改写的(英文版是译文),没写的用数据原文
    basics_zh = [(k, ls, "".join(f"⟦{x}⟧" for x in dict.fromkeys(PR._MARK.findall(m)))) for k, ls, m in basics_zh]  # 来源号去重
    basics = [(zh if lang == "zh" else en, [x.strip() for x in re.split("[;；]", got.get((zh if lang == "zh" else en).lower(), "")) if x.strip()] or ls, m)
              for (zh, en), (_, ls, m) in zip(BASICS, basics_zh)]
    acts = _actions(ctx, o, lang, internal, ana.get("actions", ""))
    leader = [x for x in (ana.get("leader") or "").splitlines() if x.strip()] if internal else []
    partner = [x for x in (ana.get("partner") or "").splitlines() if x.strip()]
    if lang == "zh":
        quals = [(k, (f"{elig}(以招标文件为准)" if k == "合格性与注册" and elig else
                      "当地注册、执照与许可要求待核实" if k == "本地许可" else f"{gen or '一般要求认证与类似项目的环境社会管理经验'}(框架,以招标文件为准)"))
                 for k, gen in PR.QUALS]
    else:
        quals = [(QUALS_EN[k], ("to be confirmed in the tender documents" if k in ("合格性与注册", "本地许可") else
                                f"{QUALS_EN_TEXT.get(k, '')} (framework; subject to the tender documents)")) for k, _ in PR.QUALS]
    tpl_org = PR.default_org(TEMPLATES[lang])
    if lang == "en":  # 来源标题:模型的译文;没有时去掉中文括注
        tr = {r[0].strip(): r[1] for r in _rows(ana.get("sources", ""), 2)}
        for k, n in num.items():
            row = src.rows[k]
            row["title"] = tr.get(str(n)) or re.sub(r"\s*[((][^()()]*[\u4e00-\u9fff][^()()]*[))]", "", row["title"]).strip() or row["title"]
    first = lambda i: [x for x in basics[i][1] if not x.startswith(("总投资", "Total cost"))]  # noqa: E731
    names = [x for x in lines("aliases") if not _CJK.search(x) and x != str(g("name_local") or "") and " " in x]  # 当地文字全称(单个词的简称不算)
    content = {
        "lang": lang, "id": code, "iso3": iso, "country": country,
        "name": str(g("name_zh") or code) if lang == "zh" else str(g("name_local") or g("name_zh") or code),
        "name_en": str(g("name_local") or "") if lang == "zh" else (names[0] if names else ""),
        "field": ("、".join(g("sectors") or []) if lang == "zh" else ", ".join(SECTOR_EN.get(x, x) for x in g("sectors") or []).capitalize()) or "—",
        "owner": basics[2][1][0] if basics[2][1] else "—",
        "funding": ((";" if lang == "zh" else "; ").join(first(4)[:3]) or (basics[4][1][0] if basics[4][1] else "—"))[:160],
        "doc_no": f"{cinfo.get('iso2') or iso}-XM-{ctx.today.year}-{code.split('-')[-1]}", "as_of": ctx.today.isoformat(),
        "org": tpl_org or ("【部门】" if lang == "zh" else "—"), "to": "公司领导" if lang == "zh" else f"Partners in {country}",
        "core": [("项目", t("project", tbd)), ("进展", t("progress", f"公开进度:{g('public_stage') or '待核实'}。{g('public_stage_note') or ''}")),
                 ("判断", t("judgement", tbd)), ("建议", t("advice", tbd))],
        "background": t("background", tbd), "basics": basics, "basics_labels": lang == "en", "country_rows": [], "phases": phases,
        "pros": [(k, t(k, tbd)) for k in PR.PROS], "cons": [(k, t(k, ("待核实:" + "、".join(pend[:3])) if k == "不确定性" and pend and lang == "zh" else tbd))
                                                       for k in PR.CONS],
        "quals": quals, "entry": [(k, t(k, tbd)) for k in PR.ENTRY], "actions": acts,
        "leader": leader or (["是否同意将本项目列为跟踪项目", "前期费用预算:待编制", "对外接触与签署保密协议的授权", "资质、业绩与人员资源的协调"] if internal else []),
        "partner": partner or ((["核实:" + x for x in pend[:3]] or ["核实下一节点的时间与标段划分"]) if lang == "zh"
                               else ["Confirm the next milestone and how the works will be packaged"]),
        "leads": [], "note": note,
    }
    content = PR._number(content, src)
    cited = {r["id"] for r in leads}
    return content, cited, code


def _rows(text: str, n: int) -> list[list[str]]:
    out = []
    for line in str(text or "").splitlines():
        parts = [x.strip() for x in PR.plain(line).split("|")]
        if len(parts) >= n:
            out.append(parts[:n])
    return out


def _actions(ctx: Ctx, o: dict, lang: str, internal: bool, model: str) -> list[tuple[str, str, str, str]]:
    """计划动作:内部版先取商机上人写的下一步与内部备注里的「建议动作」,没有再用模型的;公开版只用模型的。"""
    b = ctx.base
    g = lambda p: b.get("opportunity", o, p) if b.has("opportunity", p) else None  # noqa: E731
    todo = "待启动" if lang == "zh" else "To start"
    out: list[tuple[str, str, str, str]] = []
    if internal:
        who = R._cell(g("owner")) or "【待定】"
        if g("next_step"):
            due = tstore.day(g("next_due"), b.tz) or "待定"
            out.append((str(g("next_step")), who, due, "待核实" if due < ctx.today.isoformat() else todo))
        for x in str(g("internal_note") or "").splitlines():
            m = re.match(r"\s*建议动作[::](.+?)(?:[((]到期\s*([\d-]+)[))])?\s*$", x)
            if m:
                out.append((m.group(1).strip(), who, m.group(2) or "待定", todo))
    if not out:
        out = [(a, w, d, todo) for a, w, d in _rows(model, 3)]
    if not out:
        out = [("跟踪项目下一节点", "【待定】", "待定", todo)] if lang == "zh" else [("Follow the next project milestone", "Head office BD team", "TBC", todo)]
    return out[:8]


def _ask(ctx: Ctx, lang: str, data: str, back: dict[str, str]) -> tuple[dict, str]:
    """两次模型调用:判断与建议各段;进展表(英文版另加基本情况表的译文)。→ (各字段, 编制说明)。"""
    clf = ctx.clf
    if clf is None or not getattr(clf, "enabled", False) or not hasattr(clf, "write"):
        return {}, ("本报告未使用模型,只填了事实;判断部分写\"待研判\"。" if lang == "zh"
                    else "No model was available: the report shows facts only, in their original Chinese wording, and the assessment is left blank.")
    system = SYSTEM[lang].format(bidder=(ctx.profile.bidder or {}).get("nationality") or "未设")
    facts = {"phases": FIELDS_FACTS["phases"][lang], "basics": FIELDS_FACTS["basics"][lang]}
    if lang == "en":
        facts["sources"] = ("Source titles: translate each line of the 来源编号 list into an English title as 'number | title', e.g. '3 | Project "
                            "summary (EBRD project 56006)'; keep official English titles as they are")
    out: dict = {}
    bad: list[str] = []
    errs = []
    for fields in (FIELDS[lang], facts):
        got = clf.write(system, data, fields)
        if not got or got.get("_error"):
            errs.append((got or {}).get("_error") or "no reply")
            continue

        def mark(m: re.Match) -> str:
            nums = [x for x in re.split(r"[,,、\s]+", m.group(1)) if x]
            bad.extend(x for x in nums if x not in back)
            return "".join(f"⟦{back[x]}⟧" for x in nums if x in back)

        for k, v in got.items():
            v = re.sub(r"\[(\d+(?:\s*[,,、]\s*\d+)*)\]", mark, str(v or ""))
            out[k] = v.strip() if k in ("leader", "partner", "actions", "phases", "basics", "sources") else PR.plain(v)
    if lang == "zh":
        note = (f"判断、分析与建议由模型({clf.label})据商机、来源线索与内部记录起草,编制方判断已注明\"推断\";请核对后使用。"
                + (f"其中 {len(bad)} 处来源编号在数据里没有,已删去。" if bad else "") + (f"部分段落模型没写成({errs[0]})。" if errs else ""))
    else:
        note = (f"Assessment and recommendations were drafted by a language model ({clf.label}) from public sources and checked against them; "
                "our own judgements are marked." + (" Some sections could not be drafted." if errs else ""))
    return out, note


def render(ctx: Ctx, opp_rid: str, internal: bool, out: Path, template: Path | None = None) -> tuple[Path | None, set[str], str, str]:
    """出 Word → (文件;出不成时 None, 引用的线索记录编号, 商机编号, Markdown 留底)。
    选的模板认不出表(如国别报告模板)时改用程序自带的同语言模板;自带的也没有(private 没挂上)或出错时返回 None,由调用方传 Markdown。"""
    c, cited, code = build(ctx, opp_rid, internal)
    md = PR.to_markdown(c)
    for tpl in dict.fromkeys(x for x in (template, TEMPLATES[c["lang"]]) if x):
        if not tpl.exists():
            continue
        try:
            return PR.to_docx(c, out, tpl), cited, code, md
        except ValueError as e:
            ctx.log(f"  ! 项目报告模板认不出({e})" + (",改用自带的项目分析报告模板" if tpl != TEMPLATES[c["lang"]] else ""))
    return None, cited, code, md
