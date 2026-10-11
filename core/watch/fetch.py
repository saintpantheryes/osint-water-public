"""带纪律的网页抓取:如实 UA、按主机限速、403/429 即停、超时重试一次、下载限额。

与 core/sources/http.py 同一套纪律,区别是这里取回的是网页、RSS、PDF 等原始字节,并按主机分别限速。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from core.sources.http import SourceBlocked
from core.storage import USER_AGENT

MIN_INTERVAL_S = 1.0  # 同一主机两次请求的最小间隔
_last_call: dict[str, float] = {}
_blocked: dict[str, str] = {}  # 本次运行里回过 403/429 的主机:不再请求


class FetchError(RuntimeError):
    """HTTP 4xx/5xx(403/429 除外,它们抛 SourceBlocked)、超出下载限额等。"""


@dataclass(frozen=True)
class Page:
    url: str  # 跟随跳转后的最终地址
    status: int
    content: bytes
    content_type: str

    @property
    def text(self) -> str:
        charset = "utf-8"
        for part in self.content_type.split(";"):
            part = part.strip()
            if part.lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _throttle(host: str) -> None:
    """同一主机两次请求至少间隔 MIN_INTERVAL_S;本次运行回过 403/429 的主机不再请求。"""
    if host in _blocked:
        raise SourceBlocked(f"{_blocked[host]}(本次运行已停止请求 {host})")
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def _read(r: httpx.Response, url: str, host: str, max_bytes: int) -> Page:
    if r.status_code in (403, 429):
        _blocked[host] = f"{r.status_code} from {url}"
        raise SourceBlocked(_blocked[host])
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} from {url}")
    buf = bytearray()
    for chunk in r.iter_bytes():
        buf += chunk
        if len(buf) > max_bytes:
            raise FetchError(f"超过下载限额 {max_bytes} 字节:{url}")
    return Page(str(r.url), r.status_code, bytes(buf), r.headers.get("content-type", ""))


def reset_blocked() -> None:
    """新的一轮采集开始:忘掉上一轮回过 403/429 的主机。后台任务是常驻进程,不清的话一次限流会一直停到重启。"""
    _blocked.clear()


def get(url: str, params: dict | None = None, timeout: float = 60.0, max_bytes: int = 20_000_000,
        timeout_retries: int = 1, wait_429: float = 0.0, retries_429: int = 0) -> Page:
    """GET 一个地址。403/429 抛 SourceBlocked(不换 UA,本次运行不再请求该主机);超时重试 timeout_retries 次,间隔 5 秒。
    对方明说了限速间隔的(如 GDELT「每 5 秒一次」),可按 wait_429 秒的间隔对 429 再试 retries_429 次;403 一律不重试。"""
    host = urlparse(url).netloc
    timeouts = left_429 = 0
    while True:
        _throttle(host)
        try:
            with httpx.stream("GET", url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                              follow_redirects=True) as r:
                return _read(r, url, host, max_bytes)
        except httpx.TimeoutException:
            timeouts += 1
            if timeouts > timeout_retries:
                raise
            time.sleep(5)
        except SourceBlocked:
            if not _blocked.get(host, "").startswith("429") or left_429 >= retries_429:
                raise
            left_429 += 1
            del _blocked[host]
            time.sleep(wait_429)


class Session:
    """带 cookie 的会话:要先打开检索页、再回发表单才出结果的网站(如 Atexo 公共采购平台)用。
    纪律与 get() 相同:如实 UA、按主机限速、403/429 即停、下载限额;只做页面上公开的检索,不登录。"""

    def __init__(self, timeout: float = 60.0):
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def request(self, method: str, url: str, data: dict | None = None, max_bytes: int = 20_000_000) -> Page:
        host = urlparse(url).netloc
        _throttle(host)
        with self._client.stream(method, url, data=data) as r:
            return _read(r, url, host, max_bytes)

    def get(self, url: str) -> Page:
        return self.request("GET", url)

    def post(self, url: str, data: dict) -> Page:
        return self.request("POST", url, data=data)


def session(timeout: float = 60.0) -> Session:
    return Session(timeout)
