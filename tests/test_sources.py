"""数据源适配器的离线测试(打桩,不联网)。"""
from types import SimpleNamespace

import httpx

from core.sources import wdi_indicators as wdi


def test_wdi_skips_indicator_that_times_out(monkeypatch, capsys):
    """单个指标超时只跳过该指标,其余照常入表;403/429 仍由 get_json 抛 SourceBlocked 整源停止。"""
    slow = "FP.CPI.TOTL.ZG"

    def fake_get_json(url, params=None, **kw):
        code = url.rsplit("/", 1)[-1]
        if code == slow:
            raise httpx.ReadTimeout("slow")
        return [{"page": 1}, [{"countryiso3code": "ZZZ", "date": "2025", "value": 1.0}]]

    monkeypatch.setattr(wdi, "get_json", fake_get_json)
    monkeypatch.setattr(wdi, "write_raw", lambda *a, **k: None)
    out = wdi.run(SimpleNamespace(iso3s=("ZZZ",), known_zero_countries=()))["country_indicator"]
    got = {r["indicator"] for r in out}
    assert slow not in got and len(got) == len(wdi.INDICATORS) - 1
    assert slow in capsys.readouterr().out
