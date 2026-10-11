"""台账:部分日期、校验、投标资格、下一节点与最晚启动日期、报告渲染。全部离线,夹具为虚构数据。"""
from datetime import date
from pathlib import Path

import pytest
import yaml

from core.ledger import (
    RULES_PATH, LedgerError, cited, derive, eligibility, load_ledger, load_overlay, load_rules, parse_pdate, timing,
)
from core.profile import load_profile
from core.report import _alloc, money, render_country

ROOT = Path(__file__).resolve().parent.parent
FX = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 9, 24)


@pytest.fixture(scope="module")
def ctx():
    rules = load_rules(FX / "rules.yaml")
    prof = load_profile(FX / "profile.yaml")
    c = prof.country("ZZZ")
    led = load_ledger(c.ledger, rules)
    return rules, prof, c, led, {d.item["id"]: d for d in derive(led, rules, prof.bidder, prof.lead_times, TODAY)}


def test_parse_pdate():
    assert parse_pdate(date(2026, 1, 2)).precision == "day"
    q = parse_pdate("2026-Q4")
    assert (q.start, q.end, q.precision) == (date(2026, 10, 1), date(2026, 12, 31), "quarter")
    h = parse_pdate("2027-H1")
    assert (h.start, h.end) == (date(2027, 1, 1), date(2027, 6, 30))
    assert parse_pdate("2026-02").end == date(2026, 2, 28)
    assert parse_pdate(2026).end == date(2026, 12, 31)
    with pytest.raises(ValueError):
        parse_pdate("2026年底")


def test_profile_paths_and_nationality(ctx):
    _, prof, c, led, _ = ctx
    assert prof.bidder["nationality"] == "CHN"  # 大写归一
    assert c.ledger.exists() and c.overlay.exists()
    assert led.country == "ZZZ" and len(led.items) == 4 and set(led.sources) == {"S01", "S02"}


def test_validation_catches_errors(tmp_path):
    bad = {
        "schema": "osint-water/ledger@1", "country": "ZZZ", "as_of": "2026-09-01", "fx": {"USD": 1},
        "opportunities": [
            {"id": "ZZZ-001", "name_zh": "甲", "segments": ["water_supply", "magic"], "stage": "dreaming",
             "financing": [{"funder": "X", "amount": 1, "currency": "GBP", "status": "signed", "src": ["S99"]}],
             "milestones": [{"type": "bid_deadline", "date": "明年", "status": "planned"}],
             "procurement": {"regime": "NOPE"}, "verification": {"status": "verified"}},
            {"id": "ZZZ-001", "name_zh": "重复", "segments": ["wastewater"], "stage": "plan", "verification": {"status": "partial"}},
        ],
        "sources": {},
    }
    p = tmp_path / "ledger.yaml"
    p.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    with pytest.raises(LedgerError) as e:
        load_ledger(p, load_rules(FX / "rules.yaml"))
    msg = str(e.value)
    for frag in ("stage='dreaming'", "segment='magic'", "GBP", "S99", "日期格式", "NOPE", "id 重复"):
        assert frag in msg, frag


def test_eligibility(ctx):
    rules, prof, _, led, ds = ctx
    assert ds["ZZZ-001"].elig.status == "eligible"  # EBRD:各国均可
    assert ds["ZZZ-002"].elig.status == "restricted"  # IsDB:CHN 非成员
    assert ds["ZZZ-003"].elig.status == "conditional"  # 本国采购法:有条件
    assert ds["ZZZ-004"].elig.status == "eligible"  # 项目公告明示条款优先于通用规则
    it = led.items[1]
    assert eligibility(it, rules, "TUR").status == "eligible"  # 成员国
    assert eligibility(it, rules, "BRA").status == "unknown"  # 成员身份未登记
    reg = {"procurement": {"regime": "ZZ_REG"}}
    assert eligibility(reg, rules, "CHN").status == "conditional"  # 投标前须当地注册
    assert eligibility(reg, rules, "CHN", has_local_entity=True).status == "eligible"
    unv = eligibility({"procurement": {"regime": "ZZ_UNV"}}, rules, "CHN")  # 规则本身未核实 → 推定
    assert unv.status == "presumed" and "规则未核实" in unv.basis


