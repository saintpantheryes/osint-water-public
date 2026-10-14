"""法规原文与排放标准(core/jobs/laws.py A37/A39/A40,core/law.py 助手的法规问答):
导入只新建、只填空,摘录新建时「待核实」;条文后台译中文;问法认得出排放标准 / 定义 / 原文,追问「那吉尔吉斯斯坦呢」沿用上一问;
按条文作答带出处,没收原文的国家单列。数据全是虚构的,不联网。"""
from __future__ import annotations

import csv
import gzip

from core import law, tstore
from core.jobs import laws
from tests.test_jobs import env  # noqa: F401


def _csv(path, cols, rows):
    with (gzip.open(path, "wt", encoding="utf-8", newline="") if path.suffix == ".gz" else open(path, "w", encoding="utf-8", newline="")) as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        w.writerows(rows)


def _data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    _csv(d / "laws.csv", laws.LAW_COLS, [
        {"key": "LAW-AZE-T1", "iso3": "AZE", "name": "虚构排放规则", "name_orig": "Fictional effluent rules", "kind": "排放标准与技术规范", "status": "现行", "language": "英文"},
        {"key": "LAW-EGY-T1", "iso3": "EGY", "name": "国别表里没有的"}])
    _csv(d / "law_clauses.csv.gz", laws.CLAUSE_COLS, [
        {"key": "LAW-AZE-T1-0001", "law_key": "LAW-AZE-T1", "seq": 1, "label": "第 1 条", "text": "Wastewater means water discharged after use.", "page": "1"},
        {"key": "LAW-AZE-T1-0002", "law_key": "LAW-AZE-T1", "seq": 2, "label": "附表 1", "text": "BOD5 25 mg/l; COD 125 mg/l", "page": "3"}])
    _csv(d / "local_terms.csv", laws.TERM_COLS, [
        {"key": "AZE-T1-污水", "iso3": "AZE", "concept": "污水", "term_orig": "wastewater", "term_zh": "废水", "definition": "使用后排出的水", "relation": "相同",
         "law_key": "LAW-AZE-T1", "clause_key": "LAW-AZE-T1-0001"}])
    _csv(d / "discharge_limits.csv", laws.LIMIT_COLS, [
        {"key": "AZE-T1-BOD5-1", "iso3": "AZE", "law_key": "LAW-AZE-T1", "clause_key": "LAW-AZE-T1-0002", "param": "BOD5", "param_orig": "BOD5", "value": "25", "unit": "mg/L"},
        {"key": "AZE-T1-X-1", "iso3": "AZE", "law_key": "LAW-AZE-T1", "param": "不在选项里的指标", "value": "1"}])
    return d


def test_import_fills_blanks_and_marks_extracts_unverified(env, tmp_path):
    fake, admin, b, ctx = env
    d = _data(tmp_path)
    out = laws.run(b, d, dry_run=True, log=lambda *a: None)
    assert out["法规"] == {"新建": 1, "补空": 0, "国别表里没有": 1} and not b.rows("law")
    out = laws.run(b, d, log=lambda *a: None)
    assert out["条文"]["新建"] == 2 and out["国别定义"]["新建"] == 1 and out["排放限值"]["新建"] == 2
    lw = b.rows("law")[0]
    cl = {b.get("law_clause", r, "key"): r for r in b.rows("law_clause")}
    assert b.get("law", lw, "kind") == "排放标准与技术规范" and len(cl) == 2
    assert tstore.link_id(b.get("law_clause", cl["LAW-AZE-T1-0002"], "law")) == lw["id"]
    lim = {b.get("discharge_limit", r, "key"): r for r in b.rows("discharge_limit")}
    bod = lim["AZE-T1-BOD5-1"]
    assert b.get("discharge_limit", bod, "value") == 25 and b.get("discharge_limit", bod, "verified") == "待核实"
    assert tstore.link_id(b.get("discharge_limit", bod, "clause")) == cl["LAW-AZE-T1-0002"]["id"]
    assert not b.get("discharge_limit", lim["AZE-T1-X-1"], "param")  # 选项里没有的值不写
    t = b.rows("local_term")[0]
    assert b.get("local_term", t, "verified") == "待核实" and b.get("local_term", t, "relation") == "相同"
    b.update("discharge_limit", bod["id"], {"value": 20, "verified": "已核实"})  # 人核过改了的:再导入不覆盖
    again = laws.run(b, d, log=lambda *a: None)
    assert again["排放限值"] == {"新建": 0, "补空": 0, "国别表里没有": 0} and again["条文"]["新建"] == 0
    bod = b.record("discharge_limit", bod["id"])
    assert b.get("discharge_limit", bod, "value") == 20 and b.get("discharge_limit", bod, "verified") == "已核实"


