"""助手接口:一个很小的 HTTP 服务,跑在后台任务(worker)进程里的一个线程上,只在容器内网监听;Caddy 把门户的
/portal/api/assistant/* 转过来(portal.caddy),前面照样有「先问表格这是谁」的登录检查——没登录的请求到不了这里。

    POST /ask   {"q": "最近 7 天埃及有哪些新线索"}  →  core.assistant.plan 的计划(JSON)
    POST /link  {"url": "https://…", "note": "用户附言"} →  core.link_check.check 的分析(读原文、信源、库里相关、旁证、入库建议);
                                                       游客不能用(要查真实的库)
    GET  /health                                    →  {"ok": true, "llm": "deepseek-v4-pro(DeepSeek)" | null, "note": "…"}
    GET  /calendar.ics?country=AZE&opp=AZE-003      →  日历文件(core/calendar_feed.py):登录后经 Caddy 来的带 X-Portal-Auth: 1;
                                                       订阅程序经 /portal/feed/calendar.ics?t=<令牌> 来(Caddy 不做登录检查),令牌不对 403
    GET  /feed-link                                 →  {"url": "webcal://…/portal/feed/calendar.ics?t=…"}(给日历页「订阅到日历」用)
    GET  /guest/policy、/guest/table/{表}/record[/{记录}]、/guest/table/{表}/aggregation/row-count
                                                    →  游客只读通道(core/guest.py):不在库里的登录账号读公开内容;只认登录后经 Caddy 来的

单独跑(调试):python -m core.assistant_server --profile private/profile.yaml --port 8765
环境变量 ASSISTANT_PORT 改端口(0 = 不起);密钥与采集判定共用(DEEPSEEK_API_KEY / ANTHROPIC_API_KEY)。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from datetime import date, datetime
from hmac import compare_digest as hmac_compare
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from core import assistant, calendar_feed, guest as guest_mod, link_check, tstore
from core.profile import Profile, load_profile
from core.watch.llm import Classifier, LLMConfig

DEFAULT_PORT = 8765
FORGIVE_AFTER = 600  # 秒:模型连续出错停用后,过这么久再试
TERMS_EVERY = 60  # 秒:隔这么久重读一次 Teable「行业术语」表(人在表里加的词下一个提问就用上)


class Assistant:
    """进程内的助手:持有模型接入(可为 None),按本地时区算「今天」。线程安全(Classifier 自带锁)。"""

    def __init__(self, profile: Profile | None, use_llm: bool = True, tz: str = "Asia/Shanghai", log=print, feed: calendar_feed.Feed | None = None,
                 guest: guest_mod.Guest | None = None):
        self.tz = ZoneInfo(tz)
        self.log = log
        # 日历订阅、游客只读通道:有 profile(后台任务进程里)就连库;单独跑或测试时可注入
        self.feed = feed if feed is not None else (calendar_feed.Feed(tz=tz, log=log) if profile is not None else None)
        self.guest = guest if guest is not None else (guest_mod.Guest(log=log) if profile is not None else None)
        # 游客(库外的登录账号)下载日历:示例数据(本体 guest.data: sample 时游客通道的库就是示例库)
        self.guest_feed = calendar_feed.Feed(base_factory=lambda: self.guest.base(), tz=tz, log=log) if self.guest is not None else None
        self.clf: Classifier | None = None
        if profile is not None and use_llm:
            cfg = LLMConfig.for_assistant((profile.watch or {}).get("llm"), (getattr(profile, "assistant", None) or {}).get("llm"))
            self.clf = Classifier(cfg)
        self._disabled_at: float | None = None
        self.allow_link_without_auth = False  # 测试用:本机小服务没有 Caddy 加的登录标头
        self.feed_token: str | None = None  # 测试用:指定订阅令牌;None 则按环境变量 / 机器令牌派生(calendar_feed.feed_token)
        # 行业术语表(真实库,不走游客的示例库):有 profile 才连;单独跑或测试时可注入 terms_base
        self.terms_base = (lambda: tstore.Base(tstore.Client(*tstore.credentials(prompt=False)))) if profile is not None else None
        self._terms_db: tstore.Base | None = None
        self._terms_at: float | None = None
        self._terms_db_at = 0.0
        self.link_kw: dict = {}  # 测试用:注入 fetch / outside

    @property
    def today(self) -> date:
        return datetime.now(self.tz).date()

    def llm_label(self) -> str | None:
        return self.clf.label if self.clf is not None and self.clf.client is not None else None

    def note(self) -> str:
        if self.clf is None:
            return "未启用模型,按词表解析"
        if self.clf.client is None:
            return self.clf.note
        if self.clf.fatal:
            return self.clf.fatal
        if not self.clf.enabled:
            return "模型连续出错,暂按词表解析;稍后自动再试:" + (self.clf.errors[-1] if self.clf.errors else "")
        return ""

    def _maybe_forgive(self) -> None:
        if self.clf is None or self.clf.enabled or self.clf.fatal:
            self._disabled_at = None
            return
        now = time.monotonic()
        if self._disabled_at is None:
            self._disabled_at = now
        elif now - self._disabled_at >= FORGIVE_AFTER:
            self.clf.forgive()
            self._disabled_at = None

    def db(self) -> tstore.Base:
        """真实库的连接(行业术语、看链接时查库共用);每 10 分钟重连一次:表、字段是后来才建的也认得到。"""
        now = time.monotonic()
        if self._terms_db is None or now - self._terms_db_at > 600:
            self._terms_db, self._terms_db_at = self.terms_base(), now
        return self._terms_db

    def check_link(self, url: str, note: str = "") -> dict:
        """看一条用户贴来的链接(core/link_check.py);抓不到、不让抓时给原因。查库出错照样给其余部分。"""
        self._maybe_forgive()
        db = None
        if self.terms_base is not None:
            try:
                db = self.db()
            except Exception as e:  # noqa: BLE001
                self._terms_db = None
                self.log(f"看链接时连不上表格:{type(e).__name__}: {e}")
        try:
            return link_check.check(url, note, self.today, self.clf, db, **self.link_kw)
        except link_check.LinkError as e:
            return {"ok": False, "error": str(e)}

    def refresh_terms(self) -> None:
        """隔 TERMS_EVERY 秒重读一次「行业术语」表;表没建、读不到时用本体的初始词,不影响提问。"""
        if self.terms_base is None:
            return
        now = time.monotonic()
        if self._terms_at is not None and now - self._terms_at < TERMS_EVERY:
            return
        self._terms_at = now
        try:
            b = self.db()
            if "term" not in b.t:
                assistant.set_terms(None)
                return
            props = ("term", "aliases", "covers", "sectors", "match", "means", "disabled")
            assistant.set_terms([{p: b.get("term", r, p) for p in props if b.has("term", p)} for r in b.rows("term")])
        except Exception as e:  # noqa: BLE001  读表出错:沿用上一次读到的(或本体初始词),下次重连
            self._terms_db = None
            self.log(f"读行业术语表出错,沿用上一次的:{type(e).__name__}: {e}")

    def ask(self, q: str) -> dict:
        self._maybe_forgive()
        self.refresh_terms()
        try:
            p = assistant.plan(q, self.today, self.clf)
        except Exception as e:  # noqa: BLE001  解析出错也要给页面一个能用的计划
            self.log(f"助手解析出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            p = assistant.rules_parse(q, self.today) if q.strip() else assistant.finish(assistant.empty_plan())
            p["note"] = f"解析出错,按词表解析({type(e).__name__})"
        if not p.get("note"):
            p["note"] = self.note()
        p["examples"] = assistant.EXAMPLES
        return p


def make_handler(bot: Assistant):
    class H(BaseHTTPRequestHandler):
        server_version = "osint-water-assistant"

        def log_message(self, fmt, *args):  # 不刷访问日志;出错在 _send 里记
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _outsider(self) -> bool:
            """登录了、但不是库里的人(游客)。没有游客通道(单独跑、测试)时不算。"""
            if bot.guest is None:
                return False
            return bot.guest.is_outsider(self.headers.get("Cookie"))

        def _site(self) -> str:
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            proto = self.headers.get("X-Forwarded-Proto") or "https"
            return f"{proto}://{host}" if host else ""

        def _calendar(self, qs: dict) -> None:
            if bot.feed is None:
                self._send(503, {"ok": False, "error": "日历订阅未启用"})
                return
            token = bot.feed_token if bot.feed_token is not None else calendar_feed.feed_token()
            given = (qs.get("t") or [""])[0]
            logged_in = self.headers.get("X-Portal-Auth") == "1"
            if not logged_in and not (token and given and hmac_compare(given, token)):
                self._send(403, {"ok": False, "error": "订阅令牌不对"})
                return
            feed = bot.feed
            if logged_in and self._outsider():
                if bot.guest_feed is None:
                    self._send(403, {"ok": False, "error": "游客帐号不能下载日历"})
                    return
                feed = bot.guest_feed
            try:
                text = feed.ics(bot.today, (qs.get("country") or [None])[0], (qs.get("opp") or [None])[0], self._site())
            except Exception as e:  # noqa: BLE001
                bot.log(f"日历订阅出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
                self._send(502, {"ok": False, "error": f"读不到表格:{type(e).__name__}"})
                return
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="water-osint.ics"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            self.wfile.write(body)

        def _guest(self, path: str, qs: dict) -> None:
            """游客只读通道:只认登录后经 Caddy 来的请求(X-Portal-Auth: 1);表、字段越界 403;读表出错 502。"""
            if self.headers.get("X-Portal-Auth") != "1" and not bot.allow_link_without_auth:
                self._send(403, {"ok": False, "error": "请先登录"})
                return
            if bot.guest is None:
                self._send(503, {"ok": False, "error": "游客通道未启用"})
                return
            parts = path.split("/")[2:]  # ["policy"] | ["table", 表, "record"[, 记录]] | ["table", 表, "aggregation", "row-count"]
            try:
                if parts == ["policy"]:
                    out: dict | None = bot.guest.policy().public()
                elif len(parts) == 3 and parts[0] == "table" and parts[2] == "record":
                    out = bot.guest.records(parts[1], qs)
                elif len(parts) == 4 and parts[0] == "table" and parts[2] == "record":
                    out = bot.guest.record(parts[1], parts[3])
                    if out is None:
                        self._send(404, {"ok": False, "error": "没有这条记录"})
                        return
                elif len(parts) == 4 and parts[0] == "table" and parts[2:] == ["aggregation", "row-count"]:
                    out = bot.guest.count(parts[1], qs)
                else:
                    self._send(404, {"ok": False, "error": "not found"})
                    return
            except guest_mod.Denied as e:
                self._send(403, {"ok": False, "error": str(e)})
                return
            except Exception as e:  # noqa: BLE001
                bot.log(f"游客通道出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
                self._send(502, {"ok": False, "error": f"读不到表格:{type(e).__name__}"})
                return
            self._send(200, out)

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            qs = parse_qs(query)
            if path.startswith("/guest/"):
                self._guest(path, qs)
            elif path in ("/health", "/"):
                self._send(200, {"ok": True, "llm": bot.llm_label(), "note": bot.note(), "examples": assistant.EXAMPLES})
            elif path == "/calendar.ics":
                self._calendar(qs)
            elif path == "/feed-link":
                # 只给登录后经 Caddy 来的请求(订阅路由不转这个路径);令牌派生不了(没有机器令牌)时说明原因
                if self.headers.get("X-Portal-Auth") != "1" and not bot.allow_link_without_auth:
                    self._send(403, {"ok": False, "error": "请先登录"})
                    return
                if self._outsider():
                    self._send(200, {"ok": False, "error": "游客帐号不能订阅日历(订阅链接给的是真实数据);可以用「下载 .ics」看示例"})
                    return
                token = bot.feed_token if bot.feed_token is not None else calendar_feed.feed_token()
                if not token or bot.feed is None:
                    self._send(200, {"ok": False, "error": "后台没有配置日历订阅令牌"})
                    return
                site = self._site()
                extra = "".join(f"&{k}={quote(v[0])}" for k, v in qs.items() if k in ("country", "opp") and v and v[0])
                path_q = f"/portal/feed/calendar.ics?t={token}{extra}"
                host = site.split("://", 1)[1] if "://" in site else ""
                self._send(200, {"ok": True, "url": (f"webcal://{host}" if host else "") + path_q, "https": site + path_q})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path not in ("/ask", "/link"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20_000:
                self._send(413, {"ok": False, "error": "问题太长"})
                return
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                q = str(data.get("q") or "")
            except (ValueError, AttributeError):
                self._send(400, {"ok": False, "error": "请求体不是 JSON"})
                return
            if path == "/link":
                if self.headers.get("X-Portal-Auth") != "1" and not bot.allow_link_without_auth:
                    self._send(403, {"ok": False, "error": "请先登录"})
                    return
                if self._outsider():
                    self._send(200, {"ok": False, "error": "游客帐号不能用这个功能(要查真实的线索库);可以用正式帐号登录后再试"})
                    return
                try:
                    out = bot.check_link(str(data.get("url") or ""), str(data.get("note") or "")[:link_check.DOC_CHARS])  # 网站不让读时附言就是正文
                except Exception as e:  # noqa: BLE001
                    bot.log(f"看链接出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
                    out = {"ok": False, "error": f"分析出错({type(e).__name__})"}
                self._send(200, out)
                return
            out = bot.ask(q)
            out["ok"] = True
            self._send(200, out)

    return H


def serve(bot: Assistant, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(bot))
    srv.daemon_threads = True
    return srv


def start_in_thread(profile: Profile | None, port: int, use_llm: bool = True, tz: str = "Asia/Shanghai", log=print) -> ThreadingHTTPServer | None:
    """后台任务进程里起助手线程;port <= 0 不起。起不来(端口被占等)只记日志,不影响后台任务。"""
    if port <= 0:
        return None
    try:
        bot = Assistant(profile, use_llm=use_llm, tz=tz, log=log)
        srv = serve(bot, port)
    except OSError as e:
        log(f"助手接口没起来(端口 {port}):{e}")
        return None
    threading.Thread(target=srv.serve_forever, name="assistant", daemon=True).start()
    log(f"助手接口已启动:端口 {port},模型 {bot.llm_label() or '未配置(按词表解析)'}")
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="助手接口(单独运行)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)
    profile = load_profile(a.profile)
    from core import ontology
    tz = ontology.load_map().get("timezone") or "Asia/Shanghai"
    bot = Assistant(profile, use_llm=not a.no_llm, tz=tz)
    srv = serve(bot, a.port, host="127.0.0.1")
    print(f"助手接口:http://127.0.0.1:{a.port}/ask,模型 {bot.llm_label() or '未配置'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
