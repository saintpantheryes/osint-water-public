"""阶段 E 只读页面(portal):编号配置从库里只读生成;页面是本地静态文件,只读表格接口;Caddy 先问表格"这是谁"、再从只读挂载提供文件。
有 Playwright 与本地 Chromium 时,再用内存版 Teable 当接口后端把三页真的打开一遍(没有就跳过那部分)。"""
from __future__ import annotations

import os
import pathlib
import tempfile
from datetime import date, timedelta
import json
import re
from pathlib import Path

import pytest
import yaml

from core import ontology, schema, tstore
from tests.teable_fake import FakeTeable

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "portal"
KIT = ROOT / "deploy" / "teable"
ONTO, TMAP = ontology.load(), ontology.load_map()
ONTO_RAW = ontology.raw()
# 菜单(使用方 2026-10-08 定,10-09 加「市场背景」,10-14 水务片区挪进国别概况):首页 + 四组,组内顺序也按这个;「后台数据表格」是表格本身
MENU_SPEC = [("工作原理", ["工作流程", "本体", "信源", "后台数据表格 ↗"]), ("市场背景", ["国别概况", "城市概况", "趋势机会"]), ("项目线索", ["线索总览", "线索分析", "线索处理"]), ("商机跟进", ["商机总览", "日历", "报告"])]
NEW = "水务项目机会 新版"
READ_PAGES = ("calendar.html", "country.html", "city.html", "trend.html", "analysis.html", "ontology.html", "contractor.html", "area.html")  # 只读
WRITE_PAGES = ("index.html", "overview.html", "opps.html", "opp.html", "inbox.html", "reports.html", "sources.html")  # 信源页只改「启用」; 以登录人身份写(跟进、报告申请、阶段、归属;首页只在看完一条链接后按「收进线索库」建一行线索;线索总览只有清单里的「转为商机」)
PAGES = READ_PAGES + WRITE_PAGES


def _fake_with_schema():
    fake = FakeTeable()
    admin = tstore.Client("https://water.example", "admin-token", transport=fake.transport, sleep=lambda s: None)
    schema.apply(admin, schema.build_plan(ONTO, TMAP, None), NEW, log=lambda *a: None)
    return fake, admin


def test_config_is_read_only_and_matches_committed_shape():
    fake, admin = _fake_with_schema()
    before = json.dumps(fake.fields, sort_keys=True, default=str)
    bot = tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None)
    cfg = schema.read_config(bot, NEW, ONTO)  # 机器账号也能只读生成
    assert json.dumps(fake.fields, sort_keys=True, default=str) == before  # 不建不改
    assert set(cfg["tables"]) == set(TMAP["tables"]) and cfg["base"]["name"] == NEW
    for oid, t in cfg["tables"].items():
        assert t["id"] and t["primary"] and "全部" in t["views"]
        if TMAP["tables"][oid].get("key"):
            assert t["key"], oid
        assert set(t["links"].values()) <= {"one", "many"}  # 关联 / 人员字段写值的形状
        for p in ONTO.objects[oid].props:
            if p.name in t["fields"]:
                assert t["props"][p.id] == t["fields"][p.name]
    cfg_path = next((p for p in (ROOT / "private" / "portal" / "config.json", PORTAL / "config.json") if p.exists()), None)
    if cfg_path is None:
        return  # 公开核心仓里没有部署的编号(在私有配置仓);下面的检查只在有私有检出时做
    committed = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert committed["base"]["name"].endswith("新版") and set(committed["tables"]) == set(cfg["tables"])
    # 页面脚本用到的属性与视图,提交的配置里都有
    used = set()
    for page in PAGES + ("portal.js",):
        used |= {(m.group(1), m.group(2)) for m in re.finditer(r'(?:P\.(?:cond|get|f|between)|this\.(?:f|cond))\("(\w+)", "(\w+)"', (PORTAL / page).read_text(encoding="utf-8"))}
        used |= {(m.group(1), m.group(2)) for m in re.finditer(r'props: \[([^\]]*)\]', (PORTAL / page).read_text(encoding="utf-8")) for _ in [0] if False}
    for oid, prop in used:
        t = committed["tables"][oid]
        assert prop in t["props"] or prop in t["fields"] or (prop == "key" and t["key"]), (oid, prop)
    for page in PAGES:
        for m in re.finditer(r'P\.(?:view|tableUrl)\("(\w+)", "([^"]+)"\)', (PORTAL / page).read_text(encoding="utf-8")):
            assert m.group(2) in committed["tables"][m.group(1)]["views"], m.groups()


FUNDERS = {g["v"]: list(g["patterns"]) for g in yaml.safe_load((ROOT / "docs" / "ontology" / "ontology.yaml").read_text(encoding="utf-8"))["groupings"]["funders"]["groups"]}


def test_portal_reads_the_ontology_at_runtime():
    """页面不再抄本体(v2.2):portal.js 从 ontology.json 取阶段、口径与资金来源归组;ontology.json 是 ontology.yaml 转的,里面就有这些。"""
    js = (PORTAL / "portal.js").read_text(encoding="utf-8")
    assert 'fetch("ontology.json"' in js and "FUNDERS:" not in js
    onto = json.loads((PORTAL / "ontology.json").read_text(encoding="utf-8"))
    assert {g["v"]: list(g["patterns"]) for g in onto["groupings"]["funders"]["groups"]} == FUNDERS
    assert onto["glossary"]["tracked"]["stages"] == ["跟踪中", "准备投标", "已投标", "中标"]


def test_pages_are_local_static_and_read_only():
    for name in PAGES + ("portal.js", "portal.css", "echarts.min.js", "world.geo.json", "ontology.json", "zh_terms.json", "logo.png", "favicon.png"):
        assert (PORTAL / name).exists(), name
    for page in PAGES:
        s = (PORTAL / page).read_text(encoding="utf-8")
        assert not re.search(r'(?:src|href)="https?://', s), page  # 全部本地托管,不走公共 CDN
        assert 'name="viewport"' in s and 'portal.js' in s
    js = (PORTAL / "portal.js").read_text(encoding="utf-8")
    assert "fieldKeyType" in js and "aggregation/row-count" in js and 'credentials: "same-origin"' in js
    for page in READ_PAGES:
        s = (PORTAL / page).read_text(encoding="utf-8")
        assert not re.search(r'(?:method:\s*"(POST|PUT|PATCH|DELETE)"|P\.(create|patch|send|upload)\()', s), page  # 只读页没有写操作
    for page in PAGES + ("portal.js",):
        s = (PORTAL / page).read_text(encoding="utf-8")
        assert "TEABLE_TOKEN" not in s and "Bearer" not in s  # 没有任何令牌,全靠登录 cookie
    assert "DELETE" not in js  # 页面从不删记录


