"""自己的登录页(portal/login.html):Caddy 把 /auth/login 指到这个文件,左边系统标志、站名、首页同一段 ASCII 动画,右边登录框;
登录走表格自己的接口 POST /api/auth/signin,登录后回到 redirect 指的本站页面。登录页用到的字体、标志、动画脚本不用登录也能取,其余照旧要登录。
有 Playwright 与本地 Chromium 时,用一个仿 Caddy + 假表格的小服务把登录真的走一遍(没有就跳过那部分)。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "portal"
KIT = ROOT / "deploy" / "teable"
OPEN_FILES = ("/fonts/huiwen-title.woff2", "/fonts/truetypewriter-latin.woff2", "/ascii-water.js", "/logo.png", "/favicon.png")


def _caddy() -> str:
    return (KIT / "portal.caddy").read_text(encoding="utf-8")


def test_caddy_serves_own_login_page_and_only_its_assets_without_login():
    c = _caddy()
    # /auth/login 直接给自己的页面,不经过表格;带上不缓存、防嵌套的标头
    login = c[c.index("@ow_login path /auth/login"):c.index("@ow_login_data path_regexp")]
    assert "root * /srv/portal" in login and "rewrite * /login.html" in login and "file_server" in login
    assert 'X-Frame-Options "DENY"' in login and 'Cache-Control "no-cache"' in login
    # 表格页面里的站内跳转取登录页数据时给 404,浏览器改成整页打开 /auth/login
    assert "@ow_login_data path_regexp ^/_next/data/[^/]+/auth/login\\.json$" in c and re.search(r"handle @ow_login_data \{\s*respond 404\s*\}", c)
    # 不用登录就能取的只有这几个文件(写死文件名,不用通配);页面、配置照旧要登录
    m = re.search(r"@ow_open path ([^\n]+)", c)
    assert m and tuple(m.group(1).split()) == OPEN_FILES
    assert "*" not in m.group(1) and "config.json" not in m.group(1) and ".html" not in m.group(1)
    portal = c[c.index("@ow_portal path /portal /portal/*"):c.index("# 根网址进只读页面")]
    assert portal.index("handle @ow_open") < portal.index("forward_auth teable:3000") < portal.index("handle_path /api/assistant/*") < portal.rindex("file_server")
    assert "redir * /auth/login?redirect=%2Fportal%2F 302" in portal  # 没登录照旧转到登录页(现在是自己的这一页)
    for f in OPEN_FILES:
        assert (PORTAL / f.lstrip("/")).is_file(), f


def test_login_page_carries_the_same_intro_as_home():
    """登录页下面也有系统简介(使用方 2026-10-09):与首页那六段标题、正文一字不差;登录页上没有入口链接,也不读地图文件(没登录取不到),地图是内嵌轮廓。"""
    def texts(name, cls):
        s = (PORTAL / name).read_text(encoding="utf-8")
        a = s.index('<section class="' + cls + '"')
        sec = s[a:s.index("</section>", a)]
        return re.findall(r"<h2>(.*?)</h2>\s*<p>(.*?)</p>", sec), sec
    home, _ = texts("index.html", "intro")
    login, sec = texts("login.html", "about")
    assert len(home) == 6 and login == home
    assert 'class="more"' not in sec and "world.geo" not in sec and 'class="land"' in sec and "echarts" not in (PORTAL / "login.html").read_text(encoding="utf-8")


@pytest.mark.skipif(not (os.environ.get("CADDY_BIN") or shutil.which("caddy")), reason="本机没有 caddy(设 CADDY_BIN 指向 caddy 可执行文件就会核对)")
def test_caddy_accepts_the_config():
    """真的让 Caddy 读一遍配置(主 Caddyfile + portal.caddy),语法、指令顺序有错这里就报。"""
    import tempfile
    caddy = os.environ.get("CADDY_BIN") or shutil.which("caddy")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "portal.caddy").write_text(_caddy(), encoding="utf-8")
        main = (KIT / "Caddyfile").read_text(encoding="utf-8").replace("{$TEABLE_HOST}", "http://127.0.0.1:18080").replace("/etc/caddy/caddy.d/*.caddy", str(Path(d, "portal.caddy")))
        Path(d, "Caddyfile").write_text(main, encoding="utf-8")
        r = subprocess.run([caddy, "adapt", "--config", str(Path(d, "Caddyfile")), "--adapter", "caddyfile"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-800:]
        conf = json.loads(r.stdout)
        assert "/auth/login" in json.dumps(conf) and "login.html" in json.dumps(conf)


def test_login_page_is_self_contained_and_uses_the_table_sign_in():
    s = (PORTAL / "login.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in s and not re.search(r'src="https?://|<link[^>]*href="https?://', s)  # 不引外站的资源(去 GitHub 的普通链接可以)
    assert "docs/AI_INSTALL.md" in s and 'id="copyCmd"' in s  # 页面最后:自己部署一套的那句提示
    assert not re.search(r'(?:href|src)="[^"]*portal\.(?:css|js)"', s)  # 不引 portal.css / portal.js:那两个要登录后才取得到
    assert '"/api/auth/signin"' in s and 'method: "POST"' in s and 'credentials: "same-origin"' in s
    assert "TEABLE_TOKEN" not in s and "Bearer" not in s
    for f in re.findall(r'(?:src|href|poster|data-src)="(/portal/[^"]+)"', s) + re.findall(r'url\("(/portal/[^"]+)"\)', s):
        assert f[len("/portal"):] in OPEN_FILES, f  # 页面用到的每个文件都在不用登录的名单里
    assert "全球水务开源情报" in s and "Global Water OSINT" in s and 'src="/portal/ascii-water.js"' in s and "/portal/logo.png" in s and "media/hero" not in s  # 动画脚本随页面来;10 MB 的视频不再用
    assert 'href="/auth/forget-password"' in s and "/auth/signup" in s


# ---------- 真的登录一遍(有 Playwright 与本地 Chromium 时) ----------



def _serve():
    """仿主机上的样子:/auth/login → login.html;/portal/ 下只有登录页那几个文件不用登录,别的没登录就 302 到登录页;
    /api/auth/* 是假表格(ada@example.com / Right-pass-1 能登上,locked@example.com 试错太多)。"""
    import mimetypes
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        signins: list = []

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def authed(self):
            return "auth_session=ok" in (self.headers.get("Cookie") or "")

        def do_GET(self):  # noqa: N802
            p = self.path.split("?")[0]
            if p == "/auth/login":
                return self._send(200, (PORTAL / "login.html").read_bytes(), "text/html; charset=utf-8")
            if p == "/api/auth/user":
                return self._send(200, '{"id":"usr1"}') if self.authed() else self._send(401, '{"code":"unauthorized"}')
            if p == "/api/admin/setting/public":
                return self._send(200, '{"disallowSignUp": true}')
            if p.startswith("/portal/"):
                rel = p[len("/portal"):]
                if rel in OPEN_FILES:
                    f = PORTAL / rel.lstrip("/")
                    return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream") if f.is_file() else self._send(404, "{}")
                if not self.authed():
                    return self._send(302, "", extra={"Location": "/auth/login?redirect=%2Fportal%2F"})
                return self._send(200, "<!doctype html><title>landed " + rel + "</title><p>ok</p>", "text/html; charset=utf-8")
            return self._send(404, "{}")

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/auth/signin":
                H.signins.append(body)
                if body.get("email") == "ada@example.com" and body.get("password") == "Right-pass-1":
                    return self._send(200, '{"id":"usr1"}', extra={"Set-Cookie": "auth_session=ok; Path=/; HttpOnly; SameSite=Lax"})
                if body.get("email") == "locked@example.com":
                    return self._send(429, '{"message":"Too many requests","status":429,"code":"too_many_requests","data":{"minutes":15}}')
                return self._send(400, '{"message":"Email or password is incorrect","status":400,"code":"invalid_credentials","data":{"attempts":1}}')
            return self._send(404, "{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.handler = H
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.skipif(pytest.importorskip("playwright.sync_api", reason="没有 Playwright") is None, reason="没有 Playwright")
def test_login_page_signs_in_and_returns_to_the_page():
    from playwright.sync_api import Error as PWError, sync_playwright
    srv = _serve()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PWError:
                browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1920, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/auth/login?redirect=%2Fportal%2Fme.html")
            # 左边:标志、站名(中英)、首页同一段 ASCII 动画(脚本画在画布上,不取任何文件)
            page.wait_for_function("(() => { const c = document.getElementById('heroAscii'); return c && c.width > 0 && c.getContext('2d').getImageData(0, 0, c.width, c.height).data.some((v, i) => i % 4 !== 3 && v > 80); })()", timeout=10000)
            assert page.get_attribute(".brand img", "src") == "/portal/logo.png" and page.inner_text(".intro h1") == "全球水务开源情报" and page.inner_text(".intro p") == "Global Water OSINT"
            v = page.evaluate("(() => { const b = document.querySelector('.vid').getBoundingClientRect(); return { w: b.width, h: b.height, bottom: b.bottom, doc: document.querySelector('.page').getBoundingClientRect().bottom, about: document.getElementById('about').getBoundingClientRect().top }; })()")
            assert abs(v["w"] / v["h"] - 1.6) < 0.01 and v["bottom"] <= 950 and v["doc"] <= 951 and v["about"] >= 950, v  # 比例不变、首屏一屏放下;简介在下面,往下滚才看到
            assert page.evaluate("document.fonts.check('20px \"Huiwen Mincho\"')")  # 站名字体没登录也取得到
            assert not page.locator("#signup").is_visible()  # 没开注册、也不是邀请链接:不露「先注册」
            # 填错的几种情况
            page.click("#go")
            assert "请填邮箱和密码" in page.inner_text("#err")
            page.fill("#email", "not-an-email")
            page.fill("#password", "whatever-1")
            page.click("#go")
            assert "邮箱格式不对" in page.inner_text("#err")
            page.fill("#email", "ada@example.com")
            page.fill("#password", "wrong-pass-1")
            page.click("#go")
            page.wait_for_function("document.getElementById('err').innerText.includes('不对')", timeout=5000)
            assert "邮箱或密码不对" in page.inner_text("#err") and page.url.endswith("/auth/login?redirect=%2Fportal%2Fme.html")
            page.fill("#email", "locked@example.com")
            page.click("#go")
            page.wait_for_function("document.getElementById('err').innerText.includes('分钟')", timeout=5000)
            assert "15 分钟" in page.inner_text("#err")
            # 显示 / 隐藏密码
            page.click("#peek")
            assert page.get_attribute("#password", "type") == "text"
            page.click("#peek")
            assert page.get_attribute("#password", "type") == "password"
            # 登对了:回到原来要去的页面;邮箱去掉空格、转小写再交给表格
            page.fill("#email", "  Ada@example.com ")
            page.fill("#password", "Right-pass-1")
            page.click("#go")
            page.wait_for_url("**/portal/me.html", timeout=8000)
            assert srv.handler.signins[-1] == {"email": "ada@example.com", "password": "Right-pass-1"}
            # 已经登录的再开登录页:直接过去
            page.goto(base + "/auth/login?redirect=%2Fportal%2Fopps.html%3Fs%3D1")
            page.wait_for_url("**/portal/opps.html?s=1", timeout=8000)
            assert not errors, errors
            # 外站、登录页本身当 redirect 一律不认,登录后回首页
            for bad in ("https%3A%2F%2Fevil.example%2F", "%2F%2Fevil.example%2Fx", "%2Fauth%2Flogin"):
                ctx = browser.new_context()
                p2 = ctx.new_page()
                p2.goto(base + "/auth/login?redirect=" + bad)
                p2.fill("#email", "ada@example.com")
                p2.fill("#password", "Right-pass-1")
                p2.click("#go")
                p2.wait_for_url(base + "/portal/", timeout=8000)
                ctx.close()
            # 邀请链接来的:露出「先注册」,注册页仍是表格自己的,带着同样的 redirect
            ctx = browser.new_context()
            p3 = ctx.new_page()
            q = "?redirect=%2Finvite%3FinvitationId%3Dinv1%26invitationCode%3Dabc"
            p3.goto(base + "/auth/login" + q)
            p3.wait_for_selector("#signup", state="visible", timeout=5000)
            assert p3.get_attribute("#signupLink", "href") == "/auth/signup" + q
            ctx.close()
            # 手机:站名、登录框在上(第一屏就能填),动画在下面
            ctx = browser.new_context(viewport={"width": 390, "height": 760})
            p4 = ctx.new_page()
            p4.goto(base + "/auth/login?redirect=%2Fportal%2F")
            p4.wait_for_function("document.getElementById('heroAscii').width > 0", timeout=10000)
            f = p4.evaluate("(() => { const a = document.getElementById('f').getBoundingClientRect(), b = document.querySelector('.vid').getBoundingClientRect(); return { formBottom: a.bottom, vidTop: b.top, w: document.documentElement.scrollWidth }; })()")
            assert f["formBottom"] <= 760 and f["vidTop"] >= f["formBottom"] and f["w"] <= 390, f
            ctx.close()
            browser.close()
    finally:
        srv.shutdown()
