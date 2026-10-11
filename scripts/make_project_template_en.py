"""项目分析报告的英文(外部)模板:从中文模板逐段翻成英文,表格、样式、页眉页脚与版式不变。

用法:python scripts/make_project_template_en.py private/templates/项目分析报告模板.docx private/templates/项目分析报告模板_英文.docx ["编制单位英文名"]
编制单位写在文档属性「作者」里(中文模板是中文名),报告首页的 Prepared by 取它。
英文版发前方伙伴:第六部分只留「请前方伙伴跟进」,「请领导决策与支持」整段删掉;页眉写 English | Confidential。
中文模板改了文字后重跑本脚本;遇到没翻译的文字会列出来,补进 EN 再跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

EN = {
    "【国别】": "", "【项目名称】": "【Project name】", "【项目外文名称（官方名称）】": "【Local or official name】",
    "项目分析报告": "Project Analysis Report",
    "国别": "Country", "【国家】": "【Country】", "项目领域": "Sector", "【如市政水务、水利、电力、交通】": "【e.g. municipal water, irrigation, power】",
    "业主机构": "Owner", "【业主或执行机构】": "【Owner or implementing agency】", "资金来源": "Funding",
    "【出资方、融资方式与金额】": "【Funder, instrument and amount】", "文件编号": "Ref. No.", "【国别代码-XM-年份-序号】": "【Country code-XM-year-no.】",
    "信息截至": "As of", "【日期】": "【Date】", "编制单位": "Prepared by", "【部门】": "【Department】", "报送对象": "Recipients",
    "【中文版报领导，外文版发伙伴】": "【Partners in the country】",
    "填写说明": "Notes for authors", "（正式报送前删除本框）": " (delete this box before sending)",
    "篇幅一般控制在 6 页以内。领导只读第一部分，也应能掌握项目要点和请示事项。":
        "Keep the report within about 6 pages. A reader of Section 1 alone should grasp the project and our requests.",
    "每条事实在句末标注来源编号，如 ": "End each fact with its source number, e.g. ",
    "，并在第七部分登记发布方、日期、等级和网址。只引用官方公开信息，不引用公司内部文件、内部报告或前方反馈材料。":
        ", and list the publisher, date, grade and URL in Section 7. Cite only public information, never internal company documents.",
    "编制方自己的判断须注明“估算”或“推断”，不与官方数据混写。": "Mark our own judgements “est.” or “our inference” and keep them apart from official data.",
    "报告语言标在页眉右上角。中文版报公司领导，英文或属地语言版发前方伙伴，两版共用文件编号。伙伴版应删去第六部分（一）等仅供内部的内容。":
        "The report language is shown in the header. This version goes to partners and shares its reference number with the Chinese version.",
    "状态统一使用“已完成、进行中、待核实、未开始”四种。每次更新改写“信息截至”日期和第三部分进展表，旧版本另存备查。":
        "Use four statuses only: Completed, In progress, Unconfirmed, Not started. On each update, change the “As of” date and the Progress table.",
    "一、核心结论": "1. Key Findings", "项目：": "Project: ", "【项目内容、总投资与资金来源，一至两句】": "【Scope, total cost and funding, in one or two sentences】",
    "【来源编号】": "【source no.】", "进展：": "Progress: ", "【当前所处阶段、最近的关键节点和下一个节点】": "【Current stage, latest milestone and next milestone】",
    "判断：": "Assessment: ", "【是否值得跟踪；最主要的有利因素和难点】": "【Whether it is worth pursuing; main strengths and difficulties】",
    "建议：": "Next steps: ", "【是否列为跟踪项目及下一步动作；请领导批准和请前方跟进的核心事项】": "【What we will do next and what we ask our partners to do】",
    "二、项目基本情况": "2. Project Overview", "项目": "Item", "内容": "Details", "来源": "Source",
    "项目名称与编号": "Name and ID", "【中文名称】": "【English name】", "【外文名称（官方名称）】": "【Local or official name】",
    "【出资方项目号或招标编号】": "【Funder project ID or tender no.】", "【n】": "【n】",
    "项目地点": "Location", "【城市、区县；人口或服务范围】": "【City and district; population or service area】",
    "业主与执行机构": "Owner and implementing agency", "【业主；具体执行部门】": "【Owner; implementing unit】",
    "借款人": "Borrower", "【借款人或出资方；国家预算项目填“国家预算”】": "【Borrower or funder; “state budget” for budget-funded projects】",
    "总投资与资金来源": "Cost and funding",
    "【总投资或合同估算价，注明币种；出资方、金额、是否已承诺、配套资金】": "【Total cost or contract estimate with currency; funders, amounts, commitment, counterpart funds】",
    "建设内容": "Scope", "【主要工程量：管网长度、水厂或污水厂规模、泵站、水池等；分期或分批时逐项列出】":
        "【Main quantities: network length, plant capacity, pumping stations, reservoirs; list each phase or tranche】",
    "采购规则": "Procurement rules", "【出资方采购规则或本国公共采购法；是否限国别；招标平台】":
        "【Funder procurement rules or national law; nationality restrictions; tender platform】",
    "资格要求": "Qualification", "【资格条件是否已公布、何时公布】": "【Whether and when the criteria are published】", "；对照表见": "; see ",
    "第四部分（二）": "Section 4.2", "设计与监理": "Design and supervision",
    "【设计方、监理方或咨询方；施工合同还是设计加施工】": "【Designer, supervisor or consultant; build-only or design-build】",
    "工期": "Duration", "【施工期；预计开工与完工时间】": "【Construction period; expected start and completion】",
    "环境与社会": "Environmental and social", "【环境类别；征地、文物或其他敏感因素】": "【E&S category; land acquisition, heritage or other sensitivities】",
    "建成后运营": "Operator after completion", "【运营单位；推断须注明】": "【Operator; mark inferences】",
    "业主联系人": "Owner contact", "【姓名、部门、公开联系方式】": "【Name, unit and public contact details】",
    "三、项目进展": "3. Progress", "背景：": "Background: ", "【项目由来与此前阶段的完成情况】": "【How the project started and what earlier phases achieved】",
    "阶段": "Stage", "状态": "Status", "时间": "Date", "事项": "Event", "前期研究": "Preparation", "【状态】": "【Status】", "【时间】": "【Date】",
    "【可研、环评等关键事项】": "【Feasibility, E&S and other key steps】", "融资批准": "Funding approval", "【出资方批准情况】": "【Funder approval】",
    "咨询招标": "Consultant procurement", "【设计、监理或实施支持咨询的采购】": "【Procurement of design, supervision or implementation support】",
    "融资签约": "Loan signing", "【贷款或资金协议签署】": "【Signing of the loan or funding agreement】", "详细设计": "Detailed design",
    "【设计单位与进度】": "【Designer and progress】", "施工招标": "Works tenders", "【招标方式、平台与分标】": "【Procurement method, platform and lots】",
    "施工建设": "Construction", "【工期与开工条件】": "【Duration and conditions for starting】",
    "状态分四种：已完成、进行中、待核实、未开始，阶段与状态两格分别以绿、黄、红、灰底色标示。阶段可按项目增删，例如国家预算项目可改为立项、预算下达、招标、签约、施工。尚未发生的时间注明“估算”。":
        "Legend: green = completed, yellow = in progress, red = unconfirmed, grey = not started. Dates marked “est.” are our estimates.",
    "四、分析判断": "4. Analysis", "（一）有利因素与不利因素": "4.1 Strengths and Risks", "有利因素": "Strengths", "不利因素与风险": "Weaknesses and risks",
    "资金。": "Funding. ", "【是否落实、支付币种与付款保障】": "【Whether secured; payment currency and security】",
    "采购。": "Procurement. ", "【是否开放、评标方式】": "【Openness and evaluation method】",
    "匹配度。": "Fit. ", "【与我方主业和业绩的吻合程度】": "【Fit with our core business and track record】",
    "信息与渠道。": "Information and access. ", "【信息透明度；已有的关系或对接渠道】": "【Transparency; existing relationships or channels】",
    "技术与施工。": "Technical and construction. ", "【主要技术或施工难点】": "【Main technical or construction difficulties】",
    "合同与接口。": "Contract and interfaces. ", "【续建、分标、合同条件等风险】": "【Risks from completion works, lot splitting or contract terms】",
    "不确定性。": "Uncertainties. ", "【资金、征地、审批等尚未落实的事项】": "【Funding, land or approvals not yet secured】",
    "我方短板。": "Our gaps. ", "【资质、业绩、本地化方面的不足】": "【Gaps in licences, references or local presence】",
    "（二）资格要求对照": "4.2 Qualification Requirements", "项目要求": "Requirement", "我方已满足": "Met by us", "需联合体补充": "JV partner to cover",
    "合格性与注册：": "Eligibility and registration: ", "【国籍、制裁与禁止名单、利益冲突；报名或注册平台】":
        "【Nationality, sanctions and debarment, conflict of interest; registration platform】",
    "财务稳健：": "Financial soundness: ", "【财务报表年限；破产、欠税等要求】": "【Years of financial statements; bankruptcy and tax arrears】",
    "平均年营业额：": "Average annual turnover: ", "【金额与年限；联合体各方比例】": "【Amount and period; shares for JV members】",
    "融资能力：": "Financial resources: ", "【流动资金或授信金额】": "【Liquid assets or credit lines】",
    "一般经验：": "General experience: ", "【合同数量与金额】": "【Number and value of contracts】",
    "管理经验：": "Management experience: ", "【总承包或牵头经验要求】": "【Experience as main contractor or JV lead】",
    "类似工程经验：": "Specific experience: ", "【同类合同数量与关键工作量】": "【Number of similar contracts and key quantities】",
    "履约与诉讼：": "Performance and litigation: ", "【履约记录与未决诉讼要求】": "【Contract performance and pending litigation】",
    "环境健康安全：": "Environment, health and safety: ", "【认证与环境社会管理经验】": "【Certifications and E&S management experience】",
    "关键人员：": "Key personnel: ", "【岗位与资历要求】": "【Positions and qualifications】",
    "本地许可：": "Local licensing: ", "【当地注册、执照与许可要求】": "【Local registration, licences and permits】",
    "说明：资格条件以招标文件为准；未公布时可按出资方标准采购文件列出框架，并注明“以招标文件为准”。“我方已满足”填是、否或部分；“需联合体补充”填需由伙伴补足的内容和拟合作方。每条要求注明联合体各方须分别满足还是合计满足。":
        "Note: criteria follow the tender documents. Until they are published, this table follows the framework of the funder’s standard "
        "procurement documents. The last two columns are completed by our bid team.",
    "（三）我方切入点与竞争": "4.3 Entry Points and Competition", "首选：": "First choice: ", "【最适合我方的标段或合同及理由】": "【The lot or contract that suits us best, and why】",
    "次选：": "Second choice: ", "【备选切入方式】": "【Alternative way in】", "补充：": "Supply: ", "【设备材料供应等机会】": "【Equipment and material supply opportunities】",
    "潜在竞争者：": "Potential competitors: ", "【已知或可能的竞争对手及其优势】": "【Known or likely competitors and their strengths】",
    "五、计划动作": "5. Action Plan", "序号": "No.", "动作": "Action", "责任方": "Responsible", "【具体动作】": "【Action】", "【责任方】": "【Responsible】",
    "【完成时间】": "【Timing】", "1": "1", "2": "2", "3": "3", "[1]": "[1]", "[2]": "[2]", "[3]": "[3]",
    "六、需要的支持": "6. Requested Follow-up from Partners", "【需要核实的关键信息】": "【Key facts to confirm】",
    "【需要拜访或对接的机构与人员】": "【Agencies and people to meet】", "【需要收集的现场、价格与合作伙伴信息】": "【Site, price and partner information to collect】",
    "七、信息来源": "7. Sources", "编号": "No.", "来源与网址": "Source and URL", "发布方": "Publisher", "日期": "Date", "等级": "Grade",
    "【文件或网页标题】": "【Document or page title】", "【网址】": "【URL】", "【发布方】": "【Publisher】", "【等级】": "【Grade】",
    "等级说明：官方一手指出资方、业主或政府的正式文件；企业官网指相关企业在其官网公开的信息；本地媒体指项目所在国新闻机构的公开报道。只引用公开信息，不引用公司内部文件。注明网址的访问日期。文中标注“估算”“推断”的内容为编制方判断，不是官方数据。":
        "Grades: Official = formal documents of the funder, owner or government; Company website = information the company concerned publishes; "
        "Media = public news reports. Only public information is cited, never internal company documents. "
        "Content marked “est.” or “our inference” is our own judgement, not official data.",
    "项目分析报告｜": "Project Analysis Report | ", "报告语言：": "Report language: ", "【中文 / English / 属地语言】": "English", "｜内部资料": " | Confidential",
    "第 ": "Page ", " 页　共 ": " of ", " 页": "",
}
DROP = ("（一）请领导决策与支持", "（二）请前方伙伴跟进", "【是否同意列为跟踪或介入项目】", "【前期费用预算】", "【对外接触或签署文件的授权】",
        "【资质、业绩、人员等资源的协调】")  # 伙伴版不要的段落(整段删)


def translate(el, missing: list[str]) -> None:
    for r in el.iter(qn("w:r")):
        ts = list(r.iter(qn("w:t")))
        text = "".join(t.text or "" for t in ts)
        if not text.strip():
            continue
        if text not in EN:
            missing.append(text)
            continue
        ts[0].text = EN[text]
        ts[0].set(qn("xml:space"), "preserve")
        for t in ts[1:]:
            t.text = ""
        for fonts in r.iter(qn("w:rFonts")):  # 英文正文不用中文字体的西文字形
            fonts.attrib.pop(qn("w:hint"), None)


def main(src: str, dst: str, org: str = "") -> None:
    d = Document(src)
    for p in list(d.paragraphs):
        if p.text.strip() in DROP:
            p._p.getparent().remove(p._p)
    missing: list[str] = []
    translate(d.element.body, missing)
    for s in d.sections:
        for part in (s.header, s.footer, s.first_page_header, s.first_page_footer, s.even_page_header, s.even_page_footer):
            if part is not None and not part.is_linked_to_previous:
                translate(part._element, missing)
    if missing:
        raise SystemExit("没翻译的文字(补进 EN):\n" + "\n".join(repr(x) for x in dict.fromkeys(missing)))
    for p in d.paragraphs:  # 段落与文档语言改成英文(拼写检查、断词)
        for lang in p._p.iter(qn("w:lang")):
            lang.set(qn("w:val"), "en-GB")
            lang.attrib.pop(qn("w:eastAsia"), None)
    d.core_properties.title = "Project Analysis Report"
    d.core_properties.author = org  # 编制单位英文名由参数给;不给就留空,不沿用中文模板里的作者
    d.core_properties.language = "en-GB"
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    d.save(dst)
    print(f"{src} → {dst}({Path(dst).stat().st_size:,} 字节)")


if __name__ == "__main__":
    main(*sys.argv[1:4])