def test_missing_file_skips(env, tmp_path):
    fake, admin, b, ctx = env
    assert "跳过" in laws.run(b, tmp_path, log=lambda *a: None)


class _Clf:
    enabled = True

    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def ask(self, system, user, schema, guide):
        self.calls.append(user)
        return self.outs.pop(0) if self.outs else None


def test_translate_fills_chinese_only_where_empty(env, tmp_path):
    fake, admin, b, ctx = env
    laws.run(b, _data(tmp_path), log=lambda *a: None)
    assert laws.translate(b, None, log=lambda *a: None) == 0
    clf = _Clf({"items": [{"i": 0, "zh": "废水指使用后排出的水。"}, {"i": 1, "zh": "BOD5 25 毫克/升;COD 125 毫克/升"}]})
    assert laws.translate(b, clf, log=lambda *a: None) == 2
    assert {b.get("law_clause", r, "text_zh") for r in b.rows("law_clause")} == {"废水指使用后排出的水。", "BOD5 25 毫克/升;COD 125 毫克/升"}
    assert laws.translate(b, _Clf(), log=lambda *a: None) == 0  # 都译过了,不再请模型


def test_detect_law_questions_and_follow_ups():
    p = law.detect("摩洛哥污水厂排放标准是多少")
    assert p["kind"] == "limit" and p["countries"] == ["MAR"]
    assert law.detect("阿塞拜疆污水处理厂出水标准")["countries"] == ["AZE"]
    f = law.detect("那吉尔吉斯斯坦呢", {"object": "law", "law": p, "filters": {"countries": [{"iso3": "MAR"}]}})
    assert f["kind"] == "limit" and f["countries"] == ["KGZ"] and f.get("inherited")
    assert law.detect("阿塞拜疆法律里污水怎么定义")["kind"] == "define"
    for q in ("摩洛哥的营商环境", "智慧水务的线索", "摩洛哥污水厂项目"):
        assert law.detect(q) is None and law.detect(q, {"object": "law", "law": p}) is None, q


class _Lib:
    def __init__(self, data):
        self.data = data

    def country_rows(self, iso):
        return self.data.get(iso, ([], {}))


def test_answer_cites_clauses_and_lists_countries_without_texts():
    lib = _Lib({"AZE": ([{"id": "rec1", "key": "K-0002", "label": "附表 1", "text": "BOD5 25 mg/l", "text_zh": "BOD5 25 毫克/升", "page": "3", "seq": 2, "law": "recL"},
                        {"id": "rec2", "key": "K-0001", "label": "第 1 条", "text": "Scope", "text_zh": "适用范围", "page": "1", "seq": 1, "law": "recL"}],
                       {"recL": {"key": "LAW-AZE-T1", "name": "虚构排放规则", "url": "https://example.org/x.pdf", "language": "英文"}})})
    plan = {"keywords": ["BOD5"]}
    res = law.answer(lib, "阿塞拜疆的 BOD5 限值", plan, ["AZE", "KGZ"], None, log=lambda *a: None)
    assert [c["key"] for c in res["cites"]] == ["K-0002"] and res["missing"] == [{"name": "吉尔吉斯斯坦", "iso3": "KGZ"}] and "没配模型" in res["note"]
    clf = _Clf({"zh": ["生化需氧量"], "orig": ["BOD5"]}, {"answer": "阿塞拜疆 BOD5 限值 25 毫克/升[1]。", "used": [1]})
    res = law.answer(lib, "阿塞拜疆的 BOD5 限值", plan, ["AZE"], clf, log=lambda *a: None)
    assert res["answer"].endswith("[1]。") and res["used"] == [1] and res["cites"][0]["law"] == "虚构排放规则"
