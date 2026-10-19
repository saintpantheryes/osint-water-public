"""生成本体图 本体模型.svg(SAM/SCBRD 形状语义 + OBR;内容与 ontology.yaml 对应,改本体后同步改这里)。用法:python docs/ontology/draw_svg.py docs/ontology/本体模型.svg"""
import sys
from html import escape

W, H = 1800, 1720
_ONTO = __import__("yaml").safe_load(open(__import__("os").path.join(__import__("os").path.dirname(__file__), "ontology.yaml"), encoding="utf-8"))
VER, DATE = _ONTO["version"], _ONTO["date"]
N_CORE = sum(1 for o in _ONTO["objects"] if o.get("core"))
N_SUPPORT = len(_ONTO["objects"]) - N_CORE
FONT = "'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','WenQuanYi Zen Hei',sans-serif"
C = {
    "ai": "#D5E8D4", "ai_b": "#82B366", "hu": "#F8CECC", "hu_b": "#B85450", "dv": "#F0F0F0", "dv_b": "#BDBDBD",
    "head": "#E3F2FD", "head_b": "#1976D2", "band": "#F7F7F7", "band_b": "#BDBDBD",
    "red": "#D32F2F", "blue": "#1976D2", "green": "#388E3C", "orange": "#F57C00", "grey": "#757575", "black": "#212121",
    "rule": "#FFCDD2", "rule_b": "#C62828", "view_b": "#7B1FA2", "view": "#FBF7FC",
}
out: list[str] = []


def tw(s: str, size: float) -> float:
    w = 0.0
    for ch in s:
        o = ord(ch)
        if ch == " ":
            w += 0.32
        elif ch in "·":
            w += 0.62
        elif o < 128:
            w += 0.56
        else:
            w += 1.0
    return w * size


def wrap(text: str, width: float, size: float) -> list[str]:
    lines, cur = [], ""
    for tok in text.split(" · "):
        cand = tok if not cur else cur + " · " + tok
        if tw(cand, size) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur + " ·")
        cur = ""
        while tw(tok, size) > width:  # 单项过长:按字切
            n = len(tok)
            while n > 1 and tw(tok[:n], size) > width:
                n -= 1
            lines.append(tok[:n])
            tok = tok[n:]
        cur = tok
    if cur:
        lines.append(cur)
    return lines


def T(x, y, s, size=13, fill="#212121", weight="normal", anchor="start", italic=False, halo=False):
    st = ' font-style="italic"' if italic else ""
    h = ' paint-order="stroke" stroke="#ffffff" stroke-width="4" stroke-linejoin="round"' if halo else ""
    out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" font-weight="{weight}" '
               f'text-anchor="{anchor}"{st}{h}>{escape(s)}</text>')


def R(x, y, w, h, fill, stroke, sw=1.5, rx=0, dash=None, extra=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" '
               f'stroke="{stroke}" stroke-width="{sw}"{d}{extra}/>')


SEC = {"ai": (C["ai"], C["ai_b"]), "hu": (C["hu"], C["hu_b"]), "dv": (C["dv"], C["dv_b"]), "nt": ("#FFFFFF", "#90A4AE")}


def obj(x, y, w, name, stereo="«object»", sections=(), core=False, view=False):
    """对象框:表头(构造型 + 名称)+ 若干属性分区(机器写绿、人写粉、派生灰)。返回 (x, y, w, h)。"""
    size, lh, pad = 13, 18.5, 10
    blocks = []
    for kind, label, text in sections:
        body = []
        for para in text.split("\n"):
            body += wrap(para, w - 2 * pad - 4, size)
        blocks.append((kind, label, body))
    head_h = 46
    h = head_h + sum(8 + 17 + len(b) * lh + 8 for _, _, b in blocks) + 6
    fill, stroke = (C["view"], C["view_b"]) if view else ("#FFFFFF", "#37474F")
    R(x, y, w, h, fill, stroke, sw=2.5 if core else 1.8, rx=6, dash="7 5" if view else None)
    R(x + 1.5, y + 1.5, w - 3, head_h - 3, "#F3E5F5" if view else C["head"], "none", rx=5)
    T(x + 12, y + 18, stereo, size=11.5, fill="#666666", italic=True)
    T(x + 12, y + 37, name, size=17, weight="bold")
    if core:
        T(x + w - 12, y + 18, "核心对象", size=11.5, fill=C["red"], anchor="end", weight="bold")
    cy = y + head_h
    for kind, label, body in blocks:
        f, b = SEC[kind]
        bh = 8 + 17 + len(body) * lh + 2
        R(x + 6, cy + 4, w - 12, bh, f, b, sw=1, rx=4)
        T(x + 6 + pad - 2, cy + 20, label, size=11.5, fill=b, weight="bold")
        ty = cy + 20 + lh
        for line in body:
            T(x + 6 + pad - 2, ty, line, size=size, fill="#666666" if kind == "dv" else "#212121", italic=kind == "dv")
            ty += lh
        cy += bh + 8
    return (x, y, w, h)


