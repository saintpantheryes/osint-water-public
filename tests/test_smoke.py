"""冒烟检查:demo profile 能被加载、三张 P0 表的快照存在且每个样本国都有数据。
需要网络;第一次运行前先 `python -m core.cli ingest --profile profiles/demo/profile.yaml`。
没有快照(未联网 ingest)时这三项跳过而不是失败,离线也能跑其余测试。
"""
from pathlib import Path

import duckdb
import pytest

from core.profile import load_profile
from core.storage import latest_snapshot

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profiles" / "demo" / "profile.yaml"


def test_profile_loads():
    p = load_profile(PROFILE)
    assert len(p.countries) == 10
    assert all(c.tier in ("focus", "cultivate", "watch") for c in p.countries)
    assert len(set(p.iso3s)) == 10


def test_country_snapshot_complete():
    p = load_profile(PROFILE)
    fp = latest_snapshot("country")
    if not fp:
        pytest.skip("no country snapshot — run ingest first (needs network)")
    rows = duckdb.sql(f"select iso3, income_group, lending_type from '{fp}'").fetchall()
    assert {r[0] for r in rows} == set(p.iso3s)
    assert all(r[1] and r[2] for r in rows), "income_group/lending_type must be filled"


def test_indicator_snapshot_covers_every_country():
    p = load_profile(PROFILE)
    fp = latest_snapshot("country_indicator")
    if not fp:
        pytest.skip("no country_indicator snapshot — run ingest first (needs network)")
    q = f"""select iso3, count(distinct indicator), max(year) filter (where not is_forecast)
            from '{fp}' group by iso3"""
    got = {r[0]: (r[1], r[2]) for r in duckdb.sql(q).fetchall()}
    missing = [c for c in p.iso3s if c not in got]
    assert not missing, f"countries with no indicator rows: {missing}"
    for iso3, (n_ind, max_year) in got.items():
        assert n_ind >= 10, f"{iso3}: only {n_ind} indicators"
        assert max_year >= 2023, f"{iso3}: latest actual year {max_year} looks stale"
    # 预测值存在且标记正确
    n_fc = duckdb.sql(f"select count(*) from '{fp}' where is_forecast").fetchone()[0]
    assert n_fc > 0


def test_project_snapshot_sane():
    p = load_profile(PROFILE)
    fp = latest_snapshot("project")
    if not fp:
        pytest.skip("no project snapshot — run ingest first (needs network)")
    q = f"""select iso3, status, water_class, count(*) from '{fp}' group by all"""
    rows = duckdb.sql(q).fetchall()
    seen = {r[0] for r in rows if r[0]}
    assert set(p.iso3s) <= seen | set(p.known_zero_countries)
    classes = {r[2] for r in rows}
    assert "core" in classes, "no core-water Active projects classified"
    # 储备项目不做机器判定:不得出现 core/irrigation(部门占比字段在该阶段不可靠)
    assert not any(r[1] == "Pipeline" and r[2] in ("core", "irrigation") for r in rows)
    # 每行都有来源链接与抓取时间
    n_bad = duckdb.sql(f"select count(*) from '{fp}' where source_url is null or fetched_at is null").fetchone()[0]
    assert n_bad == 0
