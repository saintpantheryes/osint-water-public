"""命令行入口。

  python -m core.cli ingest --profile profiles/demo/profile.yaml [--only wdi_indicators]
  python -m core.cli show   --profile profiles/demo/profile.yaml
  python -m core.cli ledger --profile <profile> [--country AZE] [--today 2026-09-24]    # 校验台账、派生字段、写快照
  python -m core.cli report --profile <profile> [--country AZE] [--docx] [--public] [--out reports/out]   # 不给国别则逐国出
  python -m core.cli watch  --profile <profile> [--country AZE] [--no-llm]     # 监测:采集、判定、挂台账、写提醒页
  python -m core.cli autoledger --profile <profile>                            # 挂接可靠的线索自动写进台账,逐条提交并推送 main
  python -m core.cli digest --profile <profile> [--country AZE] [--today …]     # 周报(截至 today 的 7 天;不给国别则逐国出)
  python -m core.cli brief  --profile <profile> --type country|sector|brief [--country AZE] [--sectors hydraulic,…]
                            [--days 30] [--view 视图名] [--no-llm] [--out 目录]    # 按成稿模板出 Word 报告(带模型分析)
  python -m core.cli teable --profile <profile> [--url https://…] [--token …]   # 线索与台账同步到自托管 Teable(多维表格);
                            不给参数时读环境变量或程序目录 .env 的 TEABLE_URL、TEABLE_TOKEN
  python -m core.cli teable --pull --profile <profile>                          # 把同事在表格里改的人管字段拉回系统
                            (处理状态、进展、负责人、下一步、备注;项目的优先级等写进 overlay);profile 的 teable.pull 打开时才拉
  python -m core.cli teable --after-restore 2026-10-05T03:30+08:00 --profile <profile>   # 表格刚从备份恢复:下次拉回之前
                            把备份以后同步过的记录改回上次同步时的值(monitor 手动运行的「表格刚从备份恢复」一栏即调它)
  python -m core.cli commit-data --profile <profile> --message "watch: …"      # 定时任务:数据目录提交并推送到 data 分支
                            (远端取环境变量 DATA_REMOTE,缺省 origin)
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from core import storage
from core.ledger import ELIG, derive, load_ledger, load_overlay, load_rules, rows
from core.profile import Profile, load_profile
from core.report import render_country, to_docx
from core.sources import REGISTRY, load_all
from core.storage import latest_snapshot, log_run, now_iso, write_snapshot


def ingest(profile: Profile, only: str | None = None) -> int:
    load_all()
    tables: dict[str, list[dict]] = {}
    failed = 0
    for name in profile.sources_enabled:
        if only and name != only:
            continue
        fn = REGISTRY.get(name)
        if fn is None:
            print(f"  · {name}: 适配器未实现,跳过")
            continue
        started = now_iso()
        try:
            out = fn(profile)
        except Exception as e:  # 一个源失败不拖累其它源;失败记 run_log,保留上次快照
            failed += 1
            log_run(name, "error", 0, started, f"{type(e).__name__}: {e}")
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=1)
            continue
        n = sum(len(v) for v in out.values())
        for t, rows_ in out.items():
            tables.setdefault(t, []).extend(rows_)
        log_run(name, "ok", n, started)
        print(f"  ✓ {name}: {n} 行 → {', '.join(out)}")
    for t, rows_ in tables.items():
        fp = write_snapshot(t, rows_)
        prev = latest_snapshot(t, before=fp.parent.name)
        band = ""
        if prev is not None:
            pn = duckdb.sql(f"select count(*) from '{prev}'").fetchone()[0]
            if pn and abs(len(rows_) - pn) / pn > 0.3:
                band = f"  ⚠ 行数较上期 {pn} 变动超 30%"
        print(f"  ▸ snapshot {t}: {len(rows_)} 行 → {fp.relative_to(storage.DATA_DIR.parent)}{band}")
    return failed


def show(profile: Profile) -> None:
    ci = latest_snapshot("country_indicator")
    co = latest_snapshot("country")
    if not ci or not co:
        print("尚无快照,先 ingest")
        return
    q = f"""
    with latest as (
      select iso3, indicator, arg_max(value, year) as value, max(year) as year
      from '{ci}' where not is_forecast group by iso3, indicator
    )
    select c.tier, c.iso3, c.name_zh, c.income_group,
           round(g.value/1e9,1) as gdp_bn_usd, g.year as gdp_year,
           round(u.value,1) as urban_pct, u.year as urban_year,
           round(w.value,1) as basic_water_pct, w.year as water_year,
           round(f.value,1) as imf_gdp_growth_next, f.year as f_year
    from '{co}' c
    left join latest g on g.iso3=c.iso3 and g.indicator='NY.GDP.MKTP.CD'
    left join latest u on u.iso3=c.iso3 and u.indicator='SP.URB.TOTL.IN.ZS'
    left join latest w on w.iso3=c.iso3 and w.indicator='SH.H2O.BASW.ZS'
    left join (select iso3, arg_min(value, year) as value, min(year) as year
               from '{ci}' where indicator='NGDP_RPCH' and is_forecast group by iso3) f on f.iso3=c.iso3
    order by c.tier, gdp_bn_usd desc nulls last
    """
    print(duckdb.sql(q))


def _ledger_countries(profile: Profile, only: str | None):
    cs = [c for c in profile.countries if c.ledger and (not only or c.iso3 == only.upper())]
    if only and not cs:
        raise SystemExit(f"profile 中 {only} 没有配置 ledger")
    return cs


def ledger(profile: Profile, only: str | None, today: date) -> int:
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    tables: dict[str, list[dict]] = {}
    for c in _ledger_countries(profile, only):
        led = load_ledger(c.ledger, rules)
        ds = derive(led, rules, profile.bidder, profile.lead_times, today)
        print(f"  ✓ {c.iso3} 台账 {len(led.items)} 项 · 来源 {len(led.sources)} 条 · 截至 {led.as_of}")
        for d in ds:
            t = d.timing
            nxt = f"{t.node.type} {t.node.when.raw}" if t.node else "—"
            ls = f"最晚启动 {t.latest_start}({t.days_left:+d} 天)" if t.latest_start else ""
            print(f"    {d.item['id']} {d.item['stage']:<16} {ELIG[d.elig.status]:<6} 下一节点 {nxt:<28} {ls}")
            for s in d.stale:
                print(f"      ⚠ {s}")
        for t_, r in rows(led, ds, now_iso()).items():
            tables.setdefault(t_, []).extend(r)
    for t_, r in tables.items():
        fp = write_snapshot(t_, r)
        print(f"  ▸ snapshot {t_}: {len(r)} 行 → {fp.relative_to(storage.DATA_DIR.parent)}")
    return 0


def _indicators(iso3: str) -> dict | None:
    """最近一期 country_indicator 快照里该国的行;没有快照(未 ingest)时报告不出国别背景。"""
    fp = latest_snapshot("country_indicator")
    if not fp:
        return None
    rel = duckdb.sql(f"select * from '{fp}' where iso3 = '{iso3}'")
    cols = rel.columns
    return {"as_of": fp.parent.name, "rows": [dict(zip(cols, r)) for r in rel.fetchall()]}


def report(profile: Profile, only: str | None, today: date, out_dir: Path, docx: bool, public: bool) -> int:
    for c in _ledger_countries(profile, only):
        _report_country(profile, c, today, out_dir, docx, public)
    return 0


def _report_country(profile: Profile, c, today: date, out_dir: Path, docx: bool, public: bool) -> None:
    rules = load_rules(profile.rules) if profile.rules else load_rules()
    led = load_ledger(c.ledger, rules)
    overlay = {} if public else load_overlay(c.overlay)
    ds = derive(led, rules, profile.bidder, profile.lead_times, today)
    from core.country_facts import load_facts
    from core.markets import load_markets

    facts = load_facts(storage.DATA_DIR, load_markets(profile.markets), [c.iso3]).get(c.iso3)
    md = render_country(profile, c, led, rules, ds, overlay, today, public=public, indicators=_indicators(c.iso3), facts=facts)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{c.iso3}_项目机会分析_{today}" + ("_公开版" if public else "")
    fp = out_dir / f"{stem}.md"
    fp.write_text(md, encoding="utf-8")
    print(f"  ▸ {fp}")
    if docx:
        print(f"  ▸ {to_docx(fp, out_dir / f'{stem}.docx')}")


def autoledger(profile: Profile, today: date) -> int:
    """挂接可靠的线索自动写进台账,逐条提交并推送到 main(定时任务在监测之后运行);写了什么追加到运行摘要。"""
    from core.ledger_auto import run as auto_run
    from core.ledger_edit import brief_changes
    from core.watch.store import Store

    store = Store(storage.DATA_DIR)
    done, warns = auto_run(profile, store, today, push=True)
    lines = [f"## 台账自动更新:{len(done)} 条线索写进台账(挂接可靠;系统「待确认」页可核对、撤销)\n"]
    lines += [f"- {d['item']} {d['name']} ← 线索 {d['id'][:8]}:{brief_changes(d['changes'])}" for d in done]
    lines += [f"- ⚠ {w}" for w in warns]
    print("\n".join(lines))
    fp = store.root / "summary.md"
    if fp.exists():
        with fp.open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")
    from core.watch.publish import update_summary

    update_summary(storage.DATA_DIR, autoledger=len(done))
    return 0


def commit_data(message: str) -> int:
    """定时任务:数据目录(data 分支的检出)的改动提交并推送,监测后、自动入账与同步后各一次(core.sync.commit_data)。"""
    from core import sync

    r = sync.commit_data(storage.DATA_DIR, message, remote=os.environ.get("DATA_REMOTE") or "origin")
    print(r.output or ("完成" if r.ok else "失败"))
    return 0 if r.ok else 1


def watch(profile: Profile, only: str | None, today: date, use_llm: bool) -> int:
    from core.watch.outputs import relevant
    from core.watch.publish import write_outputs
    from core.watch.run import run as run_watch

    runs, clf = run_watch(profile, only, today, use_llm=use_llm)
    if not runs:
        raise SystemExit("profile 里没有配置 watch 的国别")
    for r in runs:
        rel = sum(1 for s in r.signals if relevant(s))
        print(f"  ✓ {r.iso3} 新线索 {len(r.signals)} 条(相关 {rel})" + (f",补判旧线索 {r.reclassified} 条" if r.reclassified else ""))
        for sid, st in r.sources.items():
            flag = {"ok": "·", "empty": "·", "error": "✗", "blocked": "⛔"}.get(st["status"], "?")
            extra = f"条目 {st.get('items')} 新增 {st.get('new')}" + (" 基线" if st.get("baseline") else "") if st["status"] in ("ok", "empty") else st.get("error")
            print(f"    {flag} {sid:<22} {extra}")
    if clf is not None:
        print(f"  模型:调用 {clf.calls} 次;{clf.note or ''}{';出错 ' + str(len(clf.errors)) if clf.errors else ''}")
    print(f"  ▸ {write_outputs(profile, runs, clf, today)}")
    return 0


def digest(profile: Profile, only: str | None, today: date) -> int:
    from core.watch.publish import write_digest

    cs = [c for c in profile.countries if c.watch and (not only or c.iso3 == only.upper())]
    if not cs:
        raise SystemExit("profile 里没有配置 watch 的国别" + (f" {only}" if only else ""))
    for c in cs:
        for fp in write_digest(profile, c.iso3, today):
            print(f"  ▸ {fp}")
    return 0


def brief(profile: Profile, a, today: date) -> int:
    from core.watch.llm import Classifier, LLMConfig
    from core.watch.query import Filter, load_views
    from core.watch.reporting import Spec, write

    view = load_views(storage.DATA_DIR).get(a.view) if a.view else None
    if a.view and view is None:
        raise SystemExit(f"没有名为 {a.view} 的保存筛选")
    f = Filter.from_dict(view) if view else Filter(days=a.days, status=[])
    if a.sectors:
        f.sectors = a.sectors.split(",")
    countries = [a.country] if a.country else ([c.iso3 for c in profile.countries if c.ledger] if a.type == "country" else [None])
    clf = None if a.no_llm else Classifier(LLMConfig.from_profile((profile.watch or {}).get("llm")))
    from core.watch.store import Store

    for iso in countries:
        spec = Spec(kind=a.type, filter=f, country=(iso or "").upper(), note=f"(按保存的筛选\"{a.view}\")" if a.view else "")
        for fp in write(profile, Store(storage.DATA_DIR), spec, today, clf, Path(a.out) if a.out != "reports/out" else None):
            print(f"  ▸ {fp}")
    if clf is not None and (clf.errors or clf.note):
        print(f"  模型:{clf.note or ''} {';'.join(clf.errors[:2])}")
    return 0


def run_job(profile: Profile, job: str | None, today: date, only: str | None = None, source: str | None = None, use_llm: bool = True) -> int:
    """后台任务按名跑一次(与 core.worker 的常驻循环同一段代码;架构方案第 4 节"命令行")。"""
    from core.jobs import collect, common, countries, enrich, expire, reports, stage_log, suggest

    if not job:
        raise SystemExit("run 要给任务名:collect | suggest | enrich | stage-log | reports | countries | expire")
    ctx = common.connect(profile, today, use_llm=use_llm)
    if job == "collect":
        res = collect.run(ctx, only=only, source=source)
        print(res.text())
        return 0
    if job == "suggest":
        print(f"  建议与更新商机:{suggest.run_recent(ctx)}")
        return 0
    if job == "enrich":
        print(f"  补全上传的线索:{enrich.run(ctx) or '没有待补全的'}")
        return 0
    if job == "stage-log":
        print(f"  阶段变更记跟进:{stage_log.run(ctx) or '没有新的阶段变更'}")
        return 0
    if job == "reports":
        print(f"  生成报告:{reports.run(ctx) or '没有待生成的'}")
        return 0
    if job == "countries":
        print(f"  国别信息:{countries.run(ctx, refresh=True)}")
        return 0
    if job == "expire":
        print(f"  过期节点改已发生:{expire.run(ctx) or '没有过期的'}")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osint-water")
    ap.add_argument("cmd", choices=["ingest", "show", "ledger", "report", "watch", "autoledger", "digest", "brief", "teable",
                                    "commit-data", "ledger-leads", "run"])
    ap.add_argument("job", nargs="?", choices=["collect", "suggest", "enrich", "stage-log", "reports", "countries", "expire"],
                    help="run:后台任务(新库;机器账号令牌 TEABLE_TOKEN)。collect 采集与判定(含建议与更新商机);suggest 只对最近判定的线索做建议;"
                         "enrich 补全上传的线索;stage-log 阶段变更记跟进;reports 生成报告表里待生成的报告;countries 刷新国别信息")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--only", help="只跑一个源")
    ap.add_argument("--country", help="ISO3;ledger 不给则处理全部已配置台账的国别")
    ap.add_argument("--today", help="YYYY-MM-DD,默认 UTC 今天(用于复现某一期)")
    ap.add_argument("--out", default="reports/out", help="报告输出目录")
    ap.add_argument("--docx", action="store_true", help="同时出 Word")
    ap.add_argument("--public", action="store_true", help="只含公开事实(不含 overlay、资格结论与最晚启动日期)")
    ap.add_argument("--no-llm", action="store_true", help="watch/brief 不调用模型(只用规则;报告不写分析段落)")
    ap.add_argument("--type", default="country", choices=["country", "sector", "brief"], help="brief 的报告类型")
    ap.add_argument("--sectors", help="brief:领域,逗号分隔(water_supply,wastewater,hydraulic,desalination,stormwater,environment)")
    ap.add_argument("--days", type=int, default=30, help="brief:最近多少天的线索")
    ap.add_argument("--view", help="brief:用界面上保存的筛选")
    ap.add_argument("--url", help="teable:实例地址,如 https://water.example.uk(默认 TEABLE_URL)")
    ap.add_argument("--token", help="teable:个人访问令牌(默认 TEABLE_TOKEN;不要写进命令历史,优先用 .env)")
    ap.add_argument("--message", help="commit-data:提交说明")
    ap.add_argument("--pull", action="store_true", help="teable:把同事在表格里改的人管字段拉回系统(profile 的 teable.pull 打开时)")
    ap.add_argument("--after-restore", metavar="备份时间",
                    help="teable:表格刚从备份恢复,在下一次拉回之前把备份以后同步过的记录改回上次同步时的值(如 2026-10-05T03:30+08:00)")
    a = ap.parse_args(argv)
    profile = load_profile(a.profile)
    if profile.data_dir:  # 私有 profile 的快照与 demo 分开放,互不覆盖
        storage.DATA_DIR = profile.data_dir
    today = date.fromisoformat(a.today) if a.today else datetime.now(UTC).date()
    print(f"profile: {profile.name} · {len(profile.countries)} 国 · data: {storage.DATA_DIR}")
    if a.cmd == "run":
        return run_job(profile, a.job, today, only=a.country, source=a.only, use_llm=not a.no_llm)
    if a.cmd == "ingest":
        return 1 if ingest(profile, a.only) else 0
    if a.cmd == "ledger":
        return ledger(profile, a.country, today)
    if a.cmd == "watch":
        return watch(profile, a.country, today, use_llm=not a.no_llm)
    if a.cmd == "autoledger":
        return autoledger(profile, today)
    if a.cmd == "digest":
        return digest(profile, a.country, today)
    if a.cmd == "brief":
        return brief(profile, a, today)
    if a.cmd == "report":
        return report(profile, a.country, today, Path(a.out), a.docx, a.public)
    if a.cmd == "teable":
        return teable(profile, today, a.url, a.token, pull=a.pull, after_restore=a.after_restore)
    if a.cmd == "commit-data":
        return commit_data(a.message or f"watch: {today.isoformat()}")
    if a.cmd == "ledger-leads":  # 正式商机的台账出处补成线索(core.ledger_leads;服务器上与表格同步时自动做)
        from core import ledger_leads
        from core.watch.store import Store

        added = ledger_leads.backfill(profile, Store(storage.DATA_DIR), today=today)
        print(f"正式商机的台账出处补成线索 {len(added)} 条")
        return 0
    show(profile)
    return 0


def teable(profile: Profile, today: date, url: str | None, token: str | None, pull: bool = False,
           after_restore: str | None = None) -> int:
    """线索与台账同步到 Teable(本机改了、表格没人改过的人管字段一并写回);pull:把表格里改的人管字段拉回系统
    (profile 的 teable.pull 没打开时跳过);after_restore(备份时间):表格刚从备份恢复,先对齐到上次同步(core.teable.after_restore)。
    没配地址或令牌时提示并返回 2(定时任务里视为跳过)。profile 的 teable.server_sync 打开时表格只由服务器上的系统同步
    (core.live_sync):定时任务里的拉回、推送跳过,对齐照常做。"""
    import httpx

    from core import sync as git
    from core import teable as tb
    from core.watch.store import Store

    env_url, env_token = tb.settings()
    url, token = url or env_url, token or env_token
    if not url or not token:
        print("没有 TEABLE_URL / TEABLE_TOKEN(环境变量或程序目录 .env),跳过 Teable 同步")
        return 2
    if after_restore:
        try:
            at = datetime.fromisoformat(after_restore.strip())
            if at.tzinfo is None:
                raise ValueError
        except ValueError:
            print(f"备份时间认不出:{after_restore}(要带时区,如 2026-10-05T03:30+08:00)")
            return 1
        try:
            tb.after_restore(profile, Store(storage.DATA_DIR), at, url, token)
        except (tb.TeableError, httpx.HTTPError) as e:
            print(f"Teable 对齐失败:{e}")
            return 1
        return 0
    if (profile.teable or {}).get("server_sync") and os.environ.get("GITHUB_ACTIONS") == "true":
        print("表格由服务器上的系统同步(profile 的 teable.server_sync,每 30 秒一次):定时任务不拉回、不推送;"
              "这次的新线索、自动入账,系统取到 data 分支以后几分钟内推到表格")
        return 0
    if pull and not (profile.teable or {}).get("pull"):
        print("profile 的 teable.pull 没打开,不拉回表格里的改动")
        return 0
    try:
        if pull:  # 定时任务:系统身份,推送失败退回、下次再试;本机:界面身份,提交留在本机、联网后推送
            bot = os.environ.get("GITHUB_ACTIONS") == "true"
            r = tb.pull(profile, Store(storage.DATA_DIR), today, url, token, identity=git.BOT_ID if bot else git.APP_ID,
                        keep_unpushed=not bot)
            print(f"完成:线索 {r['leads']} 条、项目 {r['projects']} 个,写成操作记录 {r['events']} 条,警告 {len(r['warnings'])} 条")
            return 0
        r = tb.sync(profile, Store(storage.DATA_DIR), today, url, token)
    except (tb.TeableError, httpx.HTTPError) as e:
        print(f"Teable {'拉回' if pull else '同步'}失败:{e}")
        return 1
    print(f"完成:项目 {r['projects']} 条,线索 {r['leads']} 条,写回本机的改动 {r['written_back']} 条,警告 {len(r['warnings'])} 条 → {r['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