def marker(mid, color, shape="arrow"):
    if shape == "arrow":
        out.append(f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                   f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')
    elif shape == "diamond":
        out.append(f'<marker id="{mid}" viewBox="0 0 16 10" refX="1" refY="5" markerWidth="14" markerHeight="9" orient="auto-start-reverse">'
                   f'<path d="M0,5 L8,0 L16,5 L8,10 z" fill="{color}"/></marker>')


def path(d, color, sw=2, dash=None, end="", start=""):
    da = f' stroke-dasharray="{dash}"' if dash else ""
    me = f' marker-end="url(#{end})"' if end else ""
    ms = f' marker-start="url(#{start})"' if start else ""
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{da}{me}{ms} stroke-linecap="round"/>')


def label(x, y, s, color, size=12.5, anchor="middle", weight="normal"):
    T(x, y, s, size=size, fill=color, anchor=anchor, halo=True, weight=weight)


def octagon(cx, cy, w, h, title, sub):
    c = 14
    pts = [(cx - w / 2 + c, cy - h / 2), (cx + w / 2 - c, cy - h / 2), (cx + w / 2, cy - h / 2 + c), (cx + w / 2, cy + h / 2 - c),
           (cx + w / 2 - c, cy + h / 2), (cx - w / 2 + c, cy + h / 2), (cx - w / 2, cy + h / 2 - c), (cx - w / 2, cy - h / 2 + c)]
    out.append(f'<polygon points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}" fill="{C["rule"]}" stroke="{C["rule_b"]}" stroke-width="1.8"/>')
    T(cx, cy - 14, title, size=13.5, fill=C["rule_b"], anchor="middle", weight="bold")
    for i, line in enumerate(wrap(sub, w - 26, 12)):
        T(cx, cy + 6 + i * 16, line, size=12, fill="#424242", anchor="middle")


def state(x, y, w, h, s, fill="#FFFFFF", stroke="#37474F", bold=False):
    R(x, y, w, h, fill, stroke, sw=1.6, rx=h / 2)
    T(x + w / 2, y + h / 2 + 5, s, size=13, anchor="middle", weight="bold" if bold else "normal")


# ---------------- 画布 ----------------
out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">')
out.append("<defs>")
for mid, col in (("aR", C["red"]), ("aB", C["blue"]), ("aG", C["green"]), ("aO", C["orange"]), ("aY", C["grey"]),
                 ("aK", C["black"]), ("aP", C["hu_b"]), ("aV", C["view_b"])):
    marker(mid, col)
marker("dK", C["black"], "diamond")
out.append("</defs>")
R(0, 0, W, H, "#FFFFFF", "none")

# 标题
T(40, 50, f"全球水务开源情报 本体模型 v{VER} · 对象 — 行为 — 规则", size=28, weight="bold")
T(40, 80, "SAM/SCBRD × OBR(对象 · 行为 · 规则)  |  "
          f"{N_CORE} 个核心对象 + {N_SUPPORT} 个支撑对象  |  判定是行为、日历是查询  |  每个对象只存一处  |  每个口径只定一次", size=14, fill="#555555")
T(W - 40, 50, f"«system» Ontology · Global Water OSINT v{VER}", size=15, fill=C["blue"], anchor="end", weight="bold")
T(W - 40, 78, f"{DATE} · 唯一业务库 Teable · 机器可读版 docs/ontology/ontology.yaml(分类体系与口径见 本体.md)", size=12.5, fill="#888888", anchor="end")
out.append(f'<line x1="40" y1="96" x2="{W - 40}" y2="96" stroke="#E0E0E0" stroke-width="1.5"/>')

# 主链路横幅
T(40, 124, "关键路径 «keypath»:", size=14, fill=C["red"], weight="bold")
steps = ["① 采集线索", "② 判定", "③ 归属 / 建议商机", "④ 立项跟踪", "⑤ 记跟进", "⑥ 生成报告", "⑦ 日历与提醒"]
sx, sw_, sh = 210, 214, 34
for i, s in enumerate(steps):
    x = sx + i * (sw_ + 8)
    who = "机器" if i in (0, 1, 2, 5) else ("查询" if i == 6 else "人")
    fill = C["ai"] if who == "机器" else (C["hu"] if who == "人" else C["dv"])
    stroke = C["ai_b"] if who == "机器" else (C["hu_b"] if who == "人" else C["dv_b"])
    pts = f"{x},{106} {x + sw_ - 14},{106} {x + sw_},{106 + sh / 2} {x + sw_ - 14},{106 + sh} {x},{106 + sh} {x + 12},{106 + sh / 2}"
    out.append(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    T(x + sw_ / 2 + 3, 128, f"{s}({who})", size=13, anchor="middle", weight="bold")

# ---------------- 第一栏 ----------------
B1_TOP = 158
# 信源(云:外部网站),用圆的并集画轮廓
cx0, cy0 = 50, 200
circles = [(60, 92, 46), (112, 64, 52), (162, 90, 46), (110, 112, 40)]
for cx_, cy_, r_ in circles:
    out.append(f'<circle cx="{cx0 + cx_}" cy="{cy0 + cy_}" r="{r_}" fill="#9E9E9E"/>')
for cx_, cy_, r_ in circles:
    out.append(f'<circle cx="{cx0 + cx_}" cy="{cy0 + cy_}" r="{r_ - 1.8}" fill="#F5F5F5"/>')
T(cx0 + 110, cy0 + 40, "«external»", size=11, fill="#666666", anchor="middle", italic=True)
T(cx0 + 110, cy0 + 62, "信源", size=17, weight="bold", anchor="middle")
T(cx0 + 110, cy0 + 82, "网站 · RSS · 接口 · 门户", size=12, fill="#424242", anchor="middle")
T(cx0 + 110, cy0 + 104, "人写:名称 网址 类型 范围 启用", size=11.5, fill=C["hu_b"], anchor="middle")
T(cx0 + 110, cy0 + 122, "机器写:最近采集 · 状态", size=11.5, fill=C["ai_b"], anchor="middle")
cloud_right, cloud_y = cx0 + 208, cy0 + 70
label(cx0 + 110, 190, "A01 采集 · 机器 · 每天", C["red"], weight="bold")

lead = obj(296, 196, 310, "线索", core=True, sections=[
    ("ai", "机器写 · 采集", "标题 · 原文网址 · 信源 · 来源方式 · 发布日期 · 发现时间 · 原文摘录"),
    ("ai", "机器写 · 判定(只判一次,判过不回头)", "判定方式 · 是否相关 · 类别 · 领域 · 国别 · 中文摘要 · 出资方 · 项目名称 · 金额 · "
                                         "投资规模 · 货币 · 折合人民币 · 建议所属商机 · 可能是新项目\n所属商机:可靠且为空时机器代填,人确认或改定"),
    ("hu", "人写", "处理状态(待处理 / 已处理 / 已忽略) · 处理备注"),
    ("dv", "派生(不存)", "最近截止 · 写进的报告"),
])
lx, ly, lw, lh = lead
lb = ly + lh
# 同事上传
up_y = 392
R(62, up_y, 182, 54, C["hu"], C["hu_b"], sw=1.6, rx=6)
T(153, up_y + 22, "A02 同事上传链接", size=13.5, anchor="middle", weight="bold")
T(153, up_y + 42, "人 · 不做相关性判定", size=12, fill="#555555", anchor="middle")

mile = obj(296, lb + 54, 310, "时间节点", sections=[
    ("ai", "机器写(人可改) · 每条带出处", "类型:截止 / 发布 / 开标 / 授标 / 签约 / 开工 / 完工… · 日期(可只到月、季) · "
                                       "状态:已发生 / 官方计划 / 推断 · 说明 · 出处线索"),
    ("nt", "属于", "一个商机(组合),或一条还没归属的线索"),
])
mx, my, mw, mh = mile
country = obj(296, my + mh + 48, 310, "国别", core=True, sections=[
    ("ai", "机器写(每项带年份与来源)", "国名 · 区域 · 基础信息 · 宏观与水务指标 · 收入组 · 水务管理机构 · 水务国企 · 相关法律法规"),
    ("hu", "人写", "经营区域 · 市场分级 · 我方在当地 · 国别备注"),
    ("dv", "派生(不存)", "线索数 · 商机数"),
])
cxx, cyy, cww, chh = country
# 城市(支撑对象):国别下面;线索、商机可选关联它(本栏的底边按它算)
city = obj(296, cyy + chh + 22, 310, "城市", sections=[
    ("ai", "机器写(GeoNames、WRI Aqueduct 等,每月)", "中文名 · 英文名 · 所属国别 · 行政区 · 坐标 · 人口 · 密度 · "
                                               "水风险(总体、用水压力、干旱、洪水) · 无安全饮水 / 卫生设施 · 人均 GDP"),
    ("hu", "人补", "面积 · 地区生产总值 · 现行水价与货币 · 调价时间 · 备注"),
    ("dv", "派生(不存)", "线索数 · 商机数 · 自来水厂数 · 污水厂数"),
])
ctx_, cty_, ctw_, cth_ = city
# 研究资料(公开对象,v2.17):国别左边(线索「涉及」的线在它上面拐进国别)
study = obj(56, cyy + 66, 196, "研究资料", sections=[
    ("ai", "机器写(世行文件库,一次性导入)", "标题 · 中文标题 · 类型 · 发布机构 · 发布日期 · 语言 · 国别 · 相关项目 · 原文链接 · 要点"),
    ("hu", "人补", "别的来源补录 · 备注"),
])
# 水务事件(公开对象,v2.17):研究资料下面、城市左边;国别、城市页的年度大事记
event = obj(56, study[1] + study[3] + 26, 196, "水务事件", sections=[
    ("ai", "机器写(世行项目库、原文摘录)", "日期 · 事件 · 类别 · 国别 · 城市 · 出处 · 原文链接"),
    ("hu", "人补", "别的事件 · 备注"),
])

# 水务片区(公开对象,v2.18):城市右边;包含几座城市,属于国别;商机可选所属片区
area = obj(640, cty_ + 34, 240, "水务片区", sections=[
    ("hu", "人划定(表格,A35)", "名称 · 英文名 · 国别 · 包含城市 · 服务水司 · 主要水源 · 范围说明 · 划分依据 · 备注"),
    ("dv", "派生(页面现算)", "合计人口 · 片区内商机、线索、大事记、中标、水厂"),
    ("nt", "被谁挂", "商机.所属片区(可选,人选)"),
])

# 判定(菱形)
dx, dy, dr = 743, 300, 58
out.append(f'<polygon points="{dx},{dy - dr} {dx + dr},{dy} {dx},{dy + dr} {dx - dr},{dy}" fill="#FFE0B2" stroke="{C["orange"]}" stroke-width="2" stroke-dasharray="6 4"/>')
T(dx, dy - 16, "«behavior» A03", size=10.5, fill="#666666", anchor="middle", italic=True)
T(dx, dy + 5, "判定", size=17, weight="bold", anchor="middle")
T(dx, dy + 23, "行为,不是表", size=11, fill=C["orange"], anchor="middle", weight="bold")

opp = obj(880, 196, 350, "商机", core=True, sections=[
    ("ai", "机器写 · 公开事实(只填空、列表只追加,每条带出处)", "商机编号 · 名称 · 别名 · 外部编号 · 国别 · 城市 · 地点 · 领域 · 业主与执行机构 · 资金 · 规模 · 货币 · 折合人民币 · 公开进度 · "
                                         "建设内容 · 标段 · 投标资格与依据 · 参与方 · 待核实 · 核实状态 · 建议理由 · 出处线索 · 待核更新"),
    ("hu", "人写 · 我方", "我方阶段:建议 → 跟踪中 → 准备投标 → 已投标\n→ 中标 / 未中标;或 不跟进 / 放弃\n优先级 · 负责人 · 内部备注"),
    ("dv", "派生(不存)", "所属线索数 · 下一时间节点 · 最晚启动 · 最近跟进 · 当前判断 · 下一步 · 下一步到期"),
])
ox, oy, ow, oh = opp
ob = oy + oh
report = obj(1290, 196, 250, "报告", sections=[
    ("ai", "机器写 · 生成后不改", "类型 · 日期 / 期间 · 范围(国别 / 商机) · 版本(公开 / 内部) · 文件 · 引用的线索与商机 · 生成状态 · 模板"),
    ("nt", "四类", "国别报告、项目报告:一次性,\n人在报告表新建一行发起(A13)\n周报每周一 · 月报每月 1 日"),
    ("hu", "人写", "发起人"),
])
rpx, rpy, rpw, rph = report
rpb = rpy + rph
# 存量水厂(公开对象,v2.9)与行业术语(v2.11):城市下面一行;商机、报告两列下面有好几条竖线穿过,不放那里
PLANT_Y = cty_ + cth_ + 40
supply = obj(296, PLANT_Y, 210, "自来水厂", sections=[
    ("ai", "机器写(导入,每月)", "名称 · 类型 · 状态 · 国别 · 城市 · 规模(万吨/日) · 服务人口 · 建设时间 · 工艺 · 运营方 · 坐标"),
    ("hu", "人补", "中文名 · 改造扩建 · 备注"),
])
sewage = obj(526, PLANT_Y, 220, "污水厂", sections=[
    ("ai", "机器写(导入,每月)", "同自来水厂,另有处理级别"),
    ("hu", "人补", "中文名 · 改造扩建 · 备注"),
])
# 行业术语:助手理解提问用,和别的对象没有关联
term = obj(56, PLANT_Y, 220, "行业术语", sections=[
    ("hu", "人写(表格,A27)", "术语 · 同义说法 · 跨哪些领域 · 按领域筛 · 相关词 · 说明 · 停用"),
    ("nt", "谁读", "助手每分钟重读:提问里的行业词照此翻成筛选条件"),
])

# 已中标项目(公开对象,v2.14):水厂下面;属于国别、可挂城市
award = obj(296, max(supply[1] + supply[3], sewage[1] + sewage[3]) + 22, 450, "已中标项目", sections=[
    ("ai", "机器写(开发银行中标记录,一次性导入)", "名称 · 类别 · 合同类型 · 中标价 · 货币 · 折合人民币 · 中标时间 · 工程量 · 中标单位与国别 · "
                                          "所属项目 · 资金来源 · 采购方式 · 国别 · 城市 · 数据来源"),
    ("hu", "人补", "中文名(配了模型时机器译) · 别的来源补录 · 备注"),
])

# 供应商(公开对象,v2.15):已中标项目左边;联合体各家都是中标方
contractor = obj(56, max(award[1], term[1] + term[3] + 22), 176, "供应商", sections=[
    ("ai", "机器写(导入时建行)", "名称 · 国别 · 国别码 · 外部编号"),
    ("hu", "人补(竞争对手分析)", "中文名 · 性质 · 母公司 · 官网 · 主营领域 · 简介 · 备注"),
    ("dv", "派生(不存)", "中标数 · 中标总额"),
])

# 法规、法规条文、排放限值、国别定义(公开对象,v2.24):已中标项目下面一行;商机、报告两列的竖线在右边,不放那里
LAW_Y = max(award[1] + award[3], contractor[1] + contractor[3]) + 34
lawo = obj(296, LAW_Y, 200, "法规", sections=[
    ("ai", "机器写(按法规清单取原文,A37)", "名称 · 原文名称 · 类别 · 文号 · 颁布日期 · 现行状态 · 版本说明 · 语言 · 国别 · 原文链接"),
    ("dv", "派生(不存)", "条文数"),
])
clause = obj(526, LAW_Y, 196, "法规条文", sections=[
    ("ai", "机器写(取原文、识别、按条拆)", "条号 · 顺序 · 所在章节 · 原文 · 页码 · 法规 · 国别\n中文:后台请模型译,只填空"),
])
limit = obj(752, LAW_Y, 196, "排放限值", sections=[
    ("ai", "机器写(读条文摘出,A40)", "指标 · 限值 · 单位 · 口径 · 排到哪里 · 适用范围 · 取样 · 法规 · 出处条文"),
    ("hu", "人核实(A41)", "核实状态:待核实 → 已核实 · 备注"),
])
lterm = obj(56, LAW_Y, 210, "国别定义", sections=[
    ("ai", "机器写(读条文摘出,A39)", "通用概念(行业术语里的词) · 原文用词 · 本国说法 · 本国定义 · 与通用概念的关系 · 差别 · 出处条文"),
    ("hu", "人核实(A41)", "核实状态 · 备注"),
])
path(f"M{clause[0]},{LAW_Y + 60} L{lawo[0] + lawo[2]},{LAW_Y + 60}", C["black"], sw=1.6, end="aK")
label((clause[0] + lawo[0] + lawo[2]) / 2, LAW_Y + 52, "出自", C["black"], size=11.5)
path(f"M{limit[0]},{LAW_Y + 60} L{clause[0] + clause[2]},{LAW_Y + 60}", C["black"], sw=1.6, end="aK")
label((limit[0] + clause[0] + clause[2]) / 2, LAW_Y + 52, "出处", C["black"], size=11.5)
path(f"M{lterm[0] + lterm[2]},{LAW_Y + 60} L{lawo[0]},{LAW_Y + 60}", C["black"], sw=1.6, end="aK")
label((lterm[0] + lterm[2] + lawo[0]) / 2, LAW_Y + 52, "出处", C["black"], size=11.5)
label(526, max(lawo[1] + lawo[3], clause[1] + clause[3], limit[1] + limit[3]) + 16, "四者都属于国别 *..1;国别定义的通用概念 = 行业术语里的一个词(语义层)", C["grey"], size=11.5)
# 语义层说法(v2.25):国别定义下面;助手理解提问的口语,和别的对象没有关联
phrase = obj(56, lterm[1] + lterm[3] + 22, 210, "语义层说法", sections=[
    ("hu", "人写(表格,A42)", "类别 · 指向 · 说法 · 说明"),
    ("nt", "谁读", "助手每分钟重读;表里没有的指向\n后台按本体补行(A43)"),
])
B1_BOT = max(cty_ + cth_, supply[1] + supply[3], sewage[1] + sewage[3], term[1] + term[3], award[1] + award[3], contractor[1] + contractor[3],
             lawo[1] + lawo[3], clause[1] + clause[3], limit[1] + limit[3] + 20, phrase[1] + phrase[3]) + 40
R(40, B1_TOP, 1500, B1_BOT - B1_TOP, C["band"], C["band_b"], sw=1.5, rx=8, extra=' fill-opacity="0.55"')
out.insert(next(i for i, s in enumerate(out) if s.startswith('<circle')), out.pop())  # 底色放到最下层
T(790, B1_TOP + 24, "AI · 公开数据(机器采集、判定、建议;人可增删改)", size=15, fill="#455A64", anchor="middle", weight="bold")

# ---------------- 第二栏 ----------------
B2_TOP = B1_BOT + 16
member = obj(296, B2_TOP + 64, 310, "成员", sections=[
    ("hu", "人写(管理员)", "显示名 · 代号 · 角色(管理员 / 跟进人 / 只读)"),
])
fu = obj(880, B2_TOP + 54, 350, "跟进记录", core=True, sections=[
    ("hu", "人写 · 一次跟进一行", "商机(必填) · 日期 · 记录人 · 内容(做了什么、了解到什么) · 判断(继续跟进 / 重点推进 / 观望 / 放弃)与依据 · "
                              "下一步 · 下一步到期 · 附件"),
    ("ai", "机器写", "阶段变更:人在商机上改阶段,机器自动补这一条(A18)"),
])
fx, fy, fw, fh = fu
cal = obj(1290, fy + 64, 250, "日历", stereo="«view» 查询,不存", view=True, sections=[
    ("dv", "= 三者合并", "跟踪商机的时间节点\n∪ 跟进记录(日期、下一步到期)\n∪ 报告(日期)"),
])
kx, ky, kw, kh = cal
tpl = obj(1290, ky + kh + 26, 250, "报告模板", sections=[
    ("hu", "人写(页面或表格,A21)", "名称 · 适用类型 · 模板文件(.docx) · 缺省 · 说明"),
    ("nt", "只管版式", "页面、字体、页眉页脚;章节结构仍由 A14 按类型固定"),
])
tx, ty, tw_, th = tpl
# 公司资料、个人设置(业务对象,2026-10-08):成员下面
company = obj(296, member[1] + member[3] + 22, 310, "公司资料", sections=[
    ("hu", "人写(页面或表格,A23)", "类别(资质 / 业绩 / 其他) · 名称 · 内容 · 适用领域 · 适用国别 · 年份 · 金额 · 附件 · 写进报告 · 备注"),
    ("nt", "进报告", "勾了「写进报告」的行附在国别 / 项目报告的内部版后"),
])
pref = obj(296, company[1] + company[3] + 22, 310, "个人设置", sections=[
    ("hu", "人写(「我的」页,A24)", "名称 · 账号 · 关注国别 · 忽略国别(采集不再涉及) · 日历订阅(名称 | 链接) · 备注"),
])
# 助手提问记录(业务对象,v2.26):跟进记录下面;每问一句一行,答错的是错题本
asklog = obj(880, fy + fh + 22, 350, "助手提问记录", sections=[
    ("ai", "机器写(A44)", "提问 · 编号 · 接着哪一问 · 理解为 · 解析方式 · 提问人 · 提问时间 · 复查(A46,每天)"),
    ("hu", "人写(答案下面的按钮,A45)", "反馈(答对了 / 答错了) · 哪里不对 · 答出了什么 · 处理状态 · 应理解为"),
])
# 助手复核记录(业务对象,v2.32):助手提问记录下面;要核实的要点、库里可能是同一项目的线索,人点的和助手核的各一行
rev = obj(880, asklog[1] + asklog[3] + 22, 350, "助手复核记录", sections=[
    ("ai", "机器写(按钮经助手接口写,A47 / A48)", "编号 · 材料 · 项目 · 条目类型(要核实 / 库里线索) · 条目 · 线索 · 处理(已复核 / 已忽略 / 是同一项目 / 不是同一项目 / 助手已核) · 结论 · 依据 · 补充事实 · 操作人 · 时间"),
])
B2_BOT = max(fy + fh, ky + kh, pref[1] + pref[3], ty + th, rev[1] + rev[3]) + 30
R(40, B2_TOP, 1500, B2_BOT - B2_TOP, "#FFF6F5", C["band_b"], sw=1.5, rx=8)
out.insert(next(i for i, s in enumerate(out) if "成员" in s) - 3, out.pop())
T(56, B2_TOP + 26, "人 · 业务数据(公司内部的跟进与判断)", size=15, fill="#455A64", weight="bold")

# ---------------- 视图列 ----------------
vx, vw = 1572, 190
R(vx - 10, B1_TOP, vw + 20, B2_BOT - B1_TOP, C["view"], C["view_b"], sw=1.6, rx=8, dash="7 5")
T(vx + vw / 2, B1_TOP + 26, "视图 «view»", size=15, fill=C["view_b"], anchor="middle", weight="bold")
T(vx + vw / 2, B1_TOP + 46, "查询,不存(R10)", size=12, fill="#666666", anchor="middle")
views = [
    ("待处理线索", "相关且待处理 · 按最近截止排序"),
    ("待确认归属", "有建议所属商机或可能是新项目 · 所属商机为空"),
    ("AI 推荐的商机", "我方阶段 = 建议 · 即国别报告的项目机会清单"),
    ("跟踪看板", "跟踪中、准备投标、已投标 · 按阶段分列"),
    ("日历", "跟踪中商机的节点 · 跟进 · 报告(见左下)"),
    ("线索总览漏斗", "采集 → 相关 → 归入商机 → 归入跟踪中的商机 → 写进报告 · 五级严格子集"),
    ("国别概况", "国别信息 · 该国商机 · 线索统计"),
    ("线索处理", "待确认归属 · AI 推荐 · 批量归入 / 忽略 / 跟踪 / 不跟进"),
    ("商机总览", "商机列表(缺省只列跟踪中的)· 下一步已逾期 · 待核更新"),
    ("商机工作台", "一个商机一页 · 记跟进 · 要报告 · 改负责人"),
    ("报告页", "历史报告 · 要报告 · 管理模板"),
    ("线索分析", "日报正文 · 按时间、国别、类别、资金来源现算"),
    ("本体", "对象与关系图 · 分类体系 · 口径 · 规则"),
    ("我方资料页", "公司资料筛选、新增、勾选写进报告"),
    ("我的", "关注 / 忽略的国别 · 日历订阅"),
    ("城市概况", "规模以上城市 · 地图滑块 · 商机、线索、水厂"),
    ("趋势机会", "水风险 × 经济水平 × 贷款项目 · 趋势参考,不计入线索"),
]
vy = B1_TOP + 64
for i, (n, q) in enumerate(views):
    lines = wrap(q, vw - 20, 12)
    hh = 30 + len(lines) * 16 + 8
    R(vx, vy, vw, hh, "#FFFFFF", "#CE93D8", sw=1.2, rx=5)
    T(vx + 10, vy + 21, f"V{i + 1} {n}", size=13.5, weight="bold", fill="#4A148C")
    for j, line in enumerate(lines):
        T(vx + 10, vy + 40 + j * 16, line, size=12, fill="#424242")
    vy += hh + 12
T(vx + vw / 2, vy + 18, "页面与表格视图都从这里取数,", size=12, fill="#666666", anchor="middle")
T(vx + vw / 2, vy + 36, "没有哪张表单独存它们", size=12, fill="#666666", anchor="middle")

# ---------------- 连线 ----------------
def ortho(pts, color, sw=1.6, dash=None, end="", start="", r=10):
    """直角走线(圆角)。"""
    d = f"M{pts[0][0]},{pts[0][1]}"
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
        def toward(ax, ay, bx, by, k):
            L = max(abs(bx - ax), abs(by - ay)) or 1
            return ax + (bx - ax) * min(k, L / 2) / L, ay + (by - ay) * min(k, L / 2) / L
        p1 = toward(x1, y1, x0, y0, r)
        p2 = toward(x1, y1, x2, y2, r)
        d += f" L{p1[0]:.1f},{p1[1]:.1f} Q{x1},{y1} {p2[0]:.1f},{p2[1]:.1f}"
    d += f" L{pts[-1][0]},{pts[-1][1]}"
    path(d, color, sw=sw, dash=dash, end=end, start=start)

# 关键路径:信源 → 线索 → 判定 → 商机 → 报告
path(f"M{cloud_right},{cloud_y} L{lx},{cloud_y}", C["red"], sw=4, end="aR")
path(f"M{lx + lw},{dy} L{dx - dr},{dy}", C["red"], sw=4, end="aR")
path(f"M{dx + dr},{dy} L{ox},{dy}", C["red"], sw=4, end="aR")
label((dx + dr + ox) / 2, dy - 12, "A05 归属", C["red"], weight="bold")
label((dx + dr + ox) / 2, dy + 24, "A06 建议商机", C["red"], weight="bold")
path(f"M{ox + ow},{dy} L{rpx},{dy}", C["red"], sw=4, end="aR")
label((ox + ow + rpx) / 2, dy - 30, "A13 发起", C["red"], weight="bold", size=12)
label((ox + ow + rpx) / 2, dy - 12, "A14 生成", C["red"], weight="bold", size=12)
label((ox + ow + rpx) / 2, dy + 24, "一次性", C["red"], size=12)
# 判定写回线索(反馈)
path(f"M{dx},{dy + dr} C{dx},{dy + dr + 24} {lx + lw + 40},{dy + 66} {lx + lw},{dy + 66}", C["orange"], sw=2, dash="3 5", end="aO")
label(dx - 2, dy + dr + 40, "写回判定属性", C["orange"])
# 同事上传 → 线索
path(f"M244,{up_y + 27} L{lx},{up_y + 27}", C["hu_b"], sw=2.2, dash="7 5", end="aP")
# 线索 —属于→ 商机
yb = min(lb, ob) - 72
path(f"M{lx + lw},{yb} L{ox},{yb}", C["black"], sw=1.6, end="aK")
label(dx, yb - 8, "属于 *..0..1", C["black"], weight="bold")
label(dx, yb + 20, "可靠时机器代填 / 人确认", C["grey"], size=12)
# 线索 → 时间节点:截止日(A03)
path(f"M{lx + 230},{lb} L{lx + 230},{my}", C["blue"], sw=2, end="aB")
label(lx + 238, lb + 31, "截止日(A03)", C["blue"], anchor="start", size=12)
# 线索 → 国别 涉及(走左侧)
ortho([(lx, lb - 24), (276, lb - 24), (276, cyy + 36), (lx, cyy + 36)], C["black"], end="aK")
label(270, (lb + cyy) / 2 + 40, "涉及 *..0..1", C["black"], anchor="end", size=12)
# 商机 ◆— 时间节点(组合)
path(f"M{ox},{ob - 30} C{ox - 120},{ob - 30} {lx + lw + 120},{my + 46} {lx + lw},{my + 46}", C["black"], sw=1.8, start="dK")
label(dx + 8, (ob - 30 + my + 46) / 2 + 22, "包含 1..*(组合)", C["black"], weight="bold", size=12)
# 商机 → 国别 位于
path(f"M{ox},{ob - 10} C{ox - 140},{ob - 10} {lx + lw + 140},{cyy + 46} {lx + lw},{cyy + 46}", C["black"], sw=1.6, end="aK")
label(lx + lw + 8, cyy + 22, "位于 *..1", C["black"], anchor="start", weight="bold", size=12)
# 跟进记录 → 商机:关于 + 推进阶段
path(f"M{ox + 80},{fy} L{ox + 80},{ob}", C["black"], sw=1.6, end="aK")
label(ox + 86, ob + (fy - ob) * 0.30, "关于 *..1", C["black"], anchor="start", weight="bold", size=12)
path(f"M{ox + 190},{ob} L{ox + 190},{fy}", C["blue"], sw=2, end="aB")
label(ox + 196, ob + (fy - ob) * 0.58, "A18 改阶段即自动补一条跟进", C["blue"], anchor="start", weight="bold", size=12)
label(ox + 196, ob + (fy - ob) * 0.58 + 18, "机器 · 谁改的、何时、从什么改成什么", C["blue"], anchor="start", size=12)
# 报告 → 报告模板 套用(沿两框左侧的空隙走)
ortho([(tx, ty + 24), (tx - 28, ty + 24), (tx - 28, rpb - 14), (rpx, rpb - 14)], C["black"], sw=1.6, end="aK")
label(tx - 36, (ty + rpb) / 2, "套用 *..0..1", C["black"], anchor="end", size=12)
# 商机 → 成员 负责人
mbx, mby, mbw, mbh = member
ortho([(ox + 26, ob), (ox + 26, B2_TOP + 34), (mbx + mbw - 60, B2_TOP + 34), (mbx + mbw - 60, mby)], C["black"], end="aK")
label(ox + 18, B1_BOT - 26, "负责人 *..0..1", C["black"], anchor="end", weight="bold", size=12)
# 个人设置 → 成员 属于(沿左侧走)
path(f"M{pref[0] + 40},{pref[1]} L{pref[0] + 40},{company[1] + company[3]}", C["black"], sw=1.2, dash="4 3")
path(f"M{pref[0] + 40},{company[1]} L{pref[0] + 40},{mby + mbh}", C["black"], sw=1.6, end="aK")
label(pref[0] + 48, company[1] - 6, "属于 *..1(账号)", C["black"], size=11.5, anchor="start")
# 跟进记录 → 成员 记录人
path(f"M{fx},{fy + 66} L{mbx + mbw},{fy + 66}", C["black"], sw=1.6, end="aK")
label((fx + mbx + mbw) / 2, fy + 58, "记录人 *..1", C["black"], weight="bold", size=12)
# 城市 → 国别 属于
path(f"M{ctx_ + 60},{cty_} L{ctx_ + 60},{cyy + chh}", C["black"], sw=1.6, end="aK")
label(ctx_ + 68, cty_ - 6, "属于 *..1", C["black"], size=11.5, anchor="start")
# 自来水厂、污水厂 → 城市 位于(可空:不在规模以上城市里的只挂国别)
path(f"M{supply[0] + 60},{supply[1]} L{supply[0] + 60},{cty_ + cth_}", C["black"], sw=1.6, end="aK")
label(supply[0] + 68, supply[1] - 8, "位于 *..0..1", C["black"], size=11.5, anchor="start")
ortho([(sewage[0] + 60, sewage[1]), (sewage[0] + 60, cty_ + cth_ + 18), (ctx_ + ctw_ - 30, cty_ + cth_ + 18), (ctx_ + ctw_ - 30, cty_ + cth_)], C["black"], sw=1.6, end="aK")
# 已中标项目 → 城市:从左边绕上去(术语框与城市之间的空当)
ortho([(award[0], award[1] + 30), (award[0] - 10, award[1] + 30), (award[0] - 10, cty_ + cth_ - 24), (ctx_, cty_ + cth_ - 24)], C["black"], sw=1.6, end="aK")
label(award[0] + 6, award[1] - 8, "位于 *..0..1(属于国别 *..1)", C["black"], size=11.5, anchor="start")
path(f"M{award[0]},{award[1] + 96} L{contractor[0] + contractor[2]},{award[1] + 96}", C["black"], sw=1.6, end="aK")
mid = (contractor[0] + contractor[2] + award[0]) / 2
label(mid, award[1] + 74, "中标方", C["black"], size=11.5)
label(mid, award[1] + 88, "*..*", C["black"], size=11.5)
# 研究资料 → 国别 属于
path(f"M{study[0] + study[2]},{study[1] + 70} L{cxx},{study[1] + 70}", C["black"], sw=1.6, end="aK")
label((study[0] + study[2] + cxx) / 2, study[1] + 88, "属于", C["black"], size=11.5)
label((study[0] + study[2] + cxx) / 2, study[1] + 102, "*..1", C["black"], size=11.5)
# 水务片区 → 城市 包含;→ 国别 属于(从上面拐进国别右边)
path(f"M{area[0]},{area[1] + 60} L{ctx_ + ctw_},{area[1] + 60}", C["black"], sw=1.6, end="aK")
label((area[0] + ctx_ + ctw_) / 2, area[1] + 52, "包含", C["black"], size=11.5)
label((area[0] + ctx_ + ctw_) / 2, area[1] + 78, "*..*", C["black"], size=11.5)
ortho([(area[0] + 60, area[1]), (area[0] + 60, cyy + chh - 30), (cxx + cww, cyy + chh - 30)], C["black"], sw=1.6, end="aK")
label(area[0] + 68, area[1] - 8, "属于 *..1", C["black"], size=11.5, anchor="start")
# 水务事件 → 城市 发生在(可空;都属于国别)
path(f"M{event[0] + event[2]},{event[1] + 50} L{ctx_},{event[1] + 50}", C["black"], sw=1.6, end="aK")
label((event[0] + event[2] + ctx_) / 2, event[1] + 68, "发生在", C["black"], size=11.5)
label((event[0] + event[2] + ctx_) / 2, event[1] + 82, "*..0..1", C["black"], size=11.5)
label(event[0], event[1] + event[3] + 16, "属于国别 *..1", C["black"], size=11.5, anchor="start")
# 国别 → 报告(沿底部走)
gy_ = cyy + 120
ortho([(cxx + cww, gy_), (rpx + 40, gy_), (rpx + 40, rpb)], C["blue"], sw=2, end="aB")
label(ox + ow - 60, gy_ - 8, "国别信息写进报告", C["blue"], size=12)
# 跟进记录 → 报告:周期报告
ortho([(fx + fw, fy + 30), (rpx + 120, fy + 30), (rpx + 120, rpb)], C["blue"], sw=2, end="aB")
label(rpx + 128, rpb + 120, "A14 周报 / 月报", C["blue"], anchor="start", weight="bold", size=12)
label(rpx + 128, rpb + 138, "机器 · 定期", C["blue"], anchor="start", size=12)
# 派生 → 日历
path(f"M{rpx + rpw - 18},{rpb} L{rpx + rpw - 18},{ky}", C["grey"], sw=1.8, dash="2 5", end="aY")
path(f"M{fx + fw},{ky + kh / 2} L{kx},{ky + kh / 2}", C["grey"], sw=1.8, dash="2 5", end="aY")
label(rpx + rpw - 26, ky - 12, "派生", C["grey"], anchor="end", size=12)

# ---------------- 状态机 ----------------
sy = B2_BOT + 52
T(40, sy, "状态 «state»", size=15, weight="bold", fill="#455A64")
T(150, sy, "线索·处理状态(人)", size=13.5, fill=C["hu_b"], weight="bold")
state(60, sy + 18, 110, 36, "待处理", C["hu"], C["hu_b"], True)
state(240, sy + 2, 110, 32, "已处理", C["hu"], C["hu_b"])
state(240, sy + 44, 110, 32, "已忽略", C["hu"], C["hu_b"])
path(f"M170,{sy + 30} L240,{sy + 18}", C["hu_b"], sw=1.6, end="aP")
path(f"M170,{sy + 44} L240,{sy + 60}", C["hu_b"], sw=1.6, end="aP")
T(400, sy + 28, "可撤回到待处理;", size=12.5, fill="#555555")
T(400, sy + 46, "线索上没有进展、", size=12.5, fill="#555555")
T(400, sy + 64, "负责人、下一步(R7)", size=12.5, fill="#555555")

ax0 = 560
T(ax0, sy, "商机·我方阶段(机器只能新建为「建议」;其余转换都由人在商机上改,机器自动补一条跟进 R5 R6)", size=13.5, fill=C["hu_b"], weight="bold")
st = [("建议", C["ai"], C["ai_b"]), ("跟踪中", C["hu"], C["hu_b"]), ("准备投标", C["hu"], C["hu_b"]), ("已投标", C["hu"], C["hu_b"])]
bx = ax0
coords = {}
for n, f, s in st:
    state(bx, sy + 18, 104, 36, n, f, s, True)
    coords[n] = (bx, sy + 18)
    bx += 150
for a, b in (("建议", "跟踪中"), ("跟踪中", "准备投标"), ("准备投标", "已投标")):
    path(f"M{coords[a][0] + 104},{sy + 36} L{coords[b][0]},{sy + 36}", C["hu_b"], sw=1.6, end="aP")
label(coords["建议"][0] + 127, sy + 28, "立项", C["hu_b"], size=11.5)
state(bx, sy + 0, 96, 30, "中标", "#C8E6C9", "#2E7D32", True)
state(bx, sy + 40, 96, 30, "未中标", C["hu"], C["hu_b"])
path(f"M{coords['已投标'][0] + 104},{sy + 30} L{bx},{sy + 15}", C["hu_b"], sw=1.6, end="aP")
path(f"M{coords['已投标'][0] + 104},{sy + 42} L{bx},{sy + 55}", C["hu_b"], sw=1.6, end="aP")
state(coords["建议"][0], sy + 76, 104, 30, "不跟进", C["dv"], C["dv_b"])
path(f"M{coords['建议'][0] + 52},{sy + 54} L{coords['建议'][0] + 52},{sy + 76}", C["hu_b"], sw=1.6, end="aP")
state(coords["准备投标"][0], sy + 76, 104, 30, "放弃", C["dv"], C["dv_b"])
for n in ("跟踪中", "准备投标", "已投标"):
    path(f"M{coords[n][0] + 52},{sy + 54} L{coords['准备投标'][0] + 52},{sy + 76}", C["hu_b"], sw=1.2, end="aP")
T(bx + 120, sy + 22, "建议 = AI 推荐的商机", size=12.5, fill="#555555")
T(bx + 120, sy + 40, "跟踪中、准备投标、已投标", size=12.5, fill="#555555")
T(bx + 120, sy + 58, "= 正在跟踪的商机", size=12.5, fill="#555555")
T(bx + 120, sy + 76, "两者同一张表,只差这一列", size=12.5, fill=C["red"], weight="bold")

# ---------------- 规则 ----------------
ry = sy + 172
T(40, ry - 34, "规则 «principle»(不变式,详见 本体.md R1–R11)", size=15, weight="bold", fill=C["rule_b"])
rules = [
    ("R1 一个对象只存一处", "全在 Teable;页面只读,不存副本"),
    ("R2 人写的不被机器覆盖", "只填空、只新建、列表只追加;改值写待核更新"),
    ("R3 公开事实带出处", "每条引用一条线索,推断写明依据"),
    ("R4 线索至多属一个商机", "可靠才自动归属,其余人确认"),
    ("R5 阶段只由人推进", "机器只能新建「建议」商机"),
    ("R6 改阶段留一条跟进", "机器自动补记,人补原因"),
    ("R9 报告是快照", "生成后不改;公开版不含内部信息"),
    ("R10 派生不存", "日历、漏斗、下一步都现算"),
]
ow8, gap = 204, 9
for i, (t1, t2) in enumerate(rules):
    octagon(40 + ow8 / 2 + i * (ow8 + gap), ry + 30, ow8, 72, t1, t2)

# ---------------- 图例 ----------------
gy = ry + 92
out.append(f'<line x1="40" y1="{gy - 12}" x2="{W - 40}" y2="{gy - 12}" stroke="#E0E0E0" stroke-width="1.5"/>')
T(40, gy + 10, "图例 «legend»", size=13.5, weight="bold")
items = [("ai", "机器写(只填空、列表只追加;人可改)"), ("hu", "人写(机器不写)"), ("dv", "派生(查询,不存)")]
x = 150
for k, s in items:
    f, b = SEC[k]
    R(x, gy - 4, 26, 18, f, b, sw=1.2, rx=3)
    T(x + 34, gy + 10, s, size=12.5)
    x += 34 + tw(s, 12.5) + 26
R(x, gy - 6, 30, 22, C["head"], "#37474F", sw=1.6, rx=4)
T(x + 38, gy + 10, "矩形 = 对象 «object»", size=12.5)
x += 38 + tw("矩形 = 对象 «object»", 12.5) + 24
out.append(f'<polygon points="{x + 14},{gy - 8} {x + 28},{gy + 5} {x + 14},{gy + 18} {x},{gy + 5}" fill="#FFE0B2" stroke="{C["orange"]}" stroke-width="1.4"/>')
T(x + 36, gy + 10, "菱形 = 判定(行为)", size=12.5)
x += 36 + tw("菱形 = 判定(行为)", 12.5) + 24
R(x, gy - 6, 30, 22, C["view"], C["view_b"], sw=1.4, rx=4, dash="5 3")
T(x + 38, gy + 10, "虚线框 = 视图", size=12.5)
x += 38 + tw("虚线框 = 视图", 12.5) + 24
out.append(f'<polygon points="{x + 6},{gy - 6} {x + 24},{gy - 6} {x + 30},{gy} {x + 30},{gy + 10} {x + 24},{gy + 16} {x + 6},{gy + 16} {x},{gy + 10} {x},{gy}" '
           f'fill="{C["rule"]}" stroke="{C["rule_b"]}" stroke-width="1.4"/>')
T(x + 38, gy + 10, "八边形 = 规则", size=12.5)
gy2 = gy + 34
lines = [
    (C["red"], 4, None, "aR", None, "关键路径"),
    (C["blue"], 2, None, "aB", None, "信息流(读进报告等)"),
    (C["green"], 2.2, "8 5", "aG", None, "人的动作改状态"),
    (C["hu_b"], 2.2, "7 5", "aP", None, "人工录入 / 状态转换"),
    (C["orange"], 2, "3 5", "aO", None, "写回(反馈)"),
    (C["black"], 1.6, None, "aK", None, "关联(带基数)"),
    (C["black"], 1.8, None, None, "dK", "组合(实心菱形)"),
    (C["grey"], 1.8, "2 5", "aY", None, "派生依赖"),
]
x = 40
for col, swd, dash, end, start, s in lines:
    path(f"M{x + (14 if start else 0)},{gy2} L{x + 52},{gy2}", col, sw=swd, dash=dash, end=end or "", start=start or "")
    T(x + 60, gy2 + 5, s, size=12.5)
    x += 60 + tw(s, 12.5) + 30


Hf = int(gy2 + 52)
T(W - 40, Hf - 14, "SAM/SCBRD 系统架构可视化建模 · OBR 本体  |  参考 github.com/sharptoolbox/visual-model(CC BY 4.0)", size=11.5,
  fill="#999999", anchor="end")
out[0] = out[0].replace(f'height="{H}" viewBox="0 0 {W} {H}"', f'height="{Hf}" viewBox="0 0 {W} {Hf}"')
for k, s in enumerate(out):
    if s.startswith('<rect x="0.0" y="0.0"'):
        out[k] = s.replace(f'height="{H:.1f}"', f'height="{Hf:.1f}"')
        break

out.append("</svg>")
open(sys.argv[1], "w", encoding="utf-8").write("\n".join(out))
print("ok", len(out))
