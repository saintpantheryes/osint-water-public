"""数据源适配器注册表。键名 = profile.sources_enabled 里的名字。

每个适配器:run(profile) -> dict(table -> rows)。只碰公开数据;只读 profile 参数。
"""
from __future__ import annotations

from collections.abc import Callable

from core.profile import Profile

Adapter = Callable[[Profile], dict[str, list[dict]]]
REGISTRY: dict[str, Adapter] = {}


def register(name: str):
    def deco(fn: Adapter) -> Adapter:
        REGISTRY[name] = fn
        return fn

    return deco


def load_all() -> None:
    # 显式导入以触发注册;新适配器在此追加
    from core.sources import imf_datamapper, wb_country_meta, wb_projects_v3, wdi_indicators  # noqa: F401
