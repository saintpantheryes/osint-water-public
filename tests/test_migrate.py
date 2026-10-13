"""阶段 C 迁移(scripts/migrate_to_teable.py):从测试国的台账、内部叠加与一份造出来的 data 分支拼出新库的行,核对口径;
再写进内存版的 Teable(先按本体建表):关联、成员、附件都写上;再跑一次不重复建;--sync 才覆盖已有的值;派生字段不写(R10)。不联网。"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from core import ontology, schema, tstore
from core.profile import load_profile
from core.watch.events import add_follow, set_stage
from core.watch.query import set_status
from core.watch.store import Store
from tests.teable_fake import FakeTeable

FX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 3)
_spec = importlib.util.spec_from_file_location("migrate_to_teable", Path(__file__).resolve().parent.parent / "scripts" / "migrate_to_teable.py")
mig = importlib.util.module_from_spec(_spec)
sys.modules["migrate_to_teable"] = mig
_spec.loader.exec_module(mig)


def _sig(sid: str, title: str, url: str, llm: dict | None, links=(), first="2026-09-25") -> dict:
    return {"id": sid, "title": title, "url": url, "published": first, "first_seen": f"{first}T08:00:00+00:00", "source": "zz_notices",
            "source_name": "虚构采购网", "kind": "html_list", "country": "ZZZ", "excerpt": f"{title} 正文", "fields": {},
            "rules": {"candidate": bool(llm)}, "links": [{"item": i, "confidence": c, "by": "rule"} for i, c in links], "llm": llm}


def _llm(relevant=True, new=False, deadlines=()) -> dict:
    return {"relevant": relevant, "category": "eoi", "sectors": ["wastewater"], "summary_zh": "虚构摘要", "funder": "虚构银行",
            "project": "甲市污水", "amount": None, "deadlines": [{"type": "eoi_deadline", "date": d, "evidence": "…"} for d in deadlines],
            "ledger_links": [], "new_opportunity": new}


@pytest.fixture
def env(tmp_path):
    """测试国:台账四个项目(ZZZ-001 引用 S01、S02;ZZZ-002 不在业务范围;ZZZ-003 引用 S02;ZZZ-004 没有出处);
    data 分支:四条线索(一条的网址就是 S02)、处理状态、操作记录(立项、跟进、不列入)、一份国别报告与一份周报;旧库链接表。"""
    prof_dir = tmp_path / "prof"
    shutil.copytree(FX, prof_dir)
    data = tmp_path / "data"
    (prof_dir / "profile.yaml").write_text((FX / "profile.yaml").read_text(encoding="utf-8") + f"data_dir: {data}\n", encoding="utf-8")
    prof = load_profile(prof_dir / "profile.yaml")
    led = prof_dir / "ZZZ" / "ledger.yaml"  # 台账再加一条市场准入说明,出处 S03 只被它引用(不被任何项目引用)
    led.write_text(led.read_text(encoding="utf-8") + "access:\n  - {text: 虚构的市场准入说明, src: [S03]}\n", encoding="utf-8")
    src = prof_dir / "ZZZ" / "sources.yaml"
    src.write_text(src.read_text(encoding="utf-8") + '  S03: {title: 虚构准入指南, publisher: 虚构部委, date: 2026-01-01, grade: B, '
                   'via: primary_read, url: "https://example.org/access/3"}\n', encoding="utf-8")
    mar = _sig("e5e5e5e5e5e5e5e5", "Appel d'offres eau", "https://example.org/t/e", _llm())
    mar["country"] = "MAR"  # 真实国别:国别资料(机构、法规)从 reference 里来
    st = Store(data)
    st.append_signals([
        _sig("a1a1a1a1a1a1a1a1", "EOI: Gamma WWTP", "https://example.org/news/2", _llm(deadlines=("2026-10-20",)), links=(("ZZZ-001", "high"),)),
        _sig("b2b2b2b2b2b2b2b2", "Road\ntender", "https://example.org/t/b", _llm(relevant=False)),  # 标题带换行(旧数据里有)
        _sig("c3c3c3c3c3c3c3c3", "Notice", "https://example.org/t/c", None),
        _sig("d4d4d4d4d4d4d4d4", "New desal plant", "https://example.org/t/d", _llm(new=True)),
        mar,
    ])
    set_status(st, ["a1a1a1a1a1a1a1a1"], "done", "看过了", by="部门甲-05")
    set_stage(st, "ZZZ-001", "tracking", "立项跟踪", by="部门甲-05", at="2026-09-26T09:00:00+08:00")
    add_follow(st, "ZZZ-001", "约见业主", "focus", reason="资金已落实", day="2026-09-28", by="teable:张三", step="准备资审", due="2026-10-10")
    st.log_event("ZZZ-003", "listing", "no", "不在我方业务范围", by="部门甲-05")
    add_follow(st, "b2b2b2b2b2b2b2b2", "电话问过业主", "focus", day="2026-09-29", by="teable:张三", step="等回复", due="2026-10-05")  # 没有商机的线索上的跟进
    rep = data / "reports" / "monthly"
    rep.mkdir(parents=True)
    (rep / "国别报告_测试国_2026-10-01.md").write_text("## 4.1 项目机会清单\n| ZZZ-001 | 甲市 | [a1a1a1a1] |\n", encoding="utf-8")
    (rep / "国别报告_测试国_2026-10-01.docx").write_bytes(b"docx")
    wk = data / "reports" / "weekly"
    wk.mkdir(parents=True)
    (wk / "ZZZ_周报_2026-W40.md").write_text("| [b2b2b2b2] |\n", encoding="utf-8")
    (st.root / "teable_links.json").write_text(json.dumps({"base": "bseOLD", "leads": {"a1a1a1a1a1a1a1a1": "rec1", "b2b2b2b2b2b2b2b2": "rec2"},
                                                           "projects": {"ZZZ-001": "rec9"}, "tables": {}}), encoding="utf-8")
    return prof, data


def _build(env):
    prof, data = env
    b = mig.Builder(prof, data, TODAY)
    b.build()
    return b


def test_plan_follows_the_ontology(env):
    b = _build(env)
    leads, opps = b.plan.rows["lead"], b.plan.rows["opportunity"]
    s01 = mig.signal_id(mig.LEDGER, "https://example.org/project/1")[:8]
    s03 = mig.signal_id(mig.LEDGER, "https://example.org/access/3")[:8]
    assert set(leads) == {"a1a1a1a1", "b2b2b2b2", "c3c3c3c3", "d4d4d4d4", "e5e5e5e5", s01, s03}  # S02 的网址已是线索 a1a1a1a1;S01、S03 补成线索
    a, c, d, s = leads["a1a1a1a1"], leads["c3c3c3c3"], leads["d4d4d4d4"], leads[s01]
    assert (a.fields["判定方式"], a.fields["处理状态"], a.fields["处理备注"], a.links["所属商机"]) == ("模型", "已处理", "看过了", ["ZZZ-001"])
    assert a.fields["类别"] == "意向书" and a.fields["领域"] == ["污水"] and a.fields["是否相关"] is True
    assert (c.fields["判定方式"], c.fields["是否相关"]) == ("规则", None)
    assert d.fields["可能是新项目"] is True and mig._pending(d)  # 待处理的可能新项目进待确认
    assert (s.fields["判定方式"], s.fields["处理状态"], s.links["信源"], s.links["所属商机"]) == ("不判定", "已处理", ["ledger"], ["ZZZ-001"])
    assert s.fields["发现时间"].startswith("2026-09-20")  # 取商机的核实日期
    assert {k: r.fields["我方阶段"] for k, r in opps.items()} == {"ZZZ-001": "跟踪中", "ZZZ-002": "不跟进", "ZZZ-003": "不跟进", "ZZZ-004": "建议"}
    z1 = opps["ZZZ-001"]
    assert set(z1.links["出处线索"]) == {s01, "a1a1a1a1"} and z1.fields["优先级"] == "A"
    assert f"[{s01}]" in z1.fields["资金"] and f"[{s01}]" in z1.fields["标段"]  # 事实句末标出处线索号
    assert "业务线:EPC" in z1.fields["内部备注"] and "建议动作:准备资审文件(到期 2026-10-15)" in z1.fields["内部备注"]
    assert "不在我方业务范围" in opps["ZZZ-003"].fields["内部备注"]
    cty = b.plan.rows["country"]["ZZZ"].fields
    assert cty["市场分级"] == "市场" and "[情报] 虚构的国别级非公开信息" in cty["国别备注"] and "[建议动作] 虚构的国别级动作" in cty["国别备注"]
    fol = list(b.plan.rows["followup"].values())
    by_opp = {}
    for r in fol:
        by_opp.setdefault(r.links["商机"][0], []).append(r)
    z1f = {r.fields["内容"]: r for r in by_opp["ZZZ-001"]}
    assert z1f["立项跟踪"].fields["阶段变更"] == "跟踪中" and z1f["立项跟踪"].people == {"记录人": "部门甲-05"}
    assert (z1f["约见业主"].fields["判断"], z1f["约见业主"].fields["下一步到期"], z1f["约见业主"].people) == ("重点推进", "2026-10-10", {"记录人": "张三"})
    assert [r.fields["阶段变更"] for r in by_opp["ZZZ-002"]] == ["不跟进"] and "不在业务范围" in by_opp["ZZZ-002"][0].fields["内容"]
    assert [(r.fields["阶段变更"], r.fields["内容"]) for r in by_opp["ZZZ-003"]] == [("不跟进", "不在我方业务范围")]
    ms = b.plan.rows["milestone"]
    assert any(k.startswith("opp:ZZZ-001|资格预审截止|") for k in ms) and any(k.startswith("lead:a1a1a1a1|意向书截止|2026-10-20") for k in ms)
    assert all(r.fields["日期"] and r.fields["日期原文"] for r in ms.values())
    rep = b.plan.rows["report"]
    country = rep["国别报告_测试国_2026-10-01.docx"]
    assert country.links["引用的线索"] == ["a1a1a1a1", s01] and country.links["写进的商机"] == ["ZZZ-001"]  # 10.22:经所属商机的也算
    assert rep["ZZZ_周报_2026-W40.md"].fields["期间"] == "2026-W40" and rep["ZZZ_周报_2026-W40.md"].links["引用的线索"] == ["b2b2b2b2"]
    md = mig.report_md(b, "水务项目机会 新版")
    assert "旧库线索表:2 行" in md and "数据分支有、旧库没有:3 条" in md and "**否**,没有的:ZZZ-004" in md


def _schema(fake: FakeTeable, prof) -> list:
    api = tstore.Client("https://x", "admin-token", transport=fake.transport, sleep=lambda s: None)
    plans = schema.build_plan(ontology.load(), ontology.load_map(), prof.lead_times)
    schema.apply(api, plans, "水务项目机会 新版", log=lambda *a: None)
    return plans


def _write(fake, plans, plan, mode="fill", alias=None):
    api = tstore.Client("https://x", "admin-token", transport=fake.transport, sleep=lambda s: None)
    w = mig.Writer(api, "水务项目机会 新版", plans, mode, log=lambda *a: None, team={"Ada": "部门甲-05"}, alias=alias)
    return w, w.run(plan)


def test_written_into_the_new_base_once(env):
    prof, _ = env
    fake = FakeTeable()
    plans = _schema(fake, prof)
    b = _build(env)
    w, stats = _write(fake, plans, b.plan)
    rows = {t: fake.rows(t) for t in ("线索", "商机", "跟进记录", "时间节点", "国别", "信源", "报告")}
    assert len(rows["线索"]) == 7 and len(rows["商机"]) == 4 and stats["线索·新建"] == 7
    a = next(r for r in rows["线索"] if r["线索号"] == "a1a1a1a1")
    assert a["所属商机"]["title"] == "甲市污水处理厂" and a["信源"]["title"] == "虚构采购网" and a["国别"]["title"] == "测试国"
    assert a["发布日期"] == "2026-09-24T16:00:00.000Z"  # 按上海当天零点存
    z1 = next(r for r in rows["商机"] if r["商机编号"] == "ZZZ-001")
    assert {x["title"] for x in z1["出处线索"]} == {"EOI: Gamma WWTP", "虚构出资方项目页"} and z1["国别"]["title"] == "测试国"
    f = {r["内容"]: r for r in rows["跟进记录"]}
    assert f["立项跟踪"]["记录人"]["title"] == "Ada" and f["约见业主"]["记录人"]["title"] == "张三" and f["立项跟踪"]["商机"]["title"] == "甲市污水处理厂"
    rep = next(r for r in rows["报告"] if r.get("类型") == "国别报告")
    assert [x["name"] for x in rep["文件"]] == ["国别报告_测试国_2026-10-01.docx"] and len(rep["引用的线索"]) == 2
    assert not w.warnings, w.warnings
    n = len(fake.calls)
    _, again = _write(fake, plans, b.plan)  # 再跑一次:什么也不新建、不改
    assert sum(v for k, v in again.items() if "新建" in k or "上传" in k) == 0 and sum(again.values()) == 0
    assert not [c for c in fake.calls[n:] if c[0] in ("POST", "PATCH")]


def test_fill_keeps_new_base_edits_and_sync_overwrites(env):
    prof, _ = env
    fake = FakeTeable()
    plans = _schema(fake, prof)
    b = _build(env)
    _write(fake, plans, b.plan)
    t = fake.table_by_name("线索", next(x for x in fake.bases.values() if x["name"] == "水务项目机会 新版")["id"])
    rec = next(r for r in fake.records[t["id"]] if r["fields"].get(fake.field(t["id"], "线索号")["id"]) == "d4d4d4d4")
    fid = fake.field(t["id"], "处理状态")["id"]
    rec["fields"][fid] = "已处理"  # 有人在新库里改了
    _write(fake, plans, b.plan)
    assert rec["fields"][fid] == "已处理"  # 缺省只补空的
    _write(fake, plans, b.plan, mode="sync")
    assert rec["fields"][fid] == "待处理"  # --sync 按迁移数据覆盖


def test_unknown_people_derived_fields_and_old_base(env):
    prof, _ = env
    fake = FakeTeable()
    plans = _schema(fake, prof)
    b = _build(env)
    k = mig.followup_key("ZZZ-001", "2026-09-29", "电话")  # 跟进的键:商机 + 日期 + 内容等的指纹
    b.plan.rows["followup"][k] = mig.Row(k, {"日期": "2026-09-29", "内容": "电话"}, links={"商机": ["ZZZ-001"]}, people={"记录人": "admin"})
    w, _ = _write(fake, plans, b.plan)
    assert any("「admin」在表格里没有账号" in x for x in w.warnings)
    assert any(r["内容"] == "(记录人:admin) 电话" for r in fake.rows("跟进记录"))
    _, again = _write(fake, plans, b.plan)  # 加了前缀的那条再跑也认得出,不重建
    assert again["跟进记录·新建"] == 0
    fake2 = FakeTeable()
    plans2 = _schema(fake2, prof)
    w2, _ = _write(fake2, plans2, b.plan, alias={"admin": "Ada"})
    assert any(r["内容"] == "电话" and r["记录人"]["title"] == "Ada" for r in fake2.rows("跟进记录")) and not w2.warnings
    b.plan.rows["opportunity"]["ZZZ-001"].fields["所属线索数"] = 3
    with pytest.raises(ValueError, match="派生的不写"):
        _write(fake2, plans2, b.plan)
    with pytest.raises(tstore.TStoreError, match="先跑 python -m core.schema"):  # 迁移不建库
        mig.Writer(tstore.Client("https://x", "admin-token", transport=FakeTeable().transport), "水务项目机会 新版", plans2)
    with pytest.raises(tstore.TStoreError, match="只动新库"):
        mig.Writer(tstore.Client("https://x", "admin-token", transport=fake2.transport), "水务项目机会", plans2)


def test_dry_run_needs_no_token(env, tmp_path, monkeypatch, capsys):
    prof, data = env
    monkeypatch.delenv("TEABLE_ADMIN_TOKEN", raising=False)
    out = tmp_path / "对账报告.md"
    assert mig.main(["--profile", str(prof.path), "--dry-run", "--report", str(out), "--today", "2026-10-03"]) == 0
    assert "试运行(--dry-run),没有写表格" in out.read_text(encoding="utf-8") and "| 线索 | 7 |" in capsys.readouterr().out


def test_country_fields_and_access_only_sources(env):
    """国别的机构、法规等字段真的迁了(reference 里有摩洛哥的资料);台账 access 写进「市场准入与竞争」,引用的出处也建成线索,线索号都在新库里;
    对账报告的字段核对能看出哪些字段有值。"""
    b = _build(env)
    mar = b.plan.rows["country"]["MAR"].fields
    assert mar["水务管理机构"] and mar["相关法律法规"] and "http" in mar["水务管理机构"] and mar["基础信息"].startswith("首都:")
    s03 = mig.signal_id(mig.LEDGER, "https://example.org/access/3")[:8]
    zzz = b.plan.rows["country"]["ZZZ"].fields
    assert zzz["市场准入与竞争"] == f"虚构的市场准入说明 [{s03}]" and not zzz.get("基础信息")  # 测试国没有国别资料:基础信息留空,不混进准入说明
    assert b.plan.rows["lead"][s03].fields["标题"] == "虚构准入指南"
    assert b.plan.rows["lead"][s03].links == {"信源": ["ledger"], "国别": ["ZZZ"], "所属商机": [], "建议所属商机": []}
    md = mig.report_md(b, "水务项目机会 新版")
    assert "都在新库里:是" in md and "**否**,缺" not in md
    assert "- 国别(2 行):" in md and "水务管理机构 1" in md and "全空**:" in md.split("- 国别(2 行):")[1].split("\n")[0]  # 没有快照:指标全空,标出来


def test_same_treats_equal_instants_as_unchanged():
    assert mig._same("2026-09-25T08:00:00.000Z", "2026-09-25T08:00:00+00:00")
    assert mig._same("2026-09-24T16:00:00.000Z", "2026-09-25") and not mig._same("2026-09-25T08:00:00.000Z", "2026-09-25T09:00:00+00:00")
    assert mig._same({"id": "rec1", "title": "x"}, {"id": "rec1"}) and not mig._same("甲", "乙")


def test_rerun_on_another_day_creates_nothing(env):
    """换一天重跑:补的"(迁移)…"阶段变更跟进按台账 as_of 定日期,不随运行日期变,所以不重复建。"""
    prof, data = env
    fake = FakeTeable()
    plans = _schema(fake, prof)
    _write(fake, plans, _build(env).plan)
    later = mig.Builder(prof, data, TODAY + timedelta(days=3))
    later.build()
    mig_rows = [r for r in later.plan.rows["followup"].values() if r.fields["内容"].startswith("(迁移)")]
    assert mig_rows and all(r.fields["日期"] == "2026-09-20" for r in mig_rows)  # 台账 as_of
    _, again = _write(fake, plans, later.plan)
    assert sum(again.values()) == 0


def test_report_rows_survive_a_failed_upload(env):
    """报告按「文件名」标识:附件上传半途失败(行建了、没附件),重跑认得出这一行,只补上传、不重复建。"""
    prof, _ = env
    fake = FakeTeable()
    plans = _schema(fake, prof)
    plan = _build(env).plan
    _write(fake, plans, plan)
    t = fake.table_by_name("报告", next(x for x in fake.bases.values() if x["name"] == "水务项目机会 新版")["id"])
    fid, name_id = fake.field(t["id"], "文件")["id"], fake.field(t["id"], "文件名")["id"]
    for rec in fake.records[t["id"]]:
        rec["fields"].pop(fid, None)  # 像上传失败了那样
    assert {r["fields"][name_id] for r in fake.records[t["id"]]} == {"国别报告_测试国_2026-10-01.docx", "ZZZ_周报_2026-W40.md"}
    _, again = _write(fake, plans, plan)
    assert again["报告·新建"] == 0 and again["报告·上传文件"] == 2 and len(fake.records[t["id"]]) == 2


def test_judgment_method_follows_r8(env):
    """判定方式:上传的一律「不判定」(哪怕旧库里有模型摘要);送了模型没结果的留空(新库里 A03 再判);规则初筛没过的「规则」。"""
    prof, data = env
    st = Store(data)
    up = _sig("f6f6f6f6f6f6f6f6", "同事上传的", "https://example.org/t/f", _llm())
    up.update(source="manual", source_name="人工提交", kind="manual")
    pend = _sig("0707070707070707", "送了模型没结果", "https://example.org/t/g", None)
    pend["rules"] = {"candidate": True}
    err = _sig("0808080808080808", "模型出错", "https://example.org/t/h", {"_error": "timeout"})
    err["rules"] = {"candidate": True}
    st.append_signals([up, pend, err])
    leads = _build(env).plan.rows["lead"]
    assert (leads["f6f6f6f6"].fields["判定方式"], leads["f6f6f6f6"].fields["来源方式"]) == ("不判定", "用户上传")
    assert leads["07070707"].fields["判定方式"] is None and leads["08080808"].fields["判定方式"] is None
    assert leads["c3c3c3c3"].fields["判定方式"] == "规则" and leads["a1a1a1a1"].fields["判定方式"] == "模型"


def test_text_primary_is_filled_when_the_formula_fallback_fails(env, monkeypatch):
    """主字段换不成公式(schema 记警告、留单行文本)时,迁移按公式的意思合成主字段:看板卡片、关联单元格、日历标题才不是空的。"""
    prof, _ = env
    fake = FakeTeable()
    real = fake.route

    def no_formula_primary(method, path, params, body, request):
        if method == "PUT" and path.endswith("/convert") and (body or {}).get("type") == "formula":
            from tests.teable_fake import HTTPError
            raise HTTPError(400, "这个版本的主字段不能是公式")
        return real(method, path, params, body, request)
    monkeypatch.setattr(fake, "route", no_formula_primary)
    plans = _schema(fake, prof)
    _write(fake, plans, _build(env).plan)
    fol = {r["内容"]: r for r in fake.rows("跟进记录")}
    assert fol["约见业主"]["摘要"] == "2026-09-28 · 约见业主"
    assert any(r.get("节点") == "资格预审截止 · 2026-11-15" for r in fake.rows("时间节点"))
    assert any(r.get("报告号") == "国别报告 · 2026-10-01 · 内部版" for r in fake.rows("报告"))


def test_lead_only_followups_newlines_followup_keys_and_one_download_per_table(env):
    """旧跟进记录表允许只关联线索:没有商机的跟进写进线索的处理备注并警告(审查二-2);单行文本里的换行写入前换成空格(二-1);
    跟进的键算上阶段变更、判断、下一步(二-3);每表每次运行只下载一遍记录(二-5);Writer 不再要本体与映射两个参数(二-7)。"""
    b = _build(env)
    lead = b.plan.rows["lead"]["b2b2b2b2"]
    assert "(迁移)线索上的跟进 2026-09-29 张三:电话问过业主;判断:重点推进;下一步:等回复(到期 2026-10-05)" in lead.fields["处理备注"]
    assert any("b2b2b2b2 上的跟进没有商机可放" in w for w in b.plan.warnings)
    assert lead.fields["标题"] == "Road\ntender" and not [r for r in b.plan.rows["followup"].values() if r.links["商机"] == ["b2b2b2b2"]]
    fake = FakeTeable()
    plans = _schema(fake, env[0])
    _write(fake, plans, b.plan)
    assert next(r for r in fake.rows("线索") if r["线索号"] == "b2b2b2b2")["标题"] == "Road tender"
    k1 = mig.followup_key("ZZZ-001", "2026-09-28", "约见业主", None, "重点推进", "准备资审")
    assert k1 == mig.followup_key("ZZZ-001", "2026-09-28", "约见业主", judgment="重点推进", step="准备资审")
    assert k1 != mig.followup_key("ZZZ-001", "2026-09-28", "约见业主", None, "观望", "准备资审")
    assert mig.followup_key("ZZZ-001", "2026-09-28", "x", "跟踪中") != mig.followup_key("ZZZ-001", "2026-09-28", "x", "建议")
    n = len(fake.calls)
    _, again = _write(fake, plans, b.plan)
    assert again["跟进记录·新建"] == 0 and again["线索·新建"] == 0 and again["跟进记录·关联"] == 0
    gets = [path for m, path in fake.calls[n:] if m == "GET" and re.search(r"/table/[^/]+/record$", path.split("?")[0])]
    assert len(gets) == 19, gets  # 十九张表各下载一遍(含没有迁移行的报告模板、城市、公司资料、个人设置、自来水厂、污水厂、行业术语、已中标项目、供应商、研究资料、水务事件、水务片区表);关联与附件复用
