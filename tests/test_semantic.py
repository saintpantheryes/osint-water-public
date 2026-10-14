"""语义层说法(core/semantic.py,本体 phrase,A42/A43):后台按本体给表里没有的指向补行、只新建;助手每分钟读表,
表里有这一行的按表里的说法认(删掉的说法不再认),没有的用本体的;指向写错的行不用。"""
from __future__ import annotations

from datetime import date

from core import assistant as A, law, semantic
from core.assistant_server import Assistant
from tests.test_jobs import env  # noqa: F401

TODAY = date(2026, 10, 14)


def test_seed_covers_every_kind():
    rows = semantic.seed()
    kinds = {r["kind"] for r in rows}
    assert kinds == set(semantic.KINDS)
    ev = next(r for r in rows if r["kind"] == "指标" and r["key"] == "eval_days")
    assert ev["target"] == "评标天数" and "评标时间" in ev["words"] and "单位 天" in ev["note"] and "缺数时" in ev["note"]
    assert not any(r["target"] == "数量" for r in rows)  # 数量不靠说法认,不出行
    assert {r["target"] for r in rows if r["kind"] == "法规问法"} == {"问排放标准", "问定义", "问法规条文"}


def test_fill_adds_missing_targets_only(env):
    fake, admin, b, ctx = env
    out = semantic.fill(b, log=lambda *a: None)
    assert out == {"phrases_added": len(semantic.seed())}
    row = next(r for r in b.rows("phrase") if b.get("phrase", r, "target") == "评标天数")
    assert b.get("phrase", row, "kind") == "指标" and "评标时间" in b.get("phrase", row, "words")
    b.update("phrase", row["id"], {"words": "开标多久出结果"})  # 人改过的:再补不动
    assert semantic.fill(b, log=lambda *a: None) == {}
    assert b.get("phrase", b.record("phrase", row["id"]), "words") == "开标多久出结果"


def test_assistant_reads_table_phrases():
    try:
        A.set_phrases([{"kind": "指标", "target": "评标天数", "words": "开标多久出结果、评标周期"},
                       {"kind": "法规问法", "target": "问排放标准", "words": "出水要多干净\n排放标准"},
                       {"kind": "主体工程", "target": "污水处理厂", "words": "污水厂、净化站"},
                       {"kind": "指标", "target": "不存在的指标", "words": "多少钱"}])
        assert [m["id"] for m in A.parse_measures("摩洛哥污水项目开标多久出结果")] == ["eval_days"]
        assert A.parse_measures("摩洛哥污水项目平均评标时间") == []  # 表里删掉了「评标时间」,不再认
        assert [m["id"] for m in A.parse_measures("摩洛哥平均投标家数")] == ["bidders"]  # 表里没有这一行:用本体的
        assert A.assets("摩洛哥的净化站中标项目") == ["污水处理厂"]
        assert law.kind_of("摩洛哥污水厂出水要多干净") == "limit"
        assert semantic.unknown([{"kind": "指标", "target": "不存在的指标"}, {"kind": "指标", "target": "评标天数"}]) == [{"kind": "指标", "target": "不存在的指标"}]
    finally:
        A.set_phrases(None)
    assert [m["id"] for m in A.parse_measures("摩洛哥污水项目平均评标时间")] == ["eval_days"]
    assert A.assets("摩洛哥的净化站中标项目") == []


def test_assistant_server_refreshes_phrases_with_terms():
    class FakeBase:
        t = {"term": "tblT", "phrase": "tblP"}
        def has(self, oid, p): return True
        def rows(self, oid): return [] if oid == "term" else [{"kind": "指标", "target": "投标家数", "words": "几家来抢"}]
        def get(self, oid, r, p): return r.get(p)
    bot = Assistant(None, use_llm=False)
    bot.terms_base = lambda: FakeBase()
    try:
        bot.refresh_terms()
        assert [m["id"] for m in A.parse_measures("摩洛哥污水厂一般几家来抢")] == ["bidders"]
    finally:
        A.set_terms(None)
        A.set_phrases(None)
