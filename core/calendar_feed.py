"""日历订阅(.ics):把门户日历里的东西导成标准日历文件,苹果日历、Outlook、Google 日历都能订阅或导入。

两条路都到同一个接口(后台任务进程里的助手接口,core/assistant_server.py):
- 登录后在日历页点「下载 .ics」:/portal/api/assistant/calendar.ics(Caddy 的登录检查在前,转过来时带 X-Portal-Auth: 1);
- 订阅:/portal/feed/calendar.ics?t=<令牌>(Caddy 不做登录检查,苹果日历这类程序没法登录;令牌不对就 403)。
  令牌是整个团队共用的一串(环境变量 OW_FEED_TOKEN;没设就按机器账号令牌派生一串,不用另配),日历页「订阅到日历」里给出链接;
  只读、只含日历里本来就有的内容(跟踪中商机的时间节点、跟进的下一步到期、国别级节点),不含内部备注。

内容窗口:今天往前 30 天、往后 365 天;可按国别(ISO3)、商机(编号)筛。每条是全天事件;5 分钟内重复请求用缓存。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from core import ontology as onto_mod
from core import tstore

PAST_DAYS, AHEAD_DAYS = 30, 365
CACHE_SECONDS = 300
CAL_NAME = "全球水务开源情报"
PRODID = "-//Global Water OSINT//calendar//CN"


def feed_token(machine_token: str | None = None) -> str:
    """订阅链接里的令牌:OW_FEED_TOKEN,否则按机器账号令牌派生(同一台服务器上稳定,换了机器令牌就换)。"""
    t = (os.environ.get("OW_FEED_TOKEN") or "").strip()
    if t:
        return t
    seed = machine_token if machine_token is not None else (os.environ.get("TEABLE_TOKEN") or "")
    if not seed:
        return ""
    return hashlib.sha256(("calendar-feed:" + seed).encode("utf-8")).hexdigest()[:32]


def _esc(s: object) -> str:
    return str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545:一行最多 75 字节,续行以一个空格开头(按字节切,不切断 UTF-8 字符)。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur)
            cur = b
        else:
            cur += b
    out.append(cur)
    return "\r\n ".join(x.decode("utf-8") for x in out)


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def render(events: list[dict], stamp: datetime | None = None, tz: str = "Asia/Shanghai") -> str:
    """events = [{uid, day(date), summary, description?, url?}] → .ics 文本(全天事件)。"""
    stamp = stamp or datetime.now(timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(CAL_NAME)}", f"X-WR-TIMEZONE:{tz}", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for e in sorted(events, key=lambda x: (x["day"], x["uid"])):
        d: date = e["day"]
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}",
                  f"DTSTART;VALUE=DATE:{_ymd(d)}", f"DTEND;VALUE=DATE:{_ymd(d + timedelta(days=1))}", f"SUMMARY:{_esc(e['summary'])}"]
        if e.get("description"):
            lines.append(f"DESCRIPTION:{_esc(e['description'])}")
        if e.get("url"):
            lines.append(f"URL:{e['url']}")
        if e.get("categories"):
            lines.append(f"CATEGORIES:{_esc(e['categories'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def collect(b: tstore.Base, today: date, country: str | None = None, opp: str | None = None, site: str = "") -> list[dict]:
    """从库里取日历内容(与页面日历同一口径:跟踪中的商机;本体 glossary.tracked)。country = ISO3,opp = 商机编号。"""
    tracked = set(onto_mod.glossary_stages("tracked"))
    start, end = today - timedelta(days=PAST_DAYS), today + timedelta(days=AHEAD_DAYS)
    country = (country or "").strip().upper() or None
    opp = (opp or "").strip().upper() or None
    opps = b.rows("opportunity", props=["code", "name_zh", "stage", "country"] + (["archived"] if b.has("opportunity", "archived") else []))
    iso_of = {r["id"]: str(b.get("country", r, "key") or "") for r in b.rows("country", props=["key"])}
    keep: dict[str, dict] = {}
    for r in opps:
        if str(b.get("opportunity", r, "stage") or "") not in tracked or b.is_archived("opportunity", r):  # 已归档(放弃跟踪)的不进日历
            continue
        code = str(b.get("opportunity", r, "code") or "")
        iso = iso_of.get(tstore.link_id(b.get("opportunity", r, "country")) or "", "")
        if opp and code != opp:
            continue
        if country and iso != country:
            continue
        keep[r["id"]] = {"code": code, "name": str(b.get("opportunity", r, "name_zh") or ""), "iso": iso}
    crid = next((rid for rid, iso in iso_of.items() if iso == country), None) if country else None
    window = b.filter(b.cond("milestone", "day", "isOnOrAfter", {"mode": "exactDate", "exactDate": start.isoformat() + "T00:00:00.000Z", "timeZone": b.tz}),
                      b.cond("milestone", "day", "isOnOrBefore", {"mode": "exactDate", "exactDate": end.isoformat() + "T00:00:00.000Z", "timeZone": b.tz}))
    events: list[dict] = []
    link = (site.rstrip("/") + "/portal/") if site else ""
    for m in b.rows("milestone", filter=window, props=["opportunity", "lead", "country", "kind", "day", "status", "label"]):
        orid = tstore.link_id(b.get("milestone", m, "opportunity"))
        crid_m = tstore.link_id(b.get("milestone", m, "country"))
        day = tstore.day(b.get("milestone", m, "day"), b.tz)
        if not day:
            continue
        kind, status, label = (str(b.get("milestone", m, p) or "") for p in ("kind", "status", "label"))
        if orid:
            o = keep.get(orid)
            if not o:
                continue
            summary = f"{kind} · {o['code']} {o['name']}".strip()
            desc = " · ".join(x for x in (status, label, o["iso"]) if x)
            url = link + "opp.html?id=" + orid if link else ""
        elif crid_m and not tstore.link_id(b.get("milestone", m, "lead")):
            if opp or (country and crid_m != crid):
                continue
            iso = iso_of.get(crid_m, "")
            summary = f"{kind} · {iso}".strip()
            desc = " · ".join(x for x in (status, label) if x)
            url = link + "country.html?iso=" + iso if link and iso else ""
        else:
            continue  # 未归商机的线索截止:页面日历缺省也不显示
        events.append({"uid": f"ms-{m['id']}@water-osint", "day": date.fromisoformat(day), "summary": summary, "description": desc, "url": url, "categories": "时间节点"})
    if keep:
        due = b.filter(b.cond("followup", "next_due", "isOnOrAfter", {"mode": "exactDate", "exactDate": start.isoformat() + "T00:00:00.000Z", "timeZone": b.tz}),
                       b.cond("followup", "next_due", "isOnOrBefore", {"mode": "exactDate", "exactDate": end.isoformat() + "T00:00:00.000Z", "timeZone": b.tz}))
        for f in b.rows("followup", filter=due, props=["opportunity", "next_step", "next_due", "author"]):
            orid = tstore.link_id(b.get("followup", f, "opportunity"))
            o = keep.get(orid or "")
            day = tstore.day(b.get("followup", f, "next_due"), b.tz)
            if not o or not day:
                continue
            step = str(b.get("followup", f, "next_step") or "")
            who = b.get("followup", f, "author")
            who = (who.get("title") if isinstance(who, dict) else "") or ""
            events.append({"uid": f"due-{f['id']}@water-osint", "day": date.fromisoformat(day), "summary": f"到期 · {o['code']} · {step}".rstrip(" ·"),
                           "description": " · ".join(x for x in (o["name"], who) if x), "url": (link + "opp.html?id=" + orid) if link else "", "categories": "下一步到期"})
    return events


class Feed:
    """给助手接口用:懒连库(机器账号),按参数出 .ics,5 分钟缓存。base_factory 便于测试换成内存版。"""

    def __init__(self, base_factory=None, tz: str = "Asia/Shanghai", log=print):
        self.tz, self.log = tz, log
        self._factory = base_factory or self._connect
        self._base: tstore.Base | None = None
        self._cache: dict[tuple, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _connect() -> tstore.Base:
        url, token = tstore.credentials()
        return tstore.Base(tstore.Client(url, token))

    def base(self) -> tstore.Base:
        if self._base is None:
            self._base = self._factory()
        return self._base

    def ics(self, today: date, country: str | None = None, opp: str | None = None, site: str = "") -> str:
        key = (today.isoformat(), (country or "").upper(), (opp or "").upper(), site)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
        try:
            text = render(collect(self.base(), today, country, opp, site), tz=self.tz)
        except Exception:
            self._base = None  # 连库出错下次重连
            raise
        with self._lock:
            self._cache[key] = (now, text)
        return text
