"""汇率与投资规模(core/fx.py + core/jobs/fx.py,A21):金额文字解析;接口 → 欧央行 → 内置表的顺序;按日缓存;
线索「金额」拆成投资规模 + 货币;线索、商机折成人民币万元(商机顺带美元);只填空;字段没建时跳过。不联网(httpx.MockTransport)。"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from core import fx as fx_mod
from core.jobs import fx as fx_job
from tests.test_jobs import LLM, ITEMS, _llm, _rows, _val, env  # noqa: F401  复用后台任务的内存环境
from core.jobs import collect
from core.watch.store import Item


def test_parse_amount_text():
    assert fx_mod.parse_amount("5,000,000 USD") == (5e6, "USD")
    assert fx_mod.parse_amount("USD 5000000") == (5e6, "USD")
    assert fx_mod.parse_amount("12.5 EUR") == (12.5, "EUR")
    assert fx_mod.parse_amount("EUR 25 million(EBRD 贷款)") is None and fx_mod.parse_amount("") is None and fx_mod.parse_amount(None) is None
    assert fx_mod.parse_amount("USD 5 EUR") is None


def _client(erapi=None, ecb=None):
    def handle(req: httpx.Request) -> httpx.Response:
        if "er-api" in req.url.host:
            return erapi() if callable(erapi) else httpx.Response(500)
        if "ecb" in req.url.host:
            return ecb() if callable(ecb) else httpx.Response(500)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handle))


ECB_XML = b"""<?xml version="1.0"?><gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube><Cube time="2026-10-08"><Cube currency="USD" rate="1.10"/><Cube currency="CNY" rate="8.25"/><Cube currency="JPY" rate="165.0"/></Cube></Cube></gesmes:Envelope>"""


def test_fetch_order_and_fallbacks():
    logs = []
    rates, src = fx_mod.fetch(_client(erapi=lambda: httpx.Response(200, json={"result": "success", "rates": {"CNY": 1, "USD": 0.14, "EUR": 0.125, **{f"X{i:02d}": 1.0 for i in range(12)}}})), log=logs.append)
    assert src == "open.er-api.com" and rates["USD"] == pytest.approx(1 / 0.14) and rates["CNY"] == 1.0
    rates, src = fx_mod.fetch(_client(ecb=lambda: httpx.Response(200, content=ECB_XML)), log=logs.append)
    assert src == "欧央行" and rates["EUR"] == 8.25 and rates["USD"] == pytest.approx(8.25 / 1.10) and rates["JPY"] == pytest.approx(0.05)
    rates, src = fx_mod.fetch(_client(), log=logs.append)
    assert src == "内置近似表" and rates == fx_mod.STATIC and any("都没取到" in x for x in logs)


def test_rates_cache_by_day_and_conversions(tmp_path):
    calls = []
    def erapi():
        calls.append(1)
        return httpx.Response(200, json={"result": "success", "rates": {"CNY": 1, "USD": 0.14, "EUR": 0.125, **{f"X{i:02d}": 1.0 for i in range(12)}}})
    r = fx_mod.Rates(cache_dir=tmp_path, today=date(2026, 10, 8), client=_client(erapi=erapi), log=lambda *a: None)
    assert r.to_cny(1_000_000, "USD") == pytest.approx(1e6 / 0.14) and r.to_usd(1_000_000, "USD") == pytest.approx(1e6)
    assert r.to_cny(None, "USD") is None and r.to_cny(5, None) is None and r.rate("VUV") == fx_mod.STATIC["VUV"]  # 接口没有的币种用内置表
    assert json.loads((tmp_path / "fx.json").read_text(encoding="utf-8"))["date"] == "2026-10-08"
    r2 = fx_mod.Rates(cache_dir=tmp_path, today=date(2026, 10, 8), client=_client(), log=lambda *a: None)  # 同一天:读缓存,不再取
    assert r2.to_cny(14, "USD") == pytest.approx(100) and r2.source == "open.er-api.com" and len(calls) == 1
    r3 = fx_mod.Rates(cache_dir=tmp_path, today=date(2026, 10, 9), client=_client(), log=lambda *a: None)  # 第二天:取不到用内置表,不写缓存
    assert r3.to_cny(1, "USD") == fx_mod.STATIC["USD"] and r3.source == "内置近似表"
    assert json.loads((tmp_path / "fx.json").read_text(encoding="utf-8"))["date"] == "2026-10-08"
    assert fx_mod.wan(123_456_789) == 12346 and fx_mod.wan(None) is None


def test_job_splits_lead_amounts_and_converts(env):
    fake, admin, abase, ctx = env
    ctx._fx = fx_mod.Rates(client=_client(), today=ctx.today, log=lambda *a: None)  # 内置表:USD 7.15
    b = abase
    c = b.rows("country")[0]
    # 判定出金额的线索:入库时就带投资规模 + 货币;商机(新建)也带
    ITEMS["fake_src"] = [Item(key="a", url="https://example.org/a", title="EOI: Ganja WWTP", published="2026-10-01", text="wastewater plant", fields={})]
    LLM["https://example.org/a"] = _llm(new=True, project="Ganja WWTP", amount={"value": 5e6, "currency": "USD"})
    collect.run(ctx)
    a = _rows(abase, "lead")[0]
    assert _val(abase, "lead", a, "amount") == "5,000,000 USD" and _val(abase, "lead", a, "amount_value") == 5e6 and _val(abase, "lead", a, "currency") == "USD"
    o = _rows(abase, "opportunity")[0]
    assert _val(abase, "opportunity", o, "size_value") == 5e6 and _val(abase, "opportunity", o, "currency") == "USD" and not _val(abase, "opportunity", o, "size_cny")
    # 旧线索:只有「金额」文字;一条写法解析不了;一条币种不在选项里;一条人已填了折合(不动)
    b.create("lead", [{"key": "11111111", "title": "x", "url": "https://example.org/x", "amount": "EUR 2,000,000", "country": c["id"], "status": "待处理", "origin": "机器采集"},
                      {"key": "22222222", "title": "y", "url": "https://example.org/y", "amount": "EUR 25 million", "country": c["id"], "status": "待处理", "origin": "机器采集"},
                      {"key": "33333333", "title": "z", "url": "https://example.org/z", "amount": "ZZZ 100", "country": c["id"], "status": "待处理", "origin": "机器采集"},
                      {"key": "44444444", "title": "w", "url": "https://example.org/w", "amount_value": 100, "currency": "JPY", "amount_cny": 7, "country": c["id"], "status": "待处理", "origin": "机器采集"}])
    out = fx_job.run(ctx)
    assert out == {"线索拆金额": 2, "线索折算": 2, "商机折算": 1, "汇率": "内置近似表"}, out
    rows = {_val(abase, "lead", r, "key"): r for r in _rows(abase, "lead")}
    assert _val(abase, "lead", rows["11111111"], "amount_value") == 2e6 and _val(abase, "lead", rows["11111111"], "currency") == "EUR"
    assert _val(abase, "lead", rows["11111111"], "amount_cny") == round(2e6 * fx_mod.STATIC["EUR"] / 1e4)
    assert not _val(abase, "lead", rows["22222222"], "amount_value")
    assert _val(abase, "lead", rows["33333333"], "amount_value") == 100 and not _val(abase, "lead", rows["33333333"], "currency") and not _val(abase, "lead", rows["33333333"], "amount_cny")
    assert _val(abase, "lead", rows["44444444"], "amount_cny") == 7  # 人填的不动
    assert _val(abase, "lead", rows[_val(abase, "lead", a, "key")], "amount_cny") == round(5e6 * 7.15 / 1e4)
    o = _rows(abase, "opportunity")[0]
    assert _val(abase, "opportunity", o, "size_cny") == round(5e6 * 7.15 / 1e4) and _val(abase, "opportunity", o, "size_usd") == 5e6
    assert fx_job.run(ctx) == {}  # 再跑没事可做
    # 表里没建这些字段(旧库):跳过,不报错
    for name in ("投资规模", "货币", "折合人民币(万元)"):
        f = fake.field(abase.t["lead"], name)
        fake.fields[abase.t["lead"]] = [x for x in fake.fields[abase.t["lead"]] if x["id"] != f["id"]]
    ctx.base.f["lead"] = {x["name"]: x for x in ctx.api.fields(ctx.base.t["lead"])}
    assert fx_job.split_lead_amounts(ctx) == 0 and fx_job.convert(ctx, "lead", "amount_value", "amount_cny") == 0
