"""游客只读通道:不是库协作者的登录账号(游客演示帐号)在页面上看到的全部内容都从这里过。

为什么:这版表格的「查看者」是整库可看的,没有按表、按行、按字段的权限;游客只要是库里的协作者,打开表格的网址就能看到
内部备注、跟进记录和内部版报告。所以游客不进库:页面发现登录人在库里没有角色(GET /api/base/{库} 是 403),所有读表请求改走
/portal/api/assistant/guest/*(Caddy 的登录检查在前,只有登录的人到得了),后台任务进程用机器账号去表格里读,只放行本体
guest 节里列的对象与属性,报告只给定期报告的公开版;跟进记录、成员、报告模板整表不经过。

接口(core/assistant_server.py 挂上):
    GET /guest/policy                                  →  {"objects": {线索: {"id": 表编号, "deny": [字段编号…]}…}, "note": …}
    GET /guest/table/{表}/record?filter=…&projection=…  →  同表格的读记录接口,去掉不放行的字段;filter / orderBy 里引用了不放行的字段 → 403
    GET /guest/table/{表}/record/{记录}                  →  一条记录(报告要符合行规则,否则 404)
    GET /guest/table/{表}/aggregation/row-count?filter=… →  计数

示例数据(本体 guest.data: sample,2026-10-09 起):游客看到的记录全部来自 core/sample.py 编的示例,真实库只提供表与字段的结构;
放行范围照旧按上面的规则算(两道关)。日历下载也给游客换成示例(is_outsider 按登录 cookie 问表格这人是不是库里的人)。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx

from core import ontology as onto_mod
from core import sample, tstore

ALLOWED_PARAMS = {"take", "skip", "filter", "orderBy", "projection", "viewId"}
TAKE = 1000


class Denied(Exception):
    """请求越过了游客的范围(表、字段);接口答 403。"""


class Policy:
    """按本体 guest 节 + 库里的编号算出:哪些表能看、每表去掉哪些字段、报告的行规则。"""

    def __init__(self, b: tstore.Base, raw: dict | None = None):
        g = (raw if raw is not None else onto_mod.raw()).get("guest") or {}
        self.note = str(g.get("note") or "")
        self.by_table: dict[str, dict] = {}
        self.by_oid: dict[str, dict] = {}
        for oid, spec in (g.get("objects") or {}).items():
            spec = spec or {}
            if oid not in b.t:  # 库里还没建的表(新对象,等跑 schema)
                continue
            tid = b.t[oid]
            deny_ids = {b.fid(oid, p) for p in (spec.get("deny") or []) if b.has(oid, p)}
            allowed = {f["id"] for f in b.f[oid].values()} - deny_ids
            rows = None
            if spec.get("edition") or spec.get("kinds_group"):
                conds = []
                if spec.get("edition"):
                    conds.append(b.cond(oid, "edition", "is", spec["edition"]))
                if spec.get("kinds_group"):
                    conds.append(b.cond(oid, "kind", "isAnyOf", list(onto_mod.group_values("report_kind", spec["kinds_group"]))))
                rows = b.filter(*conds)
            entry = {"oid": oid, "id": tid, "allowed": allowed, "deny": sorted(deny_ids), "rows": rows, "rows_text": str(spec.get("rows") or "")}
            self.by_table[tid] = entry
            self.by_oid[oid] = entry

    def public(self) -> dict:
        """给页面的版本(字段编号,不含放行集合)。"""
        return {"objects": {oid: {"id": e["id"], "deny": e["deny"], "rows": e["rows_text"]} for oid, e in self.by_oid.items()}, "note": self.note}

    def table(self, tid: str) -> dict:
        e = self.by_table.get(tid)
        if e is None:
            raise Denied("这张表不对游客开放")
        return e

    def check_fields(self, e: dict, node: Any) -> None:
        """filter / orderBy 里出现的 fieldId 必须都在放行范围里(递归看嵌套的 filterSet)。"""
        if isinstance(node, dict):
            fid = node.get("fieldId")
            if fid is not None and fid not in e["allowed"]:
                raise Denied("条件里用了游客看不到的字段")
            for v in node.values():
                self.check_fields(e, v)
        elif isinstance(node, list):
            for v in node:
                self.check_fields(e, v)

    def strip(self, e: dict, rec: dict) -> dict:
        fields = rec.get("fields") or {}
        return {**rec, "fields": {k: v for k, v in fields.items() if k in e["allowed"]}}

    def matches_rows(self, e: dict, rec: dict) -> bool:
        """单条记录是否符合行规则(只有报告有;按「是 / isAnyOf」两种条件自己算)。"""
        if not e["rows"]:
            return True
        fields = rec.get("fields") or {}
        for c in e["rows"]["filterSet"]:
            v = fields.get(c["fieldId"])
            if c["operator"] == "is" and v != c.get("value"):
                return False
            if c["operator"] == "isAnyOf" and v not in (c.get("value") or []):
                return False
        return True


class Guest:
    """给助手接口用:懒连库(机器账号),把页面的读请求按策略转给表格。base_factory 便于测试换成内存版。"""

    def __init__(self, base_factory=None, log=print, outsider=None):
        self.log = log
        self._factory = base_factory or self._connect
        self._base: tstore.Base | None = None
        self._policy: Policy | None = None
        self._lock = threading.Lock()
        self._outsiders: dict[str, tuple[bool, float]] = {}
        self._ask_outsider = outsider  # 测试用:cookie → 是不是库外的人;None 则拿 cookie 问表格

    @staticmethod
    def sample_mode(raw: dict | None = None) -> bool:
        g = (raw if raw is not None else onto_mod.raw()).get("guest") or {}
        return str(g.get("data") or "") == "sample"

    @staticmethod
    def _connect() -> tstore.Base:
        url, token = tstore.credentials()
        real = tstore.Client(url, token)
        return tstore.Base(sample.SampleClient(real) if Guest.sample_mode() else real)

    def is_outsider(self, cookie: str | None) -> bool:
        """这个登录 cookie 的主人是不是库外的人(游客):拿 cookie 问表格 GET /api/base/{库},403 / 404 就是。结果按 cookie 记 10 分钟。
        问不到(表格没起、没带 cookie)当作游客,宁可少给。"""
        if self._ask_outsider is not None:
            return bool(self._ask_outsider(cookie))
        if not cookie:
            return True
        now = time.monotonic()
        hit = self._outsiders.get(cookie)
        if hit and now - hit[1] < 600:
            return hit[0]
        try:
            url = (os.environ.get("TEABLE_URL") or "").rstrip("/")
            r = httpx.get(f"{url}/api/base/{self.base().id}", headers={"Cookie": cookie}, timeout=10)
            out = r.status_code != 200
        except Exception:  # noqa: BLE001
            out = True
        if len(self._outsiders) > 500:
            self._outsiders.clear()
        self._outsiders[cookie] = (out, now)
        return out

    def base(self) -> tstore.Base:
        with self._lock:
            if self._base is None:
                self._base = self._factory()
                self._policy = Policy(self._base)
            return self._base

    def policy(self) -> Policy:
        self.base()
        assert self._policy is not None
        return self._policy

    def _forget(self) -> None:
        with self._lock:
            self._base, self._policy = None, None

    def _req(self, path: str, params: list[tuple[str, Any]]) -> Any:
        try:
            return self.base().api.req("GET", path, params=params)
        except tstore.TStoreError as e:
            if e.status is None or e.status >= 500 or e.status == 401:
                self._forget()  # 连库出错下次重连
            raise

    def _params(self, e: dict, qs: dict[str, list[str]]) -> list[tuple[str, Any]]:
        """只带白名单里的参数;filter / orderBy 先核字段;projection 去掉不放行的(表格本身总是要一个 fieldKeyType=id)。"""
        p = self.policy()
        out: list[tuple[str, Any]] = [("fieldKeyType", "id")]
        flt = None
        for k, vs in qs.items():
            if k not in ALLOWED_PARAMS or not vs:
                continue
            if k in ("filter", "orderBy"):
                try:
                    node = json.loads(vs[0]) if vs[0] else None
                except ValueError:
                    raise Denied("条件不是 JSON") from None
                p.check_fields(e, node)
                if k == "filter":
                    flt = node
                elif node:
                    out.append((k, json.dumps(node, ensure_ascii=False)))
            elif k == "projection":
                out += [("projection", v) for v in vs if v in e["allowed"]]
            elif k == "take":
                out.append(("take", str(min(int(vs[0] or TAKE), TAKE))))
            else:
                out.append((k, vs[0]))
        if e["rows"]:
            flt = e["rows"] if not flt else {"conjunction": "and", "filterSet": [flt, e["rows"]]}
        if flt:
            out.append(("filter", json.dumps(flt, ensure_ascii=False)))
        return out

    def records(self, tid: str, qs: dict[str, list[str]]) -> dict:
        e = self.policy().table(tid)
        page = self._req(f"/table/{tid}/record", self._params(e, qs)) or {}
        return {**page, "records": [self.policy().strip(e, r) for r in page.get("records") or []]}

    def count(self, tid: str, qs: dict[str, list[str]]) -> dict:
        e = self.policy().table(tid)
        params = [x for x in self._params(e, qs) if x[0] in ("filter", "viewId")]
        r = self._req(f"/table/{tid}/aggregation/row-count", params) or {}
        return {"rowCount": int(r.get("rowCount") or 0)}

    def record(self, tid: str, rid: str) -> dict | None:
        e = self.policy().table(tid)
        rec = self._req(f"/table/{tid}/record/{rid}", [("fieldKeyType", "id")]) or {}
        if not rec or not self.policy().matches_rows(e, rec):
            return None
        return self.policy().strip(e, rec)
