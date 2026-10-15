"""助手接口:一个很小的 HTTP 服务,跑在后台任务(worker)进程里的一个线程上,只在容器内网监听;Caddy 把门户的
/portal/api/assistant/* 转过来(portal.caddy),前面照样有「先问表格这是谁」的登录检查——没登录的请求到不了这里。

    POST /ask   {"q": "最近 7 天埃及有哪些新线索"}  →  core.assistant.plan 的计划(JSON)
    POST /link  {"url": "https://…", "note": "用户附言"} →  core.link_check.check 的分析(读原文、信源、库里相关、旁证、入库建议);
                                                       游客不能用(要查真实的库)
    POST /feedback {"key": "Q…", "verdict": "答对了" | "答错了", "note": "哪里不对", "result": "答出了什么", "q", "prev_q", "understood"}
                                                    →  core.ask_log.feedback:记在「助手提问记录」这一行(/ask 返回的 log_key);游客不能用
    POST /law   {"q": "…", "law": 计划里的 law, "countries": ["MAR"]}  →  core.law.answer:按国别与关键词查法规条文,配了模型的按条文作答(带出处序号);
                                                       游客不能用
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
import base64
import json
import sys
import threading
import time
import traceback
from collections import OrderedDict
from datetime import date, datetime
from hmac import compare_digest as hmac_compare
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from core import ask_log, assistant, calendar_feed, guest as guest_mod, law, link_check, tstore
from core.jobs import common
from core.profile import Profile, load_profile
from core.watch.llm import Classifier, LLMConfig

DEFAULT_PORT = 8765
UPLOAD_JSON = 17_000_000  # 「看链接」带文件时的请求体上限:12 MB 文件转 base64 后约 16 MB
FORGIVE_AFTER = 600  # 秒:模型连续出错停用后,过这么久再试
TERMS_EVERY = 60  # 秒:隔这么久重读一次 Teable「行业术语」表(人在表里加的词下一个提问就用上)
RECHECK_AT = (8, 40)  # 本地时间:每天这之后复查一遍错题本(系统 08:00 自动更新之后)


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
        if profile is not None:  # 问金额时按后台当天的汇率折人民币
            assistant.RATES_FILE = common.cache_root(profile) / "fx" / "fx.json"
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
        self.law_lib = law.Library(self.db) if self.terms_base is not None else None  # 法规条文(真实库;十分钟内不重读)
        self.rechecked: str | None = None  # 错题本复查过的那天
        self.who = ask_log.who  # 登录 cookie → 提问人(测试可换)
        self.raws: OrderedDict[str, dict] = OrderedDict()  # 提问编号 → 那次模型给的解析(JSON);用户对当场重答点了「答对了」时记进错题本(只留最近 1000 问)
        self._raws_lock = threading.Lock()

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

    def check_link(self, url: str, note: str = "", upload: bytes | None = None) -> dict:
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
            return link_check.check(url, note, self.today, self.clf, db, upload=upload, **self.link_kw)
        except link_check.LinkError as e:
            return {"ok": False, "error": str(e)}

    def refresh_terms(self) -> None:
        """隔 TERMS_EVERY 秒重读一次「行业术语」「语义层说法」两张表;表没建、读不到时用本体的初始词,不影响提问。"""
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
            # 语义层说法(指标、算法、主体工程、法规问法、排放指标的口语):表没建时用本体的
            assistant.set_phrases([{p: b.get("phrase", r, p) for p in ("target", "kind", "words")} for r in b.rows("phrase")] if "phrase" in b.t else None)
            assistant.set_learned(ask_log.learned_rows(b))  # 错题本里学会的(人改成「不改」或清空「学到的解析」的不再用)
        except Exception as e:  # noqa: BLE001  读表出错:沿用上一次读到的(或本体初始词),下次重连
            self._terms_db = None
            self.log(f"读行业术语表出错,沿用上一次的:{type(e).__name__}: {e}")

    def law_answer(self, q: str, law_plan: dict, countries: list[str]) -> dict:
        """法规问题按条文作答(core/law.py);没给国别的查收了原文的各国。"""
        self._maybe_forgive()
        self.refresh_terms()
        if self.law_lib is None:
            return {"ok": False, "error": "助手服务没连表格"}
        if not countries:
            b = self.db()
            if "law" not in b.t:
                return {"ok": True, "answer": "", "cites": [], "searched": {}, "missing": [], "note": "库里还没有「法规」表"}
            ids = {tstore.link_id(b.get("law", r, "country")) for r in b.rows("law", props=["country"])}
            countries = [str(b.get("country", r, "key")) for r in b.rows("country", props=["key"]) if r["id"] in ids]
        return law.answer(self.law_lib, q, law_plan, countries[:10], self.clf, log=self.log)

    def can_log(self) -> bool:
        """库里建了「助手提问记录」表(没建时不记、页面不给答对答错按钮)。"""
        if self.terms_base is None:
            return False
        try:
            return "ask_log" in self.db().t
        except Exception as e:  # noqa: BLE001
            self._terms_db = None
            self.log(f"连不上表格,这一问不记:{type(e).__name__}: {e}")
            return False

    def log_ask(self, q: str, prev: dict | None, p: dict, cookie: str | None, raw: dict | None = None) -> str | None:
        """A44:记这一问(另起线程写表,不拖慢回答)→ 这一行的编号;没建表 → None。raw:模型给的解析,留着备用户认可重答时记进错题本。"""
        if not q.strip() or not self.can_log():
            return None
        now = datetime.now(self.tz).replace(tzinfo=None)
        key = ask_log.new_key(now)
        if isinstance(raw, dict):
            with self._raws_lock:
                self.raws[key] = raw
                while len(self.raws) > 1000:
                    self.raws.popitem(last=False)
        prev_q = str((prev or {}).get("q") or "") if any(x[0] == "沿用上一问" for x in p.get("understood") or []) else ""

        def run():
            try:
                ask_log.record(self.db(), key, q, prev_q, p, self.who(cookie), now)
            except Exception as e:  # noqa: BLE001
                self.log(f"记提问出错:{type(e).__name__}: {e}")
        threading.Thread(target=run, name="ask-log", daemon=True).start()
        return key

    def maybe_recheck(self) -> dict | None:
        """A46:每天本地 08:40 以后复查一遍错题本(一天一次;出错只记日志,明天再来)。"""
        now = datetime.now(self.tz)
        if self.terms_base is None or (now.hour, now.minute) < RECHECK_AT or self.rechecked == now.date().isoformat():
            return None
        self.rechecked = now.date().isoformat()
        try:
            self.refresh_terms()
            return ask_log.recheck(self.db(), now.date(), self.clf, log=self.log)
        except Exception as e:  # noqa: BLE001
            self.log(f"错题本复查出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            return None

    def learn(self, key: str, fixes: str, q: str, prev_q: str, understood: str) -> bool:
        """用户对当场重答点了「答对了」:原来那一问(fixes)记下这次重答的解析,马上生效(不等重读表)。"""
        with self._raws_lock:
            raw = self.raws.get(key)
        if not raw:
            return False
        prev_q = prev_q if "沿用上一问" in understood else ""  # 没沿用上一问的,学成单问(同记提问 A44)
        assistant.add_learned(q, prev_q, raw)
        try:
            return ask_log.learn(self.db(), fixes, raw, understood)
        except Exception as e:  # noqa: BLE001  表没写上:这次进程里已经学会,重启后要重教
            self.log(f"错题本记「学到的解析」出错:{type(e).__name__}: {e}")
            return False

    def ask(self, q: str, prev: dict | None = None, fix: dict | None = None) -> dict:
        """prev:页面带来的上一问的计划(接着问时沿用对象、条件;assistant.plan 逐项校验)。fix:答错了以后按说明当场重答({understood, note})。
        返回的计划里 _raw 是模型给的解析(接口不发给页面,见 do_POST)。"""
        self._maybe_forgive()
        self.refresh_terms()
        try:
            p = assistant.plan(q, self.today, self.clf, prev, fix)
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
            if path not in ("/ask", "/link", "/law", "/feedback"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > (UPLOAD_JSON if path == "/link" else 20_000):
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
                    upload = base64.b64decode(str(data.get("file") or ""), validate=True) if data.get("file") else None
                except ValueError:
                    self._send(400, {"ok": False, "error": "文件没传完整,请重新选一次"})
                    return
                try:
                    out = bot.check_link(str(data.get("url") or ""), str(data.get("note") or "")[:link_check.DOC_CHARS], upload)  # 网站不让读时附言就是正文
                except Exception as e:  # noqa: BLE001
                    bot.log(f"看链接出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
                    out = {"ok": False, "error": f"分析出错({type(e).__name__})"}
                self._send(200, out)
                return
            if path == "/feedback":
                if self.headers.get("X-Portal-Auth") != "1" and not bot.allow_link_without_auth:
                    self._send(403, {"ok": False, "error": "请先登录"})
                    return
                if self._outsider():
                    self._send(403, {"ok": False, "error": "游客帐号不能反馈"})
                    return
                if not bot.can_log():
                    self._send(200, {"ok": False, "error": "库里还没有「助手提问记录」表"})
                    return
                info = {k: str(data.get(k) or "")[:2000] for k in ("q", "prev_q", "understood")}
                try:
                    out = ask_log.feedback(bot.db(), str(data.get("key") or "")[:40], str(data.get("verdict") or ""), str(data.get("note") or ""),
                                           str(data.get("result") or ""), info)
                    if out.get("ok") and data.get("verdict") == "答对了" and data.get("fixes"):  # 对当场重答点的「答对了」:原来那一问学会这次的理解
                        out["learned"] = bot.learn(str(data.get("key") or "")[:40], str(data["fixes"])[:40], info["q"], info["prev_q"], info["understood"])
                except ValueError as e:
                    out = {"ok": False, "error": str(e)}
                except Exception as e:  # noqa: BLE001
                    bot.log(f"记反馈出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
                    out = {"ok": False, "error": f"没记上({type(e).__name__})"}
                self._send(200, out)
                return
            if path == "/law":
                if self.headers.get("X-Portal-Auth") != "1" and not bot.allow_link_without_auth:
                    self._send(403, {"ok": False, "error": "请先登录"})
                    return
                if self._outsider():
                    self._send(403, {"ok": False, "error": "游客帐号不能查法规条文"})
                    return
                lp = data.get("law") if isinstance(data.get("law"), dict) else {}
                isos = [c for c in (data.get("countries") or [])[:10] if isinstance(c, str) and assistant.geo.iso3(c) == c]
                try:
                    out = bot.law_answer(q[:assistant.MAX_Q], {"kind": "text", "keywords": [str(k)[:40] for k in (lp.get("keywords") or [])[:8]]}, isos)
                except Exception as e:  # noqa: BLE001
                    bot.log(f"法规作答出错:{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
                    out = {"ok": False, "error": f"查条文出错({type(e).__name__})"}
                self._send(200, out)
                return
            prev = data.get("prev") if isinstance(data.get("prev"), dict) else None
            member = (self.headers.get("X-Portal-Auth") == "1" or bot.allow_link_without_auth) and not self._outsider()
            fx_ = data.get("fix") if isinstance(data.get("fix"), dict) and member else None  # 当场重答只给登录的内部帐号
            fix = {"understood": str(fx_.get("understood") or "")[:600], "note": str(fx_.get("note") or "")[:500]} if fx_ and str(fx_.get("note") or "").strip() else None
            out = bot.ask(q, prev, fix)
            raw = out.pop("_raw", None)
            out["ok"] = True
            if fix:
                out["refixed"] = True
            if member:  # 游客的提问不记
                out["log_key"] = bot.log_ask(q, prev, out, self.headers.get("Cookie"), raw)
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

    def daily():  # 错题本复查(A46):十分钟看一次到没到点
        while True:
            bot.maybe_recheck()
            time.sleep(600)
    threading.Thread(target=daily, name="ask-recheck", daemon=True).start()
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