def test_next_node_and_latest_start(ctx):
    _, prof, _, led, ds = ctx
    t1 = ds["ZZZ-001"].timing  # 已发生的 EOI 被忽略;资审 11-15 早于标段截止 2026-12
    assert t1.node.type == "pq_deadline" and t1.node.when.start == date(2026, 11, 15)
    assert t1.latest_start == date(2026, 10, 16) and t1.lead_days == 30 and t1.days_left == 22
    t2 = ds["ZZZ-002"].timing  # 推断的 2026-Q4 取区间起点
    assert t2.node.type == "tender_launch" and t2.latest_start == date(2026, 9, 1) and t2.days_left < 0
    t3 = ds["ZZZ-003"].timing  # 本国采购法要求投标前注册 → 加上本地注册周期
    assert t3.lead_days == 40 + 15 + 120 and "本地注册 120 天" in t3.lead_basis
    assert ds["ZZZ-004"].timing.node is None and ds["ZZZ-004"].timing.latest_start is None
    assert ds["ZZZ-002"].stale == ["贷款/协议签署(2026-06,官方计划)已过期未更新"]
    # 不传内部周期时最晚启动 = 节点日期
    assert timing(led.items[0], {}, TODAY).latest_start == date(2026, 11, 15)


def test_report_internal_vs_public(ctx):
    rules, prof, c, led, ds = ctx
    overlay = load_overlay(c.overlay)
    internal = render_country(prof, c, led, rules, list(ds.values()), overlay, TODAY)
    for frag in ("、项目机会台账(总表)", "、时间窗口", "、项目卡片", "### ZZZ-001 甲市污水处理厂", "内部叠加(不公开)",
                 "虚构的非公开信息", "最晚启动", "受限", "## 附录 A 来源登记", "<https://example.org/project/1>",
                 "虚构投资论坛", "建议动作清单(内部)", "虚构的国别级动作", "虚构的国别级非公开信息"):
        assert frag in internal, frag
    assert internal.index("### ZZZ-001") < internal.index("### ZZZ-003") < internal.index("### ZZZ-002")  # 核心在前,参考项在后
    public = render_country(prof, c, led, rules, list(ds.values()), overlay, TODAY, public=True)
    assert "虚构投资论坛" in public  # 活动是公开事实
    for frag in ("内部叠加", "虚构的非公开信息", "最晚启动", "投标资格(投标人国籍", "内部流程周期", "建议动作", "国别级非公开"):
        assert frag not in public, frag


def test_report_orders_table_priorities_and_shows_owner(ctx):
    """内部优先级既有表格里定的 A/B/C(项目表人管列拉回 overlay),也有手写的数字:A/B/C 在前、数字随后,混在一起照样排;
    表格里填的负责人、内部备注进内部版的项目卡片。"""
    rules, prof, c, led, ds = ctx
    overlay = {**load_overlay(c.overlay), "items": {"ZZZ-001": {"priority": 1}, "ZZZ-003": {"priority": "A", "owner": "部门甲-01",
                                                                                          "note": "集团重点"}}}
    md = render_country(prof, c, led, rules, list(ds.values()), overlay, TODAY)
    assert md.index("### ZZZ-003") < md.index("### ZZZ-001")
    card = md[md.index("### ZZZ-003"):md.index("### ZZZ-001")]
    assert "我方负责人" in card and "部门甲-01" in card and "内部备注" in card and "集团重点" in card
    assert "部门甲-01" not in render_country(prof, c, led, rules, list(ds.values()), overlay, TODAY, public=True)


def test_appendix_lists_only_cited_sources(ctx):
    rules, prof, c, _, ds = ctx
    led = load_ledger(c.ledger, rules)  # 另载一份,别改到模块级夹具
    led.sources["S03"] = {"title": "虚构未引用来源", "publisher": "虚构", "grade": "C", "via": "report_baseline"}
    assert cited(led) == ["S01", "S02"]
    md = render_country(prof, c, led, rules, list(ds.values()), {}, TODAY)
    assert "只列本报告引用的 2 条;完整登记 3 条" in md
    assert "虚构未引用来源" not in md


