"""后台任务的常驻循环(架构方案第 4 节):主机 compose 里的 worker 容器跑它;开发时在本机连同一个库手动跑单个任务用 core.cli run。

    python -m core.worker --profile private/profile.yaml            # 常驻
    python -m core.worker --profile private/profile.yaml --once     # 把到点的任务跑一遍就退出(调试)

节奏(北京时间,profile 的 teable_map timezone):
- 每 2 分钟:补全上传的线索(enrich)、阶段变更记跟进(stage_log)、生成待生成的报告(reports)、线索补中文标题(leads_zh)
- 每天 08:30:采集与判定 + 建议与更新商机(collect);当天已跑过(信源表里最近采集是今天)就不再跑——重启不重复采;
  采集完成后自建当天的日报行(reports.ensure_daily,下个周期生成,正文显示在「线索分析」页顶部);
  随后补新国别的国别信息(countries,只补空着的);每月 1 日按期刷新全部国别
- 周一 09:00 自建周报、每月 1 日 09:30 自建月报(报告表里同期间已有就不建),随后由 reports 生成
同一进程里另起一个线程跑助手接口(core/assistant_server.py,门户「助手」页的提问解析;端口 ASSISTANT_PORT,缺省 8765,0 = 不起)。
后台无状态:上次做到哪从新库读;启动时把超过 30 分钟仍「生成中」的报告改回「待生成」。出错只记日志、下个周期再试;模型没配时只用规则初筛。
采集成功后 ping 一下 HEALTHCHECK_URL(dead-man's switch,配了才 ping;一天没 ping 对方发邮件)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx

from core import assistant_server, semantic, storage, tstore
from core.jobs import awards, bready, cities, collect, common, countries, enrich, expire, fx, laws, leads_zh, plants, reports, risk, stage_log, studies
from core.profile import load_profile

EVERY = 120  # 秒
COLLECT_AT = dtime(8, 30)


def log(msg: str) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}", flush=True)


def collected_today(ctx: common.Ctx, today: date) -> bool:
    """信源表里有信源的最近采集是今天(本地时区)的:今天已经采过。"""
    for r in ctx.sources().values():
        d = tstore.day(ctx.base.get("source", r, "last_run"), ctx.base.tz)
        if d == today.isoformat():
            return True
    return False


def ping(url: str | None) -> None:
    if not url:
        return
    try:
        httpx.get(url, timeout=10)
    except httpx.HTTPError as e:
        log(f"告警心跳没发出去:{type(e).__name__}")


def tick(profile, now: datetime, state: dict, use_llm: bool = True) -> None:
    """一个周期:到点的任务各跑一次。state 记今天采集过没有(进程内;重启后从信源表看)。"""
    today = now.date()
    ctx = common.connect(profile, today, use_llm=use_llm, log=log)
    def _reports():
        out = reports.ensure_periodic(ctx, now)
        out.update(reports.run(ctx))
        return out

    if state.get("expired") != today.isoformat():  # 每天第一个周期:过期节点改「已发生」,让含 TODAY 的公式重算(Teable 隔天不自动算)
        try:
            out = expire.run(ctx)
            out.update(expire.archive_leads(ctx))  # 过期线索标「已归档」(A49)
            state["expired"] = today.isoformat()
            if out:
                log(f"expire:{out}")
        except Exception as e:  # noqa: BLE001
            log(f"expire 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
    for name, fn in (("enrich", lambda: enrich.run(ctx)), ("stage_log", lambda: stage_log.run(ctx)), ("fx", lambda: fx.run(ctx)), ("reports", _reports),
                     ("awards_zh", lambda: awards.translate(ctx.base, ctx.clf_light, log=log)),  # 已中标项目译中文名(A28;不思考的便宜模式,没配模型时什么也不做)
                     ("studies_zh", lambda: studies.translate(ctx.base, ctx.clf_light, log=log)),  # 研究资料译中文标题(同上)
                     ("leads_zh", lambda: leads_zh.translate(ctx, log=log)),  # 线索补中文标题(A03;老线索一批 40 条,原题是中文的照抄)
                     ("laws_zh", lambda: laws.translate(ctx.base, ctx.clf_light, log=log)),
                     ("phrases", lambda: semantic.fill(ctx.base, log=log))):  # 语义层说法:本体里有、表里没有的指向补一行(A43)  # 法规条文译中文(A37;同上,一批约 5000 字)
        try:
            out = fn()
            if out:
                log(f"{name}:{out}")
        except Exception as e:  # noqa: BLE001  单个任务出错不影响其余
            log(f"{name} 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
    if now.time() >= COLLECT_AT and state.get("collected") != today.isoformat():
        if collected_today(ctx, today):
            state["collected"] = today.isoformat()
        else:
            try:
                res = collect.run(ctx)
                state["collected"] = today.isoformat()
                log("collect:\n" + res.text())
                ping(os.environ.get("HEALTHCHECK_URL"))
            except Exception as e:  # noqa: BLE001
                log(f"collect 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
    if state.get("collected") == today.isoformat() and state.get("daily") != today.isoformat():  # 采集完了:建当天的日报行(E10)
        state["daily"] = today.isoformat()  # 失败也只试一次,明天再来(原因在日志;报告表选项没补时先跑 python -m core.schema)
        try:
            out = reports.ensure_daily(ctx, today)
            if out:
                log(f"daily:{out}")
        except Exception as e:  # noqa: BLE001
            log(f"daily 出错(今天不再试;报告表「类型」里没有「日报」选项时先跑 python -m core.schema 补选项):{type(e).__name__}: {e}")
    if now.time() >= COLLECT_AT and state.get("countries") != today.isoformat():
        state["countries"] = today.isoformat()
        try:
            out = countries.run(ctx, refresh=(today.day == 1), only_missing=(today.day != 1))
            if out:
                log(f"countries:{out}")
        except Exception as e:  # noqa: BLE001
            log(f"countries 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        try:  # 规模以上城市(A22):每月 1 日全量,其余日子只补还没有城市的国别
            out = cities.run(ctx, refresh=(today.day == 1), only_missing=(today.day != 1))
            if out:
                log(f"cities:{out}")
        except Exception as e:  # noqa: BLE001
            log(f"cities 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        try:  # 水风险与经济数据(A26):城市维护之后补新城市、新国别(只填空)
            out = risk.run(ctx.base, log=log)
            if any(out.values()):
                log(f"risk:{out}")
        except Exception as e:  # noqa: BLE001
            log(f"risk 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        try:  # 营商环境(A36,世行 B-READY):新国别跟着补;数据文件换了新一期整组替换
            out = bready.run(ctx.base, log=log)
            if any(out.values()):
                log(f"bready:{out}")
        except Exception as e:  # noqa: BLE001
            log(f"bready 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        if today.day == 1:
            try:  # 存量水厂(A25):每月 1 日城市维护之后跑一遍,补新数据与新城市的关联(只新建、只填空)
                out = plants.run(ctx.base, log=log)
                if out:
                    log(f"plants:{out}")
            except Exception as e:  # noqa: BLE001
                log(f"plants 出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="后台任务常驻循环")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--once", action="store_true", help="跑一个周期就退出")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--every", type=int, default=EVERY, help="周期(秒)")
    ap.add_argument("--no-assistant", action="store_true", help="不起助手接口")
    a = ap.parse_args(argv)
    profile = load_profile(a.profile)
    if profile.data_dir:
        storage.DATA_DIR = profile.data_dir
    from core import ontology
    tz = ZoneInfo(ontology.load_map().get("timezone") or "Asia/Shanghai")
    state: dict = {}
    log(f"worker 启动:profile {profile.name},每 {a.every} 秒一个周期,采集 {COLLECT_AT:%H:%M}")
    if not a.no_assistant:
        assistant_server.start_in_thread(profile, int(os.environ.get("ASSISTANT_PORT") or assistant_server.DEFAULT_PORT),
                                         use_llm=not a.no_llm, tz=str(tz), log=log)
    try:
        n = reports.reset_stale(common.connect(profile, datetime.now(tz).date(), use_llm=False, log=log))
        if n:
            log(f"启动:{n} 份卡在「生成中」的报告改回「待生成」")
    except Exception as e:  # noqa: BLE001
        log(f"启动检查没做成(下个周期照常):{type(e).__name__}: {e}")
    while True:
        try:
            tick(profile, datetime.now(tz), state, use_llm=not a.no_llm)
        except tstore.TStoreError as e:
            log(f"连不上表格:{e}")
        except Exception as e:  # noqa: BLE001
            log(f"周期出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        if a.once:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
