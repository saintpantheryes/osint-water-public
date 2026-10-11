"""阶段 A 能力核验(scripts/teable_probe.py)在内存版 Teable 上跑一遍:各项都有结论;只在新库里建试验表,核验完删掉;
旧库不动;机器账号的令牌跑不了(建不了表)。不联网。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core import tstore
from tests.teable_fake import FakeTeable

_spec = importlib.util.spec_from_file_location("teable_probe", Path(__file__).resolve().parent.parent / "scripts" / "teable_probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["teable_probe"] = probe  # dataclass 要在 sys.modules 里找到模块
_spec.loader.exec_module(probe)


def _probe(fake: FakeTeable, token: str = "admin-token", **kw) -> probe.Probe:
    api = tstore.Client("https://water.example", token, transport=fake.transport, sleep=lambda s: None)
    return probe.Probe(api, "水务项目机会 新版", log=lambda *a: None, sleep=lambda s: None, **kw)


def test_every_capability_gets_a_verdict_and_the_trial_tables_are_removed():
    fake = FakeTeable()
    p = _probe(fake)
    p.discover()
    p.setup()
    p.checks_all(mention="张三")
    by = {c.name: c for c in p.checks}
    # 经关联的查找带 sort/limit:接口静默丢掉(同 Teable 的请求模式),预期 ✗,说明里写明回显了什么;条件查找才是"最近一条"的写法
    assert [c.name for c in p.checks if c.result.startswith("✗")] == ["经关联的查找带 sort/limit"]
    assert "接口收下了 无;结果 ['观望', '继续跟进', '观望']" in by["经关联的查找带 sort/limit"].note
    for name in ("条件查找(isConditionalLookup,按日期倒序取第一条)", "最后修改人/时间只跟踪一个字段", "汇总 max / countall",
                 "写记录(按字段编号)与按条件计数", "单次读 1000 行与翻页", "单选的缺省值", "附件上传", "记录历史接口", "关联与反向字段改名",
                 "单向关联(isOneWay)", "含 TODAY 的公式与 today 筛选(隔天重算要第二天 --recheck)"):
        assert by[name].result == "✓ 可以", (name, by[name].note)
    assert "甲 ['继续跟进']" in by["条件查找(isConditionalLookup,按日期倒序取第一条)"].note
    assert by["看板拖动改阶段"].result.startswith("?") and by["公式 DATE_ADD"].result.startswith("?")  # 假接口不算公式(只认 TODAY 的两种写法)
    assert fake.comments and fake.comments[0]["content"][0]["children"][1]["value"] == "usrZS"
    new = next(b for b in fake.bases.values() if b["name"] == "水务项目机会 新版")
    assert len([t for t in fake.tables.values() if t["baseId"] == new["id"]]) == 3
    p.cleanup()
    assert not [t for t in fake.tables.values() if t["baseId"] == new["id"]] and not [t for t in fake.tables.values() if t["baseId"] == "bseOLD"]
    assert "| 条件查找(isConditionalLookup,按日期倒序取第一条) | ✓ 可以 |" in p.table()


def test_probe_refuses_the_old_base_and_the_bot_token():
    fake = FakeTeable()
    with pytest.raises(tstore.TStoreError, match="只动新库"):
        probe.Probe(tstore.Client("https://x", "admin-token", transport=fake.transport), "水务项目机会")
    p = _probe(fake, "bot-token")
    with pytest.raises(tstore.TStoreError) as e:
        p.setup()
    assert e.value.status == 403


def test_a_failed_prerequisite_does_not_abort_and_a_wrong_mention_is_reported():
    """前一项(关联字段)失败:依赖它的项记"没测",其余照测,核验表照样输出;--mention 对不上协作者记 ✗,不是 ✓。"""
    from tests.teable_fake import HTTPError
    fake = FakeTeable()
    real = fake.create_field

    def no_links(table_id, spec):
        if spec.get("type") == "link":
            raise HTTPError(400, "这个版本建不了关联")
        return real(table_id, spec)
    fake.create_field = no_links
    p = _probe(fake)
    p.setup()
    p.checks_all(mention="張三")  # 写错了字,对不上「张三」
    by = {c.name: c for c in p.checks}
    assert by["关联与反向字段改名"].result.startswith("✗") and "建不了关联" in by["关联与反向字段改名"].note
    for name in ("写记录(按字段编号)与按条件计数", "条件查找(isConditionalLookup,按日期倒序取第一条)", "汇总 max / countall", "附件上传", "评论 @成员"):
        assert by[name].result.startswith("?") and "没测" in by[name].note, name
    assert by["单选的缺省值"].result == "✓ 可以" and by["单次读 1000 行与翻页"].result == "✓ 可以"  # 不依赖前置项的照测
    assert "| 关联与反向字段改名 | ✗ 不行 |" in p.table()
    p.cleanup()
    fake2 = FakeTeable()
    p2 = _probe(fake2)
    p2.setup()
    p2.checks_all(mention="張三")
    c = next(x for x in p2.checks if x.name == "评论 @成员")
    assert c.result.startswith("✗") and "不是这个库的协作者" in c.note and not fake2.comments


def test_recheck_the_next_day_judges_recalculation_and_removes_the_trial_tables(monkeypatch):
    """--keep 留下试验表;同一天 --recheck 说看不出;第二天(假接口的"今天"往后拨一天)判三项,并删试验表(keep 不删)。"""
    from datetime import date, timedelta
    fake = FakeTeable()
    p = _probe(fake)
    p.setup()
    p.checks_all(mention=None)
    d0 = probe.today_sh()
    got = next(c for c in p.checks if c.name.startswith("含 TODAY"))
    assert got.result == "✓ 可以" and f"都应为 {d0}" in got.note
    new = next(b for b in fake.bases.values() if b["name"] == "水务项目机会 新版")
    same = _probe(fake)
    same.recheck(keep=True)
    assert same.checks[0].result.startswith("?") and "同一天看不出" in same.checks[0].note
    assert len([t for t in fake.tables.values() if t["baseId"] == new["id"]]) == 3  # keep:没删
    d1 = d0 + timedelta(days=1)
    monkeypatch.setattr(probe, "today_sh", lambda: d1)
    fake._today = d1  # 假接口按"今天"现算 TODAY() 与 today 筛选,相当于一个会重算的实例
    nxt = _probe(fake)
    nxt.recheck(keep=False)
    by = {c.name: c for c in nxt.checks}
    assert by["含 TODAY 的公式隔天重算"].result == "✓ 可以" and f"应为 {d1}" in by["含 TODAY 的公式隔天重算"].note
    assert by["含 TODAY 的公式:过期的行隔天清空"].result == "✓ 可以"
    assert by["today 筛选的条件查找隔天重算"].result == "✓ 可以" and f"应为 {d1}" in by["today 筛选的条件查找隔天重算"].note
    assert not [t for t in fake.tables.values() if t["baseId"] == new["id"]] and "第二天复核" in nxt.table()
    assert isinstance(d0, date)