def test_background_section(ctx):
    rules, prof, c, led, ds = ctx
    row = lambda src, ind, label, unit, year, value, fc=False: {  # noqa: E731
        "source": src, "indicator": ind, "indicator_label": label, "unit": unit, "year": year, "value": value, "is_forecast": fc}
    ind = {"as_of": "2026-09-24", "rows": [
        row("WB-WDI", "NY.GDP.MKTP.KD.ZG", "GDP 实际增速", "%", 2024, 4.1),
        row("WB-WDI", "NY.GDP.MKTP.KD.ZG", "GDP 实际增速", "%", 2025, 1.4),
        row("IMF-WEO", "NGDP_RPCH", "GDP 实际增速(IMF,含预测)", "%", 2025, 1.5),
        row("IMF-WEO", "NGDP_RPCH", "GDP 实际增速(IMF,含预测)", "%", 2026, 2.2, True),
        row("IMF-WEO", "NGDP_RPCH", "GDP 实际增速(IMF,含预测)", "%", 2027, 2.5, True),
        row("WB-WDI", "SH.H2O.BASW.ZS", "基本饮水覆盖", "%", 2024, 97.7),
        row("WB-WDI", "NY.GDP.MKTP.CD", "GDP(现价美元)", "USD", 2025, 75_937_647_058.8),
    ]}
    md = render_country(prof, c, led, rules, list(ds.values()), {}, TODAY, indicators=ind)
    assert "、国别背景(宏观与供排水指标)" in md
    assert "1.4%(2025)" in md and "2026 2.2%、2027 2.5%" in md  # WDI 实际值 + IMF 后续预测
    assert "97.7%(2024)" in md and "759.38 亿美元(2025)" in md
    assert "国别背景" not in render_country(prof, c, led, rules, list(ds.values()), {}, TODAY)  # 未 ingest 时不出这一节


def test_profile_data_dir(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "profile.yaml").write_text(
        "name: t\ncountries:\n  - {iso2: ZZ, iso3: ZZZ, name_en: T, tier: focus}\ndata_dir: ../d\n", encoding="utf-8")
    assert load_profile(tmp_path / "p" / "profile.yaml").data_dir == (tmp_path / "d").resolve()
    assert load_profile(FX / "profile.yaml").data_dir is None


def test_column_allocation_respects_floors():
    need, floor = [7, 40, 20, 12, 8, 30, 30, 12, 20, 8], [1.7, 1.2, 1.2, 1.9, 1.9, 1.2, 1.2, 1.9, 1.9, 1.9]
    cm = _alloc(need, floor, 26.1)
    assert abs(sum(cm) - 26.1) < 1e-9
    assert all(c >= f - 1e-9 for c, f in zip(cm, floor))  # 编号列等短列不被压到下限以下
    assert cm[1] > cm[0]  # 长文本列仍按内容多分
    tight = _alloc([10, 10], [20, 20], 30)  # 下限放不下时整体缩放
    assert abs(sum(tight) - 30) < 1e-9


def test_money_format():
    assert money(91_000_000, "EUR") == "9,100 万欧元"
    assert money(436_670_000, "USD") == "4.37 亿美元"
    assert money(None, "USD") == "未公开"


def test_repo_rules_and_ledgers_valid():
    """仓库内真实的规则表与各国台账必须通过校验(不存在则跳过)。"""
    if not RULES_PATH.exists():
        pytest.skip("reference/procurement_rules.yaml 不存在")
    rules = load_rules()
    ledgers = sorted([*(ROOT / "countries").glob("*/ledger.yaml"), *(ROOT / "private" / "countries").glob("*/ledger.yaml")])  # 真实台账在私有配置仓
    if not ledgers:
        pytest.skip("countries/*/ledger.yaml 不存在")
    for p in ledgers:
        led = load_ledger(p, rules)
        assert led.country == p.parent.name
