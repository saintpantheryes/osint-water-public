"""线索的中文标题(A03 的补译):每个周期给「中文标题」空着的线索补一批。

页面清单 2026-10-28 起一律中文标题在前、原题小字在后,不再拿项目名或中文摘要当标题(摘要里混着模型的判断,如「不属于……」)。
新线索判定时模型顺手译;老线索在这里补:原题本来是中文的照抄,不花钱;外文的请不思考的便宜模式一次译 TITLE_ZH_BATCH 条,
与报告用的译名缓存(title_zh.json)同一份,译过的不再花钱。只填空;没配模型、表里没这一列时什么也不做。
"""
from __future__ import annotations

import json
import re


from .common import Ctx
from .reports import TITLE_ZH_BATCH, TITLE_ZH_GUIDE, TITLE_ZH_SCHEMA, TITLE_ZH_SYSTEM

_CJK = re.compile(r"[㐀-鿿]")


def _cache(ctx: Ctx) -> tuple[dict[str, str], object]:
    fp = ctx.cache.root.parent / "title_zh.json"
    try:
        return (json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}), fp
    except (OSError, ValueError):
        return {}, fp


def translate(ctx: Ctx, limit: int = TITLE_ZH_BATCH, log=print) -> int:
    """→ 这次写了几条中文标题。"""
    b = ctx.base
    if not b.has("lead", "title_zh"):
        return 0
    it = b.api.iter_records(b.t["lead"], filter=b.filter(b.cond("lead", "title_zh", "isEmpty"), b.cond("lead", "title", "isNotEmpty")),
                            projection=[b.fid("lead", "title")], take=limit)
    rows = [r for _, r in zip(range(limit), it)]  # 只要第一页
    if not rows:
        return 0
    first = lambda r: (str(b.get("lead", r, "title") or "").strip().splitlines() or [""])[0][:300]  # noqa: E731
    done, fp = _cache(ctx)
    ups: list[tuple[str, dict]] = []
    want: list[tuple[dict, str]] = []
    for r in rows:
        t = first(r)
        if not t:
            continue
        if _CJK.search(t):
            ups.append((r["id"], {"title_zh": t}))  # 原题已是中文(同事上传的、台账出处的):照抄
        elif done.get(t):
            ups.append((r["id"], {"title_zh": done[t][:300]}))
        else:
            want.append((r, t))
    clf = ctx.clf_light
    if want and clf is not None and getattr(clf, "enabled", False) and hasattr(clf, "ask"):
        out = clf.ask(TITLE_ZH_SYSTEM, json.dumps([{"i": i, "t": t} for i, (_, t) in enumerate(want)], ensure_ascii=False), TITLE_ZH_SCHEMA, TITLE_ZH_GUIDE)
        if not out or out.get("_error"):
            log(f"  ! 线索中文标题没译成:{(out or {}).get('_error') or '模型未返回'}")
        else:
            for item in out.get("items") or []:
                try:
                    i, zh = int(item.get("i")), str(item.get("zh") or "").strip()
                except (TypeError, ValueError, AttributeError):
                    continue
                if 0 <= i < len(want) and zh and _CJK.search(zh):
                    done[want[i][1]] = zh
                    ups.append((want[i][0]["id"], {"title_zh": zh[:300]}))
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(json.dumps(done, ensure_ascii=False, indent=0), encoding="utf-8")
            except OSError:
                pass
    if ups:
        b.update_many("lead", ups)
    return len(ups)