def test_caddy_route_asks_the_table_who_you_are_then_serves_files():
    c = (KIT / "portal.caddy").read_text(encoding="utf-8")
    assert "forward_auth teable:3000" in c and "uri /api/auth/user" in c and "redir * /auth/login?redirect=%2Fportal%2F 302" in c
    # 程序文件过了登录检查后让浏览器缓存(点下一页不再逐个问「这是谁」);转登录页的跳转不缓存;页面与 config.json 每次带 ETag 再问
    assert 'header @ow_code Cache-Control "private, max-age=300"' in c and 'header @ow_lib Cache-Control "private, max-age=86400"' in c
    assert c.index('header Cache-Control "no-store"') < c.index("redir * /auth/login?redirect=%2Fportal%2F 302") and c.index("forward_auth teable:3000") < c.index("header @ow_code")
    assert re.search(r"@ow_code path ([^\n]+)", c).group(1).split() == ["/portal.js", "/portal.css", "/ontology.json", "/zh_terms.json"]
    assert re.search(r"@ow_page path ([^\n]+)", c).group(1).split() == ["/", "/*.html", "/config.json"] and 'header @ow_page Cache-Control "private, no-cache"' in c
    assert "root * /srv/portal" in c and "file_server" in c and "uri strip_prefix /portal" in c
    assert re.search(r"handle / \{\s*redir \* /portal/ 302\s*\}", c)
    compose = yaml.safe_load((KIT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "/opt/osint-water/portal:/srv/portal:ro" in compose["services"]["caddy"]["volumes"]
    assert "/opt/osint-water-private/portal/media:/srv/portal/media:ro" in compose["services"]["caddy"]["volumes"]  # 首页视频来自私有配置仓
    upd = (KIT / "worker_update.sh").read_text(encoding="utf-8")
    assert '"$REPO_DIR/portal/media"' in upd and '"$PRIV_DIR/portal/media"' in upd and "compose up -d --no-deps caddy" in upd  # 源目录与挂载点都得先在主机上建好
    assert '"$REPO_DIR/portal/media"' in (KIT / "worker_install.sh").read_text(encoding="utf-8")
    inst = (KIT / "worker_install.sh").read_text(encoding="utf-8")
    assert "portal.caddy" in inst and "caddy reload" in inst
    assert "portal.caddy" in (KIT / "worker_update.sh").read_text(encoding="utf-8")


# ---------- 真的打开一遍(有 Playwright 与本地 Chromium 时) ----------

def _serve(fake: FakeTeable, cfg: dict, token: str = "admin-token", sample_data: bool = False, log_asks: bool = False):
    """本机小服务:/api/* 转给内存版 Teable(带上 token 模拟登录态,缺省管理员),config.json 用刚生成的编号,其余是 portal/ 的静态文件。
    log_asks:提问与反馈也转给真的助手接口(记进内存库的「助手提问记录」,错题本);缺省不记,页面只读"""
    import mimetypes
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _api(self, method):
            H.browser_api.append((method, self.path.split("?")[0]))
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            req = httpx.Request(method, "https://water.example" + self.path, headers={"authorization": "Bearer " + token, "content-type": self.headers.get("Content-Type") or "application/json"}, content=body)
            resp = fake.handle(req)
            self.send_response(resp.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp.content)

        def do_POST(self):  # noqa: N802
            if self.path.startswith("/api/assistant/link") or self.path.startswith("/api/assistant/review"):  # 看一条链接、复核按钮:像 Caddy 那样带上登录标记转给真的助手接口
                n = int(self.headers.get("Content-Length") or 0)
                r = httpx.post(f"http://127.0.0.1:{self.assistant_port}" + self.path[len("/api/assistant"):], content=self.rfile.read(n), timeout=30,
                               headers={"X-Portal-Auth": "1", "Content-Type": "application/json", "Cookie": "auth_session=" + token})
                self.send_response(r.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(r.content)
                return
            if self.path.startswith("/api/assistant/law"):  # 法规按条文作答:助手接口读的是同一个内存库(没配模型:只列查到的条文)
                from core import tstore as ts
                from core.assistant_server import Assistant
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                bot = Assistant(None)
                bot.terms_base = lambda: ts.Base(ts.Client("https://water.example", token, transport=fake.transport, sleep=lambda s: None))
                from core import law as law_mod
                bot.law_lib = law_mod.Library(bot.db)
                out = bot.law_answer(data.get("q") or "", data.get("law") or {}, [c for c in data.get("countries") or [] if isinstance(c, str)])
                body = json.dumps(out, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if log_asks and (self.path.startswith("/api/assistant/ask") or self.path.startswith("/api/assistant/feedback")):  # 错题本:像 Caddy 那样带上登录标记转给真的助手接口
                n = int(self.headers.get("Content-Length") or 0)
                r = httpx.post(f"http://127.0.0.1:{self.assistant_port}" + self.path[len("/api/assistant"):], content=self.rfile.read(n), timeout=30,
                               headers={"X-Portal-Auth": "1", "Content-Type": "application/json", "Cookie": "auth_session=" + token})
                self.send_response(r.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(r.content)
                return
            if self.path.startswith("/api/assistant/"):  # 主机上由 Caddy 转给后台任务进程里的助手接口;这里直接调它
                from core.assistant_server import Assistant
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                out = Assistant(None).ask(data.get("q") or "", data.get("prev") if isinstance(data.get("prev"), dict) else None)
                out["ok"] = True
                body = json.dumps(out, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self._api("POST")

        def do_PATCH(self):  # noqa: N802
            self._api("PATCH")

        def do_DELETE(self):  # noqa: N802  账号管理页:移出协作者、删邀请链接
            self._api("DELETE")

        def do_HEAD(self):  # noqa: N802  首页探视频文件在不在(主机上由 Caddy 答;portal/media/ 是挂载点,测试里通常是空的 → 404)
            fp = PORTAL / self.path.split("?")[0].lstrip("/")
            self.send_response(200 if fp.is_file() else 404)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/assistant/health"):
                body = json.dumps({"ok": True, "llm": None, "note": "测试:按词表解析", "examples": ["最近 7 天埃及有哪些新线索"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/assistant/"):
                H.bot_calls.append(("GET", self.path[len("/api/assistant"):].split("?")[0]))
                r = httpx.get(f"http://127.0.0.1:{self.assistant_port}" + self.path[len("/api/assistant"):], headers={"X-Portal-Auth": "1", "Host": "water.example", "Cookie": "auth_session=" + token})
                self.send_response(r.status_code)
                self.send_header("Content-Type", r.headers.get("content-type", "application/json"))
                if r.headers.get("content-disposition"):  # 评估报告下载:像 Caddy 那样把文件名透传
                    self.send_header("Content-Disposition", r.headers["content-disposition"])
                self.end_headers()
                self.wfile.write(r.content)
                return
            if self.path.startswith("/api/"):
                H.browser_api.append(("GET", self.path.split("?")[0]))
                req = httpx.Request("GET", "https://water.example" + self.path, headers={"authorization": "Bearer " + token})
                resp = fake.handle(req)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp.content)
                return
            rel = self.path.split("?")[0].lstrip("/") or "index.html"
            if rel == "config.json":
                data, ctype = json.dumps(cfg, ensure_ascii=False).encode(), "application/json"
            else:
                fp = PORTAL / rel
                if not fp.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                data, ctype = fp.read_bytes(), mimetypes.guess_type(rel)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(data)

    # 日历下载、订阅链接、游客只读通道:起一个真的助手接口(连内存版表格),像 Caddy 那样带上登录标记转过去;游客通道用机器账号读
    from core import calendar_feed, guest as guest_mod
    from core.assistant_server import Assistant, serve as serve_assistant
    H.browser_api = []  # 浏览器以登录人身份直接打到表格的请求(游客不该有任何读表的)
    H.bot_calls = []  # 页面打到助手接口的请求 (method, path)

    def _guest_client():  # 游客通道:机器账号读真实库;sample_data 时换成示例数据(core/sample.py,本体 guest.data: sample)
        from core import sample
        real = tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None)
        return sample.SampleClient(real, today=_today) if sample_data else real
    _tok = lambda cookie: str(cookie or "").split("=", 1)[-1]  # noqa: E731
    bot = Assistant(None, feed=calendar_feed.Feed(base_factory=lambda: tstore.Base(tstore.Client("https://water.example", token, transport=fake.transport, sleep=lambda s: None))),
                    guest=guest_mod.Guest(base_factory=lambda: tstore.Base(_guest_client()),
                                          real_factory=lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None)),
                                          partners=["@bewg.example"],  # 伙伴名单(私有 team.yaml partners):库外账号里邮箱是这个域名的走伙伴档
                                          whoami=lambda cookie: fake.users.get(fake.tokens.get(_tok(cookie), ("",))[0], {}).get("email", ""),
                                          outsider=lambda cookie: fake.tokens.get(_tok(cookie), ("", "none"))[1] == "none"))
    bot.feed_token = "test-token"
    if log_asks:  # 记提问用机器账号写真实库(同主机上的助手接口)
        bot.who = lambda cookie: "管理员 <admin@example.org>"
        bot.terms_base = lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    asrv = serve_assistant(bot, 0, host="127.0.0.1")
    threading.Thread(target=asrv.serve_forever, daemon=True).start()
    H.assistant_port = asrv.server_port

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.assistant = asrv
    srv.bot = bot
    srv.browser_api = H.browser_api
    srv.bot_calls = H.bot_calls
    return srv


def _today():
    """页面按北京时间算「今天」;种数据也按它。用机器的日期(UTC)的话,北京过了零点(UTC 16:00 以后)「今天」会差一天,按天筛的断言就对不上。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _held(page, pattern, answer=None):
    """模拟线上慢:浏览器里匹配 pattern 的请求先扣住,返回「放行」函数;answer(url) 给出字符串的,放行时直接拿它当 JSON 回,其余照常转发。"""
    hold, held = [True], []

    def gate(route):
        body = answer(route.request.url) if answer else None
        go = (lambda: route.fulfill(status=200, content_type="application/json", body=body)) if body is not None else route.continue_
        if hold[0]:
            held.append(go)
        else:
            go()
    page.route(pattern, gate)

    def release():
        hold[0] = False
        while held:
            held.pop(0)()
    return release


FIRST_FRAMES = """(() => { const rec = []; window.__frames = rec;  // 每一帧绘制之前记下:正文出来了没有、顶栏在不在
  const tick = () => { rec.push([!!document.querySelector('main'), !!document.querySelector('#nav header')]); if (rec.length < 6) requestAnimationFrame(tick); };
  requestAnimationFrame(tick); })();"""


def _seed(admin: tstore.Client, fake: FakeTeable):
    from datetime import timedelta
    b = tstore.Base(admin)
    today = _today()
    c = b.create("country", [{"name_zh": "阿塞拜疆", "ISO3": "AZE", "tier": "重点", "market": "示例甲区域", "indicators": "GDP:720 亿美元(2025)(世行 WDI https://data.worldbank.org/indicator/NY.GDP.MKTP.CD?locations=AZ)", "note": "[备注] 甲城项目采购已启动 [aaaaaaaa]\n2026-09 [建议动作] 在 ECEPP 注册(到期 2026-10-10,待定)",
                                    "bready_total": 66.0, "bready_water": 79.3, "bready_procure": 45.7, "bready_dispute": 65.3, "water_tariff": 2, "water_tariff_cur": "AZN", "water_tariff_usd": 1.18,
                                    "water_reliable": 97.1, "water_conn_days": 5, "foreign_bid": "部分招标有限制", "award_days": 365, "gov_pay_days": 7,
                                    "bready_items": "[供水] 法规设了降低漏损的目标:是\n[供水] 有独立于水司的投诉渠道:否\n[采购] 外企参标或授标受限:部分招标有\n[采购] 外企须与本地企业联合才能投标:否",
                                    "bready_edition": "B-READY 2025(数据版本 December 10, 2025)"}])[0]
    o = b.create("opportunity", [{"code": "AZE-001", "name_zh": "甘贾污水厂", "country": c["id"], "stage": "跟踪中", "public_stage": "采购进行中", "scope": "新建污水厂,一期 10 万吨/日 [aaaaaaaa]", "sectors": ["污水"], "financing": "[aaaaaaaa] World Bank (IDA) · 主权贷款 · 50,000,000 USD · 已批准", "size_value": 5e7, "currency": "USD", "size_cny": 36000}])[0]
    ld = b.create("lead", [{"key": "aaaaaaaa", "title": "Ganja WWTP EOI", "url": "https://example.org/a", "relevant": True, "category": "意向书", "funder": "World Bank (IDA)",
                            "country": c["id"], "found_at": today.isoformat() + "T02:00:00.000Z", "opportunity": o["id"], "status": "待处理", "origin": "机器采集"}])[0]
    b.create("milestone", [{"opportunity": o["id"], "lead": ld["id"], "kind": "意向书截止", "day": (today + timedelta(days=3)).isoformat(),
                            "日期原文": (today + timedelta(days=3)).isoformat(), "status": "官方计划"}])
    b.create("followup", [{"opportunity": o["id"], "day": today.isoformat(), "content": "与业主通话", "next_step": "递交意向书", "next_due": (today + timedelta(days=5)).isoformat()}])
    sug = b.create("opportunity", [{"code": "AZE-002", "name_zh": "苏姆盖特污水厂", "country": c["id"], "stage": "建议", "ai_reason": "欧佩克基金出资,国际竞争性招标"}])[0]
    b.create("lead", [{"key": "bbbbbbbb", "title": "Sumgait WWTP tender notice", "url": "https://example.org/b", "relevant": True, "category": "招标", "country": c["id"],
                       "found_at": today.isoformat() + "T03:00:00.000Z", "suggested_opp": sug["id"], "status": "待处理", "origin": "机器采集", "summary_zh": "苏姆盖特新污水厂主体工程招标"},
                      {"key": "cccccccc", "title": "New reservoir pipeline EOI", "url": "https://example.org/c", "relevant": True, "category": "意向书", "country": c["id"],
                       "found_at": today.isoformat() + "T04:00:00.000Z", "maybe_new": True, "project_name": "哈卡里恰伊输水干管", "status": "待处理", "origin": "机器采集"}])
    b.create("city", [{"key": "587084", "name_zh": "巴库", "name_en": "Baku", "country": c["id"], "admin": "Baki", "coords": "40.3777,49.8920", "population": 2300000, "area_km2": 2130, "density": 1080}])
    r = b.create("report", [{"kind": "周报", "day": today.isoformat(), "period": "2026-W40", "gen_status": "完成", "key": "周报_2026-W40.docx"}])[0]
    admin.upload_attachment(b.t["report"], r["id"], b.fid("report", "file"), "周报_2026-W40.docx", b"PK fake")
    # 当天的日报:正文由后台按同一批数据写(core.jobs.brief),这里直接用它,页面顶部显示
    from core.jobs import brief as brief_mod, common
    from core.profile import load_profile
    import tempfile
    prof_dir = Path(tempfile.mkdtemp()) / "prof"
    prof_dir.mkdir()
    (prof_dir / "profile.yaml").write_text("name: t\ncountries: []\nbidder: {nationality: CHN}\n", encoding="utf-8")
    ctx = common.connect(load_profile(prof_dir / "profile.yaml"), today, use_llm=False, api=admin, base=b, cache_dir=prof_dir / "cache", log=lambda *a: None)
    md, _, _, _ = brief_mod.build_daily(ctx, today)
    d = b.create("report", [{"kind": "日报", "day": today.isoformat(), "period": today.isoformat(), "gen_status": "完成", "key": f"日报_{today}.docx", "summary": md}])[0]
    admin.upload_attachment(b.t["report"], d["id"], b.fid("report", "file"), f"日报_{today}.docx", b"PK fake")
    return c, o, ld


# 名称-数值行与指标卡不变形(2026-10-14 使用方截图:营商环境里「365 天」的警示被挤成一字宽一列、说明压到隔壁、外企参标那格拉成一大块黄底)。
# 各页的名称-数值行都由 P.factHtml 生成;这里在常用宽度下量一遍:每格都在自己那一行里,值那一栏不被挤窄,行与行不重叠,整页不横向滚
LAYOUT = """(sel) => { const bad = [];
  document.querySelectorAll(sel).forEach((root) => {
    const rows = [...root.querySelectorAll('.facts > .fact')].filter((e) => e.offsetParent);
    rows.forEach((row, i) => { const rr = row.getBoundingClientRect();
      row.querySelectorAll('*').forEach((k) => { const r = k.getBoundingClientRect();
        if (r.width && (r.right > rr.right + 1 || r.left < rr.left - 1)) bad.push('出格:' + k.textContent.slice(0, 20)); });
      const v = row.querySelector(':scope > .fact-v');
      if (!v || v.getBoundingClientRect().width < 80) bad.push('值被挤窄:' + row.textContent.slice(0, 20));
      rows.slice(i + 1).forEach((o) => { const ro = o.getBoundingClientRect();
        if (Math.min(rr.right, ro.right) - Math.max(rr.left, ro.left) > 1 && Math.min(rr.bottom, ro.bottom) - Math.max(rr.top, ro.top) > 1) bad.push('重叠:' + row.textContent.slice(0, 12)); }); });
    root.querySelectorAll('.kpi').forEach((k) => { if (k.scrollWidth > k.clientWidth + 1) bad.push('卡片溢出:' + k.textContent.slice(0, 12)); });
    root.querySelectorAll('.kpis').forEach((g) => { const ks = [...g.children].filter((k) => k.offsetParent), rows = {};  // 最后一张不单独落一行(占满一行的除外)
      if (ks.length < 2) return; ks.forEach((k) => { const t = Math.round(k.getBoundingClientRect().top); (rows[t] = rows[t] || []).push(k); });
      Object.values(rows).forEach((r) => { if (r.length === 1 && r[0].getBoundingClientRect().width < g.getBoundingClientRect().width * 0.9) bad.push('单独一张:' + r[0].textContent.slice(0, 12)); }); });
  });
  if (document.documentElement.scrollWidth > innerWidth + 1) bad.push('整页横向滚动');
  return bad; }"""


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_fact_rows_and_cards_keep_their_shape_at_common_widths():
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    tstore.Base(admin).update("country", c["id"], {"indicators": "\n".join(["GDP:720 亿美元(2025)(世行 WDI https://data.worldbank.org/x)", "通胀(CPI):12.5%(2025)(IMF WEO)",
                                                                          "安全管理卫生服务覆盖率:31%(2022)(联合国 JMP)", "直接投资净流入:-2.1 亿美元(2024)(世行 WDI)"])})
    srv = _serve(fake, schema.read_config(admin, NEW, ONTO))
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            for w in (1366, 1536, 1920, 390):  # 使用方常用的桌面宽度与手机
                page = browser.new_page(viewport={"width": w, "height": 900})
                for t in ("biz", "info"):
                    page.goto(f"http://127.0.0.1:{srv.server_port}/country.html?iso=AZE&t={t}")
                    page.wait_for_selector(f".tab[data-t={t}] .facts > .fact", timeout=15000)
                    assert page.evaluate(LAYOUT, f".tab[data-t={t}]") == [], (w, t)
                    assert page.evaluate(LAYOUT, "#cards") == [], (w, t)
                biz = page.locator("#biz")
                assert biz.locator(".fact.is-warn .fact-w").inner_text() == "部分招标对外企设限,投标前要核实"
                assert "超过 180 天,授标慢" in biz.locator(".fact.is-bad .fact-w").inner_text() and "发标到授标" in biz.locator(".fact.is-bad .fact-n").inner_text()
                assert biz.locator(".kpi").nth(3).locator(".kn").inner_text() == "1.18"  # 「约 1.18 美元/m³」数字照样放大
                tones = page.evaluate("['建议', '跟踪中', '已投标', '中标', '未中标', '放弃'].map((s) => P.stageTag(s).match(/class=\"([^\"]+)\"/)[1])")
                assert tones == ["tag t-warn", "tag t-info", "tag t-info", "tag t-ok", "tag", "tag"]  # 标签只一种胶囊,颜色按含义(界面规范第三步)
                ready = "[...document.querySelectorAll('#cards .kpi .kn')].every((e) => e.textContent !== '…') && document.querySelector('#cards .kpi')"
                for pth, wait in (("overview.html", ready), ("analysis.html", ready), ("ontology.html", ready),  # 顶上一排数(界面规范第二步:都是指标卡)
                                  ("reports.html", "document.querySelector('#list table')"),  # 报告清单:标签与筛选计数(2026-10-16)
                                  (f"opp.html?id={o['id']}", "document.querySelector('#head .facts > .fact')")):  # 商机页的名称-数值行(第四步)
                    page.goto(f"http://127.0.0.1:{srv.server_port}/{pth}")
                    page.wait_for_function(wait, timeout=15000)
                    assert page.evaluate(LAYOUT, "main") == [], (w, pth)
                page.close()
            browser.close()
    finally:
        srv.shutdown()
        srv.assistant.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_pages_open_in_a_browser_against_the_fake_table():
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)  # 之前的建表、造数据是写;页面打开以后不能再有写
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text} @ {page.url}") if m.type == "error" and "/media/" not in (m.location or {}).get("url", "") else None)  # 首页探视频文件,没有是正常的 404
            # 日历:下载 .ics 按当前筛选、「订阅到日历」给出 webcal 链接与步骤
            page.wait_for_load_state("networkidle")
            page.goto(base + "/calendar.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert page.get_attribute("#icsDl", "href") == "api/assistant/calendar.ics" and page.get_attribute("#icsDl", "download")
            page.select_option("#fCountry", label="阿塞拜疆")
            assert page.get_attribute("#icsDl", "href") == "api/assistant/calendar.ics?country=AZE"
            ics = page.evaluate("fetch('api/assistant/calendar.ics?country=AZE').then(r => r.text())")
            assert "BEGIN:VCALENDAR" in ics and "AZE-001" in ics
            page.click("#subBtn")
            page.wait_for_selector("#subUrl", timeout=15000)
            assert "webcal://water.example/portal/feed/calendar.ics?t=" in page.inner_text("#subUrl") and "country=AZE" in page.inner_text("#subUrl") and "iPhone" in page.inner_text("#subbox")
            page.click("#subClose")
            assert page.locator("#subbox").is_hidden()
            # 工作原理:流程两栏、分工按本体的行为现列、底线是本体的规则、各页导览
            page.goto(base + "/how.html")
            page.wait_for_selector("#rules details", timeout=15000)
            assert page.locator(".flow .band").count() == 2 and page.locator(".flow .step").count() == 10
            assert page.locator("#actors .acts li").count() >= 15 and "采集线索" in page.inner_text("#actors") and "上传线索" in page.inner_text("#actors")
            assert page.locator("#rules details").count() == len(ONTO_RAW["rules"]) and "R2" in page.inner_text("#rules")
            assert page.inner_text("h1") == "工作流程" and page.locator("#nav .groups a.on").inner_text() == "工作原理" and page.locator("#nav .pagetabs a.on").inner_text() == "工作流程"
            assert page.locator("#pages tr.grp th").all_inner_texts() == [g for g, _ in MENU_SPEC] and page.locator("#pages tr").count() == 1 + sum(1 + len(x) for _, x in MENU_SPEC)  # 各页导览按菜单分组
            # 线索总览(原首页):数字卡片、漏斗、地图、最新报告
            page.goto(base + "/overview.html")
            page.wait_for_selector("#reports table", timeout=15000)
            assert page.locator("#cards .kpi").count() == 5
            assert "周报_2026-W40.docx" in page.inner_text("#reports")
            assert page.locator("#funnel canvas").count() >= 1 and page.locator("#map canvas").count() >= 1
            # 筛选是平铺按钮:国别、领域(多选)、类别、时间;清单在右栏,漏斗和地图在左栏
            assert page.locator("#fCat .chip-btn").count() > 5 and page.locator("#fCountry .chip-btn").count() >= 2 and page.locator("#fSector .chip-btn").count() >= 5
            assert page.locator("select").count() == 0 and page.locator("#drill").count() == 1
            cards = page.inner_text("#cards")
            assert "AI 推荐的商机" in cards and "待人定" not in cards
            assert page.locator("#nav .groups a").all_inner_texts() == ["首页"] + [g for g, _ in MENU_SPEC] and page.locator("#nav .menu-btn").count() == 0  # 电脑上的内页:顶栏是首页与三个组名
            assert page.locator("#nav .groups a.on").inner_text() == "项目线索" and page.locator("#nav .pagetabs a").all_inner_texts() == MENU_SPEC[2][1] and page.inner_text("h1") == "线索总览"  # 下面一排页签是这一组的各页
            page.wait_for_function("document.querySelector('#acct .who') && document.querySelector('#acct .who').textContent.includes('Ada')", timeout=15000)
            assert "所有者" in page.inner_text("#acct") and "帐号设置" in page.inner_text("#acct") and "退出登录" in page.inner_text("#acct")  # 顶栏右端:登录人 · 角色 · 帐号设置 · 退出登录
            page.click("#fWin .chip-btn:has-text('7')")
            page.wait_for_function("document.getElementById('fNote').textContent.includes('已按筛选')", timeout=15000)
            page.click("#fSector .chip-btn:has-text('供水')")
            page.wait_for_function("document.querySelector('#fSector .chip-btn.on') && location.search.includes('s=')", timeout=15000)
            assert page.locator("#cards .kpi").count() == 5
            # 区域(经营区域)与国别联动:点区域,国别只剩该区域的;资金来源按机构族归组
            assert page.locator("#fRegion .chip-btn").count() >= 2 and page.locator("#fFunder .chip-btn").count() == len(FUNDERS) + 3
            page.click("#fSector .chip-btn:has-text('全部')")  # 先把领域放开
            page.click("#fRegion .chip-btn:has-text('示例甲区域')")
            page.wait_for_function("location.search.includes('r=')", timeout=15000)
            assert page.locator("#fCountry .chip-btn").count() == 2 and "阿塞拜疆" in page.inner_text("#fCountry")
            page.click("#fFunder .chip-btn:has-text('世界银行')")
            page.wait_for_function("location.search.includes('f=')", timeout=15000)
            page.wait_for_function("!document.getElementById('fNote').textContent.includes('统计中')", timeout=15000)
            assert page.inner_text("#cards .kpi .kn") == "1"  # 只有 aaaaaaaa 的出资方是 World Bank (IDA)
            # 日历:本月有节点、跟进、到期、报告四种
            page.wait_for_load_state("networkidle")
            page.goto(base + "/calendar.html")
            page.wait_for_selector("#cal .cell", timeout=15000)
            kinds = {page.locator(".chip").nth(i).get_attribute("class") for i in range(page.locator(".chip").count())}
            assert {"chip ms", "chip fu", "chip due", "chip rp"} <= kinds, kinds
            assert "chip ld" not in kinds  # 没归商机的线索截止缺省不上日历
            assert "AZE-001" in page.inner_text("#list")
            page.select_option("#fOpp", label="AZE-001 甘贾污水厂")
            assert page.locator(".chip").count() >= 3
            # 国别概况:国别信息、商机、线索、节点
            page.wait_for_load_state("networkidle")
            page.goto(base + "/country.html?iso=AZE")
            page.wait_for_selector("#opps table", state="attached", timeout=15000)  # 商机表在藏着的页签里
            page.wait_for_function("document.querySelectorAll('#tabs button b').length >= 3", timeout=15000)
            txt = page.inner_text("main")
            assert "阿塞拜疆" in txt and "GDP\n720 亿美元" in txt and "甲城项目采购已启动" in txt  # 默认页签「概况」:备注与指标
            assert page.locator(".facts a.src").count() >= 1 and "AI 推荐" in txt and "待人定" not in txt  # 指标成了「名称 / 数值 / 来源链接」,不再是一坨原文
            # 页签切换:商机 / 线索 / 节点各在自己的页签里,页签上带条数;其他页签的内容藏着
            assert page.locator("#tabs button").count() == 9 and page.locator("#tabs button.on").inner_text() == "概况" and "AZE-001" not in txt
            # 页签分三组:基本信息 → 历史信息 → 动态信息(使用方 2026-10-13 要的)
            assert page.locator("#tabs .tg").all_inner_texts() == ["基本信息", "历史信息", "动态信息"]
            assert [page.locator("#tabs button").nth(i).get_attribute("data-t") for i in range(9)] == ["info", "inst", "biz", "cities", "docs", "awards", "opps", "leads", "ms"]
            assert page.inner_text("#tabs button:has-text('商机')").split()[-1] == "2" and page.inner_text("#tabs button:has-text('线索')").split()[-1] == "3"
            page.click("#tabs button:has-text('商机')")
            assert "AZE-001" in page.inner_text("main") and "GDP" not in page.inner_text("main")
            page.click("#tabs button:has-text('线索')")
            lt = page.inner_text("#leads")
            assert "苏姆盖特新污水厂主体工程招标" in lt and "哈卡里恰伊输水干管" in lt and "Ganja WWTP EOI" in lt  # 线索也以中文名称为主,原题小字
            page.click("#tabs button:has-text('节点')")
            assert "意向书截止" in page.inner_text("main")
            page.wait_for_function("location.search.includes('t=ms')", timeout=15000)
            page.click("#tabs button:has-text('城市')")  # 规模以上城市:名称、行政区、人口、面积、密度
            ct = page.inner_text("#cities")
            assert "巴库" in ct and "Baki" in ct and "2,300,000" in ct and "2,130" in ct and "1,080" in ct and page.inner_text("#tabs button:has-text('城市')").split()[-1] == "1"
            page.click("#tabs button:has-text('机构')")
            assert "还没填机构" in page.inner_text("main")
            # 营商环境(B-READY,2026-10-14):四张大号指标卡,低于 50 分的标红写明含义;外企参标限制写明要核实;细项里对外企投标不利的标红;没覆盖的国家写明,不显示成 0
            page.click("#tabs button:has-text('营商环境')")
            bz = page.inner_text("#biz")
            assert "66.0" in bz and "79.3" in bz and "约\n1.18" in bz and "美元/m³" in bz and "2 AZN/m³" in bz and "365 天" in bz and "超过 180 天,授标慢" in bz
            assert page.locator("#biz .kpi.is-bad").count() == 1 and "低于 50 分,偏弱" in page.inner_text("#biz .kpi.is-bad") and "公共采购得分" in page.inner_text("#biz .kpi.is-bad")
            assert "部分招标对外企设限" in bz and page.locator("#biz .bitems li.bad").count() == 1 and "对外企投标不利" in page.inner_text("#biz .bitems li.bad")
            assert "December 10, 2025" in bz and page.locator("#biz a[href*='businessready']").count() == 1
            none = page.evaluate("P.bready({ fields: {} }, '埃及')")
            assert "没有覆盖埃及" in none and "0 分" not in none
            page.click("#tabs button:has-text('概况')")
            assert "重点" not in page.inner_text("#pick")  # 国别下拉只有国名,不带市场分级
            assert page.locator("#map canvas").count() >= 1  # 国别页有世界地图,点国家切换
            assert page.locator("a.cite").count() >= 1 and page.locator("a.cite").first.inner_text() == "1" and ld["id"] in page.locator("a.cite").first.get_attribute("href")  # 角标是编号,点开是线索
            # 线索分析:顶部是最新日报的正文(线索号成角标),卡片数字与首页同一口径,图都画出来,点一柱右边出清单
            page.wait_for_load_state("networkidle")
            page.goto(base + "/analysis.html")
            page.wait_for_selector("#brief h4", timeout=15000)
            page.wait_for_function("document.querySelectorAll('#cards .kpi').length == 6", timeout=15000)
            brief = page.inner_text("#brief")
            assert "今日概览" in brief and "线索累计 3 条" in brief and "待处理 3 条,待确认归属 2 条" in brief and "值得看的新线索" in brief and "[" not in brief  # 方括号的线索号成了角标
            cites = page.locator("#brief a.cite")
            assert cites.count() >= 3 and any(ld["id"] in cites.nth(i).get_attribute("href") for i in range(cites.count()))
            cards = page.inner_text("#cards")
            assert cards.split("\n")[0] == "3" and "待确认归属" in cards and "已归入商机" in cards
            # 图分五个页签,一次显示一组;切到哪个页签,那组图就画出来(隐藏时量不到尺寸,切过来补 resize)
            assert page.locator("#tabs button").count() == 5 and page.locator("#tabs button.on").inner_text() == "每日新增" and page.locator(".tab.on").count() == 1
            for name, cids in (("国别", ("cCountry", "cHeat")), ("类别与领域", ("cCat", "cSector")), ("资金来源与信源", ("cFunder", "cSource")), ("判定", ("cPies",)), ("每日新增", ("cTrend",))):
                page.click("#tabs button:has-text('" + name + "')")
                assert all(page.locator("#" + cid + " canvas").count() >= 1 and page.locator("#" + cid).bounding_box()["width"] > 100 for cid in cids), name
            page.click("#tabs button:has-text('国别')")
            page.wait_for_function("location.search.includes('t=country')", timeout=15000)
            assert page.locator("#drill").bounding_box()["y"] > page.locator("#cCountry").bounding_box()["y"]  # 清单在图的下面,不再在右边
            assert page.locator("#fWin .chip-btn").count() == 5 and page.locator("#fRel .chip-btn").count() == 2 and "示例甲区域" in page.inner_text("#fRegion") and page.locator("#fCountry .chip-btn").count() == 2
            page.click("#cards .kpi:has-text('待确认归属')")
            page.wait_for_selector("#drill table", timeout=15000)
            dr = page.inner_text("#drill")
            assert "苏姆盖特新污水厂主体工程招标" in dr and "哈卡里恰伊输水干管" in dr and "Ganja" not in dr  # 中文名称为主:模型的项目名或中文摘要
            assert "Sumgait WWTP tender notice" in dr and "New reservoir pipeline EOI" in dr  # 原题在小字里,能对上原文
            assert "要点" not in dr  # 两条都有模型给的中文,不用要点
            page.click("#cards .kpi:has-text('判为相关')")
            page.wait_for_function("document.querySelector('#drill').innerText.includes('Ganja')", timeout=15000)
            assert "污水处理厂、意向书" in page.inner_text("#drill") and page.locator("#drill .tag:has-text('要点')").count() == 1  # 没有中文摘要的,从原题挑词成「要点」
            page.click("#fRel .chip-btn:has-text('只看')")
            page.wait_for_function("location.search.includes('rel=1')", timeout=15000)
            page.click("#fWin .chip-btn:has-text('今天')")
            page.wait_for_function("location.search.includes('w=0')", timeout=15000)
            assert page.inner_text("#cards").split("\n")[0] == "3"  # 三条都是今天发现、判为相关的
            # 首页(助手):大标题 + 提问框 + 视频卡;一句话 → 「理解为」条件 → 数字 / 清单 / 图,数据仍由页面带着登录态查表格
            # (这里的解析接口由测试服务直接调进程内的助手;视频文件来自私有配置仓,测试里没有,页面收起视频只留底色)
            from urllib.parse import quote
            page.wait_for_load_state("networkidle")
            page.goto(base + "/index.html")
            page.wait_for_selector("#examples .chip-btn", timeout=15000)
            assert page.locator(".hero-title").inner_text() == "全球水务开源情报" and page.locator(".hero-en").inner_text() == "Global Water OSINT" and page.locator(".hero-card #q").count() == 1
            assert (PORTAL / "fonts" / "huiwen-title.woff2").exists() and (PORTAL / "fonts" / "truetypewriter-latin.woff2").exists() and (PORTAL / "fonts" / "OFL.txt").exists() and 'url("fonts/huiwen-title.woff2")' in (PORTAL / "portal.css").read_text(encoding="utf-8")  # 站名字体随页面一起托管
            assert page.evaluate("document.fonts.check('20px \"Huiwen Mincho\"') && document.fonts.check('20px \"Typewriter Title\"')")  # 两个字体真的加载上了
            assert page.locator("#heroVideo").get_attribute("data-src") == "media/hero.webm media/hero.mp4" and page.locator("#heroVideo").get_attribute("data-poster") == "media/hero.jpg"
            # 视频文件来自私有配置仓(portal/media/ 是挂载点):本地没放时收起只留底色;放了就接上(开源 Chromium 放不了 H.264 时换成封面图)
            page.wait_for_function("document.querySelector('.hero-media.nomedia') || document.querySelector('#heroVideo source') || document.querySelector('.hero-media img')", timeout=15000)
            page.wait_for_selector("#menu", state="attached", timeout=15000)  # 顶栏与菜单卡片一打开就画好(读完配置补上帐号区)
            assert page.locator("#nav .menu-btn").count() == 1 and not page.locator("#menu").is_visible() and not page.locator("#menuVeil").is_visible()  # 首页只有「菜单」按钮
            page.click("#nav .menu-btn")
            assert page.locator("#menu").is_visible() and page.locator("#menuVeil").is_visible() and "线索总览" in page.inner_text("#menu") and "报告" in page.inner_text("#menu")  # 磨砂遮罩 + 右上角卡片
            page.wait_for_function("document.querySelector('#menu #acct .who')", timeout=15000)
            assert "Ada" in page.inner_text("#menu #acct") and "退出登录" in page.inner_text("#menu #acct")  # 菜单卡片底部也有帐号区
            page.click("#menuClose")
            assert not page.locator("#menu").is_visible() and not page.locator("#menuVeil").is_visible()
            assert page.locator(".answer").count() == 0
            page.goto(base + "/index.html?q=" + quote("阿塞拜疆最近 7 天有多少条线索"))
            page.wait_for_selector(".answer .kpi .kn", timeout=15000)
            first = page.locator(".answer").first
            assert first.locator(".kpi .kn").inner_text() == "3" and first.locator(".kpi .ku").inner_text() == "条" and "线索" in first.locator(".kpi .kl").inner_text()
            assert page.evaluate("document.body.classList.contains('asked')") and not page.locator(".hero").is_visible()  # 提问后进对话模式:标题与视频收起
            assert page.locator("#askDock #askForm").count() == 1 and page.locator("#askDock #q").is_visible() and page.locator("#askDock #llmNote").count() == 1  # 提问框挪到底部
            und = first.locator(".understood").inner_text()
            assert "国别:阿塞拜疆" in und and "时间:最近 7 天" in und and "数一下" in und and "词表解析" in und
            assert "按词表解析" in page.inner_text("#llmNote") and page.locator("#examples .chip-btn").count() >= 1
            first.locator("a.show-list").click()
            page.wait_for_selector(".answer >> nth=0 >> table", timeout=15000)
            assert "苏姆盖特新污水厂主体工程招标" in first.inner_text()  # 清单同样以中文名称为主
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            ov = page.evaluate("(() => { const d = document.getElementById('askDock').getBoundingClientRect(); const all = document.querySelectorAll('.answer'); const m = all[all.length - 1].getBoundingClientRect(); return { dockTop: d.top, mainBottom: m.bottom, solid: getComputedStyle(document.getElementById('askDock')).backgroundImage === 'none' }; })()")
            assert ov["mainBottom"] <= ov["dockTop"] + 1 and ov["solid"], ov  # 滚到底时正文不会压在底部提问框与说明文字下面
            def ask(q):
                page.fill("#q", q)
                page.press("#q", "Enter")
                page.wait_for_function("(q) => { const all = document.querySelectorAll('.answer'); const a = all[all.length - 1]; return a && a.querySelector('.a-q').textContent === q && !a.innerText.includes('解析中') && !a.innerText.includes('查表中') && !a.innerText.includes('读取中'); }", arg=q, timeout=15000)
                return page.locator(".answer").last  # 对话模式:新答案在最下面
            a = ask("世界银行出资的线索")
            assert a.locator("table").count() == 1 and "Ganja WWTP EOI" in a.inner_text() and "Sumgait" not in a.inner_text() and "资金来源:世界银行" in a.inner_text()
            a = ask("最近 30 天的线索按类别统计")
            page.wait_for_selector(".answer >> nth=-1 >> canvas", timeout=15000)
            assert "共 3 条线索" in a.inner_text() and "分组:类别" in a.inner_text()
            a = ask("在跟踪的商机有哪些")
            assert "AZE-001" in a.inner_text() and "AZE-002" not in a.inner_text() and "在跟踪" in a.inner_text()
            a = ask("未来 30 天有哪些截止")
            assert "意向书截止" in a.inner_text() and "对象:时间节点" in a.inner_text()
            a = ask("最近的周报")
            assert "周报_2026-W40.docx" in a.inner_text()
            a = ask("生成一份阿塞拜疆的国别报告")
            assert "去报告页" in a.inner_text() and "reports.html?country=" in a.locator("a.btn").get_attribute("href")
            a = ask("你好")
            assert "没听懂" in a.inner_text() and a.locator("a.ex").count() >= 1
            # 本体页:对象按钮、关系图、详情;点一个对象详情跟着换;分类体系页签里每套分类都有
            page.wait_for_load_state("networkidle")
            page.goto(base + "/ontology.html")
            page.wait_for_selector("#graph canvas", timeout=15000)
            page.wait_for_function("document.querySelectorAll('#objs .chip-btn').length == " + str(len(ONTO.objects)), timeout=15000)
            assert page.locator("#cards .kpi").count() == 7 and "v" + str(ONTO.raw["version"]) in page.inner_text("#ver")
            assert "商机" in page.inner_text("#detail h2") and "我方阶段" in page.inner_text("#detail")  # 缺省选中商机
            page.click("#objs .chip-btn:has-text('线索')")
            page.wait_for_function("document.querySelector('#detail h2').innerText.includes('线索')", timeout=15000)
            d = page.inner_text("#detail")
            assert "原文网址" in d and "机器写" in d and "人写" in d and "采集线索" in d and "待处理 → 已处理" in d
            assert page.locator("#objs .chip-btn.on").inner_text().startswith("线索") and "o=lead" in page.url
            page.click("#detail a.olink:has-text('国别')")
            page.wait_for_function("document.querySelector('#detail h2').innerText.includes('国别')", timeout=15000)
            assert page.locator("#tabs button").count() == 7 and page.locator("#tax details").count() == len(ONTO.raw["taxonomies"])
            assert "线索类别" in page.inner_text("#tax") and "跟踪中的商机" not in page.inner_text("#tax")
            page.click("#tabs button:has-text('口径')")
            page.wait_for_function("location.search.includes('t=glossary')", timeout=15000)
            g = page.inner_text("#glossary")
            assert "跟踪中的商机" in g and "World Bank" in page.inner_text("#groupings")
            # 语义层页签:指标(单位、算法、缺数时怎么说、说法)、别的说法、行业术语、本国定义集中在一处
            page.click("#tabs button:has-text('语义层')")
            page.wait_for_function("document.querySelector('#semMeasures').innerText.includes('评标天数') && !document.querySelector('#terms').innerText.includes('读取中')", timeout=15000)
            sm = page.inner_text("#semMeasures")
            assert "评标时间" in sm and "中位数" in sm and "缺数时怎么说" in sm and "污水处理厂" in page.inner_text("#semWords") and "问排放标准" in page.inner_text("#semWords")
            assert "智慧水务" in page.inner_text("#terms") and page.locator("#semCards .kpi").count() == 5
            page.click("#tabs button:has-text('规则')")
            assert "一个对象只存一处" in page.inner_text("#rules")
            # 到这里为止全是只读页:没有任何写请求
            assert all(m == "GET" for m, _ in fake.calls[seeded:]), {m for m, _ in fake.calls[seeded:]}
            # 商机总览 → 工作台
            page.wait_for_load_state("networkidle")
            page.goto(base + "/opps.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert "AZE-001" in page.inner_text("#list") and "甘贾污水厂" in page.inner_text("#list")
            # 筛选与首页同一套平铺按钮:阶段、国别、领域(商机的 12 类,可多选)、负责人;没有下拉
            assert page.locator("select").count() == 0 and page.locator("#fStage .chip-btn").count() == 2 + len(ONTO.raw["glossary"]["decided"]["stages"]) and page.locator("#fSector .chip-btn").count() == 13 and page.locator("#fCountry .chip-btn").count() == 2
            assert "示例甲区域" in page.inner_text("#fRegion") and page.locator("#fFunder .chip-btn").count() == len(FUNDERS) + 3
            page.click("#fFunder .chip-btn:has-text('亚洲开发银行')")
            assert "AZE-001" not in page.inner_text("#list")
            page.click("#fFunder .chip-btn:has-text('世界银行')")
            assert "AZE-001" in page.inner_text("#list")
            page.click("#fFunder .chip-btn:has-text('全部')")
            page.click("#fSector .chip-btn:has-text('供水')")
            assert "AZE-001" not in page.inner_text("#list") and "s=%E4%BE%9B%E6%B0%B4" in page.url
            page.click("#fSector .chip-btn:has-text('污水')")
            assert "AZE-001" in page.inner_text("#list")  # 多选:供水 或 污水
            page.click("#fSector .chip-btn:has-text('全部')")
            page.click("#list a:has-text('AZE-001')")
            page.wait_for_selector("#timeline .tl", timeout=15000)
            assert "与业主通话" in page.inner_text("#timeline") and "意向书截止" in page.inner_text("#ms") and "aaaaaaaa" in page.inner_text("#leads")
            assert "5000 万 USD(≈ 3.6 亿 元)" in page.inner_text("#head")  # 投资规模 + 货币 + 折合人民币
            page.goto(base + "/opps.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert "5000 万 USD" in page.inner_text("#list")
            page.goto(base + f"/opp.html?id={o['id']}")
            page.wait_for_selector("#timeline .tl", timeout=15000)
            assert "aaaaaaaa" not in page.inner_text("#facts") and page.locator("#facts a.cite").count() == 2 and ld["id"] in page.locator("#facts a.cite").first.get_attribute("href")  # 建设内容与资金各引一次,同一线索同一编号
            # 记一笔跟进并把阶段改为「准备投标」:新建一条跟进(记录人 = 登录人,带阶段变更),商机阶段同步改;时间线立即出现
            page.fill("#fuContent", "拿到招标文件")
            page.select_option("#fuJudg", "重点推进")
            page.fill("#fuNext", "组织投标")
            page.select_option("#fuStage", "准备投标")
            page.click("#fuSave")
            page.wait_for_function("document.querySelector('#timeline').innerText.includes('拿到招标文件')", timeout=15000)
            b = tstore.Base(admin)
            fus = [r for r in b.rows("followup", props=["content", "author", "stage_change", "judgment", "next_step", "opportunity"]) if b.get("followup", r, "content") == "拿到招标文件"]
            assert len(fus) == 1 and b.get("followup", fus[0], "stage_change") == "准备投标" and b.get("followup", fus[0], "judgment") == "重点推进"
            assert tstore.link_id(b.get("followup", fus[0], "author")) == "usrADM" and tstore.link_id(b.get("followup", fus[0], "opportunity")) == o["id"]
            assert b.get("opportunity", b.record("opportunity", o["id"]), "stage") == "准备投标"
            assert "准备投标" in page.inner_text("#head")
            # 要一份项目报告:报告表多一行 待生成,范围商机 = 这个商机,发起人 = 登录人
            page.click("#rpSave")
            page.wait_for_function("document.querySelector('#reports').innerText.includes('待生成')", timeout=15000)
            rp = [r for r in b.rows("report", props=["kind", "gen_status", "opportunity", "requested_by", "edition"]) if b.get("report", r, "gen_status") == "待生成"]
            assert len(rp) == 1 and b.get("report", rp[0], "kind") == "项目报告" and tstore.link_id(b.get("report", rp[0], "opportunity")) == o["id"]
            assert tstore.link_id(b.get("report", rp[0], "requested_by")) == "usrADM" and b.get("report", rp[0], "edition") == "内部版"
            # 「我来负责」
            page.click("#mine")
            page.wait_for_function("document.querySelector('#ownerNow').innerText.includes('Ada')", timeout=15000)
            assert tstore.link_id(b.get("opportunity", b.record("opportunity", o["id"]), "owner")) == "usrADM"
            # 线索处理(原待我处理):两条归属待确认的线索、一个 AI 推荐的商机
            page.wait_for_load_state("networkidle")
            page.goto(base + "/inbox.html")
            page.wait_for_selector("#assign .item", timeout=15000)
            assert page.locator("#assign .item").count() == 2 and page.locator("#suggest .item").count() == 1
            assert page.inner_text("#nAssign") == "2" and page.inner_text("#nSuggest") == "1"  # 标签页上带数字,不用往下翻
            # 筛选「像是新项目」只剩一条,再回到全部;选项上的数字跟着其余筛选变(使用方 2026-10-09:数字要实时),数到 0 的不列
            chips = lambda sel: page.locator(sel + " .chip-btn").all_inner_texts()  # noqa: E731
            assert sorted(chips("#fK")) == sorted(["全部", "招标 1", "意向书 1"]) and chips("#fS") == ["全部", "机器有建议商机 1", "像是新项目 1"]
            page.click("#fS .chip-btn:has-text('像是新项目')")
            assert page.locator("#assign .item:visible").count() == 1 and chips("#fK") == ["全部", "意向书 1"] and "(1)" in page.inner_text("#fC")
            page.click("#fS .chip-btn:has-text('全部')")
            assert page.locator("#assign .item:visible").count() == 2
            # 一键归入机器建议的商机 → 线索挂上 AZE-002、已处理,从清单消失
            page.click("#assign .item:has-text('Sumgait') button:has-text('归入 AZE-002')")
            page.wait_for_function("document.querySelectorAll('#assign .item').length == 1", timeout=15000)
            assert chips("#fS") == ["全部", "像是新项目 1"] and chips("#fK") == ["全部", "意向书 1"]  # 处理掉的条目不再计数
            lb = next(r for r in b.rows("lead", props=["key", "opportunity", "status"]) if b.get("lead", r, "key") == "bbbbbbbb")
            assert b.get("lead", lb, "status") == "已处理" and b.get("opportunity", b.record("opportunity", tstore.link_id(b.get("lead", lb, "opportunity"))), "code") == "AZE-002"
            # 像是新项目的那条:新建商机并归入 → 新商机 AZE-003、跟踪中、负责人是登录人、出处线索挂上;线索归入它
            page.once("dialog", lambda d: d.accept("哈卡里恰伊输水干管"))
            page.click("#assign .item:has-text('reservoir') button:has-text('新建商机')")
            page.wait_for_function("document.querySelectorAll('#assign .item').length == 0", timeout=15000)
            new = next(r for r in b.rows("opportunity", props=["code", "name_zh", "stage", "owner", "sources", "country"]) if b.get("opportunity", r, "code") == "AZE-003")
            assert b.get("opportunity", new, "name_zh") == "哈卡里恰伊输水干管" and b.get("opportunity", new, "stage") == "跟踪中" and tstore.link_id(b.get("opportunity", new, "owner")) == "usrADM"
            lc = next(r for r in b.rows("lead", props=["key", "opportunity", "status"]) if b.get("lead", r, "key") == "cccccccc")
            assert tstore.link_id(b.get("lead", lc, "opportunity")) == new["id"] and b.get("lead", lc, "status") == "已处理"
            # AI 推荐的商机(另一个标签页):全选 → 批量跟踪 → 阶段跟踪中、负责人是登录人
            page.click("#tabs button[data-t='suggest']")
            # AI 推荐的商机也有筛选:国别、公开进度、核实
            assert "阿塞拜疆(1)" in page.inner_text("#gC") and chips("#gP") == ["全部", "未填 1"] and chips("#gV") == ["全部", "未填 1"]
            page.check("#allSuggest")
            page.once("dialog", lambda d: d.accept())
            page.click("#bTrack")
            page.wait_for_function("document.querySelectorAll('#suggest .item').length == 0", timeout=15000)
            s2 = next(r for r in b.rows("opportunity", props=["code", "stage", "owner"]) if b.get("opportunity", r, "code") == "AZE-002")
            assert b.get("opportunity", s2, "stage") == "跟踪中" and tstore.link_id(b.get("opportunity", s2, "owner")) == "usrADM"
            # 报告页:历史清单带筛选和下载;要一份国别报告 → 报告表多一行 待生成、范围国别 = 阿塞拜疆
            page.wait_for_load_state("networkidle")
            page.goto(base + "/reports.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert "周报_2026-W40.docx" in page.inner_text("#list") and page.locator("#fKind .chip-btn").count() == 7  # 全部 + 本体的六种类型(含日报、招标监测)
            page.click("#fKind .chip-btn:has-text('国别报告')")
            assert "周报" not in page.inner_text("#list")
            page.click("#fKind .chip-btn:has-text('全部')")
            page.select_option("#rpCountry", label="阿塞拜疆")
            page.click("#rpSave")
            page.wait_for_function("document.querySelector('#list').innerText.includes('国别报告')", timeout=15000)
            # 2026-10-16 使用方:清单按发起时间倒排(刚要的在最上面,不只按日期);类型、版本、状态都是带颜色的标签;筛选计数跟着变;0 条时说为什么
            first = page.locator("#list tbody tr").first
            assert first.locator(".tag.t-info").inner_text() == "国别报告" and first.locator("td").nth(3).locator(".tag").get_attribute("class") == "tag" and first.locator("td").nth(3).inner_text() == "内部版" and "待生成" in first.locator("td").nth(4).inner_text()
            assert first.locator(".tag").count() == 3 and page.locator("#list tbody tr:has-text('2026-W40') .tag").first.get_attribute("class") == "tag"  # 日报、周报是灰标签
            assert "全部 4" in page.inner_text("#fKind") and "国别报告 1" in page.inner_text("#fKind") and "待生成 2" in page.inner_text("#fSt")
            page.click("#fEd .chip-btn:has-text('公开版')")
            assert page.locator("#list .empty").count() == 1 and "版本 公开版" in page.inner_text("#list .empty") and "全部 0" in page.inner_text("#fKind")
            page.click("#list #fClear")
            assert page.locator("#list tbody tr").count() == 4 and "4 份" in page.inner_text("#count")
            rc = [r for r in b.rows("report", props=["kind", "gen_status", "country", "requested_by"]) if b.get("report", r, "kind") == "国别报告"]
            assert len(rc) == 1 and b.get("report", rc[0], "gen_status") == "待生成" and tstore.link_id(b.get("report", rc[0], "country")) == c["id"] and tstore.link_id(b.get("report", rc[0], "requested_by")) == "usrADM"
            # 报告模板:页面上传一个(新行 + 文件)、设为缺省;再要一份项目报告时能选它,报告行记下模板
            assert "还没有模板" in page.inner_text("#tpls")
            page.fill("#tpName", "集团版式")
            page.click("#tpKinds .chip-btn:has-text('项目报告')")
            page.click("#tpKinds .chip-btn:has-text('周报')")
            page.set_input_files("#tpFile", {"name": "集团版式.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "buffer": b"PK fake template"})
            page.check("#tpDefault")
            page.click("#tpSave")
            page.wait_for_function("document.querySelector('#tpls').innerText.includes('集团版式.docx')", timeout=15000)
            tp = b.rows("template", props=["name", "kinds", "file", "is_default"])
            assert len(tp) == 1 and b.get("template", tp[0], "name") == "集团版式" and set(b.get("template", tp[0], "kinds")) == {"项目报告", "周报"}
            assert b.get("template", tp[0], "is_default") is True and b.get("template", tp[0], "file")[0]["name"] == "集团版式.docx"
            page.select_option("#rpKind", "项目报告")
            assert page.locator("#rpTpl option").count() == 2  # 缺省 + 刚传的
            page.select_option("#rpTpl", tp[0]["id"])
            page.select_option("#rpOpp", label="AZE-001 甘贾污水厂")
            page.click("#rpSave")
            page.wait_for_function("document.querySelectorAll('#list tr').length >= 4", timeout=15000)
            rp2 = [r for r in b.rows("report", props=["kind", "template", "opportunity"]) if b.get("report", r, "kind") == "项目报告" and tstore.link_id(b.get("report", r, "template"))]
            assert len(rp2) == 1 and tstore.link_id(b.get("report", rp2[0], "template")) == tp[0]["id"]
            # 公司资料页:新增一项(多选领域、适用国别、附件、写进报告)→ 表里多一行;列表里点「不附」切换;改这一项
            page.goto(base + "/company.html")
            page.wait_for_function("document.querySelector('#cKind option') && !document.querySelector('#list').innerText.includes('读取中')", timeout=15000)
            assert "还没有我方资料" in page.inner_text("#list") and "公司资料" not in page.inner_text("#nav")  # 不在主菜单,从「我的」进
            page.select_option("#cKind", "业绩")
            assert page.input_value("#cOwner") == "本单位"  # 归属缺省本单位(v2.28)
            page.fill("#cName", "巴库净水厂扩建")
            page.fill("#cContent", "EPC 总承包,2023 年完工")
            page.click("#cSectors .chip-btn:has-text('供水')")
            page.select_option("#cCountryPick", label="阿塞拜疆")
            page.fill("#cYear", "2023")
            page.fill("#cAmount", "1.2 亿美元")
            page.set_input_files("#cFile", {"name": "完工证明.pdf", "mimeType": "application/pdf", "buffer": b"%PDF fake"})
            page.click("#cSave")
            page.wait_for_function("document.querySelector('#list').innerText.includes('巴库净水厂扩建')", timeout=15000)
            cr = b.rows("company", props=["kind", "name", "sectors", "countries", "year", "amount", "file", "for_report", "owner"])
            assert len(cr) == 1 and b.get("company", cr[0], "kind") == "业绩" and b.get("company", cr[0], "owner") == "本单位" and b.get("company", cr[0], "sectors") == ["供水"] and tstore.link_ids(b.get("company", cr[0], "countries")) == [c["id"]]
            assert b.get("company", cr[0], "year") == 2023 and b.get("company", cr[0], "for_report") is True and b.get("company", cr[0], "file")[0]["name"] == "完工证明.pdf"
            assert "完工证明.pdf" in page.inner_text("#list") and "✓ 附进报告" in page.inner_text("#list")
            page.click("#list button[data-a='for']")
            page.wait_for_function("document.querySelector('#list').innerText.includes('不附')", timeout=15000)
            assert not b.get("company", b.rows("company", props=["for_report"])[0], "for_report")  # 勾掉 = 空(表格不存 false)
            page.click("#list button[data-a='edit']")
            assert page.input_value("#cName") == "巴库净水厂扩建" and page.inner_text("#formTitle") == "改这一项"
            page.fill("#cName", "巴库净水厂扩建(一期)")
            page.click("#cSave")
            page.wait_for_function("document.querySelector('#list').innerText.includes('(一期)')", timeout=15000)
            assert len(b.rows("company")) == 1
            # 兄弟单位的业绩:记进来但标出来、不给「附进报告」按钮;按归属筛选能分开
            b.create("company", [{"kind": "业绩", "name": "中铁一局西安污水厂", "year": 2013, "owner": "兄弟单位", "for_report": True}])
            page.reload()
            page.wait_for_function("document.querySelector('#list').innerText.includes('中铁一局西安污水厂')", timeout=15000)
            assert page.locator("#list .tag:has-text('兄弟单位')").count() == 1 and "不附(非本单位)" in page.inner_text("#list") and page.locator("#list button[data-a='for']").count() == 1
            page.click("#fOwner .chip-btn:has-text('兄弟单位')")
            assert "中铁一局西安污水厂" in page.inner_text("#list") and "巴库净水厂扩建" not in page.inner_text("#list") and "1 / 2 项" in page.inner_text("#count")
            page.click("#fOwner .chip-btn:has-text('全部')")
            # 「我的」页:帐号与角色;关注一个国别 → 个人设置表建一行(账号 = 登录人);日历页「存到我的」→ 订阅出现在这里,可删
            page.goto(base + "/me.html")
            page.wait_for_function("document.querySelector('#cPick option') && !document.querySelector('#countries').innerText.includes('读取中')", timeout=15000)
            assert "Ada" in page.inner_text("#who") and "所有者" in page.inner_text("#who") and "还没关注国别" in page.inner_text("#countries")
            assert b.rows("preference") == []
            page.select_option("#cPick", label="阿塞拜疆")
            page.wait_for_function("document.querySelector('#countries').innerText.includes('阿塞拜疆')", timeout=15000)
            pf = b.rows("preference", props=["name", "user", "countries", "subscriptions"])
            assert len(pf) == 1 and tstore.link_id(b.get("preference", pf[0], "user")) == "usrADM" and tstore.link_ids(b.get("preference", pf[0], "countries")) == [c["id"]] and b.get("preference", pf[0], "name") == "Ada"
            assert "country.html?iso=AZE" in page.inner_html("#countries")
            page.goto(base + "/opps.html")  # 关注的国家:商机前面带 ★,国别下拉最上面一组是「★ 关注」
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator("#list .star").count() >= 2 and page.locator("#list .star").count() == page.locator("#list tbody tr").count()  # 全是阿塞拜疆的
            page.goto(base + "/country.html?iso=AZE")
            page.wait_for_function("document.querySelector('#pick optgroup')", timeout=15000)
            assert page.locator("#pick optgroup").first.get_attribute("label") == "★ 关注"
            page.goto(base + "/calendar.html")
            page.wait_for_selector("#list table", timeout=15000)
            page.select_option("#fCountry", label="阿塞拜疆")
            page.click("#subBtn")
            page.wait_for_selector("#subSave", timeout=15000)
            page.click("#subSave")
            page.wait_for_function("document.querySelector('#toast') && document.querySelector('#toast').innerText.includes('已存到')", timeout=15000)
            pf = b.rows("preference", props=["subscriptions"])
            assert len(pf) == 1 and "日历 · 阿塞拜疆 | webcal://water.example/portal/feed/calendar.ics?t=" in str(b.get("preference", pf[0], "subscriptions"))
            page.goto(base + "/me.html")
            page.wait_for_selector("#subs table", timeout=15000)
            assert "日历 · 阿塞拜疆" in page.inner_text("#subs") and "我的" in page.inner_text("#acct") and page.locator("main a[href='company.html']").count() == 1
            page.click("#countries button")  # 取消关注
            page.wait_for_function("document.querySelector('#countries').innerText.includes('还没关注')", timeout=15000)
            page.click("#subs button[data-a='del']")
            page.wait_for_function("document.querySelector('#subs').innerText.includes('还没保存')", timeout=15000)
            pf = b.rows("preference", props=["countries", "subscriptions"])
            assert len(pf) == 1 and not tstore.link_ids(b.get("preference", pf[0], "countries")) and not b.get("preference", pf[0], "subscriptions")
            # 忽略国别:没设过默认忽略中国;「我的」地图上点一个国家 → 个人设置「忽略国别」写国别码;再点恢复;按大洲、按经济水平一键忽略,
            # 清单里整组合成一项;一个都不忽略存「-」;国别概况、线索总览的地图上忽略的国家涂成灰色,地图下有说明
            page.wait_for_function("document.querySelector('#iConts button') && !document.querySelector('#iList').innerText.includes('读取')", timeout=15000)
            assert page.inner_text("#iList").strip().startswith("中国") and page.locator("#iConts button").count() == 6 and page.locator("#iEcon button").count() == 5
            click = "(iso) => { const c = echarts.getInstanceByDom(document.getElementById('iMap')); c.getZr().handler.dispatch('click', {}); c.trigger('click', {name: iso}); }"
            page.evaluate(click, "CHN")  # 中国点掉
            page.wait_for_function("document.querySelector('#iList').innerText.includes('没有忽略')", timeout=15000)
            ign = lambda: b.get("preference", b.rows("preference", props=["ignored"])[0], "ignored")
            assert ign() == "-"
            page.evaluate(click, "AZE")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('阿塞拜疆')", timeout=15000)
            assert ign() == "AZE" and page.inner_text("#iCount") == "1 个"
            page.evaluate(click, "AZE")  # 再点恢复
            page.wait_for_function("document.querySelector('#iList').innerText.includes('没有忽略')", timeout=15000)
            assert ign() == "-"
            page.click("#iConts button:has-text('忽略北美洲')")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('北美洲(全部)')", timeout=15000)
            assert {"USA", "CAN", "MEX", "CUB"} <= set(ign().split(",")) and "BRA" not in ign() and page.locator("#iConts button:has-text('恢复北美洲')").count() == 1
            page.click("#iList button[data-g='北美洲']")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('没有忽略')", timeout=15000)
            page.click("#iEcon button:has-text('忽略发达国家')")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('发达国家(全部)')", timeout=15000)
            assert {"USA", "DEU", "JPN", "KOR", "AUS"} <= set(ign().split(",")) and not {"SAU", "ARE", "BRA"} & set(ign().split(","))
            page.click("#iList button[data-g='adv']")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('没有忽略')", timeout=15000)
            page.select_option("#iPick", label="阿塞拜疆")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('阿塞拜疆')", timeout=15000)
            assert ign() == "AZE"
            hatched = "(() => { const o = echarts.getInstanceByDom(document.getElementById('map')).getOption(); const s = o.series[0]; return s.data.filter((d) => d.itemStyle && d.itemStyle.areaColor === P.IGNORED_COLOR && d.value === '-').map((d) => d.name); })()"
            for url in ("/country.html?iso=AZE", "/overview.html"):
                page.goto(base + url)
                page.wait_for_selector(".hatch-note", timeout=15000)
                assert page.evaluate(hatched) == ["AZE"] and "1 个" in page.inner_text(".hatch-note"), url
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_guest_viewer_sees_everything_but_cannot_write():
    """游客演示帐号 = 库里的「查看者」:页面照常浏览,写入区块灰掉、点不动,页面顶部有一行说明;期间没有任何写请求打到表格。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, token="guest-token")
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text} @ {page.url}") if m.type == "error" and "/media/" not in (m.location or {}).get("url", "") else None)  # 首页探视频文件,没有是正常的 404
            # 商机工作台:数据都在,三个写入区块的按钮全禁用,硬点保存也没有写请求
            page.goto(base + f"/opp.html?id={o['id']}")
            page.wait_for_selector("#timeline .tl", timeout=15000)
            assert "游客演示帐号" in page.inner_text(".guest-note") and "与业主通话" in page.inner_text("#timeline")
            page.wait_for_function("document.querySelector('#acct .who b')", timeout=15000)  # 名字先出来,角色标签读完角色才补上
            assert "游客" in page.inner_text("#acct .who") and "只读" in page.inner_text("#acct .who") and "所有者" not in page.inner_text("#acct")
            for sel in ("#fuSave", "#rpSave", "#mine", "#quickSave", "#fuContent"):
                assert page.locator(sel).is_disabled(), sel
            page.evaluate("document.getElementById('fuSave').click()")
            # 线索处理:条目和计数照常,每条的按钮与批量按钮都灰掉;勾选后批量按钮仍然禁用
            page.goto(base + "/inbox.html")
            page.wait_for_selector("#assign .item", timeout=15000)
            assert page.locator(".guest-note").count() == 1 and page.locator("#assign .it-act button").count() >= 2
            assert all(page.locator("#assign .it-act button").nth(i).is_disabled() for i in range(page.locator("#assign .it-act button").count()))
            assert page.locator("#allAssign").is_disabled() and page.locator("#bSug").is_disabled() and page.locator("#bIgn").is_disabled()  # 全选与批量都灰掉
            page.click("#tabs button[data-t=suggest]")  # 页签切换不受影响
            assert page.locator("#tab-suggest").get_attribute("class") == "tab on"
            # 报告页:清单和筛选能用,要报告、传模板的按钮禁用
            page.goto(base + "/reports.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator(".guest-note").count() == 1 and page.locator("#rpSave").is_disabled() and page.locator("#tpSave").is_disabled()
            assert page.locator("#fKind .chip-btn").count() > 1 and page.locator("#fKind .chip-btn").first.is_enabled()
            # 线索总览:没有说明条;清单里没归入的线索也没有「转为商机」
            page.goto(base + "/overview.html?q=ccccc")
            page.wait_for_selector("#drill table", timeout=15000)
            assert page.locator(".guest-note").count() == 0 and page.locator("#drill button.conv").count() == 0
            assert not errors, errors
            page.click("#acct #logout")  # 退出:先调表格的登出接口,再去登录页(本地小服务没有登录页,只看接口被调到)
            page.wait_for_timeout(1500)
            browser.close()
    finally:
        srv.shutdown()
    writes = [c for c in fake.calls[seeded:] if c[0] != "GET"]
    assert writes == [("POST", "/api/auth/signout")], writes


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_outsider_guest_sees_only_public_content_through_the_worker():
    """游客演示帐号移出库以后(登录了、但不是库协作者):页面照常打开,所有读表走后台的只读通道;看得到线索、商机的公开事实、国别、节点与
    定期报告的公开版;看不到跟进记录、内部备注、负责人、内部版报告、报告模板;没有「打开表格」「在表格里打开」;浏览器没有一个读表请求直接打到表格。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    from datetime import date
    today = date.today()
    b.create("report", [{"kind": "周报", "day": today.isoformat(), "period": "2026-W41", "edition": "公开版", "gen_status": "完成", "key": "周报_2026-W41_公开版.docx"},
                        {"kind": "项目报告", "day": today.isoformat(), "opportunity": o["id"], "edition": "内部版", "gen_status": "完成", "key": "项目报告_AZE-001_内部版.docx"}])
    b.update("opportunity", o["id"], {"internal_note": "报价底线 4000 万", "priority": "A"})
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, token="outsider-token")
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text} @ {page.url}") if m.type == "error" and "/media/" not in (m.location or {}).get("url", "") and "403" not in m.text else None)  # 问库里的角色是 403:游客就是这样认出来的
            # 商机工作台:公开事实在,内部备注、优先级、负责人、跟进记录、内部版报告都不在;没有「在表格里打开」
            page.goto(base + f"/opp.html?id={o['id']}")
            page.wait_for_selector(".guest-note", timeout=15000)
            page.wait_for_function("document.querySelector('#acct .who') && document.querySelector('#acct .who').textContent.includes('游客')", timeout=15000)
            page.wait_for_function("!document.body.innerText.includes('读取中') && document.querySelector('#reports').innerText.trim() !== ''", timeout=15000)
            body = page.inner_text("body")
            assert "游客帐号" in page.inner_text(".guest-note") and "甘贾污水厂" in body and "采购进行中" in body
            assert "报价底线" not in body and "与业主通话" not in body and "内部版" not in page.inner_text("#reports") and page.locator("#note").input_value() == ""
            assert page.locator("a.open:visible").count() == 0 and page.locator("a.table:visible").count() == 0
            assert page.locator("#fuSave").is_disabled() and page.locator("#quickSave").is_disabled()
            # 报告页:只有定期报告的公开版;模板页签空;没有「打开表格」
            page.goto(base + "/reports.html")
            page.wait_for_selector("#list table", timeout=15000)
            rows = page.inner_text("#list")
            assert "2026-W41" in rows and "内部版" not in rows and "项目报告" not in rows and "2026-W40" not in rows  # 没写版本的那份周报不算公开版
            assert page.locator("#tbl:visible").count() == 0
            # 总览与日历:数字和节点照常,到期(跟进)没有;没有去表格的链接
            page.goto(base + "/overview.html")
            page.wait_for_selector("#reports table", timeout=15000)
            assert page.locator("a[href^='/base/']:visible").count() == 0 and "阿塞拜疆" in page.inner_text("body")
            page.goto(base + "/calendar.html")
            page.wait_for_selector("#cal .chip", timeout=15000)
            assert page.locator("#cal .chip.ms:visible").count() >= 1 and page.locator("#cal .chip.due").count() == 0
            # 公司资料与「我的」:游客只看到说明,不读这两张表
            page.goto(base + "/company.html")
            page.wait_for_function("document.querySelector('#list').innerText.includes('游客')", timeout=15000)
            assert page.locator("#form").is_hidden() and "内部资料" in page.inner_text(".guest-note")
            page.goto(base + "/me.html")
            page.wait_for_function("document.querySelector('#subs').innerText.includes('游客')", timeout=15000)
            assert "游客" in page.inner_text("#who") and page.locator("#acct a[href='me.html']").count() == 0
            # 菜单里没有「打开表格」,帐号区标「游客 · 只读」
            page.goto(base + "/index.html")
            page.wait_for_function("document.querySelector('#acct .who b')", timeout=15000)  # 名字先出来,角色标签读完角色才补上
            assert page.locator("#menu a.table:visible").count() == 0 and "游客 · 只读" in page.inner_text("#acct .who")
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    direct = [p for p in srv.browser_api if p[1].startswith("/api/table/")]
    assert direct == [], direct  # 浏览器没有直接读过表格
    assert all(p[0] == "GET" or p[1] == "/api/auth/signout" for p in srv.browser_api)
    writes = [x for x in fake.calls[seeded:] if x[0] != "GET"]
    assert writes == [], writes


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_accounts_page_lets_the_owner_manage_colleagues_and_partners():
    """账号管理页(仅所有者 / 创建者):同事清单带角色下拉,改角色、两步移出都以登录人身份打到表格;生成 / 删除邀请链接;
    伙伴表增一行(邮箱、单位、可看国别、启用)、停用一行,改完通知后台重读名单;编辑者打开只看到一句说明。帐号区与「我的」页有入口。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    b.create("partner_account", [{"email": "old@bewg.example", "org": "北控水务", "enabled": True}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, token="admin-token")
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(base + "/accounts.html")
            page.wait_for_selector("#members table", timeout=15000)
            page.wait_for_selector("#partners table", timeout=15000)
            assert "账号管理" in page.inner_text("#acct")
            rows = page.inner_text("#members")
            assert "Ada" in rows and "所有者(我)" in rows and "游客" in rows and "张三" in rows and "outsider@example.com" not in rows  # 库外的不在
            # 改张三的角色成查看者
            page.select_option("#members select[data-id='usrZS']", "viewer")
            page.wait_for_function("document.querySelector('#toast') && document.querySelector('#toast').textContent.includes('角色已改成')", timeout=15000)
            assert fake.role_changes == [("usrZS", "viewer")]
            # 两步移出游客演示帐号(usrGST)
            page.click("#members button[data-a='out'][data-id='usrGST']")
            assert "确认移出" in page.inner_text("#members button[data-a='out'][data-id='usrGST']")
            page.click("#members button[data-a='out'][data-id='usrGST']")
            page.wait_for_function("!document.querySelector(\"#members button[data-a='out'][data-id='usrGST']\")", timeout=15000)
            assert fake.removed == ["usrGST"]
            # 邀请链接:生成(查看者)、出现在清单、删除
            page.select_option("#invRole", "viewer")
            page.click("#invMake")
            page.wait_for_selector("#invites li", timeout=15000)
            assert "查看者" in page.inner_text("#invites") and "invite/link" in page.inner_text("#invites") and fake.invitations[-1]["role"] == "viewer"
            page.click("#invites button[data-a='del']")
            page.wait_for_function("!document.querySelector('#invites li')", timeout=15000)
            assert fake.invitations == []
            # 伙伴:已有一行;加一行带可看国别;停用
            assert "old@bewg.example" in page.inner_text("#partners") and "全部国别" in page.inner_text("#partners")
            page.fill("#pEmail", "New@Partner.example")
            page.fill("#pOrg", "新伙伴")
            page.select_option("#pPick", c["id"])
            assert "阿塞拜疆" in page.inner_text("#pCountries")
            page.click("#pSave")
            page.wait_for_function("document.querySelector('#partners').innerText.includes('new@partner.example')", timeout=15000)
            rows = b.rows("partner_account")
            new = next(r for r in rows if b.get("partner_account", r, "email") == "new@partner.example")
            assert tstore.link_ids(b.get("partner_account", new, "countries")) == [c["id"]] and b.get("partner_account", new, "org") == "新伙伴" and b.get("partner_account", new, "enabled") is True
            assert "阿塞拜疆" in page.inner_text("#partners")
            page.click("#partners button[data-a='toggle'][data-id='" + new["id"] + "']")
            page.wait_for_function("document.querySelector('#partners').innerText.includes('已停用')", timeout=15000)
            assert not b.get("partner_account", b.record("partner_account", new["id"]), "enabled")
            assert any(p[1] == "/guest/refresh" for p in srv.bot_calls), srv.bot_calls  # 通知后台重读名单
            # 重复邮箱不让加
            page.fill("#pEmail", "old@bewg.example")
            page.click("#pSave")
            page.wait_for_function("document.querySelector('#toast') && document.querySelector('#toast').textContent.includes('已经在名单里')", timeout=15000)
            # 「我的」页有入口
            page.goto(base + "/me.html")
            page.wait_for_function("document.querySelector('#acctAdmin') && !document.querySelector('#acctAdmin').hidden", timeout=15000)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    # 编辑者:只看到说明
    srv2 = _serve(fake, cfg, token="bot-token")
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{srv2.server_port}/accounts.html")
            page.wait_for_selector("#gate .note", timeout=15000)
            assert "只有库的所有者、创建者能用" in page.inner_text("#gate") and page.locator("#body").is_hidden()
            assert "账号管理" not in page.inner_text("#acct") or page.locator("#acct a[href='accounts.html']").count() == 0
            browser.close()
    finally:
        srv2.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_partner_with_country_scope_sees_only_those_countries():
    """表「伙伴帐号」里写了可看国别(只有乌兹别克斯坦)的伙伴:线索总览与国别页只有乌兹别克斯坦,阿塞拜疆的线索、国别、报告都不出现;顶部说明写明只看这些国家。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    from datetime import date
    today = date.today()
    uzb = b.create("country", [{"name_zh": "乌兹别克斯坦", "key": "UZB", "region": "亚洲 · 中亚", "market": "重点"}])[0]
    b.create("lead", [{"key": "x-uzb-1", "title": "Tashkent north water network", "summary_zh": "塔什干北部供水管网", "url": "https://example.org/uzb1", "country": uzb["id"], "found_at": today.isoformat(), "relevant": True, "category": "招标"}])
    b.create("report", [{"kind": "国别报告", "day": today.isoformat(), "country": c["id"], "edition": "公开版", "gen_status": "完成", "key": "国别报告_AZE_公开版.docx"},
                        {"kind": "国别报告", "day": today.isoformat(), "country": uzb["id"], "edition": "公开版", "gen_status": "完成", "key": "国别报告_UZB_公开版.docx"}])
    b.create("partner_account", [{"email": "chen@bewg.example", "org": "北控水务", "countries": [uzb["id"]], "enabled": True}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, token="partner-token", sample_data=True)
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(base + "/reports.html")
            page.wait_for_selector(".guest-note", timeout=15000)
            page.wait_for_selector("#list table", timeout=15000)
            assert "只看这些国家:乌兹别克斯坦" in page.inner_text(".guest-note")
            rows = page.inner_text("#list")
            assert "乌兹别克斯坦" in rows and "阿塞拜疆" not in rows
            page.goto(base + "/overview.html")
            page.wait_for_function("document.querySelector('#acct .who b') && !document.body.innerText.includes('读取中')", timeout=15000)
            body = page.inner_text("body")
            assert "乌兹别克斯坦" in body and "阿塞拜疆" not in body and "甘贾" not in body and "Ganja" not in body  # 国别筛选里只有乌兹别克斯坦
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    direct = [p for p in srv.browser_api if p[1].startswith("/api/table/")]
    assert direct == [], direct


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_partner_sees_real_public_content_but_no_opportunities():
    """伙伴帐号(库外、邮箱在伙伴名单里,本体 partner 节):读的是真实库——线索总览有真国别,报告页只有公开版;商机总览、商机工作台、日历、线索处理
    直接打开只给一句说明,菜单里也不列;我方资料不给;帐号区标「伙伴 · 只读」;浏览器没有一个读表请求直接打到表格,没有写。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    from datetime import date
    today = date.today()
    b.create("report", [{"kind": "周报", "day": today.isoformat(), "period": "2026-W41", "edition": "公开版", "gen_status": "完成", "key": "周报_2026-W41_公开版.docx"},
                        {"kind": "国别报告", "day": today.isoformat(), "country": c["id"], "edition": "公开版", "gen_status": "完成", "key": "国别报告_AZE_公开版.docx"},
                        {"kind": "项目报告", "day": today.isoformat(), "opportunity": o["id"], "edition": "内部版", "gen_status": "完成", "key": "项目报告_AZE-001_内部版.docx"}])
    b.update("opportunity", o["id"], {"internal_note": "报价底线 4000 万", "priority": "A"})
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, token="partner-token", sample_data=True)  # 游客这时看示例;伙伴不受影响,读真实库
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text} @ {page.url}") if m.type == "error" and "/media/" not in (m.location or {}).get("url", "") and "403" not in m.text else None)
            # 线索总览:真实国别在,示例的没有;帐号区「伙伴 · 只读」;菜单里没有商机总览、日历、线索处理
            page.goto(base + "/overview.html")
            page.wait_for_function("document.querySelector('#acct .who b') && document.body.innerText.includes('阿塞拜疆')", timeout=15000)
            page.wait_for_function("!document.body.innerText.includes('读取中')", timeout=15000)
            body = page.inner_text("body")
            assert "塔什干北区供水管网改造" not in body and "报价底线" not in body
            assert "伙伴 · 只读" in page.inner_text("#acct .who")
            assert page.locator("#nav a[href='opps.html']").count() == 0 and page.locator("#nav a[href='calendar.html']").count() == 0 and page.locator("#nav a[href='inbox.html']").count() == 0
            assert page.locator("#nav .groups a[href='reports.html']").count() == 1 and page.locator("#nav .groups a[href='country.html']").count() == 1  # 组名「商机跟进」改指报告页
            assert page.locator("a[href^='/base/']:visible").count() == 0
            # 报告页:公开版的周报、国别报告在,内部版的项目报告不在
            page.goto(base + "/reports.html")
            page.wait_for_selector("#list table", timeout=15000)
            page.wait_for_selector(".guest-note", timeout=15000)
            rows = page.inner_text("#list")
            assert "伙伴帐号" in page.inner_text(".guest-note") and "2026-W41" in rows and "国别报告" in rows and "内部版" not in rows and "项目报告" not in rows
            # 商机总览、商机工作台、日历、线索处理:只有一句说明,没有商机内容
            for path in ("/opps.html", f"/opp.html?id={o['id']}", "/calendar.html", "/inbox.html"):
                page.goto(base + path)
                page.wait_for_selector(".guest-note", timeout=15000)
                txt = page.inner_text("body")
                assert "伙伴帐号不开放" in txt and "甘贾污水厂" not in txt and "AZE-001" not in txt, path
            # 我方资料:不给
            page.goto(base + "/company.html")
            page.wait_for_function("document.querySelector('#list').innerText.includes('伙伴帐号')", timeout=15000)
            assert page.locator("#form").is_hidden()
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    direct = [p for p in srv.browser_api if p[1].startswith("/api/table/")]
    assert direct == [], direct
    writes = [x for x in fake.calls[seeded:] if x[0] != "GET"]
    assert writes == [], writes


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_outsider_guest_sees_sample_data_only():
    """游客只看示例数据(本体 guest.data: sample):各页的线索、商机、国别、节点、日报都是 core/sample.py 编的,真实库的商机一条不出现;
    顶部说明写明是示例数据。SHOT=目录 时把几页截图存下来(给使用方看)。"""
    import os
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg, token="outsider-token", sample_data=True)
    base = f"http://127.0.0.1:{srv.server_port}"
    shot = os.environ.get("SHOT")
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(base + "/opps.html")
            page.wait_for_function("document.body.innerText.includes('UZB-901')", timeout=15000)
            body = page.inner_text("body")
            assert "甘贾污水厂" not in body and "AZE-001" not in body and "塔什干北区供水管网改造" in body
            if shot:
                page.screenshot(path=f"{shot}/guest_sample_opps.png", full_page=False)
            page.click("text=UZB-901")
            page.wait_for_selector(".guest-note", timeout=15000)
            page.wait_for_function("document.body.innerText.includes('多边开发银行(示例)')", timeout=15000)
            assert "示例数据" in page.inner_text(".guest-note")
            if shot:
                page.wait_for_timeout(800)
                page.screenshot(path=f"{shot}/guest_sample_opp.png", full_page=False)
            page.goto(base + "/analysis.html")
            page.wait_for_function("document.querySelector('#brief') && document.querySelector('#brief').innerText.includes('示例')", timeout=15000)
            assert "Ganja" not in page.inner_text("body")
            if shot:
                page.wait_for_timeout(800)
                page.screenshot(path=f"{shot}/guest_sample_analysis.png", full_page=False)
            page.goto(base + "/country.html")
            page.wait_for_function("document.body.innerText.includes('乌兹别克斯坦')", timeout=15000)
            assert "阿塞拜疆" not in page.inner_text("body")
            if shot:
                page.wait_for_timeout(800)
                page.screenshot(path=f"{shot}/guest_sample_country.png", full_page=False)
            page.goto(base + "/calendar.html")
            page.wait_for_selector("#cal .chip", timeout=15000)
            assert page.locator("#cal .chip.ms").count() >= 1 and "AZE-001" not in page.inner_text("#cal")
            if shot:
                page.screenshot(path=f"{shot}/guest_sample_calendar.png", full_page=False)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert [p for p in srv.browser_api if p[1].startswith("/api/table/")] == []


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_phone_layout_uses_menu_bar_and_collapsed_filters():
    """手机(窄屏):内页也是「站名 + 菜单」顶栏,页面链接与帐号区在菜单卡片里;筛选区只剩一行「筛选 ▾ + 当前条件」,点开才展开。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 390, "height": 800})
            page.goto(base + "/overview.html")
            page.wait_for_selector("#reports table", timeout=15000)
            assert page.locator("#nav .menu-btn").count() == 1 and page.locator("#nav header nav").count() == 0  # 没有平铺的一排链接
            assert page.evaluate("document.body.classList.contains('menu-page')") and not page.locator("#menu").is_visible()
            head = page.locator("#filters .frow-head")
            assert head.is_visible() and page.evaluate("document.getElementById('filters').classList.contains('collapsed')")
            assert not page.locator("#fRegion").is_visible() and "全部" in head.inner_text()
            page.click("#filters .ftoggle")
            assert page.locator("#fRegion").is_visible() and "▴" in page.inner_text("#filters .ftoggle")
            page.locator("#fWin .chip-btn", has_text="最近 7 天").click()
            page.wait_for_function("document.querySelector('#filters .fsum').textContent.includes('最近 7 天')", timeout=5000)
            page.click("#filters .ftoggle")
            assert not page.locator("#fRegion").is_visible() and "最近 7 天" in head.inner_text()  # 收起后摘要还在
            # 筛选行横向滑动而不是换行:行高只有一排
            page.click("#filters .ftoggle")
            h = page.evaluate("document.getElementById('fCountry').parentElement.getBoundingClientRect().height")
            assert h < 60, h
            page.click("#nav .menu-btn")
            assert page.locator("#menu").is_visible() and page.locator("#menu .mgrp h3").all_inner_texts() == [g for g, _ in MENU_SPEC]
            page.wait_for_function("document.querySelector('#menu #acct .who')", timeout=15000)
            assert "退出登录" in page.inner_text("#menu #acct")
            page.click("#menuClose")
            # 电脑宽度:照旧平铺导航、筛选全展开
            wide = browser.new_page(viewport={"width": 1280, "height": 800})
            wide.goto(base + "/overview.html")
            wide.wait_for_selector("#reports table", timeout=15000)
            assert wide.locator("#nav .groups a").count() == 5 and wide.locator("#filters .frow-head").count() == 0 and wide.locator("#fRegion").is_visible()
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_home_fits_one_screen_and_keeps_the_whole_video():
    """首页首屏(仿 america.gov 的大圆角面板):1920×1080、1366×768 这类屏幕(浏览器里实际可见约 950、657 高)一屏放下顶栏、标题、
    提问框、视频卡、示例与说明;视频卡按「剩下的高度 × 片子宽高比」定宽,片子整幅显示(比例不变、不裁)。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    poster = '<svg xmlns="http://www.w3.org/2000/svg" width="1434" height="1050"><rect width="100%" height="100%" fill="#345"/></svg>'

    def media(route):  # 视频文件在私有配置仓:这里假装 mp4 在(HEAD 200),但浏览器放不了 → 换成封面图(1434×1050,和线上片子同比例)
        u, m = route.request.url, route.request.method
        if u.endswith("/media/hero.jpg"):
            return route.fulfill(status=200, content_type="image/svg+xml", body=poster)
        if u.endswith("/media/hero.mp4") and m == "HEAD":
            return route.fulfill(status=200, body="")
        return route.fulfill(status=404, body="")
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            for w, h in ((1920, 950), (1536, 730), (1366, 657), (390, 700)):
                page = browser.new_page(viewport={"width": w, "height": h})
                page.route("**/media/*", media)
                page.goto(base + "/index.html")
                page.wait_for_function("document.getElementById('llmNote').textContent.includes('按词表解析') && document.querySelector('.hero-media img') && document.querySelector('.hero-media img').naturalWidth > 0", timeout=15000)
                page.wait_for_timeout(300)
                box = page.evaluate("""() => {
                  const r = (s) => document.querySelector(s).getBoundingClientRect();
                  const m = r('.hero-media');
                  return { doc: r('#heroFrame').bottom, vh: innerHeight, card: r('.hero-card').bottom, ex: r('#examples').bottom, note: r('#llmNote').bottom,
                           mw: m.width, mh: m.height, fit: getComputedStyle(document.querySelector('.hero-media img')).objectFit };
                }""")
                assert box["doc"] <= box["vh"] + 1 and max(box["card"], box["ex"], box["note"]) <= box["vh"], (w, h, box)  # 首屏面板一屏放下(简介在面板下面,往下滚才看到)
                assert abs(box["mw"] / box["mh"] - 1434 / 1050) < 0.01 and box["fit"] == "contain", (w, h, box)  # 片子整幅、比例不变
                assert box["mw"] >= (300 if w < 640 else 340), (w, h, box)
                page.close()
            browser.close()
    finally:
        srv.shutdown()


def test_home_intro_sits_below_the_first_screen():
    """首屏下面的系统简介(使用方 2026-10-09,参考 america.gov):六段左图右文,各带一个入口;在首屏面板之下、往下滚才看到;
    图卡是页面画的示意,不读表;地图是内嵌的世界轮廓(不取地图文件,手机上也有陆地);提问切到对话模式后整段收起。手机上上下排,不出横向滚动。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            for w, h in ((1440, 900), (390, 760)):
                page = browser.new_page(viewport={"width": w, "height": h})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base + "/index.html")
                page.wait_for_selector("#intro .intro-row", timeout=15000)
                assert page.locator("#intro .intro-row").count() == 6
                assert page.locator("#intro .intro-text .more").evaluate_all("as => as.map(a => a.getAttribute('href'))") == ["how.html", "analysis.html", "country.html", "trend.html", "opps.html", "ontology.html"]
                top = page.evaluate("document.getElementById('intro').getBoundingClientRect().top")
                assert top >= page.evaluate("document.getElementById('heroFrame').getBoundingClientRect().bottom") and top >= h - 1  # 首屏看不到,往下滚才有
                land = page.evaluate("(() => { const b = document.querySelector('#intro .im-world .land').getBoundingClientRect(); return [b.width, b.height]; })()")
                assert land[0] > 200 and land[1] > 60 and page.locator("#intro .im-world circle").count() >= 20  # 陆地轮廓画出来了,不只是空框
                assert page.evaluate("document.documentElement.scrollWidth") <= w  # 不出横向滚动
                page.close()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(base + "/index.html")
            page.wait_for_function("document.getElementById('llmNote').textContent.includes('按词表解析')", timeout=15000)
            page.click("#examples .chip-btn")
            page.wait_for_function("document.body.classList.contains('asked')", timeout=15000)
            assert not page.locator("#intro").is_visible()  # 对话模式收起
            assert "游客" not in (PORTAL / "index.html").read_text(encoding="utf-8").split('id="intro"')[1].split("</section>")[0]  # 数据安全只讲本地(使用方 2026-10-09)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_country_key_figures_lead_and_negatives_are_red():
    """关键指标(使用方 2026-10-09):国别概况最前面一排大号指标卡,按 GDP、人均 GDP、增速……的顺序,挑出来的不在下面重复;
    负面的(负增长、经常账户逆差 ≥ 5% 等,门槛在 portal.js 的 TONES)数字标红并写原因;水风险块三项大号,高风险标红。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, _, _ = _seed(admin, fake)
    b = tstore.Base(admin)
    b.update("country", c["id"], {"indicators": "GDP(现价):226 亿美元(2025)(世行 WDI https://data.worldbank.org/)\n人均 GDP:3,081 美元(2025)(世行 WDI https://data.worldbank.org/)\n"
                                                "GDP 实际增速:−1.5%(2025)(世行 WDI https://data.worldbank.org/)\n经常账户/GDP:−23.4%(2025)(IMF WEO https://imf.org/)\nCPI 通胀:8.2%(2025)(世行 WDI https://data.worldbank.org/)",
                                  "water_stress": 3.3, "drought": 2.8, "gdp_pc": 8009})
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/country.html?iso=AZE&t=info")
            page.wait_for_selector("#info .kpis", timeout=15000)
            kp = page.locator("#info > .kpis .kpi")
            assert [t.replace("2025", "").strip() for t in kp.locator(".kl").all_inner_texts()] == ["GDP", "人均 GDP", "GDP 实际增速"]
            assert kp.nth(0).locator(".kn").inner_text() == "226" and kp.nth(0).locator(".ku").inner_text() == "亿美元"
            assert "bad" in kp.nth(2).get_attribute("class") and "负增长" in kp.nth(2).inner_text()  # 负增长:大号数字标红
            ind = page.locator("details.sec:has(summary:text('宏观与水务指标')) .facts")
            assert "人均 GDP" not in ind.inner_text() and "经常账户" in ind.inner_text()  # 关键指标不在下面重复
            assert ind.locator(".fact.is-bad").count() == 1 and "经常账户" in ind.locator(".fact.is-bad").inner_text()  # 只有逆差 ≥ 5% 标红,通胀 8.2% 不红
            risk = page.locator("details.sec:has(summary:text('水风险与经济')) .kpis .kpi")
            assert risk.count() == 2 and "bad" in risk.nth(0).get_attribute("class")  # 用水压力 3.3 分(高)标红;人均 GDP 不红
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_home_examples_match_the_assistant():
    """首页一打开就画的示例与后台 core/assistant.py 的 EXAMPLES 同一份(后台给的不一样页面才换,一样就不再动一次)。"""
    from core import assistant
    html = (PORTAL / "index.html").read_text(encoding="utf-8")
    m = re.search(r"const EXAMPLES = (\[[^\]]*\]);", html)
    assert m and json.loads(re.sub(r",\s*\n\s*", ", ", m.group(1))) == assistant.EXAMPLES


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_home_first_screen_does_not_wait_for_data():
    """首页首屏一次画好(使用方 2026-10-08:最下面的提示晚两秒、第二步才出现):顶栏、示例、说明里的「看线索总览」不等登录角色、
    读表与助手服务;这些回来以后标题、提问框、视频卡、示例的位置与大小都不动。读表前就点的示例,先切到对话模式,读完再答;
    读完配置前点「菜单」马上打开,读完帐号区补进卡片,卡片一直开着。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    from core import assistant
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    health = json.dumps({"ok": True, "llm": None, "note": "测试:按词表解析", "examples": assistant.EXAMPLES}, ensure_ascii=False)
    slow = re.compile(r".*/api/(assistant/health|base/|table/|auth/user).*")  # 登录角色、读表、助手说明:先扣住,模拟线上慢

    def gated(page):
        return _held(page, slow, lambda u: health if "/assistant/health" in u else None)
    boxes = """() => { const r = (s) => { const b = document.querySelector(s).getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
      return { top: r('#nav header'), title: r('.hero-title'), ask: r('#askForm'), card: r('.hero-card'), ex: r('#examples') }; }"""
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 760})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script(FIRST_FRAMES)
            release = gated(page)
            page.goto(base + "/index.html")
            page.wait_for_selector("#examples .chip-btn", timeout=5000)
            page.wait_for_timeout(300)
            assert all(nav for has_main, nav in page.evaluate("window.__frames") if has_main)  # 画出来的每一帧,有标题就有顶栏
            # 角色、表格、助手说明都还没回来:顶栏、示例、「看线索总览」已经在了
            assert page.locator("#examples .chip-btn").all_inner_texts() == assistant.EXAMPLES and page.locator("#llmNote a[href='overview.html']").count() == 1
            assert page.locator("#nav .menu-btn").is_visible() and not page.locator("#menu").is_visible() and page.inner_text("#menu #acct") == ""  # 顶栏先画,帐号区还空着
            first = page.evaluate(boxes)
            page.click("#nav .menu-btn")  # 读完配置前点「菜单」:马上打开
            assert page.locator("#menu").is_visible() and page.locator("#menu .mgrp h3").all_inner_texts() == [g for g, _ in MENU_SPEC]
            release()
            page.wait_for_function("document.getElementById('llmNote').textContent.includes('按词表解析') && document.querySelector('#menu #acct .who b')", timeout=15000)  # 名字先到,角色读完才补标签与「后台数据表格」
            assert page.locator("#menu").is_visible() and page.locator("#menu a.table").is_visible()  # 读完补上帐号区与「后台数据表格」,卡片一直开着
            page.click("#menuClose")
            page.wait_for_timeout(300)
            assert page.evaluate(boxes) == first  # 数据回来以后首屏一点没动
            # 读表前就点了示例:先切到对话模式、显示「解析中」,读完再答
            page2 = browser.new_page(viewport={"width": 1366, "height": 760})
            page2.on("pageerror", lambda e: errors.append(str(e)))
            release2 = gated(page2)
            page2.goto(base + "/index.html")
            page2.wait_for_selector("#examples .chip-btn", timeout=5000)
            page2.click("#examples .chip-btn:text-is('在跟踪的商机有哪些')")
            assert page2.evaluate("document.body.classList.contains('asked')") and "解析中" in page2.inner_text("#answers")
            release2()
            page2.wait_for_function("!document.getElementById('answers').innerText.includes('解析中')", timeout=15000)
            assert "AZE-001" in page2.inner_text("#answers") and "跟踪" in page2.inner_text("#answers .understood")
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_pages_draw_the_header_right_after_its_slot():
    """顶栏紧跟在 <div id="nav"> 后面的一小段脚本里画,每页只画这一次;不放进页尾那段要等读表的大脚本(放在那里,读表那几秒没有顶栏,
    慢手机上还会先画出没有顶栏的一帧)。"""
    for f in sorted(PORTAL.glob("*.html")):
        s = f.read_text(encoding="utf-8")
        if f.name == "login.html":
            assert "P.nav(" not in s
            continue
        assert re.search(r'<div id="nav"></div>\n<script>document\.getElementById\("nav"\)\.innerHTML = P\.nav\("[a-z]+\.html"(, true)?\);', s), f.name
        assert s.count("P.nav(") == 1, f.name


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_account_name_shows_before_config_and_role():
    """帐号区不等配置、本体、角色(使用方 2026-10-15:进系统十几秒才刷出用户名):顶栏一画好就问「这是谁」,名字回来马上写进帐号区;
    这时配置、本体(几百 KB)、角色、表格都还没回来。角色回来再补上角色标签与「我的」。「这是谁」整页只问一次。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    slow = re.compile(r".*/(config\.json|ontology\.json|api/(base/|table/|assistant/).*)$")  # 配置、本体、角色、读表都扣住,只放「这是谁」
    who = fake.users[fake.user]["name"]
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            for path, acct in [("overview.html", "#nav header #acct"), ("index.html", "#menu #acct")]:
                page = browser.new_page(viewport={"width": 1366, "height": 760})
                page.on("pageerror", lambda e: errors.append(str(e)))
                asked: list[str] = []
                page.on("request", lambda r: asked.append(r.url) if r.url.endswith("/api/auth/user") else None)
                release = _held(page, slow)
                page.goto(base + "/" + path)
                page.wait_for_function(f"document.querySelector('{acct} .who')", timeout=5000)  # 配置还没回来,名字已经在了
                assert page.inner_text(acct + " .who") == who and "退出登录" in page.inner_text(acct), path
                assert page.locator(acct + " .who b").count() == 0 and page.locator(acct + " a[href='me.html']").count() == 0, path  # 角色还不知道:先不标
                release()
                page.wait_for_function(f"document.querySelector('{acct} .who b')", timeout=15000)
                assert page.inner_text(acct + " .who") == who + " 所有者" and page.locator(acct + " a[href='me.html']").count() == 1, path
                page.wait_for_timeout(500)
                assert asked == [base + "/api/auth/user"], (path, asked)  # 整页只问一次「这是谁」
                page.close()
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_inner_pages_draw_the_header_before_data():
    """内页顶栏不等读表(使用方 2026-10-09:点子菜单进页面,「读取中」那几秒还是原来没有顶栏的版式,读完才换成带页签的标题栏):
    一打开顶栏与页签就在,当前页标出;读完角色只补上帐号区与「后台数据表格」,顶栏、页签、正文的位置一点不动。
    手机上筛选区一开始就收成一行(读完不再缩一次),读完之前点「菜单」马上打开,读完帐号区补进卡片,卡片一直开着。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    slow = re.compile(r".*/api/(base/|table/|auth/user|assistant/).*")  # 登录角色与读表先扣住,模拟线上慢
    pages = [("overview.html", "线索总览"), ("analysis.html", "线索分析"), ("inbox.html", "线索处理"), ("country.html", "国别概况"), ("city.html", "城市概况"), ("trend.html", "趋势机会"),
             ("opps.html", "商机总览"), (f"opp.html?id={o['id']}", "商机总览"), ("calendar.html", "日历"), ("reports.html", "报告"),
             ("how.html", "工作流程"), ("ontology.html", "本体"), ("me.html", None), ("company.html", None)]
    boxes = """() => { const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect();
        return [Math.round(b.x), Math.round(b.y + scrollY), Math.round(b.width), Math.round(b.height)]; };
      return { top: r('#nav header'), tabs: r('#nav .pagetabs'), main: r('main').slice(0, 3) }; }"""
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            for path, on in pages:
                page = browser.new_page(viewport={"width": 1366, "height": 760})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": 6})  # 慢手机
                page.add_init_script(FIRST_FRAMES)
                release = _held(page, slow)
                page.goto(base + "/" + path)
                page.wait_for_selector("#nav header", timeout=15000)
                page.wait_for_timeout(200)
                assert all(nav for has_main, nav in page.evaluate("window.__frames") if has_main), path  # 画出来的每一帧,有正文就有顶栏
                # 角色与表格都还没回来:顶栏(首页 + 三个组名)与这一组的页签已经在了,帐号区空着
                assert page.locator("#nav .groups a").all_inner_texts() == ["首页"] + [g for g, _ in MENU_SPEC], path
                assert (page.locator("#nav .pagetabs a.on").inner_text() == on) if on else page.locator("#nav .pagetabs").count() == 0, path
                assert page.inner_text("#acct") == "" and not page.locator("#nav a.table").is_visible(), path
                first = page.evaluate(boxes)
                release()
                page.wait_for_function("document.querySelector('#acct .who')", timeout=15000)
                page.wait_for_timeout(300)
                assert page.evaluate(boxes) == first, path  # 读完以后顶栏、页签、正文一点没动
                if path == "how.html":
                    table = page.locator("#nav .pagetabs a.table")
                    assert table.is_visible() and table.get_attribute("href") == "/base/" + cfg["base"]["id"]
                page.close()
            # 手机:顶栏是站名 + 「菜单」;读完之前就能点开,读完帐号区补进卡片,卡片一直开着
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.on("pageerror", lambda e: errors.append(str(e)))
            release = _held(phone, slow)
            phone.goto(base + "/overview.html")
            phone.wait_for_selector("#nav .menu-btn", timeout=15000)
            assert phone.locator("#nav .pagetabs a.on").inner_text() == "线索总览"
            assert phone.locator("#filters .frow-head").is_visible() and not phone.locator("#fRegion").is_visible()  # 筛选区已经收成一行
            fbox = phone.locator("#filters").bounding_box()
            phone.click("#nav .menu-btn")
            assert phone.locator("#menu").is_visible() and phone.locator("#menu a.on").inner_text() == "线索总览" and phone.inner_text("#menu #acct") == ""
            release()
            phone.wait_for_function("document.querySelector('#menu #acct .who')", timeout=15000)
            assert phone.locator("#menu").is_visible() and "退出登录" in phone.inner_text("#menu #acct")
            phone.click("#menuClose")
            phone.wait_for_selector("#reports table", timeout=15000)
            assert phone.locator("#filters").bounding_box() == fbox and phone.locator("#filters .frow-head").count() == 1  # 读完没再缩一次,也没多出一行
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_menu_groups_and_moved_todo_tabs():
    """菜单分四块(使用方 2026-10-08):电脑上的内页顶栏是「首页」与三个组名,点组名进这一组;顶栏下面一排页签切换组里的各页(不用下拉);
    首页与手机上的菜单卡片三组分栏,手机上的页签一行横着滑。「待我处理」改名线索处理、只留线索的两页;「下一步已逾期」「待核更新」挪进商机总览的页签,旧链接自动转过去。"""
    from datetime import date, timedelta
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    today = date.today()
    late = b.create("opportunity", [{"code": "AZE-009", "name_zh": "甲城供水改造", "country": c["id"], "stage": "跟踪中"}])[0]
    b.create("followup", [{"opportunity": late["id"], "day": (today - timedelta(days=9)).isoformat(), "content": "现场踏勘", "next_step": "交资格预审材料", "next_due": (today - timedelta(days=2)).isoformat()}])
    b.update("opportunity", o["id"], {"review_updates": "[aaaaaaaa] 公开进度:已定标(现值:采购进行中)"})
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"

    def assert_menu_levels(pg):  # 卡片里组名与「首页」同一级,比组里的页名大、粗(使用方 2026-10-09:组名反倒比页名小,层级颠倒了)
        st = pg.evaluate("""() => Object.fromEntries([['h3', '#menu .mgrp h3'], ['home', '#menu .mtop a'], ['page', '#menu .mgrp a:not(.on)']].map(([k, s]) => {
            const c = getComputedStyle(document.querySelector(s)); return [k, [parseFloat(c.fontSize), parseInt(c.fontWeight, 10)]]; }))""")
        assert st["h3"] == st["home"] and st["h3"][0] >= st["page"][0] + 3 and st["h3"][1] > st["page"][1], st
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 760})
            page.on("pageerror", lambda e: errors.append(str(e)))
            # 电脑上的内页:顶栏「首页」+ 四个组名,当前页所在的组标蓝;下面一排页签是这一组的各页,当前页标出,鼠标停上去有一句说明
            page.goto(base + "/analysis.html")
            page.wait_for_function("document.querySelector('#acct .who')", timeout=15000)
            assert page.locator("#nav .groups a").all_inner_texts() == ["首页"] + [g for g, _ in MENU_SPEC] and page.locator("#nav .groups a.on").inner_text() == "项目线索"
            tabs = page.locator("#nav .pagetabs a")
            assert tabs.all_inner_texts() == MENU_SPEC[2][1] and page.locator("#nav .pagetabs a.on").inner_text() == "线索分析" and page.locator("#nav .pagetabs a.on").get_attribute("aria-current") == "page"
            assert all(tabs.nth(i).is_visible() for i in range(tabs.count())) and "批量" in page.locator("#nav .pagetabs a[href='inbox.html']").get_attribute("title")
            assert page.locator("#nav select, #nav button").count() == 0  # 没有下拉
            # 页签与正文左对齐,在顶栏下面
            assert abs(page.locator("#nav .pagetabs a").first.bounding_box()["x"] - page.locator("main h1").bounding_box()["x"]) <= 16
            assert page.locator("#nav .pagetabs").bounding_box()["y"] >= page.locator("#nav header").bounding_box()["y"] + page.locator("#nav header").bounding_box()["height"] - 1
            # 点页签换组里的另一页;点顶栏的组名进那一组的第一页
            page.click("#nav .pagetabs a:text-is('线索处理')")
            page.wait_for_url("**/inbox.html*", timeout=15000)
            page.wait_for_function("document.querySelector('#nav .pagetabs a.on')", timeout=15000)
            assert page.locator("#nav .pagetabs a.on").inner_text() == "线索处理"
            page.click("#nav .groups a:text-is('市场背景')")
            page.wait_for_url("**/country.html*", timeout=15000)
            page.wait_for_function("document.querySelector('#nav .pagetabs a.on')", timeout=15000)
            assert page.locator("#nav .pagetabs a.on").inner_text() == "国别概况" and page.locator("#nav .groups a.on").inner_text() == "市场背景"
            page.click("#nav .groups a:text-is('商机跟进')")
            page.wait_for_url("**/opps.html*", timeout=15000)
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator("#nav .pagetabs a").all_inner_texts() == MENU_SPEC[3][1] and page.locator("#nav .pagetabs a.on").inner_text() == "商机总览"
            page.click("#nav .groups a:text-is('工作原理')")
            page.wait_for_url("**/how.html", timeout=15000)
            page.wait_for_selector("#pages tr.grp", timeout=15000)
            table = page.locator("#nav .pagetabs a.table")
            assert page.locator("#nav .pagetabs a").all_inner_texts() == MENU_SPEC[0][1] and table.get_attribute("href").startswith("/base/") and table.get_attribute("target") == "_blank"
            # 不在组里的页面(我的)没有页签
            page.goto(base + "/me.html")
            page.wait_for_function("document.querySelector('#acct .who')", timeout=15000)
            assert page.locator("#nav .pagetabs").count() == 0 and page.locator("#nav .groups a.on").count() == 0
            # 线索处理(原待我处理):只剩两页;旧链接 #over / #review 转到商机总览
            page.goto(base + "/inbox.html")
            page.wait_for_selector("#assign .item", timeout=15000)
            assert page.inner_text("h1") == "线索处理" and page.locator("#tabs button").count() == 2 and page.locator("#over, #review").count() == 0
            page.goto("about:blank")  # 书签、旧链接是整页打开(同页只改 # 不会重新跑脚本)
            page.goto(base + "/inbox.html#over")
            page.wait_for_url(lambda u: "/opps.html" in u and "t=over" in u, timeout=15000)
            # 商机总览:三个页签;逾期页列出到期已过的跟踪中商机,待核更新页列出机器的待核内容;切页签写进地址栏
            page.wait_for_selector("#over .item", timeout=15000)
            assert page.inner_text("h1") == "商机总览" and page.locator("#tabs button.on").get_attribute("data-t") == "over" and page.locator("#tab-list").is_hidden()
            assert page.inner_text("#nOver") == "1" and "AZE-009" in page.inner_text("#over") and "交资格预审材料" in page.inner_text("#over") and "AZE-001" not in page.inner_text("#over")
            assert page.inner_text("#nReview") == "1" and page.locator("#nav .groups a.on").inner_text() == "商机跟进"
            page.click("#tabs button[data-t='review']")
            assert "已定标" in page.inner_text("#review") and "t=review" in page.url
            page.click("#tabs button[data-t='list']")
            assert page.locator("#list table").is_visible() and "t=" not in page.url and page.inner_text("#nList") == page.inner_text("#count").split(" ")[0]
            page.click("#fStage .chip-btn:has-text('全部已立项')")  # 列表的筛选照旧写进地址栏
            page.wait_for_function("location.search.includes('stage=*')", timeout=5000)
            assert "AZE-009" in page.inner_text("#list") and "AZE-002" not in page.inner_text("#list")  # AI 推荐的(建议)不在商机总览里(使用方 2026-10-16)
            # 首页的菜单卡片:第一行「首页」,四组分四栏,底下帐号区;一屏放得下
            page.goto(base + "/index.html")
            page.click("#menuBtn")
            page.wait_for_function("document.querySelector('#menu #acct .who')", timeout=15000)
            assert page.locator("#menu .mtop a.on").inner_text() == "首页" and page.locator("#menu .mgrp h3").all_inner_texts() == [g for g, _ in MENU_SPEC]
            for g, items in MENU_SPEC:
                assert page.locator(f"#menu .mgrp:has(h3:text-is('{g}')) a").all_inner_texts() == items, g
            boxes = [page.locator("#menu .mgrp").nth(i).bounding_box() for i in range(4)]
            assert len({b["y"] for b in boxes}) == 1 and boxes[0]["x"] < boxes[1]["x"] < boxes[2]["x"] < boxes[3]["x"]  # 并排四栏
            assert_menu_levels(page)
            assert page.locator("#menu").bounding_box()["y"] + page.locator("#menu").bounding_box()["height"] <= 760
            # 手机:四组上下排,每组两列;当前页所在的组标出
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.goto(base + "/opps.html")
            phone.wait_for_selector("#list table", timeout=15000)
            phone.click("#menuBtn")
            boxes = [phone.locator("#menu .mgrp").nth(i).bounding_box() for i in range(4)]
            assert boxes[0]["y"] < boxes[1]["y"] < boxes[2]["y"] < boxes[3]["y"] and phone.locator("#menu .mgrp.on h3").inner_text() == "商机跟进"
            assert phone.locator("#menu a.on").inner_text() == "商机总览" and phone.locator("#menu").bounding_box()["height"] <= 760
            assert_menu_levels(phone)
            phone.click("#menuClose")
            row = phone.locator("#nav .pagetabs .in")
            assert phone.locator("#nav .pagetabs .gname").inner_text() == "商机跟进" and row.bounding_box()["height"] < 50  # 一行,前面带组名
            assert len({round(phone.locator("#tabs button").nth(i).bounding_box()["y"]) for i in range(3)}) == 1  # 商机总览自己的三个页签也是一行
            assert phone.evaluate("document.documentElement.scrollWidth") <= 390  # 页签再多也只在这一行里横着滑,整页不横向出界
            for w, fits in ((375, True), (240, False)):  # 市场背景三页(带组名):375 宽一行放下;更窄时当前页的页签自动滑进来
                small = browser.new_page(viewport={"width": w, "height": 700})
                small.goto(base + "/country.html")
                small.wait_for_function("document.querySelector('#acct .who')", timeout=15000)
                on = small.locator("#nav .pagetabs a.on").bounding_box()
                assert small.evaluate("(r => r.scrollWidth <= r.clientWidth)(document.querySelector('.pagetabs .in'))") == fits and on["x"] >= 0 and on["x"] + on["width"] <= w, (w, on)
                small.close()
            # 工作流程页的「各页是干什么的」与菜单同一份:组名一行,下面各页带说明
            page.goto(base + "/how.html")
            page.wait_for_selector("#pages tr.grp", timeout=15000)
            assert page.locator("#pages tr.grp th").all_inner_texts() == [g for g, _ in MENU_SPEC] and "(本页)" in page.inner_text("#pages")
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_opps_list_flags_todays_lead_updates():
    """商机列表主动提醒商机有进展(使用方 2026-10-16):近 7 天挂到商机上的线索按商机一数——今天有的,顶上一个蓝框「今天有 N 个商机有新线索」、名称后面带蓝色「今日新线索 +N」,
    一点「只看这些」就只剩这些(地址栏 u=today);近 7 天有的带灰色「近 7 天 +几」;「更新」筛选行带数字。「今天」按当前日期现算(R12)。
    待核更新页不数人驳回(行首 ✗)的行(glossary.review)。"""
    from datetime import timedelta
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)  # AZE-001 今天有一条线索(aaaaaaaa)
    b = tstore.Base(admin)
    today = _today()
    old = b.create("opportunity", [{"code": "AZE-009", "name_zh": "甲城供水改造", "country": c["id"], "stage": "跟踪中", "review_updates": "✗ [dddddddd] 公开进度:已定标(现值:采购进行中)"}])[0]
    quiet = b.create("opportunity", [{"code": "AZE-010", "name_zh": "乙城管网", "country": c["id"], "stage": "跟踪中"}])[0]
    b.create("lead", [{"key": "dddddddd", "title": "Old notice", "url": "https://example.org/d", "relevant": True, "category": "招标", "country": c["id"],
                       "found_at": (today - timedelta(days=3)).isoformat() + "T02:00:00.000Z", "opportunity": old["id"], "status": "待处理", "origin": "机器采集"},
                      {"key": "eeeeeeee", "title": "Too old", "url": "https://example.org/e", "relevant": True, "category": "招标", "country": c["id"],
                       "found_at": (today - timedelta(days=20)).isoformat() + "T02:00:00.000Z", "opportunity": quiet["id"], "status": "待处理", "origin": "机器采集"}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 760})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/opps.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator("#upd .note.n-info").inner_text().startswith("今天有 1 个商机有新线索")
            row = lambda code: page.locator(f"#list tr:has-text('{code}')")  # noqa: E731
            assert row("AZE-001").locator(".tag.t-info:has-text('新线索')").inner_text() == "今日新线索 +1"  # 蓝色
            assert row("AZE-009").locator(".tag:has-text('近 7 天')").inner_text() == "近 7 天 +1" and row("AZE-009").locator(".tag.t-info:has-text('新线索')").count() == 0  # 灰色
            assert "新线索" not in row("AZE-010").inner_text()  # 20 天前的不算
            assert page.locator("#fUpd .chip-btn").all_inner_texts() == ["全部", "今天有新线索 1", "近 7 天有新线索 2"]
            page.click("#upd #seeToday")
            page.wait_for_function("location.search.includes('u=today')", timeout=5000)
            assert page.inner_text("#count").startswith("1 ") and "AZE-001" in page.inner_text("#list") and "AZE-009" not in page.inner_text("#list") and page.locator("#seeToday").count() == 0
            page.click("#fUpd .chip-btn:has-text('近 7 天')")
            assert page.inner_text("#count").startswith("2 ") and "AZE-010" not in page.inner_text("#list")
            page.reload()  # 地址栏记得住
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator("#fUpd .chip-btn.on").inner_text() == "近 7 天有新线索 2" and page.inner_text("#count").startswith("2 ")
            assert page.inner_text("#nReview") == "0"  # 驳回的那一行不算待核
            # 手机:提醒框与标签都在一屏宽里
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.on("pageerror", lambda e: errors.append(str(e)))
            phone.goto(base + "/opps.html")
            phone.wait_for_selector("#list table", timeout=15000)
            assert phone.evaluate("document.documentElement.scrollWidth") <= 390 and phone.locator("#upd .note.n-info").is_visible()
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_filters_line_up_and_overview_search():
    """筛选区(使用方 2026-10-09):① 多选的「领域」一个没选时「全部」亮着,选了别的「全部」就灭,再点「全部」清空;
    ② 电脑上每一行的标题都在左边同一栏、与这一行第一个选项对齐,选项排不下就在右栏里接着排(不再整块掉到标题下面);
    ③ 线索总览能搜:原题、项目名、中文摘要、线索号里含这个词的才算,数字卡片与右边清单一起变,地址栏带 q,清掉就复原。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    rows = """() => [...document.querySelectorAll('.filters > .frow')].filter((r) => r.offsetParent && r.querySelector(':scope > b') && r.querySelector(':scope > b').textContent.trim()).map((r) => {
        const box = (e) => { const b = e.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top + b.height / 2), Math.round(b.right)]; };
        const opts = [...r.querySelectorAll('.chip-btn, select, input')].filter((e) => e.offsetParent).map(box);
        return { label: r.querySelector(':scope > b').textContent.trim(), b: box(r.querySelector(':scope > b')), opts }; })"""

    def check_rows(page, wraps):
        got = page.evaluate(rows)
        assert got and len({r["b"][0] for r in got}) == 1, got  # 标题都在同一栏
        left = min(o[0] for r in got for o in r["opts"])
        for r in got:
            assert all(o[0] >= left and o[0] > r["b"][2] for o in r["opts"]), r  # 选项都在标题右边那一栏,换行也不跑到标题下面
            assert abs(r["opts"][0][1] - r["b"][1]) <= 2, r  # 标题与第一个选项对齐
        multi = [r["label"] for r in got if len({o[1] for o in r["opts"]}) > 1]
        assert wraps in multi, multi  # 这一行选项多,在右栏里换了行
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/overview.html")
            page.wait_for_selector("#reports table", timeout=15000)
            on = lambda: page.locator("#fSector .chip-btn.on").all_inner_texts()  # noqa: E731
            assert on() == ["全部"]
            page.click("#fSector .chip-btn:text-is('供水')")
            page.wait_for_function("location.search.includes('s=')", timeout=15000)
            assert on() == ["供水"]
            page.click("#fSector .chip-btn:text-is('全部')")
            assert on() == ["全部"]
            check_rows(page, "资金来源")
            # 搜索:四样文字里含这个词的线索才算;卡片、清单一起变
            page.wait_for_function("!document.getElementById('fNote').textContent.includes('统计中')", timeout=15000)
            assert page.inner_text("#cards .kpi .kn") == "3"
            for word, key in (("ganja", "aaaaaaaa"), ("苏姆盖特", "bbbbbbbb"), ("哈卡里恰伊", "cccccccc"), ("ccccc", "cccccccc")):
                page.fill("#fText", word)
                page.wait_for_function(f"document.getElementById('drillTitle').textContent.includes('{word}') && document.querySelector('#drill table') && !document.getElementById('fNote').textContent.includes('统计中')", timeout=15000)
                assert page.inner_text("#cards .kpi .kn") == "1" and key in page.inner_text("#drill") and page.locator("#drill tbody tr").count() == 1, word
                assert "q=" in page.url
            assert "哈卡里恰伊输水干管" in page.inner_text("#drill")  # 清单里中文名称在前
            page.fill("#fText", "不会有这个词")
            page.wait_for_function("document.getElementById('drill').textContent.includes('没有符合的线索')", timeout=15000)
            page.fill("#fText", "")
            page.wait_for_function("document.getElementById('drillTitle').textContent === '清单' && !document.getElementById('fNote').textContent.includes('统计中')", timeout=15000)
            assert page.inner_text("#cards .kpi .kn") == "3" and "q=" not in page.url and "点左边漏斗" in page.inner_text("#drill")
            # 带着搜索词打开(书签、转发的链接):一打开就是搜过的
            page.goto(base + "/overview.html?q=Sumgait")
            page.wait_for_function("document.querySelector('#drill table')", timeout=15000)
            assert page.input_value("#fText") == "Sumgait" and "bbbbbbbb" in page.inner_text("#drill")
            # 别的页面:多选的「全部」也亮;标题照样一栏
            page.goto(base + "/opps.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator("#fSector .chip-btn.on").all_inner_texts() == ["全部"]
            check_rows(page, "资金来源")
            for path, ready in (("/analysis.html", "#cards .kpi"), ("/reports.html", "#list table"), ("/inbox.html", "#assign .item"), ("/company.html", "#fKind .chip-btn")):
                page.goto(base + path)
                page.wait_for_selector(ready, timeout=15000)
                got = page.evaluate(rows)
                assert len({r["b"][0] for r in got}) == 1 and all(len(r["opts"]) and abs(r["opts"][0][1] - r["b"][1]) <= 2 and all(o[0] > r["b"][2] for o in r["opts"]) for r in got), (path, got)
                assert page.evaluate("[...document.querySelectorAll('.filters > .frow')].every((r) => r.querySelectorAll(':scope > b').length <= 1)"), path  # 每行只有一个标题
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_overview_list_turns_a_lead_into_an_opportunity():
    """线索总览(使用方 2026-10-09:在这里就能把线索转成商机):清单里没归入的线索有「转为商机」,
    展开后可新建商机并归入(与线索处理同一套:编号 国别-序号、跟踪中、负责人是登录人、出处挂这条线索),或归入已有商机;
    已归入的显示商机名,点开是商机页。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    lead = lambda key: next(r for r in b.rows("lead", props=["key", "opportunity", "status"]) if b.get("lead", r, "key") == key)  # noqa: E731
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/overview.html?q=ganja")
            page.wait_for_selector("#drill table", timeout=15000)
            assert page.locator("#drill button.conv").count() == 0 and page.locator(f"#drill a[href='opp.html?id={o['id']}']").inner_text() == "AZE-001 甘贾污水厂"
            # 新建商机并归入
            page.goto(base + "/overview.html?q=ccccc")
            page.wait_for_selector("#drill button.conv", timeout=15000)
            page.click("#drill button.conv")
            assert "苏姆盖特污水厂" in page.inner_text("#drill .convrow select") and "甘贾污水厂" in page.inner_text("#drill .convrow select")
            defaults: list[str] = []

            def name_it(d):
                defaults.append(d.default_value)
                d.accept(d.default_value)
            page.once("dialog", name_it)
            page.click("#drill .convrow button:has-text('新建商机并归入')")
            page.wait_for_selector("#drill a:has-text('AZE-003')", timeout=15000)
            assert defaults == ["哈卡里恰伊输水干管"]  # 新商机名称默认取线索的项目名
            assert page.locator("#drill .convrow").count() == 0 and page.locator("#drill button.conv").count() == 0
            new = next(r for r in b.rows("opportunity", props=["code", "name_zh", "stage", "owner", "sources", "country"]) if b.get("opportunity", r, "code") == "AZE-003")
            assert b.get("opportunity", new, "name_zh") == "哈卡里恰伊输水干管" and b.get("opportunity", new, "stage") == "跟踪中" and tstore.link_id(b.get("opportunity", new, "owner")) == "usrADM"
            assert tstore.link_id(b.get("opportunity", new, "country")) == c["id"] and tstore.link_id(b.get("opportunity", new, "sources")) == lead("cccccccc")["id"]
            assert tstore.link_id(b.get("lead", lead("cccccccc"), "opportunity")) == new["id"] and b.get("lead", lead("cccccccc"), "status") == "已处理"
            # 归入已有商机
            page.goto(base + "/overview.html?q=bbbbbbbb")
            page.wait_for_selector("#drill button.conv", timeout=15000)
            page.click("#drill button.conv")
            page.select_option("#drill .convrow select", label="AZE-001 甘贾污水厂")
            page.wait_for_selector("#drill a:has-text('AZE-001')", timeout=15000)
            assert tstore.link_id(b.get("lead", lead("bbbbbbbb"), "opportunity")) == o["id"] and b.get("lead", lead("bbbbbbbb"), "status") == "已处理"
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_city_page_lists_cities_and_opens_one():
    """城市概况(使用方 2026-10-09 要的单独一页):全部规模以上城市可按国别、人口、有无线索 / 商机筛选、搜索,地图上有分布;
    点一座城市看人口、面积、密度卡片,概况与关联到它的商机、线索;国别概况「规模以上城市」页签里的城市名点进来。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    city = b.create("city", [{"key": "586523", "name_zh": "甘贾", "name_en": "Ganja", "country": c["id"], "admin": "Ganja", "coords": "40.6828,46.3606", "population": 335600,
                              "tariff": 0.7, "tariff_currency": "AZN", "tariff_changed": "2024-03-01", "tariff_note": "居民价\n非居民 2.0"}])[0]
    b.update("opportunity", o["id"], {"city": city["id"]})
    b.update("lead", ld["id"], {"city": city["id"]})
    b.create("sewage_plant", [{"key": "WT-1", "name": "Ganja WWTP", "name_zh": "甘贾污水厂", "kind": "污水厂", "status": "运行", "country": c["id"], "city": city["id"],
                               "capacity": 12, "pop_served": 300000, "built": "1985", "level": "二级", "upgrades": "2012 扩建 6 万吨/日\n2024 提标改造", "coords": "40.70000,46.40000"}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/city.html")
            page.wait_for_selector("#rows table", timeout=15000)
            assert page.locator("#nav .pagetabs a.on").inner_text() == "城市概况"
            rows = page.locator("#rows tbody tr").all_inner_texts()
            assert len(rows) == 2 and rows[0].startswith("巴库") and "阿塞拜疆" in rows[0] and rows[1].startswith("甘贾")  # 人口多的在前
            assert page.locator("#map canvas").count() >= 1 and "2\n城市" in page.inner_text("#cards")
            # 地图左下角的滑块(和国别地图一样)按人口筛:拖到 100 万以上,清单与卡片跟着变,范围记在网址里
            sel = "(s) => echarts.getInstanceByDom(document.getElementById('map')).dispatchAction({ type: 'selectDataRange', visualMapIndex: 0, selected: s })"
            page.evaluate(sel, [6, 7])
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 1")
            assert page.inner_text("#rows tbody").startswith("巴库") and "lo=1000000" in page.url and "1\n城市" in page.inner_text("#cards")
            # 改按生产总值:两座城市都还没填,清单空,提示有几座填了
            page.click("#fBy .chip-btn[data-v='gdp']")
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 0")
            assert "现有 0 座城市填了" in page.inner_text("#filters") and "lo=" not in page.url
            page.click("#fBy .chip-btn[data-v='']")
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 2")
            page.fill("#fText", "ganja")
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 1")
            assert page.inner_text("#rows tbody").startswith("甘贾")
            if shots:
                page.fill("#fText", "")
                page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 2")
                page.screenshot(path=str(shots / "city_list.png"), full_page=True)
            # 一座城市:卡片、概况,商机与线索各在自己的页签里
            page.goto(base + f"/city.html?id={city['id']}")
            page.wait_for_function("document.querySelectorAll('#tabs button').length === 4", timeout=15000)
            assert page.inner_text("#cname") == "甘贾" and "阿塞拜疆" in page.inner_text("#info") and "34\n万" in page.inner_text("#ccards")
            info = page.inner_text("#info")
            assert "0.7 AZN/立方米" in info and "2024-03-01" in info and "居民价\n非居民 2.0" in info  # 水价挂城市(人填)
            assert page.locator("#tabs button").all_inner_texts() == ["概况", "商机 1", "线索 1", "水厂 1"]
            if shots:
                page.screenshot(path=str(shots / "city_one.png"), full_page=True)
            page.click("#tabs button[data-t='opps']")
            assert "AZE-001" in page.inner_text("#opps") and "t=opps" in page.url
            page.click("#tabs button[data-t='leads']")
            assert "Ganja WWTP EOI" in page.inner_text("#leads")
            # 水厂:自来水厂还没有,污水厂一座,改造扩建一次一行
            page.click("#tabs button[data-t='plants']")
            assert "还没有自来水厂" in page.inner_text("#supply")
            sew = page.inner_text("#sewage")
            page.wait_for_function("document.querySelector('#pmap canvas')", timeout=15000)  # 水厂地图
            assert "共 1 座有坐标" in page.inner_text("#pmapNote")
            assert "甘贾污水厂" in sew and "二级" in sew and "2012 扩建 6 万吨/日\n2024 提标改造" in sew
            if shots:
                page.screenshot(path=str(shots / "city_plants.png"), full_page=True)
            assert page.get_attribute("#back", "href") == "city.html?c=AZE"
            # 国别概况的城市名点进城市概况
            page.goto(base + "/country.html?iso=AZE&t=cities")
            page.wait_for_selector("#cities table", timeout=15000)
            page.click("#cities a:has-text('巴库')")
            page.wait_for_function("document.querySelector('#cname') && document.querySelector('#cname').textContent === '巴库'", timeout=15000)
            assert "2,130" in page.inner_text("#ccards")
            # 手机:市场背景三个页签一行放下,当前页的页签看得见;整页不横向溢出
            phone = browser.new_page(viewport={"width": 375, "height": 760})
            phone.goto(base + "/city.html")
            phone.wait_for_selector("#rows table", timeout=15000)
            on = phone.locator(".pagetabs a.on").bounding_box()
            assert on["x"] >= 0 and on["x"] + on["width"] <= 375, on
            assert phone.evaluate("document.documentElement.scrollWidth") <= 390
            if shots:
                phone.screenshot(path=str(shots / "city_phone.png"), full_page=False)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "城市概况只读"


def test_city_and_trend_pages_leave_out_cities_of_ignored_countries():
    """使用方 2026-10-15:灰色(忽略)的国别不再采集,城市概况却还列着中国的城市、算进卡片。忽略国别的城市默认不进卡片、地图、清单和国别下拉,
    筛选框下面说没算进来几座,点「显示」能临时露出(网址 ig=1);直接选了忽略的国别照常列;搜到的只在忽略国别里时空清单说清楚。趋势机会同样不算。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    cn, tr = b.create("country", [{"name_zh": "中国", "ISO3": "CHN"}, {"name_zh": "土耳其", "ISO3": "TUR"}])
    b.create("city", [{"key": "1", "name_zh": "东莞市", "name_en": "Dongguan", "country": cn["id"], "coords": "23.02,113.75", "population": 9644871},
                      {"key": "2", "name_zh": "伊斯坦布尔", "name_en": "Istanbul", "country": tr["id"], "coords": "41.01,28.98", "population": 15460000, "water_risk": 4.0, "gdp_pc": 30000},
                      {"key": "3", "name_zh": "甘贾", "name_en": "Ganja", "country": c["id"], "coords": "40.68,46.36", "population": 335600, "water_risk": 4.5, "gdp_pc": 30000}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    names = lambda page: [r.split("\t")[0].split(" ")[0] for r in page.locator("#rows tbody tr").all_inner_texts()]
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # 没设过忽略 = 默认忽略中国:东莞不列、不算
            page.goto(base + "/city.html")
            page.wait_for_selector("#rows table", timeout=15000)
            assert names(page) == ["伊斯坦布尔", "巴库", "甘贾"] and "3\n城市" in page.inner_text("#cards")
            assert "忽略国别(地图上灰色,不再采集)的 1 座城市没算进" in page.inner_text("#igNote") and "中国" not in page.inner_text("#fCountry")
            pts = "() => echarts.getInstanceByDom(document.getElementById('map')).getOption().series[0].data.map((d) => d.name)"
            assert "东莞市" not in page.evaluate(pts)
            if shots:
                page.screenshot(path=str(shots / "city_ignored.png"), full_page=True)
            # 「显示」临时露出,再点收起
            page.click("#igNote button")
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 4")
            assert names(page)[:2] == ["伊斯坦布尔", "东莞市"] and "ig=1" in page.url and "4\n城市" in page.inner_text("#cards") and "中国" in page.inner_text("#fCountry")
            assert "东莞市" in page.evaluate(pts)
            page.click("#igNote button")
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 3")
            assert "ig=" not in page.url
            # 搜到的只在忽略国别里:空清单说为什么
            page.fill("#fText", "dongguan")
            page.wait_for_function("!document.querySelector('#rows tbody')")
            assert "忽略的国别里有 1 座符合" in page.inner_text("#rows")
            # 直接点名忽略的国别(比如从它的国别概况点过来):照常列,并说明
            page.goto(base + "/city.html?c=CHN")
            page.wait_for_selector("#rows table", timeout=15000)
            assert names(page) == ["东莞市"] and "中国是你忽略的国别" in page.inner_text("#igNote") and page.eval_on_selector("#fCountry", "e => e.value") == "CHN"
            # 「我的」里再忽略土耳其:城市概况、趋势机会都不再算伊斯坦布尔
            page.goto(base + "/me.html")
            page.wait_for_function("document.querySelector('#iConts button') && !document.querySelector('#iList').innerText.includes('读取')", timeout=15000)
            click = "(iso) => { const c = echarts.getInstanceByDom(document.getElementById('iMap')); c.getZr().handler.dispatch('click', {}); c.trigger('click', {name: iso}); }"
            page.evaluate(click, "TUR")
            page.wait_for_function("document.querySelector('#iList').innerText.includes('土耳其')", timeout=15000)
            page.goto(base + "/city.html")
            page.wait_for_selector("#rows table", timeout=15000)
            assert names(page) == ["巴库", "甘贾"] and "的 2 座城市没算进" in page.inner_text("#igNote")
            page.goto(base + "/trend.html")
            page.wait_for_selector("#rows table", timeout=15000)
            assert "伊斯坦布尔" not in page.inner_text("#rows") and "土耳其" not in page.inner_text("#crows") and "土耳其" not in page.inner_text("#fCountry")
            assert "甘贾" in page.inner_text("#rows") and "忽略的国别" in page.inner_text("#intro")
            phone = browser.new_page(viewport={"width": 390, "height": 760})  # 手机:筛选收起时这行说明和「显示」也看得见,整页不横向溢出
            phone.goto(base + "/city.html")
            phone.wait_for_selector("#igNote button", timeout=15000)
            box = phone.locator("#igNote button").bounding_box()
            assert box["x"] + box["width"] <= 390 and phone.evaluate("document.documentElement.scrollWidth") <= 390
            if shots:
                phone.screenshot(path=str(shots / "city_ignored_phone.png"), full_page=False)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_lead_pages_leave_out_leads_of_ignored_countries():
    """使用方 2026-10-15「隐藏」:忽略国别(不再采集)以前采到的旧线索,线索总览、线索分析、线索处理默认不列、不算,国别筛选里也不列;
    说明行写没算进来几条,点「显示」露出(网址 ig=1),再点收起。AI 推荐的商机同样。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    today = _today().isoformat()
    cn = b.create("country", [{"name_zh": "中国", "ISO3": "CHN"}])[0]  # 没设过忽略 = 默认忽略中国
    cs = b.create("opportunity", [{"code": "CHN-001", "name_zh": "东莞污水厂", "country": cn["id"], "stage": "建议"}])[0]
    b.create("lead", [{"key": "dddddddd", "title": "Dongguan WWTP tender", "url": "https://example.org/d", "relevant": True, "category": "招标", "country": cn["id"],
                       "found_at": today + "T05:00:00.000Z", "maybe_new": True, "status": "待处理", "origin": "机器采集"},
                      {"key": "eeeeeeee", "title": "Hefei pipeline", "url": "https://example.org/e", "relevant": False, "category": "意向书", "country": cn["id"],
                       "found_at": today + "T06:00:00.000Z", "status": "已处理", "origin": "机器采集", "suggested_opp": cs["id"]}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    kpi = lambda page, label: page.locator(f"#cards .kpi:has(.kl:text-is('{label}')) .kn").inner_text()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # 线索总览:卡片只数阿塞拜疆的 3 条;国别一排没有中国;搜中国的线索搜不到
            page.goto(base + "/overview.html")
            page.wait_for_function("document.querySelector('#igNote button')", timeout=15000)
            assert kpi(page, "线索") == "3" and "2 条线索没算进来" in page.inner_text("#igNote") and "中国" not in page.inner_text("#fCountry")
            page.click("#igNote button")
            page.wait_for_function("document.querySelector('#igNote').innerText.includes('已显示')", timeout=15000)
            assert kpi(page, "线索") == "5" and "ig=1" in page.url and "中国" in page.inner_text("#fCountry")
            page.click("#igNote button")
            page.wait_for_function("document.querySelector('#igNote').innerText.includes('没算进来')", timeout=15000)
            assert kpi(page, "线索") == "3" and "ig=" not in page.url
            page.fill("#fText", "Dongguan")
            page.press("#fText", "Enter")
            page.wait_for_function("document.getElementById('drillTitle').textContent.includes('Dongguan') && !document.querySelector('#drill').innerText.includes('读取')", timeout=15000)
            assert "没有符合的线索" in page.inner_text("#drill") and "1 条线索没算进来" in page.inner_text("#igNote")
            # 线索分析:同样
            page.goto(base + "/analysis.html")
            page.wait_for_function("document.querySelector('#igNote button')", timeout=15000)
            assert kpi(page, "线索") == "3" and "2 条线索没算进来" in page.inner_text("#igNote") and "中国" not in page.inner_text("#fCountry")
            page.click("#igNote button")
            page.wait_for_function("document.querySelector('#igNote').innerText.includes('已显示')", timeout=15000)
            assert kpi(page, "线索") == "5" and "ig=1" in page.url
            # 线索处理:待确认归属里中国那条、AI 推荐的中国商机都不列,页签上的数也不算
            page.goto(base + "/inbox.html")
            page.wait_for_function("document.querySelector('#igNote button')", timeout=15000)
            assert page.inner_text("#nAssign") == "2" and page.inner_text("#nSuggest") == "1"
            assert "2 条待确认归属的线索、1 个 AI 推荐的商机没算进来" in page.inner_text("#igNote") and "Dongguan" not in page.inner_text("#assign")
            page.click("#igNote button")
            page.wait_for_function("document.getElementById('nAssign').textContent === '4'", timeout=15000)
            assert page.inner_text("#nSuggest") == "2" and "Dongguan" in page.inner_text("#assign") and "ig=1" in page.url
            phone = browser.new_page(viewport={"width": 390, "height": 760})  # 手机:说明行看得见、不横向溢出
            for path in ("overview.html", "analysis.html", "inbox.html"):
                phone.goto(base + "/" + path)
                phone.wait_for_selector("#igNote button", timeout=15000)
                box = phone.locator("#igNote button").bounding_box()
                assert box["x"] + box["width"] <= 390 and phone.evaluate("document.documentElement.scrollWidth") <= 390, path
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_trend_page_marks_self_funded_and_loan_driven_cities():
    """趋势机会(使用方 2026-10-09 要的):水风险高且人均 GDP 高 = 自付型,水风险高且所在国有开发性贷款进水务 = 贷款型,同一张地图上,门槛可调;
    城市页与国别页「概况」里列出水风险与经济。只读。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    b.update("country", c["id"], {"water_stress": 2.47, "gdp_pc": 25089, "wb_water_n": 0, "wb_water_musd": 0, "income_group": "中等偏上收入;IBRD(非优惠贷款)"})
    baku, ganja, _ = b.create("city", [
        {"key": "1", "name_zh": "巴库", "country": c["id"], "coords": "40.38,49.89", "population": 2300000, "water_risk": 4.2, "water_stress": 5, "gdp_pc": 53797, "no_drinking": 5.6},
        {"key": "2", "name_zh": "甘贾", "country": c["id"], "coords": "40.68,46.36", "population": 335600, "water_risk": 4.5, "water_stress": 3.1, "gdp_pc": 9000},
        {"key": "3", "name_zh": "明盖恰乌尔", "country": c["id"], "coords": "40.77,47.05", "population": 106000, "water_risk": 2.0, "gdp_pc": 30000}])
    cn = b.create("country", [{"name_zh": "中国", "ISO3": "CHN", "wb_water_n": 5}])[0]  # 中国境内不分析(使用方 2026-10-09)
    b.create("city", [{"key": "4", "name_zh": "北京", "country": cn["id"], "coords": "39.9,116.4", "population": 18960744, "water_risk": 4.1, "gdp_pc": 66029}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/trend.html")
            page.wait_for_selector("#rows table", timeout=15000)
            assert page.locator("#nav .pagetabs a.on").inner_text() == "趋势机会" and "不是具体项目" in page.inner_text("#intro")
            rows = page.locator("#rows tbody tr").all_inner_texts()
            assert len(rows) == 1 and rows[0].startswith("巴库") and "自付型" in rows[0]  # 国别没有世行涉水项目、也没有开发性金融线索:没有贷款型
            # 国别有世行涉水项目 → 两座高风险城市都是贷款型,巴库两类都是
            k1 = len(fake.calls)
            b.update("country", c["id"], {"wb_water_n": 2, "wb_water_musd": 300})
            k2 = len(fake.calls)
            page.reload()
            page.wait_for_selector("#rows table", timeout=15000)
            rows = page.locator("#rows tbody tr").all_inner_texts()
            assert [r.split("\t")[0] for r in rows] == ["甘贾", "巴库"] and "贷款型" in rows[0] and "两类都是" in rows[1]
            # 门槛:水风险 ≥ 2 → 明盖恰乌尔也进来(人均 GDP 3 万:两类都是);只看自付型
            page.click("#fMin .chip-btn[data-v='2']")
            page.click("#fShow .chip-btn[data-v='self']")
            page.wait_for_function("document.querySelectorAll('#rows tbody tr').length === 2")
            assert "m=2" in page.url and "s=self" in page.url and page.locator("#map canvas").count() >= 1
            # 使用方 2026-10-15:「显示」也管地图和右边的城市数(以前只管清单,点了地图还是三种颜色、写着共几座城市,看着像没筛)
            dots = "() => Object.fromEntries(echarts.getInstanceByDom(document.getElementById('map')).getOption().series.map((s) => [s.name, [s.data.length, s.itemStyle.color]]))"
            assert page.evaluate(dots)["贷款型"][0] == 0 and page.evaluate(dots)["两类都是"] == [2, "#0d9488"]  # 只看自付型:贷款型的点不画,两类都是的也用自付型的颜色
            assert page.inner_text("#fNote") == "符合 2 座 / 共 4 座城市"  # 种子里另有一座没水风险数据的巴库
            page.click("#fShow .chip-btn[data-v='loan']")
            page.wait_for_function("document.getElementById('fNote').textContent.startsWith('符合 3 座')")
            assert page.evaluate(dots)["贷款型"][0] == 1 and page.evaluate(dots)["两类都是"][1] == "#7c3aed"
            page.click("#fShow .chip-btn[data-v='both']")
            page.wait_for_function("document.getElementById('fNote').textContent.startsWith('符合 2 座')")
            assert page.evaluate(dots)["贷款型"][0] == 0 and page.evaluate(dots)["两类都是"][1] == "#dc2626"
            # 最后一张卡是「有水风险数据的城市」个数,不是风险分数,不标红(以前「3,116 / 3,116」被当成 ≥ 3 分标了「超警戒线」)
            assert page.locator("#cards .kpi.is-bad").count() == 0 and "超警戒线" not in page.inner_text("#cards")
            page.click("#fShow .chip-btn[data-v='self']")
            assert "是" in page.inner_text("#crows") and "300" in page.inner_text("#crows")
            assert "北京" not in page.inner_text("#rows") and "中国" not in page.inner_text("#crows") and "中国" not in page.inner_text("#fCountry")
            # 城市页、国别页的概况里列出水风险与经济
            page.goto(base + f"/city.html?id={baku['id']}")
            page.wait_for_function("document.getElementById('info').textContent.includes('总体水风险')", timeout=15000)
            info = page.inner_text("#info")
            assert "4.2\n分\n总体水风险" in info and "等级:极高" in info and "5.6%" in info and "53,797\n美元" in info  # 关键三项大号放前
            page.goto(base + "/country.html?iso=AZE")
            page.wait_for_function("document.getElementById('info').textContent.includes('用水压力')", timeout=15000)
            info = page.inner_text("#info")
            assert "2.5\n分\n用水压力" in info and "等级:中到高" in info and "2\n个\n世行涉水项目\n承诺额 300 百万美元" in info
            assert page.get_attribute("#info a:has-text('看趋势机会')", "href") == "trend.html?c=AZE"
            assert page.locator("#rel a").all_inner_texts() == ["城市概况 →", "趋势机会 →"]
            # 网址里点名的国别就打开它(不是排第一的国别;画页签时改写地址栏曾把它冲掉);中国不给趋势机会
            page.goto(base + "/country.html?iso=CHN")
            page.wait_for_function("document.querySelector('#rel a')", timeout=15000)
            assert page.eval_on_selector("#pick", "e => e.value") == "CHN" and page.locator("#rel a").all_inner_texts() == ["城市概况 →"]
            # 使用方 2026-10-09:趋势机会里点国家留在本页只看这个国家,上方给去国别概况、城市概况的链接;点城市进城市页,「返回」回到趋势机会(带着筛选)
            page.goto(base + "/trend.html")
            page.wait_for_selector("#rows table", timeout=15000)
            page.click("#rows a[data-iso='AZE']")
            page.wait_for_function("document.querySelector('#rel a')")
            assert "c=AZE" in page.url and page.eval_on_selector("#fCountry", "e => e.value") == "AZE"
            assert page.locator("#rel a").all_inner_texts() == ["国别概况 →", "城市概况 →", "✕ 看全部国别"]
            page.click("#rows a:has-text('巴库')")
            page.wait_for_function("(document.getElementById('back') || {}).textContent === '← 返回趋势机会'", timeout=15000)
            assert page.locator("#rel a").all_inner_texts() == ["国别概况 →", "城市概况 →", "趋势机会 →"]
            page.click("#back")
            page.wait_for_selector("#rows table", timeout=15000)
            assert "trend.html?c=AZE" in page.url
            page.click("#rel #allC")
            page.wait_for_function("!document.querySelector('#rel a')")
            assert "c=" not in page.url
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:k1] + fake.calls[k2:]), "趋势机会只读"


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_home_link_gets_analysis_and_can_be_saved_as_a_lead(monkeypatch):
    """提问框贴网址(使用方 2026-10-09):不去匹配查询条件,而是看这条链接——建议放最前(加粗加大),原文、信源、时效、库里相关、
    要核实;「收进线索库」以登录人身份建一行(来源方式「用户上传」、国别、附言写进处理备注),之后后台补全;同一网址再贴就是「已在库」。
    2026-10-18 使用方:发链接要直接给能不能投标的结论并附报告——结论(三档)放最前,八项检查每条带依据链接,「下载评估报告(Word)」按钮;入库建议退到后面。
    2026-10-19 使用方:要核实的每条、库里可能是同一项目的每条线索后面有 已复核 / 帮我复核 / 忽略此项(线索:是同一项目 / 帮我复核 / 不是同一项目);
    点了记进「助手复核记录」、八项检查当场重算;「帮我复核」助手自己查并给结论;同一网址再贴,点过的还在。"""
    import os
    from playwright.sync_api import Error as PWError, sync_playwright
    from core import bid_check
    from core.markets import load_markets
    from core.watch.fetch import Page
    from tests.test_bid_check import MARKETS
    from tests.test_link_check import HTML, MODEL, URL, _clf
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    b = tstore.Base(admin)
    uz = b.create("country", [{"name_zh": "乌兹别克斯坦", "ISO3": "UZB"}])[0]
    b.create("lead", [{"title": "Surum WWTP feasibility", "url": "https://other.example/a", "country": uz["id"], "summary_zh": "苏鲁姆污水厂可研"}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    srv.bot.terms_base = lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    srv.bot.clf = _clf({**MODEL, "deadline": "2026-11-20", "jv": "允许", "pros": ["资金方明确"], "cons": ["2023 年的信息,进展不明"]}, [])
    srv.bot.link_kw = {"fetch": lambda u: Page(u, 200, HTML, "text/html"), "outside": lambda q, iso: ([], [])}
    mfile = pathlib.Path(tempfile.mkdtemp()) / "markets.yaml"
    mfile.write_text(MARKETS, encoding="utf-8")
    srv.bot.scope = bid_check.Scope.from_markets(load_markets(mfile))
    srv.bot.report_dir = mfile.parent / "bid"
    monkeypatch.setattr(type(srv.bot), "today", property(lambda self: date(2026, 10, 18)))
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/index.html")
            page.fill("#q", "乌兹别克斯坦苏鲁姆项目 " + URL + "沙特资本投资")
            page.press("#q", "Enter")
            page.wait_for_selector(".lk-advice", timeout=15000)
            ans = page.inner_text("#answers")
            # 投标结论在最前(加粗加大),入库建议退到后面
            assert page.locator(".lk-advice").all_inner_texts() == ["有条件可投", "入库建议:入库"] and page.evaluate("getComputedStyle(document.querySelector('.lk-advice')).fontWeight") == "700"
            assert "能不能投标的结论和评估报告" in page.inner_text(".understood") and "和你说的对不上" in ans and "塔什干苏鲁姆污水处理厂" in ans
            assert page.inner_text(".lk-bid .lk-why").startswith("有条件可投,还缺:信源是否真实:")
            checks = page.locator(".lk-bid .lk-check")
            assert checks.count() == 8 and [checks.nth(i).locator(".tag").inner_text() for i in range(8)] == ["通过", "有条件", "通过", "有条件", "通过", "不明", "不明", "通过"]
            assert "乌兹别克斯坦在市场国别内(中亚片区),测试公司是主责单位" in checks.nth(2).inner_text() and "2026-11-20 截止,还有 33 天" in checks.nth(4).inner_text()
            assert "公司资料里没有本单位业绩" in checks.nth(5).inner_text() and page.locator(".lk-bid .lk-check a").count() == 0  # 依据就是原文本身时不重复放链接
            dl = page.locator(".lk-report a.btn")
            assert dl.inner_text() == "下载评估报告(Word)" and dl.get_attribute("href").startswith("api/assistant/link/") and dl.get_attribute("href").endswith("/report.docx")
            with page.expect_download() as dinfo:
                dl.click()
            got = dinfo.value
            assert got.suggested_filename.startswith("投标评估_") and got.suggested_filename.endswith(".docx")
            from docx import Document
            doc = Document(got.path())
            texts = [p.text for p in doc.paragraphs]
            assert any(t.startswith("投标评估:塔什干") for t in texts) and any("有条件可投。" in t for t in texts) and URL in [r.target_ref for r in doc.part.rels.values() if "hyperlink" in r.reltype]
            assert "Surum WWTP feasibility" in ans and "可信度 中" in ans and "出资方" in ans
            if os.environ.get("PORTAL_SHOTS"):
                page.screenshot(path=os.environ["PORTAL_SHOTS"] + "/link_check.png", full_page=True)
                page.locator(".answer").nth(0).screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手判投标_单条链接.png")
            # 复核按钮:3 条要核实 + 1 条库里线索,各三个按钮;表建了,不提示「只在这一页有效」
            assert page.locator(".rv").count() == 4 and page.locator(".rv-unsaved").is_hidden() and "不会变成一句话交给模型" in ans
            lead_box = page.locator(".rv[data-kind=库里线索]")
            assert lead_box.locator(".rv-btns .chip-btn").all_inner_texts() == ["是同一项目", "帮我复核", "不是同一项目"]
            assert page.locator(".rv[data-kind=要核实]").first.locator(".rv-btns .chip-btn").all_inner_texts() == ["已复核", "帮我复核", "忽略此项"]
            lead_box.locator("button[data-st=是同一项目]").click()
            page.wait_for_selector(".rv[data-kind=库里线索] .rv-state .tag", timeout=10000)
            assert lead_box.locator(".rv-state").inner_text().startswith("是同一项目") and lead_box.locator(".rv-btns").is_hidden() and lead_box.locator(".rv-edit").is_visible()
            page.wait_for_function("document.querySelector('.lk-bid .lk-check:nth-child(7)').textContent.includes('库里已有这一条')", timeout=10000)  # 八项检查当场重算
            # 帮我复核:助手查到了世行原公告和截止日 → 结论、把握、依据链接;信源、时间两项跟着变
            srv.bot.clf = _clf({"found": True, "conclusion": "世行同一项目的公告写明 2026-12-15 截止。", "confidence": "高",
                                "facts": {"deadline": "2026-12-15", "stage": None, "category": None, "amount": None, "requirements": None}, "evidence": [1]}, [])
            srv.bot.link_kw["verify"] = lambda item, iso: {"original": {"kind": "世行采购公告", "title": "UZ Surum WWTP EOI", "url": "https://projects.worldbank.org/n/1", "deadline": "2026-12-15"},
                                                            "sure": True, "candidates": [], "news": [], "links": [], "notes": []}
            v0 = page.locator(".rv[data-kind=要核实]").nth(0)
            v0.locator("button[data-st=帮我复核]").click()
            page.wait_for_selector(".rv[data-kind=要核实] .rv-concl", timeout=15000)
            assert v0.locator(".rv-state").inner_text().replace(" ", "") == "助手已核把握高" and v0.locator(".rv-concl").inner_text().startswith("世行同一项目的公告写明 2026-12-15 截止。")
            assert v0.locator(".rv-concl a").get_attribute("href") == "https://projects.worldbank.org/n/1"
            page.wait_for_function("document.querySelector('.lk-bid .lk-check:nth-child(5)').textContent.includes('2026-12-15 截止')", timeout=10000)
            assert "找到了原公告(世行采购公告)" in page.locator(".lk-bid .lk-check").nth(1).inner_text()
            # 忽略此项:划掉
            v1 = page.locator(".rv[data-kind=要核实]").nth(1)
            v1.locator("button[data-st=已忽略]").click()
            page.wait_for_selector(".rv[data-kind=要核实] >> nth=1 >> .rv-state .tag", timeout=10000)
            assert page.locator("li.rv-off").count() == 1 and page.evaluate("getComputedStyle(document.querySelector('li.rv-off .rv-item')).textDecorationLine") == "line-through"
            rows = b.rows("review")
            assert len(rows) == 3 and sorted(b.get("review", r, "status") for r in rows) == ["助手已核", "已忽略", "是同一项目"] and all(b.get("review", r, "material") == URL for r in rows)
            if os.environ.get("PORTAL_SHOTS"):
                page.locator(".answer").nth(0).screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手复核按钮.png")
                page.set_viewport_size({"width": 390, "height": 800})
                page.locator(".answer").nth(0).screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手复核按钮_手机.png")
                page.set_viewport_size({"width": 1366, "height": 900})
            srv.bot.clf = _clf({**MODEL, "deadline": "2026-11-20", "jv": "允许", "pros": ["资金方明确"], "cons": ["2023 年的信息,进展不明"]}, [])
            page.click(".lk-act button")
            page.wait_for_selector(".lk-act b", timeout=10000)
            row = next(r for r in b.rows("lead") if b.get("lead", r, "url") == URL)
            g = lambda p: b.get("lead", row, p)  # noqa: E731
            assert g("origin") == "用户上传" and g("country")["title"] == "乌兹别克斯坦" and g("project_name") == "塔什干苏鲁姆污水处理厂"
            assert "沙特资本投资" in g("note") and not g("judged_by")  # 留给后台补全
            page.fill("#q", URL)
            page.press("#q", "Enter")
            page.wait_for_function("document.querySelectorAll('.lk-advice').length === 4", timeout=15000)
            assert page.locator(".lk-advice").nth(3).inner_text() == "入库建议:已在库" and page.locator(".lk-act").nth(1).inner_text() == ""
            assert "库里已有这一条" in page.locator(".lk-bid").nth(1).inner_text()
            second = page.locator(".answer").nth(0)  # 新答案在最前
            assert second.locator(".rv-state .tag").all_inner_texts() == ["是同一项目", "助手已核", "已忽略"] and "2026-12-15 截止" in second.locator(".lk-bid").inner_text()  # 点过的还在,评估按它算
            if os.environ.get("PORTAL_SHOTS"):
                page.set_viewport_size({"width": 390, "height": 800})
                page.locator(".answer").nth(0).screenshot(path=os.environ["PORTAL_SHOTS"] + "/link_check_phone.png")
                page.locator(".answer").nth(0).screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手判投标_单条链接_手机.png")
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_home_list_file_is_shown_item_by_item_and_saved_by_choice(monkeypatch):
    """2026-10-16 使用方:同事发来的 AI 整理的商机清单——每条标 已在库 / 建议入库 / 先核实,给原公告和库里的链接;
    建议入库的默认勾上,已在库的没有勾;点「收进选中的 N 条」只收勾上的,按原公告入库的网址就是原公告。手机宽度看。
    2026-10-16 第二轮:非水务的直接判、没有勾;先核实的助手自己核过(整国世行公告、新闻),列结论、新闻旁证、「接着查」链接,
    默认不入库,留「仍收进(信源待核实)」小按钮。
    2026-10-18:每条先给投标结论(建议投标 / 有条件可投 / 不建议投标)和折起来的八项检查,顶上汇总三档的条数,「下载总体评估报告(Word)」。"""
    import os
    from playwright.sync_api import Error as PWError, sync_playwright
    from core import file_text as F
    from tests.test_link_check import CHECKED, ITEMS, MODEL, WB_HITS, _clf
    monkeypatch.setattr(F, "pdf_text", lambda data: ("招标项目监测 07.10.2026 清单:NRWWP-7 纳曼干污水管网;CAWRMP-ICW-08 克盖利干渠;SDP/RFB/05 基塔布污水管网;材料里自带网址的一条", 1))
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    b = tstore.Base(admin)
    uz = b.create("country", [{"name_zh": "乌兹别克斯坦", "ISO3": "UZB", "tier": "市场", "presence": "已设当地机构、有在建项目、有常驻人员"}])[0]  # 没配市场分布表时按国别表的市场分级
    b.create("lead", [{"title": "Namangan Regional Water and Wastewater: NRWWP-7 Construction and Rehabilitation of Wastewater Networks",
                       "url": "https://projects.worldbank.org/notice/7", "country": uz["id"]}])
    before = len(b.rows("lead"))
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    srv.bot.terms_base = lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    b.create("company", [{"kind": "业绩", "owner": "本单位", "name": "尼泊尔Bheri Babai引水隧道项目", "year": 2025, "amount": "折 10733万美元", "sectors": ["水利"]}])
    srv.bot.clf = _clf({**MODEL, "items": ITEMS, "question": {"kind": "资格对照", "text": "我们的业绩能满足吗?"}}, [])
    monkeypatch.setattr(type(srv.bot), "today", property(lambda self: date(2026, 10, 18)))
    srv.bot.link_kw = {"fetch": lambda u: (_ for _ in ()).throw(AssertionError("有文件时不该抓网页")), "find_original": lambda q, iso: WB_HITS.get(q, []),
                       "verify": lambda it, iso: CHECKED}
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 390, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/index.html")
            page.set_input_files("#file", {"name": "Tender_Monitoring.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 fake"})
            page.fill("#q", "我们的业绩能满足吗?")
            page.press("#q", "Enter")
            page.wait_for_selector(".lk-items", timeout=20000)
            # 附言问了问题:「理解为」改写,先答问题(本单位业绩对照),再给清单
            assert "并回答你的问题:我们的业绩能满足吗?" in page.inner_text(".understood")
            advs = page.locator(".a-body > .lk-advice").all_inner_texts()
            assert advs == ["回答:我们的业绩能满足吗?", "这是 5 条项目的清单"]
            whys = page.locator(".a-body > .lk-why").all_inner_texts()
            assert whys[0].startswith("按我方资料里 1 条本单位业绩对照(只比单个类似合同金额):4 条水务项目里,业绩够门槛的 1 条、不够的 1 条、材料没写门槛的 2 条。")
            assert whys[1] == "投标评估:建议投标 0 条、有条件可投 3 条、不建议投标 2 条;每条的结论和八项检查在下面。"  # 已截止的克盖利干渠、非水务的兽医站不建议
            assert "已在库 1 条" in whys[2] and "建议入库 2 条" in whys[2] and "按我方资料" not in whys[2] and "投标评估" not in whys[2]
            dl = page.locator(".a-body > .lk-report a.btn")
            assert dl.inner_text() == "下载总体评估报告(Word)" and dl.get_attribute("href").endswith("/report.docx")
            verdicts = [page.locator(".lk-item").nth(i).locator(".tag").first.inner_text() for i in range(5)]
            assert verdicts == ["有条件可投", "有条件可投", "不建议投标", "有条件可投", "不建议投标"]  # 基塔布业绩不够(本单位只有水利业绩)、兽医站非水务
            assert "只剩 1 天" in page.locator(".lk-item").nth(1).inner_text() and "不满足" in page.locator(".lk-item").nth(2).inner_text()
            assert page.locator(".lk-item").nth(1).locator("details.lk-checks").count() == 1 and "是不是水务项目:不是水务项目" in page.locator(".lk-item").nth(4).inner_text()
            if os.environ.get("PORTAL_SHOTS"):
                page.screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手判投标_清单_手机.png", full_page=True)
            quals = page.locator(".lk-qual .tag").all_inner_texts()
            assert quals == ["业绩不确定", "业绩满足", "业绩不满足", "业绩不确定"]  # 非水务的那条不评
            assert "10,733 万美元(2025),够" in page.locator(".lk-item").nth(1).inner_text() and "要财务部出数" in page.locator(".lk-item").nth(1).inner_text()
            assert "非水务 1 条" in whys[2] and "由你勾选决定" not in whys[2]
            rows = page.locator(".lk-item")
            assert rows.count() == 5
            tags = [rows.nth(i).locator(".tag").nth(1).inner_text() for i in range(5)]  # 第一个标签是投标结论,第二个是入库状态
            assert tags == ["已在库", "建议入库", "先核实", "建议入库", "非水务"]
            assert "纳曼干污水管网建设与修复" in rows.nth(0).inner_text() and rows.nth(0).locator("input").count() == 0 and "库里:" in rows.nth(0).inner_text()
            assert "原公告:" in rows.nth(1).inner_text() and rows.nth(1).locator("a[href='https://projects.worldbank.org/notice/1']").count() == 1
            third = rows.nth(2)
            assert "编号对不上" in third.inner_text() and "截止 2026-12-01" in third.inner_text() and third.locator("input").count() == 0
            assert "助手核过:" in third.inner_text() and "480 条公告" in third.inner_text() and "新闻旁证:" in third.inner_text() and "暂不入库" in third.inner_text()
            assert third.locator("a[href='https://news.example/kitab']").count() == 1 and third.locator(".lk-links a").all_inner_texts() == ["到法开署招标页找", "到世行采购网找"]
            assert third.locator("button.lk-keep").inner_text() == "仍收进(信源待核实)"
            assert "不是水务项目(领域:其他),不入库" in rows.nth(4).inner_text() and rows.nth(4).locator("input").count() == 0 and rows.nth(4).locator("button").count() == 0
            boxes = page.locator("input.lk-box")
            assert boxes.count() == 2 and [boxes.nth(i).is_checked() for i in range(2)] == [True, True]  # 只有找到原公告的两条有勾,都勾上了
            assert page.inner_text(".lk-act button") == "收进选中的 2 条"
            boxes.nth(1).uncheck()
            assert page.inner_text(".lk-act button") == "收进选中的 1 条"
            boxes.nth(1).check()
            if os.environ.get("PORTAL_SHOTS"):
                page.screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手看清单_逐条_手机.png", full_page=True)
            third.locator("button.lk-keep").click()  # 人仍要收先核实的那条
            page.wait_for_function("document.querySelectorAll('.lk-done b').length === 1", timeout=15000)
            assert third.locator("button.lk-keep").inner_text() == "已收进" and third.locator("button.lk-keep").is_disabled()
            page.click(".lk-act button")
            page.wait_for_function("document.querySelectorAll('.lk-done b').length === 3", timeout=15000)
            titles = {b.get("lead", r, "url") or "": b.get("lead", r, "title") for r in b.rows("lead")}
            assert titles["https://projects.worldbank.org/notice/1"].startswith("Request for Bids") and "https://www.adb.org/node/1181131" in titles
            kitab = next(r for r in b.rows("lead") if b.get("lead", r, "title") == "Kitab wastewater network construction")
            assert b.get("lead", kitab, "origin") == "用户上传" and b.get("lead", kitab, "country")["title"] == "乌兹别克斯坦" and "先核实" in b.get("lead", kitab, "note")
            assert len(b.rows("lead")) == before + 3  # 勾上的两条 + 仍收进的一条;非水务的没收
            assert "已收进线索库" in rows.nth(1).inner_text() and "已收进线索库" in rows.nth(2).inner_text() and "已收进线索库" in rows.nth(3).inner_text()
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()


def test_home_file_upload_survives_a_slow_analysis_and_dropped_connections(monkeypatch):
    """2026-10-16 使用方在 iPhone 上传招标监测 PDF,助手报「读不到数据:Load failed」:分析(模型 + 检索)要近一分钟,手机浏览器一条请求
    60 秒就断。现在页面先拿任务编号,隔两三秒取一次进度(「模型分析中(已 N 秒)」),取的时候网络断了就再取,结果照样出来;
    传文件那一步断了自动再传一次,两次都断才用中文说明(不再是英文 Load failed)。手机宽度看。"""
    import threading

    from playwright.sync_api import Error as PWError, sync_playwright
    from core import file_text as F
    from tests.test_link_check import ADB_LINES
    monkeypatch.setattr(F, "pdf_text", lambda data: ("\n".join(ADB_LINES), 1))
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    srv.bot.terms_base = lambda: tstore.Base(tstore.Client("https://water.example", "bot-token", transport=fake.transport, sleep=lambda s: None))
    gate = threading.Event()

    def slow_outside(q, iso):  # 公开渠道检索「很慢」:页面得等,期间显示走到哪了
        gate.wait(20)
        return [], []
    srv.bot.link_kw = {"fetch": lambda u: (_ for _ in ()).throw(AssertionError("有文件时不该抓网页")), "outside": slow_outside}
    base = f"http://127.0.0.1:{srv.server_port}"
    pdf = {"name": "Tender_Monitoring.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 fake"}
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 390, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            polls = {"n": 0, "dropped": 0}

            def route(r):  # 第 2 次取进度时「断网」一次(手机锁屏、切走时就是这样)
                polls["n"] += 1
                if polls["n"] == 2:
                    polls["dropped"] += 1
                    r.abort("internetdisconnected")
                else:
                    r.continue_()
            page.route("**/api/assistant/link/*", route)
            page.goto(base + "/index.html")
            page.set_input_files("#file", pdf)
            page.fill("#q", "这个项目能跟吗")
            page.press("#q", "Enter")
            assert "看一份文件" in page.inner_text(".understood")
            page.wait_for_function("/中\\(已 \\d+ 秒\\)/.test(document.querySelector('.lk-wait').textContent)", timeout=15000)
            page.wait_for_function("/网络断了一下,正在重新取结果/.test(document.querySelector('.lk-wait').textContent)", timeout=15000)
            wait_text = page.inner_text(".lk-wait")
            assert "后台还在算,不用重发" in wait_text and polls["dropped"] == 1
            page.wait_for_function("/查公开渠道中\\(已 \\d+ 秒\\)/.test(document.querySelector('.lk-wait').textContent)", timeout=15000)
            if os.environ.get("PORTAL_SHOTS"):
                page.screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手上传文件_手机_等待.png", full_page=True)
            gate.set()
            page.wait_for_selector(".lk-advice", timeout=15000)
            ans = page.inner_text("#answers")
            assert "老挝" in ans and "Invitation for Bids" in ans and "按你上传的文件分析" in ans and "Load failed" not in ans
            if os.environ.get("PORTAL_SHOTS"):
                page.screenshot(path=os.environ["PORTAL_SHOTS"] + "/助手上传文件_手机_结果.png", full_page=True)
            # 传文件那一步两次都断:中文说明,不是英文 Load failed
            page.unroute("**/api/assistant/link/*")
            page.route("**/api/assistant/link", lambda r: r.abort("internetdisconnected"))
            page.set_input_files("#file", pdf)
            page.press("#q", "Enter")
            page.wait_for_function("document.querySelectorAll('.n-bad').length === 1", timeout=15000)
            bad = page.inner_text(".n-bad")
            assert "文件没传上去(网络断了一下),请再发一次" in bad and "Load failed" not in bad and "Failed to fetch" not in bad
            # 浏览器自己的网络报错(别的页面、别的请求)也说中文
            assert page.evaluate("P.netError(new TypeError('Load failed')) && P.netError(new TypeError('Failed to fetch')) && !P.netError(new Error('助手接口 → 500'))")
            assert not errors, errors
            browser.close()
    finally:
        gate.set()
        srv.shutdown()


def test_sources_page_flags_problems_and_toggles_enabled():
    """信源页(使用方 2026-10-12 找不到管信源的地方):全部信源的状态、最近采集、线索数;出错、被拒、启用着却多日没采集的整行标红排最前,
    卡片数出有问题的几个;筛选项上的数跟着其他筛选走;编辑者点「启用」停用、再点重新启用,只改这一个字段(停用写空)。"""
    from datetime import datetime, timedelta, timezone
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    now = datetime.now(timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    b.create("source", [
        {"key": "ok_src", "name": "世行采购公告(全球)", "kind": "接口", "scope": "全球", "enabled": True, "last_run": iso(now - timedelta(minutes=1)), "last_status": "正常", "url": "https://example.org/wb"},  # 一分钟前:三小时前在北京时间零点后会变成「昨天」
        {"key": "blocked_src", "name": "GDELT 新闻", "kind": "接口", "scope": "AZE", "enabled": True, "last_run": iso(now - timedelta(minutes=1)), "last_status": "被拒"},
        {"key": "stale_src", "name": "MEED 水务", "kind": "网页列表", "scope": "全球", "enabled": True, "last_run": iso(now - timedelta(days=9)), "last_status": "正常"},
        {"key": "off_src", "name": "旧 EBRD 采购平台", "kind": "网页列表", "scope": "AZE", "last_run": iso(now - timedelta(days=16)), "last_status": "正常"},
    ])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/sources.html")
            page.wait_for_selector("#list table", timeout=15000)
            assert page.locator("#nav .groups a.on").inner_text() == "工作原理" and page.locator("#nav .pagetabs a.on").inner_text() == "信源"
            rows = page.locator("#list tbody tr").all_inner_texts()
            assert len(rows) == 4 and rows[0].startswith("GDELT") and "被拒" in rows[0] and rows[1].startswith("MEED") and "多日未采集" in rows[1]
            assert rows[2].startswith("世行") and rows[3].startswith("旧 EBRD") and "已停用" in rows[3] and "阿塞拜疆" in rows[0]
            assert page.locator("#list tbody tr.bad").count() == 2 and page.locator("#kpis .kpi.is-bad").count() == 1 and "2\n个\n有问题" in page.inner_text("#kpis") and "今天" in page.inner_text("#kpis")
            assert page.locator("#fState .chip-btn").all_inner_texts() == ["全部 4", "有问题 2", "正常 1", "已停用 1"]
            page.click("#fScope .chip-btn:has-text('阿塞拜疆')")  # 筛了范围,状态上的数跟着变
            assert page.locator("#fState .chip-btn").all_inner_texts() == ["全部 2", "有问题 1", "正常 0", "已停用 1"] and page.locator("#list tbody tr").count() == 2
            page.click("#fScope .chip-btn:has-text('全部')")
            if shots:
                page.screenshot(path=str(shots / "sources.png"), full_page=True)
            ok = next(r for r in b.rows("source", props=["key", "enabled"]) if b.get("source", r, "key") == "ok_src")
            page.click(f"#list button[data-id='{ok['id']}']")
            page.wait_for_function("document.querySelector('#fState').innerText.includes('已停用 2')", timeout=15000)
            assert not b.get("source", b.record("source", ok["id"]), "enabled")
            page.click(f"#list button[data-id='{ok['id']}']")
            page.wait_for_function("document.querySelector('#fState').innerText.includes('已停用 1')", timeout=15000)
            assert b.get("source", b.record("source", ok["id"]), "enabled") is True
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.goto(base + "/sources.html")
            phone.wait_for_selector("#list table", timeout=15000)
            if shots:
                phone.screenshot(path=str(shots / "sources_phone.png"), full_page=False)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    writes = [x for x in fake.calls if x[0] != "GET"][-2:]
    assert all(m == "PATCH" for m, _ in writes)


def test_awards_tab_on_country_page_and_table_on_city_page():
    """已中标项目(使用方 2026-10-12 要的;10-13 起在国别概况是「历史信息」里的一个页签,排在水务历史资料后面,页签上是份数;城市页仍在最下面):
    一张表,按类别、合同类型、承包模式、金额、年份筛选(多选,筛选项上的数跟着别的项走;10-13 加合同类型、承包模式、金额与排序),
    卡片是份数、合计中标价(折合人民币)、最大一笔、中国企业中标;城市页只列挂在这座城市的。只读。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    city = b.create("city", [{"key": "586523", "name_zh": "甘贾", "name_en": "Ganja", "country": c["id"], "coords": "40.6828,46.3606", "population": 335600}])[0]
    b.create("award", [
        {"key": "WB-1", "name": "Ganja water supply network, 42 km", "name_zh": "甘贾供水管网 42 公里", "sector": "供水", "contract_type": "工程", "price": 25_000_000, "currency": "USD",
         "price_cny": 17750, "awarded": "2024-05-10", "quantity": "42 km", "delivery": "施工", "winner": "China Sample Engineering Co", "winner_country": "中国", "project": "P1 Ganja Water",
         "funder": "世界银行", "country": c["id"], "city": city["id"], "url": "https://projects.worldbank.org/x"},
        {"key": "WB-2", "name": "Baku WWTP rehabilitation and operation", "sector": "污水", "contract_type": "工程", "delivery": "DBO", "price": 8_000_000, "currency": "USD", "price_cny": 5680,
         "awarded": "2022-03-01", "winner": "Sample JV", "winner_country": "土耳其、中国", "country": c["id"]},
        {"key": "WB-3", "name": "Supervision consultant", "sector": "供水", "contract_type": "咨询服务", "price": 400_000, "currency": "USD", "price_cny": 284,
         "awarded": "2022-11-20", "winner": "Sample Consult", "winner_country": "德国", "country": c["id"]},
    ])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/country.html?iso=AZE&t=awards")
            page.wait_for_selector("#awBox table", timeout=15000)
            assert page.locator(".tab.on").get_attribute("data-t") == "awards"
            page.wait_for_function("document.querySelector('#tabs').innerText.includes('已中标项目 3')", timeout=15000)
            rows = page.locator("#awBox tbody tr").all_inner_texts()
            assert len(rows) == 3 and rows[0].startswith("2024-05-10") and "甘贾供水管网 42 公里" in rows[0] and "甘贾" in rows[0] and "2,500 万美元" in rows[0]
            assert rows[1].startswith("2022-11-20") and rows[2].startswith("2022-03-01")  # 中标时间从新到旧
            k = page.inner_text("#awBox .aw-k")
            assert "3\n份" in k and "2.37\n亿元" in k and "1.78\n亿元" in k and "2\n份" in k and "占 67%" in k  # 合计、最大一笔、中国企业中标(联合体里有中国也算)
            assert page.locator("#awBox .aw-s .chip-btn").all_inner_texts() == ["全部 3", "供水 2", "污水 1"]
            assert page.locator("#awBox .aw-y .chip-btn").all_inner_texts() == ["全部 3", "2024 1", "2022 2"]
            assert page.locator("#awBox .aw-ct .chip-btn").all_inner_texts() == ["全部 3", "工程 2", "咨询服务 1"]
            assert page.locator("#awBox .aw-d .chip-btn").all_inner_texts() == ["全部 3", "施工 1", "DBO 1", "货物、咨询等 1"]  # 各项加起来等于全部
            assert page.locator("#awBox .aw-m .chip-btn").all_inner_texts() == ["全部 3", "100–500 万 1", "2,000 万–1 亿 1", "1 亿元以上 1"]
            assert "DBO" in rows[2] and "DBO" not in rows[0]  # 合同类型下面标出 DBO,施工不标
            page.click("#awBox .aw-o .chip-btn[data-v='lo']")  # 排序:金额最低在前
            assert page.locator("#awBox tbody tr").first.inner_text().startswith("2022-11-20")
            page.click("#awBox .aw-o .chip-btn[data-v='hi']")
            assert page.locator("#awBox tbody tr").first.inner_text().startswith("2024-05-10")
            page.click("#awBox .aw-m .chip-btn[data-v='2,000 万–1 亿']")  # 金额一档:别的项上的数跟着变
            assert page.locator("#awBox tbody tr").count() == 1 and page.locator("#awBox .aw-ct .chip-btn").all_inner_texts() == ["全部 1", "工程 1", "咨询服务 0"]
            page.click("#awBox .aw-m .chip-btn[data-v='']")
            page.click("#awBox .aw-o .chip-btn[data-v='new']")
            page.click("#awBox .aw-y .chip-btn[data-v='2022']")  # 选了年份,类别上的数跟着变
            assert page.locator("#awBox .aw-s .chip-btn").all_inner_texts() == ["全部 2", "供水 1", "污水 1"] and page.locator("#awBox tbody tr").count() == 2
            page.click("#awBox .aw-s .chip-btn[data-v='污水']")
            assert page.locator("#awBox tbody tr").count() == 1 and "Baku WWTP" in page.inner_text("#awBox tbody")
            assert page.locator("#awBox .aw-y .chip-btn").all_inner_texts() == ["全部 1", "2024 0", "2022 1"]
            page.click("#awBox .aw-s .chip-btn[data-v='']")
            page.click("#awBox .aw-y .chip-btn[data-v='']")
            page.fill("#awBox .aw-q", "中国")
            page.wait_for_function("document.querySelectorAll('#awBox tbody tr').length === 2")
            page.fill("#awBox .aw-q", "")
            page.wait_for_function("document.querySelectorAll('#awBox tbody tr').length === 3")
            if shots:
                page.locator("#awBox").screenshot(path=str(shots / "awards_country.png"))
            # 城市页:只列挂在这座城市的
            page.goto(base + f"/city.html?id={city['id']}")
            page.wait_for_selector("#awBox table", timeout=15000)
            assert page.locator("#awBox tbody tr").count() == 1 and "甘贾供水管网" in page.inner_text("#awBox tbody")
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.goto(base + "/country.html?iso=AZE&t=awards")
            phone.wait_for_selector("#awBox table", timeout=15000)
            assert phone.evaluate("document.documentElement.scrollWidth") <= 400
            if shots:
                phone.locator("#awBox").screenshot(path=str(shots / "awards_phone.png"))
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "已中标项目只读"


SUMMARY = """文件简称：24报告=[世行水务行业报告（2024）](https://example.org/24.pdf)；07PAD=国家供水与卫生项目评估文件（2007）。页码均为 PDF 页。

## 要点速览
- !**51.5%** 巴库漏损，一年漏掉约 1.88 亿 m³（24报告 p36–37）
- **0.70 马纳特/m³** 全国统一水价，约合 0.41 美元（24报告 p28）

## 1. 关键数字
- 巴库漏损：51.5%（24报告 p36），早期约 70%（07PAD p13；p14）。管网 10,200 km。
- 出资方：
  - 世界银行：2.3 亿美元（07PAD p78，2007年时的数字）

## 2. 对中国承包商的意义（推断）
- 推断：24报告 p41 说业主付款慢，要设计付款保障。
"""


def test_water_history_tab_and_city_chronicle():
    """水务历史资料(使用方 2026-10-13 要的,v2.17):国别概况多一个页签,上面是年度大事记(水务事件 + 每年最大三份中标,按年从新到旧,
    类别可筛、数跟着走,只到年的不写月日),下面综述展开、研究资料按类型筛;城市页有只挂这座城市的大事记。只读。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    city = b.create("city", [{"key": "1", "name_zh": "巴库", "name_en": "Baku", "country": c["id"], "coords": "40.4,49.9", "population": 2300000}])[0]
    b.create("water_event", [
        {"key": "EV-1", "day": "2004-01-01", "日期原文": "2004", "title": "Azersu 成立,统管全国城市供排水", "kind": "机构改革", "country": c["id"], "source": "世行文件 第14页", "url": "https://example.org/a.pdf"},
        {"key": "EV-2", "day": "2016-05-13", "日期原文": "2016-05-13", "title": "电价委员会批准上调水价和污水费", "kind": "水价调整", "country": c["id"], "city": city["id"]},
        {"key": "WB-P1-批准", "day": "2007-06-14", "日期原文": "2007-06-14", "title": "世界银行批准「国家供水与卫生项目」,2.3 亿美元", "kind": "项目批准", "country": c["id"]},
    ])
    b.create("award", [{"key": "WB-9", "name": "Baku WWTP", "name_zh": "巴库污水厂改造", "sector": "污水", "price_cny": 5680, "awarded": "2016-03-01",
                        "winner": "Sample JV", "winner_country": "中国", "country": c["id"], "city": city["id"]}])
    b.create("study", [
        {"key": "SUM-AZE", "name": "阿塞拜疆水务综述", "name_zh": "阿塞拜疆水务综述", "kind": "综述", "published": "2026-10-13", "country": c["id"],
         "summary": SUMMARY},
        {"key": "WB-1", "name": "Azerbaijan - National Water Supply and Sanitation Project", "kind": "项目评估", "published": "2007-05-01", "country": c["id"], "url": "https://example.org/1.pdf"},
        {"key": "WB-2", "name": "Azerbaijan - Transitioning to Efficient Water Sector Institutions", "name_zh": "阿塞拜疆水务机构转型", "kind": "行业研究", "published": "2024-08-31", "country": c["id"]},
    ])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/country.html?iso=AZE&t=docs")
            page.wait_for_selector("#chron .chron", timeout=15000)
            page.wait_for_selector("#studies table", timeout=15000)
            assert page.locator(".tab.on").get_attribute("data-t") == "docs"
            assert page.locator(".cyl").all_inner_texts() == ["2016", "2007", "2004"]  # 按年从新到旧
            y2016 = page.locator(".cy").first.inner_text()
            assert "05-13" in y2016 and "巴库" in y2016 and "中标" in y2016 and "巴库污水厂改造" in y2016 and "5,680 万元" in y2016  # 当年最大的中标并进来
            assert "Azersu 成立" in page.locator(".cy").last.inner_text() and "01-01" not in page.locator(".cy").last.inner_text()  # 只到年的不写月日
            assert page.locator("#chron .ch-k .chip-btn").all_inner_texts() == ["全部 4", "项目批准 1", "机构改革 1", "水价调整 1", "中标 1"]
            page.click("#chron .ch-k .chip-btn[data-v='项目批准']")
            assert page.locator(".cyl").all_inner_texts() == ["2007"]
            # 综述(10-13 使用方嫌排版单调、出处是一串字符):出处变小角标,要点速览是指标卡,标签单列,推断一节换底色,出处表收到最后
            secs = "\n".join(page.locator("#studies .dg-sec").all_inner_texts())
            assert "文件简称" not in secs and "24报告" not in secs and "07PAD" not in secs and " p36" not in secs and "推断：" not in secs
            assert page.locator("#studies .dg-toc a").all_inner_texts() == ["要点速览", "1 关键数字", "2 对中国承包商的意义"]
            assert page.locator("#studies .dg-kpi .kpi").count() == 2 and page.locator("#studies .dg-kpi .kpi.is-bad .kn").inner_text() == "51.5"
            assert page.locator("#studies .dg-k").all_inner_texts() == ["巴库漏损", "出资方"] and page.locator("#studies .dg-v p").count() == 3  # 一句一行
            assert page.locator("#studies sup.sref a").all_inner_texts() == ["1", "1", "1", "2", "2", "2", "1"]  # 按第一次引用编号;只写 p14 的算前一个出处
            assert "（2007年时的数字）" in secs and "世行水务行业报告（2024）" in page.inner_text("#studies .dg-sec.infer")  # 括号外的「24报告 p41」换成全称
            page.locator("#studies .dg-sec").nth(1).locator("sup.sref a").first.click()
            assert "PDF 第 36 页" in page.inner_text(".sref-pop") and page.get_attribute(".sref-pop a[target]", "href") == "https://example.org/24.pdf#page=36"
            page.click("h1")
            assert page.locator(".sref-pop").count() == 0
            page.click("#studies .dg-src summary")
            srcs = page.locator("#studies .dg-src li").all_inner_texts()
            assert len(srcs) == 2 and "引用 PDF 第 28、36–37、36、41 页" in srcs[0] and page.get_attribute("#studies .dg-src li a", "href") == "https://example.org/24.pdf"
            assert page.locator("#studies tbody tr").count() == 2 and page.locator("#studies tbody tr").first.inner_text().startswith("2024-08-31")
            assert page.locator("#studies .sd-k .chip-btn").all_inner_texts() == ["全部 2", "行业研究 1", "项目评估 1"]
            assert "水务历史资料 3" in page.inner_text("#tabs")
            if shots:
                page.locator(".tab.on").screenshot(path=str(shots / "history_country.png"))
            page.goto(base + f"/city.html?id={city['id']}")
            page.wait_for_selector("#chron .chron", timeout=15000)
            assert page.locator("#chron .cyl").all_inner_texts() == ["2016"] and page.locator("#chron li").count() == 2
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.goto(base + "/country.html?iso=AZE&t=docs")
            phone.wait_for_selector("#studies table", timeout=15000)
            assert phone.evaluate("document.documentElement.scrollWidth") <= 400
            if shots:
                phone.locator(".tab.on").screenshot(path=str(shots / "history_phone.png"))
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "水务历史资料只读"


def test_water_area_page_sums_its_cities():
    """水务片区(使用方 2026-10-13 要的,v2.18;10-14 从市场背景菜单挪进国别概况「基本信息」,v2.22):国别概况的片区页签列这国的片区、包含城市、
    合计人口(占全国)与挂上的商机,没划片区的国家不出这个页签;一个片区汇总包含城市的人口、商机(挂片区的 + 城市在片区里的)、线索、大事记、水厂与中标,
    不在片区里的城市不算,返回回到国别的片区页签。只读。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    baku, sum_, ganja = b.create("city", [{"key": "1", "name_zh": "巴库", "name_en": "Baku", "country": c["id"], "population": 2351300, "water_risk": 4.24},
                                          {"key": "2", "name_zh": "苏姆盖特", "name_en": "Sumgayit", "country": c["id"], "population": 427000},
                                          {"key": "3", "name_zh": "甘贾", "name_en": "Ganja", "country": c["id"], "population": 330663}])
    area = b.create("water_area", [{"name": "大巴库", "name_en": "Greater Baku", "country": c["id"], "cities": [baku["id"], sum_["id"]], "utility": "大城市联合供水局",
                                    "sources": "奥古兹—盖贝莱—巴库线\n萨穆尔—阿布歇隆渠", "basis": "同一家水司供水"}])[0]
    o2, o3 = b.create("opportunity", [{"code": "AZE-101", "name_zh": "阿布歇隆半岛 33 个居民点供水", "country": c["id"], "area": area["id"], "stage": "建议"},
                                      {"code": "AZE-102", "name_zh": "甘贾污水厂", "country": c["id"], "city": ganja["id"], "stage": "建议"}])
    b.create("opportunity", [{"code": "AZE-103", "name_zh": "苏姆盖特污水厂", "country": c["id"], "city": sum_["id"], "stage": "跟踪中"}])
    b.create("sewage_plant", [{"name": "Hovsan WWTP", "name_zh": "霍夫桑污水厂", "kind": "污水厂", "country": c["id"], "city": baku["id"], "capacity": 64, "status": "运行"},
                              {"name": "Ganja WWTP", "kind": "污水厂", "country": c["id"], "city": ganja["id"], "capacity": 5}])
    b.create("water_event", [{"key": "E1", "day": "2000-01-01", "日期原文": "2000", "title": "成立阿布歇隆水务公司", "kind": "机构改革", "country": c["id"], "city": baku["id"]},
                             {"key": "E2", "day": "2010-01-01", "日期原文": "2010", "title": "甘贾的事", "kind": "其他", "country": c["id"], "city": ganja["id"]}])
    b.create("country", [{"name_zh": "摩洛哥", "ISO3": "MAR"}])  # 没划片区的国家
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/country.html?iso=AZE&t=areas")
            page.wait_for_selector("#areas table", timeout=15000)
            tabs = page.locator("#tabs .tgrp").first.locator("button").all_inner_texts()
            assert [t.split(" ")[0] for t in tabs] == ["概况", "机构与法规", "营商环境", "规模以上城市", "水务片区"] and tabs[-1] == "水务片区 1"  # 基本信息组的最后一个
            assert page.locator(".tab.on").get_attribute("data-t") == "areas"
            row = page.inner_text("#areas tbody tr")
            assert "大巴库" in row and "巴库" in row and "苏姆盖特" in row and "277.8 万" in row and "\t1\t" in row  # 挂上的商机 1 个
            assert "水务片区" not in page.inner_text("#nav")  # 不在菜单里了
            page.click("#areas a:has-text('大巴库')")
            page.wait_for_selector("#chron .chron", timeout=15000)
            page.wait_for_selector("#plants table", timeout=15000)
            k = page.inner_text("#cards")
            assert "277.8\n万" in k and "2\n座" in k and "2\n个" in k and "跟踪中 1" in k
            assert page.locator("#cities tbody tr").count() == 2 and page.locator("#cities tbody tr").first.inner_text().startswith("巴库")
            opps = page.inner_text("#opps")
            assert "AZE-101" in opps and "AZE-103" in opps and "AZE-102" not in opps  # 甘贾不在片区里
            assert page.inner_text("#opps tbody tr").startswith("AZE-103")  # 跟踪中的排前
            assert "霍夫桑污水厂" in page.inner_text("#plants") and "Ganja" not in page.inner_text("#plants")
            assert "成立阿布歇隆水务公司" in page.inner_text("#chron") and "甘贾的事" not in page.inner_text("#chron")
            assert "奥古兹—盖贝莱—巴库线" in page.inner_text("#info") and "同一家水司供水" in page.inner_text("#info")
            assert page.locator("#nav .pagetabs a.on").inner_text() == "国别概况"
            if shots:
                page.screenshot(path=str(shots / "area.png"), full_page=True)
            page.goto(base + f"/area.html?id={area['id']}")  # 直接打开(没有来处):返回链接指回国别的片区页签
            page.wait_for_selector("#cities table", timeout=15000)
            assert page.locator("#back").get_attribute("href") == "country.html?iso=AZE&t=areas" and "水务片区" in page.inner_text("#back")
            # 没划片区的国家:没有这个页签,网址点名要它也回到概况
            page.goto(base + "/country.html?iso=MAR&t=areas")
            page.wait_for_function("document.querySelector('#tabs button[data-t=opps] b') !== null", timeout=15000)
            assert page.locator("#tabs button[data-t=areas]").count() == 0 and page.locator(".tab.on").get_attribute("data-t") == "info"
            phone = browser.new_page(viewport={"width": 390, "height": 760})
            phone.goto(base + f"/area.html?id={area['id']}")
            phone.wait_for_selector("#plants table", timeout=15000)
            assert phone.evaluate("document.documentElement.scrollWidth") <= 400
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "水务片区页只读"


def test_contractor_page_lists_its_awards_and_winner_names_link_to_it():
    """供应商(使用方 2026-10-12 要的,v2.15):已中标项目表里每家中标方点进供应商页;那页是基本资料(人补的没填时提示)
    和它的全部中标(多一组国别筛选、一列国别,卡片换成涉及国别);不带 id 是全部供应商按中标数排。只读。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    geo2 = b.create("country", [{"key": "GEO", "name_zh": "格鲁吉亚"}])[0]
    cg, ve = b.create("contractor", [{"key": "CHINA SAMPLE", "name": "China Sample Engineering Co", "nationality": "中国", "nationality_code": "CHN", "ext_ids": "WB:1"},
                                     {"key": "VEOLIA SAMPLE", "name": "Veolia Sample SA", "name_zh": "威立雅示例", "nationality": "法国", "kind": "外资或国际企业"}])
    b.create("award", [
        {"key": "WB-1", "name": "Ganja water supply", "sector": "供水", "contract_type": "工程", "price": 25_000_000, "currency": "USD", "price_cny": 17750,
         "awarded": "2024-05-10", "winner": "China Sample Engineering Co、Veolia Sample SA", "winner_country": "中国、法国", "country": c["id"], "winners": [cg["id"], ve["id"]]},
        {"key": "WB-2", "name": "Tbilisi O&M contract", "sector": "供水", "contract_type": "运营", "price": 3_000_000, "currency": "USD", "price_cny": 2130,
         "awarded": "2023-02-01", "winner": "China Sample Engineering Co", "winner_country": "中国", "country": geo2["id"], "winners": [cg["id"]]},
    ])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/country.html?iso=AZE&t=awards")
            page.wait_for_selector("#awBox table", timeout=15000)
            assert page.locator("#awBox tbody a[href^='contractor.html?id=']").count() == 2  # 联合体两家各一个链接
            page.click(f"#awBox tbody a[href='contractor.html?id={cg['id']}']")
            page.wait_for_selector("#awBox table", timeout=15000)
            assert page.inner_text("#fname") == "China Sample Engineering Co" and "中国" in page.inner_text("#info") and "WB:1" in page.inner_text("#info")
            assert "还没填:中文名、性质" in page.inner_text("#info")
            rows = page.locator("#awBox tbody tr").all_inner_texts()
            assert len(rows) == 2 and "格鲁吉亚" in rows[1] and "运营" in rows[1]
            k = page.inner_text("#awBox .aw-k")
            assert "2\n份" in k and "2\n个" in k  # 中标份数、涉及国别
            assert page.locator("#awBox .aw-c .chip-btn").all_inner_texts()[0] == "全部 2"
            page.click("#awBox .aw-c .chip-btn[data-v='格鲁吉亚']")
            assert page.locator("#awBox tbody tr").count() == 1
            if shots:
                page.screenshot(path=str(shots / "contractor.png"), full_page=True)
            page.goto(base + "/contractor.html")
            page.wait_for_selector("#rows table", timeout=15000)
            rows = page.locator("#rows tbody tr").all_inner_texts()
            assert len(rows) == 2 and rows[0].startswith("China Sample") and "2 份" in rows[0] and "威立雅示例" in rows[1]
            page.select_option("#fNat", "法国")
            assert page.locator("#rows tbody tr").count() == 1
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "供应商页只读"


def test_assistant_answers_business_environment_questions():
    """助手问营商环境(世界银行 B-READY,v2.23;使用方 2026-10-14 要的问法):按市场分级、外企参标、门槛筛国别,按问的指标排,偏弱的标红;
    点了国名的逐国作答(当地币也写);没覆盖的国家直说,不显示成 0;一国都不符合时说清最低、最高是多少;忽略的国别(缺省中国)不列。只读。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    br = lambda **k: {"bready_edition": "B-READY 2025(数据版本 December 10, 2025)", **k}  # noqa: E731
    b.create("country", [
        {"name_zh": "摩洛哥", "ISO3": "MAR", "tier": "市场", **br(bready_total=61.2, bready_water=70.4, bready_procure=58.0, water_tariff=9.5, water_tariff_cur="MAD", water_tariff_usd=0.95,
                                                                 foreign_bid="无限制", award_days=150, gov_pay_days=60)},
        {"name_zh": "吉尔吉斯斯坦", "ISO3": "KGZ", "tier": "市场", **br(bready_total=55.0, bready_water=45.0, bready_procure=52.1, water_tariff=30, water_tariff_cur="KGS", water_tariff_usd=0.34,
                                                                      foreign_bid="部分招标有限制", award_days=90, gov_pay_days=120)},
        {"name_zh": "中国", "ISO3": "CHN", "tier": "其他", **br(bready_total=70.0, bready_water=30.0, bready_procure=60.0, foreign_bid="无限制", gov_pay_days=200)},
        {"name_zh": "埃及", "ISO3": "EGY", "tier": "重点"},
    ])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    seeded = len(fake.calls)
    shots = Path(os.environ.get("PORTAL_SHOTS", "")) if os.environ.get("PORTAL_SHOTS") else None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/index.html")
            page.wait_for_selector("#examples .chip-btn", timeout=15000)

            def ask(q):
                page.fill("#q", q)
                page.press("#q", "Enter")
                page.wait_for_function("(q) => { const all = document.querySelectorAll('.answer'); const a = all[all.length - 1]; return a && a.querySelector('.a-q').textContent === q && !a.innerText.includes('解析中') && !a.innerText.includes('查表中'); }", arg=q, timeout=15000)
                return page.locator(".answer").last

            a = ask("供水得分低于 50 的市场国别")
            t = a.inner_text()
            assert "对象:国别" in t and "市场分级:重点、市场" in t and "门槛:供水得分低于 50 分" in t
            rows = a.locator("tbody tr").all_inner_texts()
            assert len(rows) == 1 and rows[0].startswith("吉尔吉斯斯坦") and "中国" not in t  # 中国 30 分,但缺省忽略
            assert a.locator("tbody .num-bad").count() >= 1 and "埃及" in t and "没覆盖" in t  # 偏弱的标红;没数据的说是哪几国
            assert "1\n国" in a.locator(".kpis").inner_text()
            if shots:
                page.screenshot(path=str(shots / "assistant_bready.png"), full_page=True)
            a = ask("哪些国家不限制外企参标")
            rows = a.locator("tbody tr").all_inner_texts()
            assert [r.split("\t")[0].split(" ")[0] for r in rows] == ["摩洛哥"] and "外企参标:无限制" in a.inner_text()
            a = ask("摩洛哥商业水价多少")
            t = a.inner_text()
            assert "摩洛哥的商业水价:约 0.95 美元/m³(9.5 MAD/m³)" in t and a.locator("a[href='country.html?iso=MAR&t=biz']").count() == 1
            a = ask("那吉尔吉斯斯坦呢")
            t = a.inner_text()
            assert "吉尔吉斯斯坦的商业水价:约 0.34 美元/m³(30 KGS/m³)" in t and "沿用上一问" in t
            if shots:
                a.screenshot(path=str(shots / "assistant_bready_one.png"))
            a = ask("哪些国家政府回款最慢")
            rows = a.locator("tbody tr").all_inner_texts()
            assert rows[0].startswith("吉尔吉斯斯坦") and "排序:政府合同回款天数从高到低" in a.inner_text()
            assert a.locator("tbody tr").first.locator(".num-bad").count() >= 1  # 120 天超过 90 天,标红
            a = ask("埃及的营商环境怎么样")
            t = a.inner_text()
            assert "没有覆盖埃及" in t and "0 分" not in t
            a = ask("授标天数超过 1000 天的国家")
            t = a.inner_text()
            assert "没有符合这些条件的国别" in t and "最低 90 天" in t and "最高 365 天" in t
            a = ask("水价调整的线索")
            assert "对象:线索" in a.inner_text() and "没认出来" not in a.inner_text()  # 说线索的「水价」不改查国别
            page.set_viewport_size({"width": 390, "height": 844})  # 手机:国别表在面板里横向滑,不撑出整页的横向滚动
            a = ask("哪些国家政府回款最慢")
            assert page.evaluate("document.documentElement.scrollWidth") <= 391
            if shots:
                page.screenshot(path=str(shots / "assistant_bready_phone.png"), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
    assert all(m == "GET" for m, _ in fake.calls[seeded:]), "助手问营商环境只读"


def test_portal_css_takes_colors_from_the_tokens():
    """界面规范(2026-10-14):颜色、字号、圆角在 portal.css 开头的 :root 里定成固定几档。规范里有的颜色,别处只能写 var(--…);
    还没收进规范的散色值只许减少不许增加(逐页清理时把这个上限往下调)。"""
    css = (PORTAL / "portal.css").read_text(encoding="utf-8")
    root = re.search(r"^:root \{(.*?)\n\}", css, re.S | re.M)
    assert root, "portal.css 开头要有 :root 规范"
    tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{3,6})\b", root.group(1)))
    for need in ("--ink", "--ink-strong", "--muted", "--faint", "--line", "--brand", "--bad", "--warn", "--ok", "--info"):
        assert need in tokens, need
    rest = css[:root.start()] + css[root.end():]
    lits = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", rest)}
    in_spec = lits & {v.lower() for v in tokens.values() if len(v) == 7}
    assert not in_spec, f"这些颜色规范里有,请写成 var(--…):{sorted(in_spec)}"
    assert len(set(re.findall(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b", rest))) <= 67, "portal.css 里新加了散色值;请用 :root 里的颜色"


def test_pages_style_through_classes_not_inline():
    """界面规范第六步:页面里不再临时写 style=,版式统一走 portal.css 里的类(.push / .grow / .tight / .sw-c1… 等)。
    只有三种留在行内:随数据变的颜色(background:' + 变量)、初始隐藏(display:none,脚本再打开)、地图点的动画错峰,
    以及 P.kpiHtml 按卡片个数给的 --cols。"""
    allowed = re.compile(r"^(display:none|animation-delay:[\d.]+s|--cols:.*|background:)$")
    bad = []
    for f in sorted(PORTAL.glob("*.html")) + [PORTAL / "portal.js"]:
        for m in re.finditer(r"style=\\?[\"']([^\"'\\]*)", f.read_text(encoding="utf-8")):
            if not allowed.match(m.group(1).strip()):
                bad.append(f"{f.name}: style=\"{m.group(1)}\"")
    assert not bad, "页面里又写了行内样式,请换成 portal.css 里的类:\n" + "\n".join(bad)


def test_notes_and_buttons_have_one_shape_each():
    """界面规范第五步:提示框只用 .note(n-info / n-warn / n-bad),空状态用 .empty;全局的 .warn / .err 去掉了
    (它们跟「提醒」色的类名串过,营商环境卡片变黄底、线索总览数字卡变黄底都是这个原因),页面里不许再写回来。按钮的样子只在一条规则里定。"""
    css = (PORTAL / "portal.css").read_text(encoding="utf-8")
    assert not re.search(r"^\.(warn|err)\s*\{", css, re.M)
    for f in PORTAL.glob("*.html"):
        if f.name == "login.html":  # 登录页自带样式,不引 portal.css
            continue
        t = f.read_text(encoding="utf-8")
        assert not re.search(r"""class=\\?["'](warn|err)\b""", t), f.name
    js = (PORTAL / "portal.js").read_text(encoding="utf-8")
    assert not re.search(r"""class=\\?["'](warn|err)\b""", js) and 'className = "warn' not in js
    assert len(re.findall(r"background: var\(--brand\); border-color: var\(--brand\); color: #fff", css)) == 1  # 主按钮只定义一次


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_every_page_fits_a_phone_and_a_laptop():
    """各页在手机(390)和笔记本(1366)上:整页不横着滑,名称-数值行、指标卡不变形(LAYOUT 量的那几样)。
    2026-10-14 这样扫出过报告页、公司资料页在手机上被左边表单撑宽。表格太宽在自己框里横滑,不算。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    srv = _serve(fake, schema.read_config(admin, NEW, ONTO))
    pages = ["index.html", "how.html", "ontology.html", "sources.html", "country.html?iso=AZE", "country.html?iso=AZE&t=biz", "country.html?iso=AZE&t=inst",
             "city.html", "trend.html", "overview.html", "analysis.html", "inbox.html", "opps.html", f"opp.html?id={o['id']}", "calendar.html", "reports.html", "me.html", "company.html"]
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            for w in (390, 1366):
                page = browser.new_page(viewport={"width": w, "height": 900})
                for pth in pages:
                    page.goto(f"http://127.0.0.1:{srv.server_port}/{pth}")
                    page.wait_for_load_state("networkidle")
                    assert page.evaluate(LAYOUT, "main") == [], (w, pth)
                page.close()
            browser.close()
    finally:
        srv.shutdown()
        srv.assistant.shutdown()


def test_archived_leads_hidden_by_default_but_listed_under_the_opportunity():
    """过期线索归档(使用方 2026-10-20):后台标「已归档」的线索,线索总览默认不算、一行说明「已归档 N 条 · 显示」点开才算;
    商机工作台的所属线索照旧全列,已归档的在有效线索下面单独一段(不然没法整体判断)。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    _seed(admin, fake)
    b = tstore.Base(admin)
    opp = next(r for r in b.rows("opportunity", props=["code"]) if b.get("opportunity", r, "code") == "AZE-001")
    c = b.rows("country", props=["key"])[0]
    old = (_today() - timedelta(days=400)).isoformat()
    b.create("lead", [{"key": "dddddddd", "title": "Old Ganja feasibility notice", "url": "https://example.org/d", "relevant": True, "category": "立项或规划", "country": c["id"],
                       "found_at": old + "T02:00:00.000Z", "published": old, "opportunity": opp["id"], "status": "已处理", "origin": "机器采集",
                       "archived": True, "archive_reason": "截止已过", "archived_at": _today().isoformat()},
                      {"key": "eeeeeeee", "title": "Stale unassigned notice", "url": "https://example.org/e", "relevant": True, "category": "项目动态", "country": c["id"],
                       "found_at": old + "T03:00:00.000Z", "published": old, "status": "待处理", "origin": "机器采集",
                       "archived": True, "archive_reason": "超过一年", "archived_at": _today().isoformat()}])
    cfg = schema.read_config(admin, NEW, ONTO)
    srv = _serve(fake, cfg)
    base = f"http://127.0.0.1:{srv.server_port}"
    shot = os.environ.get("SHOT")
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/overview.html")
            page.wait_for_function("document.querySelector('#cards .kpi .kn') && !document.getElementById('fNote').textContent.includes('统计中') && document.getElementById('arNote').textContent.includes('已归档')", timeout=15000)
            assert page.inner_text("#cards .kpi .kn") == "3" and "2 条线索没算进来" in page.inner_text("#arNote")
            if shot:
                page.screenshot(path=f"{shot}/线索总览_已归档说明.png", full_page=False)
            page.click("#arNote button")
            page.wait_for_function("document.querySelector('#cards .kpi .kn').textContent === '5' && location.search.includes('ar=1')", timeout=15000)
            assert "已显示归档" in page.inner_text("#arNote")
            page.click("#arNote button")
            page.wait_for_function("document.querySelector('#cards .kpi .kn').textContent === '3' && !location.search.includes('ar=1')", timeout=15000)
            # 线索分析、线索处理也默认不算,说明行一样
            page.goto(base + "/analysis.html")
            page.wait_for_selector("#fWin .chip-btn", timeout=15000)
            page.click("#fWin .chip-btn:text-is('全部')")  # 全部时间(缺省只看最近 30 天,旧线索本来就不在窗口里)
            page.wait_for_function("document.querySelector('#cards .kpi .kn') && document.getElementById('arNote').textContent.includes('已归档')", timeout=15000)
            assert "2 条线索没算进来" in page.inner_text("#arNote")
            page.goto(base + "/inbox.html")
            page.wait_for_selector("#assign .item", timeout=15000)
            assert "eeeeeeee" not in page.inner_text("#assign") or page.locator("#assign .item:has-text('eeeeeeee')").first.evaluate("(e) => !e.offsetParent")
            # 商机工作台:有效线索在上,已归档的单独一段在下面
            page.goto(base + f"/opp.html?id={opp['id']}")
            page.wait_for_function("document.getElementById('leads').textContent.includes('已归档')", timeout=15000)
            txt = page.inner_text("#leads")
            assert txt.index("aaaaaaaa") < txt.index("已归档 1 条") < txt.index("dddddddd") and "截止已过" in txt
            if shot:
                page.screenshot(path=f"{shot}/商机工作台_已归档线索.png", full_page=True)
            browser.close()
            assert not errors, errors
    finally:
        srv.shutdown()


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_opportunity_can_be_abandoned_and_archived_then_restored():
    """放弃跟踪与归档商机(使用方 2026-10-20,本体 A50):商机工作台「放弃跟踪」→ 确认(可写原因)→ 阶段改「放弃」、勾已归档、留一条跟进;
    商机总览默认不算它,一行说明「已归档 1 个商机 · 显示」点开才列;页面顶上说明日期与原因,「恢复跟踪」改回跟踪中。危险操作是红色主按钮。"""
    from playwright.sync_api import Error as PWError, sync_playwright
    fake, admin = _fake_with_schema()
    c, o, ld = _seed(admin, fake)
    b = tstore.Base(admin)
    b.create("opportunity", [{"code": "AZE-003", "name_zh": "巴库供水管网", "country": c["id"], "stage": "跟踪中"}])
    srv = _serve(fake, schema.read_config(admin, NEW, ONTO))
    base = f"http://127.0.0.1:{srv.server_port}"
    shot = os.environ.get("SHOT")
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            errors: list[str] = []
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + f"/opp.html?id={o['id']}")
            page.wait_for_selector("#arcGo", timeout=15000)
            assert page.inner_text("#arcGo") == "放弃跟踪" and "danger" in page.get_attribute("#arcGo", "class")
            assert page.evaluate("getComputedStyle(document.getElementById('arcGo')).backgroundColor") == "rgb(220, 38, 38)"
            assert page.evaluate("parseFloat(getComputedStyle(document.getElementById('fuSave')).fontSize)") >= 15  # 主按钮比普通按钮大
            if shot:
                page.screenshot(path=f"{shot}/商机工作台_放弃跟踪按钮.png", full_page=False)
            page.click("#arcGo")
            page.wait_for_selector("#arcYes", timeout=5000)
            assert "确定放弃跟踪「AZE-001 甘贾污水厂」" in page.inner_text("#arc")
            page.fill("#arcWhy", "业主改为本国预算建设")
            if shot:
                page.screenshot(path=f"{shot}/商机工作台_放弃跟踪确认.png", full_page=False)
            page.click("#arcYes")
            page.wait_for_selector("#arcUndo", timeout=15000)
            head = page.inner_text("#head")
            assert "放弃" in head and "已归档" in head
            box = page.inner_text("#arc")
            assert "这个商机已归档" in box and _today().isoformat() in box and "业主改为本国预算建设" in box
            assert "放弃跟踪,归档:业主改为本国预算建设" in page.inner_text("#timeline") and "阶段 → 放弃" in page.inner_text("#timeline")
            if shot:
                page.screenshot(path=f"{shot}/商机工作台_已归档.png", full_page=False)
            row = next(r for r in b.rows("opportunity", props=["code", "stage", "archived", "archive_reason", "archived_at"]) if b.get("opportunity", r, "code") == "AZE-001")
            assert b.get("opportunity", row, "stage") == "放弃" and b.get("opportunity", row, "archived") is True and b.get("opportunity", row, "archive_reason") == "业主改为本国预算建设"
            assert len([r for r in b.rows("lead", props=["opportunity"]) if tstore.link_id(b.get("lead", r, "opportunity")) == o["id"]]) == 1  # 所属线索不动
            # 商机总览:默认不算,说明行点「显示」才列
            page.goto(base + "/opps.html")
            page.wait_for_function("document.getElementById('arNote').textContent.includes('已归档')", timeout=15000)
            assert "1 个商机没算进来" in page.inner_text("#arNote") and page.inner_text("#nList") == "1" and "AZE-001" not in page.inner_text("#list")
            assert "全部已立项 1" in page.inner_text("#fStage")
            if shot:
                page.screenshot(path=f"{shot}/商机总览_已归档说明.png", full_page=False)
            page.click("#arNote button")
            page.wait_for_function("location.search.includes('ar=1') && document.getElementById('list').textContent.includes('AZE-001')", timeout=15000)
            assert "已显示归档" in page.inner_text("#arNote") and "全部已立项 2" in page.inner_text("#fStage") and "已归档" in page.inner_text("#list")
            if shot:
                page.screenshot(path=f"{shot}/商机总览_显示已归档.png", full_page=False)
            # 日历、首页助手也不算
            page.goto(base + "/calendar.html")
            page.wait_for_function("document.querySelectorAll('#fOpp option').length >= 2", timeout=15000)
            assert "AZE-001" not in page.inner_text("#fOpp") and "AZE-003" in page.inner_text("#fOpp")
            # 恢复跟踪
            page.goto(base + f"/opp.html?id={o['id']}")
            page.wait_for_selector("#arcUndo", timeout=15000)
            page.click("#arcUndo")
            page.wait_for_selector("#arcGo", timeout=15000)
            assert "跟踪中" in page.inner_text("#head") and "已归档" not in page.inner_text("#head") and "恢复跟踪(取消归档)" in page.inner_text("#timeline")
            row = next(r for r in b.rows("opportunity", props=["code", "stage", "archived"]) if b.get("opportunity", r, "code") == "AZE-001")
            assert b.get("opportunity", row, "stage") == "跟踪中" and not b.get("opportunity", row, "archived")
            # 手机宽度:按钮与确认框不把页面撑宽
            phone = browser.new_page(viewport={"width": 390, "height": 844})
            phone.on("pageerror", lambda e: errors.append(str(e)))
            phone.goto(base + f"/opp.html?id={o['id']}")
            phone.wait_for_selector("#arcGo", timeout=15000)
            phone.click("#arcGo")
            phone.wait_for_selector("#arcYes", timeout=5000)
            assert phone.evaluate(LAYOUT, "main") == []
            if shot:
                phone.screenshot(path=f"{shot}/商机工作台_放弃跟踪确认_手机.png", full_page=False)
            phone.click("#arcYes")
            phone.wait_for_selector("#arcUndo", timeout=15000)
            assert phone.evaluate(LAYOUT, "main") == []
            if shot:
                phone.screenshot(path=f"{shot}/商机工作台_已归档_手机.png", full_page=False)
            phone.goto(base + "/opps.html")
            phone.wait_for_function("document.getElementById('arNote').textContent.includes('已归档')", timeout=15000)
            if shot:
                phone.screenshot(path=f"{shot}/商机总览_已归档说明_手机.png", full_page=False)
            browser.close()
            assert not errors, errors
    finally:
        srv.shutdown()
        srv.assistant.shutdown()
