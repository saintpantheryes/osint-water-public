"""带纪律的 HTTP:如实 UA、串行、限速、403/429 即停不重试。"""
from __future__ import annotations

import time

import httpx

from core.storage import USER_AGENT

MIN_INTERVAL_S = 1.0
_last_call = 0.0


class SourceBlocked(RuntimeError):
    """403/429:按纪律停止,不换 UA、不换 IP、不重试。"""


def get_json(url: str, params: dict | None = None, ua: str = USER_AGENT, timeout: float = 30.0, timeout_retries: int = 1):
    """超时(对方慢)可按 timeout_retries 重试,间隔 5 秒;403/429 立即停止,不重试。"""
    global _last_call
    for attempt in range(timeout_retries + 1):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            r = httpx.get(url, params=params, headers={"User-Agent": ua}, timeout=timeout, follow_redirects=True)
        except httpx.TimeoutException:
            if attempt >= timeout_retries:
                raise
            time.sleep(5)
            continue
        if r.status_code in (403, 429):
            raise SourceBlocked(f"{r.status_code} from {url}")
        r.raise_for_status()
        return r.json()
