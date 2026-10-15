"""助手提问记录与错题本(本体 ask_log,v2.26;图一 ⑲、图二 ⑫–⑮「沉淀、回灌」)。

- 每问一句,助手接口在表格「助手提问记录」里记一行(A44):提问、接着哪一问、理解为、谁问的、什么时候(record)。
- 答案下面有「答对了」「答错了」:点了记在这一行(A45,feedback)。答对了的把当时的「理解为」抄进「应理解为」,
  答错了的处理状态记「待看」,由懂行的人或改程序的人看、修好后在「应理解为」写上对的理解、状态改「已修好」。
- 每天系统自动更新之后(助手接口里的一个线程,本地时间 08:40 以后),把「应理解为」不空、没标「不改」的问题重问一遍,
  理解和「应理解为」对不上的写进「复查」(A46,recheck)。理解为里带具体日期的一项(本月、上周……)天天变,不比。

单独跑(调试、手动复查):python -m core.ask_log --profile private/profile.yaml recheck
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime

import httpx

from core import assistant, tstore

DATE_RE = re.compile(r"\d{4}-\d\d-\d\d")
_who: dict[str, tuple[str, float]] = {}
_who_lock = threading.Lock()


def new_key(now: datetime | None = None) -> str:
    """一行的编号:Q + 时间 + 4 位随机(页面点答对、答错时按它找这一行)。"""
    return (now or datetime.now()).strftime("Q%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def understood_text(p: dict) -> str:
    """计划的「理解为」→ 一行一项「名称:值」(表里看、复查时比)。"""
    return "\n".join(f"{k}:{v}" for k, v in p.get("understood") or [])


def _lines(text: str) -> dict[str, str]:
    out = {}
    for ln in str(text or "").splitlines():
        k, sep, v = ln.replace(":", ":", 1).partition(":")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def compare(expected: str, p: dict) -> list[str]:
    """「应理解为」与这次的理解逐项比 → 不一样的地方(空 = 对得上)。值里带具体日期的一项不比(天天变)。"""
    want, got = _lines(expected), _lines(understood_text(p))
    diffs = []
    for k, v in want.items():
        if DATE_RE.search(v) or DATE_RE.search(got.get(k, "")):
            continue
        if k not in got:
            diffs.append(f"少了「{k}:{v}」")
        elif got[k] != v:
            diffs.append(f"{k} 应为「{v}」,现在是「{got[k]}」")
    for k, v in got.items():
        if k not in want and not DATE_RE.search(v):
            diffs.append(f"多了「{k}:{v}」")
    return diffs


def who(cookie: str | None) -> str:
    """登录 cookie → 「名字 <邮箱>」(问表格 /api/auth/user;十分钟内按 cookie 记住)。问不到给空。"""
    if not cookie:
        return ""
    now = time.monotonic()
    with _who_lock:
        hit = _who.get(cookie)
        if hit and now - hit[1] < 600:
            return hit[0]
    name = ""
    try:
        url = (os.environ.get("TEABLE_URL") or "").rstrip("/")
        r = httpx.get(f"{url}/api/auth/user", headers={"Cookie": cookie}, timeout=5)
        if r.status_code == 200:
            u = r.json()
            name = (str(u.get("name") or "") + (f" <{u['email']}>" if u.get("email") else "")).strip()
    except Exception:  # noqa: BLE001  记不到是谁不影响提问
        name = ""
    with _who_lock:
        if len(_who) > 500:
            _who.clear()
        _who[cookie] = (name, now)
    return name


def record(base: tstore.Base, key: str, q: str, prev_q: str, p: dict, asker: str, now: datetime) -> None:
    """A44:记一行提问(只新建)。"""
    by = "模型" + (f"({p['model']})" if p.get("model") else "") if p.get("parsed_by") == "model" else "词表"
    row = {"question": q[:500], "key": key, "understood": understood_text(p), "asked_at": now.isoformat(timespec="seconds"), "parsed_by": by}
    if prev_q:
        row["prev_q"] = prev_q[:500]
    if asker:
        row["asker"] = asker
    base.create("ask_log", [{k: v for k, v in row.items() if base.has("ask_log", k)}])


def find(base: tstore.Base, key: str) -> dict | None:
    rows = base.rows("ask_log", filter=base.filter(base.cond("ask_log", "key", "is", key)))
    return rows[0] if rows else None


def feedback(base: tstore.Base, key: str, verdict: str, note: str = "", result: str = "", info: dict | None = None, wait: float = 4.0) -> dict:
    """A45:答案下面点了「答对了」「答错了」。按编号找这一行(记行的线程可能还没写完,等一会儿);找不到就按页面带来的提问补一行。
    答对了:「应理解为」空着的抄这次的理解为;答错了:处理状态空着的记「待看」。"""
    if verdict not in ("答对了", "答错了"):
        raise ValueError("反馈只能是「答对了」或「答错了」")
    rec, t0 = find(base, key), time.monotonic()
    while rec is None and time.monotonic() - t0 < wait:
        time.sleep(0.5)
        rec = find(base, key)
    info = info or {}
    if rec is None:
        row = {"question": str(info.get("q") or "")[:500] or "(没带上提问)", "key": key, "understood": str(info.get("understood") or ""),
               "asked_at": datetime.now().isoformat(timespec="seconds")}
        if info.get("prev_q"):
            row["prev_q"] = str(info["prev_q"])[:500]
        rec = base.create("ask_log", [{k: v for k, v in row.items() if base.has("ask_log", k)}])[0]
    vals: dict = {"verdict": verdict}
    if note:
        vals["note"] = note[:2000]
    if result:
        vals["result"] = result[:500]
    if verdict == "答对了" and not base.get("ask_log", rec, "expected"):
        vals["expected"] = base.get("ask_log", rec, "understood") or str(info.get("understood") or "")
    if verdict == "答错了" and not base.get("ask_log", rec, "status"):
        vals["status"] = "待看"
    base.update("ask_log", rec["id"], {k: v for k, v in vals.items() if base.has("ask_log", k)})
    return {"ok": True}


def recheck(base: tstore.Base, today: date, clf=None, log=print) -> dict:
    """A46:「应理解为」不空、没标「不改」的问题重问一遍(接着问的先问上一问),对不上的写进「复查」。"""
    if "ask_log" not in base.t:
        return {}
    rows = [r for r in base.rows("ask_log", props=["question", "prev_q", "expected", "status", "recheck"])
            if str(base.get("ask_log", r, "expected") or "").strip() and base.get("ask_log", r, "status") != "不改"]
    ok = bad = 0
    stamp = f"{today.month}-{today.day}"
    updates = []
    for r in rows:
        q, pq = str(base.get("ask_log", r, "question") or ""), str(base.get("ask_log", r, "prev_q") or "")
        prev = None
        if pq:
            pp = assistant.plan(pq, today, clf)
            prev = {"q": pq, "object": pp["object"], "filters": pp["filters"], "measures": pp.get("measures") or [], "group_by": pp.get("group_by"),
                    "order": pp.get("order"), "law": pp.get("law")}
        diffs = compare(str(base.get("ask_log", r, "expected")), assistant.plan(q, today, clf, prev))
        text = f"通过({stamp})" if not diffs else f"不通过({stamp}):" + ";".join(diffs)
        ok, bad = ok + (not diffs), bad + bool(diffs)
        if text != base.get("ask_log", r, "recheck"):
            updates.append((r["id"], {"recheck": text[:1000]}))
    if updates and base.has("ask_log", "recheck"):
        base.update_many("ask_log", updates)
    if rows:
        log(f"错题本复查:{len(rows)} 句,通过 {ok},不通过 {bad}")
    return {"rechecked": len(rows), "failed": bad}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="助手错题本:重问「应理解为」不空的提问,对不上的写进「复查」")
    ap.add_argument("cmd", choices=["recheck"])
    ap.add_argument("--profile", default="private/profile.yaml")
    ap.add_argument("--no-llm", action="store_true", help="不用模型,只按词表解析")
    a = ap.parse_args(argv)
    from core.assistant_server import Assistant
    from core.profile import load_profile
    bot = Assistant(load_profile(a.profile), use_llm=not a.no_llm)
    bot.refresh_terms()
    print(recheck(bot.db(), bot.today, bot.clf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
