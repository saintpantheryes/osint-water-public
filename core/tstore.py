"""写入层:唯一和 Teable 打交道的地方(架构方案第 4 节"写入层")。

阶段 B 先做客户端——schema(core/schema.py)、迁移(scripts/migrate_to_teable.py)与能力核验(scripts/teable_probe.py)都用它:
分页读(单次最多 1000 行)、按条件查(filter、projection、viewId)、按条件计数(/aggregation/row-count)、批量建与批量改(每批 ≤ 200)、
上传附件、记录历史、评论、库级邀请链接、建库建表补字段补视图;429 与 5xx 重试。
阶段 D 加的(后台任务用,都在本文件下半部分):Base(按本体与 teable_map 找到库、表、字段编号,按属性名读写)、fill_empty(只填空)、
append_lines(列表只追加,键 = 出处线索号 + 规范化文本)、append_review(待核更新只追加,键 = 线索号 + 字段,认 ✗)。

令牌(架构方案第 2 节第 7 条):后台任务与命令行用机器账号的 TEABLE_TOKEN;建表与迁移用管理员的 TEABLE_ADMIN_TOKEN
(只在执行会话或操作者本机,不进服务器)。地址都是 TEABLE_URL。令牌从环境变量读,没有时在终端里提示输入(不回显、不写文件)。

只动新库:schema、迁移、能力核验都先过 require_new_base——库名不以「新版」结尾的一律拒绝(切换之前旧库照常运行,不能碰)。

接口:REST,`Authorization: Bearer <令牌>`,路径都在 <TEABLE_URL>/api 下;记录读写默认按字段编号(fieldKeyType=id),同事改了字段名不坏。
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TAKE = 1000  # 读记录单次最多(接口上限)
BATCH = 200  # 建、改记录每批
NEW_SUFFIX = "新版"  # 新库的名字以它结尾(「水务项目机会 新版」);旧库不碰
RETRY = (429, 502, 503, 504)


class TStoreError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def require_new_base(name: str) -> str:
    """只动新库:库名不以「新版」结尾的拒绝。"""
    if not str(name or "").strip().endswith(NEW_SUFFIX):
        raise TStoreError(f"只动新库:库名要以「{NEW_SUFFIX}」结尾,拒绝操作「{name}」")
    return name


def credentials(admin: bool = False, prompt: bool = True) -> tuple[str, str]:
    """(TEABLE_URL, 令牌)。admin 为真取 TEABLE_ADMIN_TOKEN(建表、迁移、能力核验),否则 TEABLE_TOKEN(机器账号)。
    环境变量里没有令牌、又是在终端里运行时,提示输入(不回显);都没有则报错。"""
    url = os.environ.get("TEABLE_URL", "").strip()
    var = "TEABLE_ADMIN_TOKEN" if admin else "TEABLE_TOKEN"
    token = os.environ.get(var, "").strip()
    if not url:
        raise TStoreError("没有 TEABLE_URL(表格地址,如 https://water.example.uk;在主机上可用 http://127.0.0.1:3000)")
    if not token and prompt and sys.stdin.isatty():
        token = getpass.getpass(f"{var}(输入时不显示):").strip()
    if not token:
        raise TStoreError(f"没有 {var}:放进环境变量,或在终端里运行时按提示输入")
    return url, token


def day(v: object, tz: str = "Asia/Shanghai") -> str | None:
    """Teable 的日期值(ISO 时间戳或 YYYY-MM-DD)→ 该时区的日期 YYYY-MM-DD。迁移与能力核验都用这一个(审查二-6)。"""
    if not v:
        return None
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).date().isoformat() if "T" in s else s[:10]
    except ValueError:
        return s[:10]


class Client:
    """Teable 接口客户端。transport:测试用的假接口(httpx.MockTransport);sleep:重试等待(测试里替换成不等)。"""

    def __init__(self, url: str, token: str, timeout: float = 60.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.url = url.rstrip("/")
        self.http = httpx.Client(base_url=self.url + "/api", timeout=timeout, transport=transport,
                                 headers={"Authorization": f"Bearer {token}"})
        self.sleep = sleep

    def req(self, method: str, path: str, *, params: Any = None, json_body: Any = None, files: Any = None) -> Any:
        for attempt in range(4):
            try:
                r = self.http.request(method, path, params=params, json=json_body, files=files)
            except httpx.TransportError as e:
                if attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                raise TStoreError(f"{method} {path}: 连不上({type(e).__name__})") from e
            if r.status_code in RETRY and attempt < 3:
                self.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise TStoreError(f"{method} {path} -> {r.status_code}: {r.text[:400]}", r.status_code)
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text
        raise TStoreError(f"{method} {path}: 重试后仍失败")

    def get_raw(self, path: str) -> httpx.Response:
        """不在 /api 下的地址(如 /docs-json、/health)。"""
        return self.http.get(self.url + path)

    # ---------- 空间、库、协作者 ----------
    def spaces(self) -> list[dict]:
        return self.req("GET", "/space") or []

    def bases(self, space_id: str) -> list[dict]:
        return self.req("GET", f"/space/{space_id}/base") or []

    def create_base(self, space_id: str, name: str) -> dict:
        return self.req("POST", "/base", json_body={"spaceId": space_id, "name": name})

    def find_base(self, name: str) -> dict | None:
        """令牌看得到的空间里按名字找库。"""
        spaces = self.spaces()
        if not spaces:
            raise TStoreError("令牌看不到任何空间:创建令牌时要勾选空间")
        return next((b for sp in spaces for b in self.bases(sp["id"]) if b.get("name") == name), None)

    def ensure_new_base(self, name: str, create: bool = True) -> dict | None:
        """找新库,没有时(create)建在令牌看得到的第一个空间里。名字必须以「新版」结尾。"""
        require_new_base(name)
        b = self.find_base(name)
        if b is None and create:
            b = self.create_base(self.spaces()[0]["id"], name)
        return b

    def collaborators(self, base_id: str) -> list[dict]:
        r = self.req("GET", f"/base/{base_id}/collaborators", params={"take": 1000}) or []
        return (r.get("collaborators") or []) if isinstance(r, dict) else r

    def base_invitation_link(self, base_id: str, role: str = "editor") -> dict:
        """库级邀请链接(机器账号用它加入新库,设为编辑者)。"""
        return self.req("POST", f"/base/{base_id}/invitation/link", json_body={"role": role}) or {}

    # ---------- 表、字段 ----------
    def tables(self, base_id: str) -> list[dict]:
        return self.req("GET", f"/base/{base_id}/table") or []

    def create_table(self, base_id: str, name: str, fields: list[dict], views: list[dict] | None = None,
                     description: str | None = None) -> dict:
        body: dict = {"name": name, "fields": fields, "views": views or [{"name": "全部", "type": "grid"}]}
        if description:
            body["description"] = description
        return self.req("POST", f"/base/{base_id}/table/", json_body=body)

    def delete_table(self, base_id: str, table_id: str) -> None:
        self.req("DELETE", f"/base/{base_id}/table/{table_id}")

    def fields(self, table_id: str) -> list[dict]:
        return self.req("GET", f"/table/{table_id}/field") or []

    def create_field(self, table_id: str, spec: dict) -> dict:
        return self.req("POST", f"/table/{table_id}/field", json_body=spec)

    def update_field(self, table_id: str, field_id: str, **props) -> None:
        """改字段的名字、描述(类型不变)。"""
        self.req("PATCH", f"/table/{table_id}/field/{field_id}", json_body=props)

    def delete_field(self, table_id: str, field_id: str) -> None:
        self.req("DELETE", f"/table/{table_id}/field/{field_id}")

    def convert_field(self, table_id: str, field_id: str, spec: dict) -> dict:
        """改字段的类型或设置(如主字段换成公式)。"""
        return self.req("PUT", f"/table/{table_id}/field/{field_id}/convert", json_body=spec)

    # ---------- 视图 ----------
    def views(self, table_id: str) -> list[dict]:
        return self.req("GET", f"/table/{table_id}/view") or []

    def create_view(self, table_id: str, spec: dict) -> dict:
        return self.req("POST", f"/table/{table_id}/view", json_body=spec)

    def set_view_filter(self, table_id: str, view_id: str, flt: dict) -> None:
        self.req("PUT", f"/table/{table_id}/view/{view_id}/filter", json_body={"filter": flt})

    def set_view_sort(self, table_id: str, view_id: str, sort_objs: list[dict]) -> None:
        self.req("PUT", f"/table/{table_id}/view/{view_id}/sort", json_body={"sort": {"sortObjs": sort_objs, "manualSort": False}})

    def set_view_group(self, table_id: str, view_id: str, group: list[dict]) -> None:
        self.req("PUT", f"/table/{table_id}/view/{view_id}/group", json_body={"group": group})

    # ---------- 记录 ----------
    def iter_records(self, table_id: str, *, filter: dict | None = None, projection: list[str] | None = None,
                     view_id: str | None = None, key: str = "id", take: int = TAKE) -> Iterator[dict]:
        """分页读记录(每页 ≤ 1000,按 skip 翻页,直到一页不满)。key:返回的字段按编号("id")还是名字("name")。"""
        skip = 0
        while True:
            params: list[tuple[str, Any]] = [("fieldKeyType", key), ("take", min(take, TAKE)), ("skip", skip)]
            if filter:
                params.append(("filter", json.dumps(filter, ensure_ascii=False)))
            if view_id:
                params.append(("viewId", view_id))
            params += [("projection", p) for p in projection or []]
            page = self.req("GET", f"/table/{table_id}/record", params=params) or {}
            recs = page.get("records") or []
            yield from recs
            if len(recs) < min(take, TAKE):
                return
            skip += len(recs)

    def records(self, table_id: str, **kw) -> list[dict]:
        return list(self.iter_records(table_id, **kw))

    def record(self, table_id: str, record_id: str, key: str = "id") -> dict:
        """读一条记录(写前读最新值用)。"""
        return self.req("GET", f"/table/{table_id}/record/{record_id}", params=[("fieldKeyType", key)]) or {}

    def count(self, table_id: str, filter: dict | None = None, view_id: str | None = None) -> int:
        """按条件计数(不下载记录)。"""
        params: list[tuple[str, Any]] = []
        if filter:
            params.append(("filter", json.dumps(filter, ensure_ascii=False)))
        if view_id:
            params.append(("viewId", view_id))
        r = self.req("GET", f"/table/{table_id}/aggregation/row-count", params=params) or {}
        return int(r.get("rowCount") or 0)

    def create_records(self, table_id: str, rows: list[dict], key: str = "id", typecast: bool = True) -> list[dict]:
        """批量建(每批 ≤ 200)→ 新记录(与 rows 同序)。"""
        out: list[dict] = []
        for i in range(0, len(rows), BATCH):
            body = {"fieldKeyType": key, "typecast": typecast, "records": [{"fields": f} for f in rows[i:i + BATCH]]}
            out.extend((self.req("POST", f"/table/{table_id}/record", json_body=body) or {}).get("records") or [])
        return out

    def update_records(self, table_id: str, rows: list[tuple[str, dict]], key: str = "id", typecast: bool = True) -> None:
        """批量改(每批 ≤ 200):[(记录编号, {字段: 值})]。"""
        for i in range(0, len(rows), BATCH):
            body = {"fieldKeyType": key, "typecast": typecast, "records": [{"id": rid, "fields": f} for rid, f in rows[i:i + BATCH]]}
            self.req("PATCH", f"/table/{table_id}/record", json_body=body)

    def upload_attachment(self, table_id: str, record_id: str, field_id: str, name: str, data: bytes,
                          mime: str = "application/octet-stream") -> dict:
        """把一个文件传到记录的附件字段(追加)。"""
        return self.req("POST", f"/table/{table_id}/record/{record_id}/{field_id}/uploadAttachment",
                        files={"file": (name, data, mime)})

    def upload_file(self, table_id: str, record_id: str, field_id: str, path: Path) -> dict:
        p = Path(path)
        return self.upload_attachment(table_id, record_id, field_id, p.name, p.read_bytes())

    def record_history(self, table_id: str, record_id: str, field_ids: list[str] | None = None) -> dict:
        """一条记录的修改历史(新的在前):{historyList: [...], userMap, nextCursor}。"""
        params = [("fieldIds", f) for f in field_ids or []]
        return self.req("GET", f"/table/{table_id}/record/{record_id}/history", params=params) or {}

    def me(self) -> dict:
        """令牌对应的账号:{id, name, email}(后台任务用它认"机器账号",A18 只记人改的阶段)。"""
        return self.req("GET", "/auth/user") or {}

    def add_comment(self, table_id: str, record_id: str, text: str, mention_user_id: str | None = None) -> Any:
        """在记录下发一条评论;mention_user_id:@ 谁(站内通知)。"""
        children: list[dict] = [{"type": "span", "value": text}]
        if mention_user_id:
            children.append({"type": "mention", "value": mention_user_id})
        return self.req("POST", f"/comment/{table_id}/{record_id}/create", json_body={"content": [{"type": "p", "children": children}]})


# ====================================================================== 阶段 D:后台任务的写入层

def _norm(s: object) -> str:
    return " ".join(str(s or "").split()).casefold()


def is_empty(v: object) -> bool:
    """Teable 里的"空值":None、空串、空列表、未勾选。机器只往这样的字段里写(R2)。"""
    return v is None or v == "" or v == [] or v is False


def link_ids(v: object) -> list[str]:
    """关联、成员字段的值 → 记录(账号)编号列表;单值与多值都认。"""
    if not v:
        return []
    xs = v if isinstance(v, list) else [v]
    return [x["id"] if isinstance(x, dict) else str(x) for x in xs if x]


def link_id(v: object) -> str | None:
    ids = link_ids(v)
    return ids[0] if ids else None


def fill_empty(have: dict, want: dict) -> tuple[dict, list[tuple[str, Any, Any]]]:
    """只填空(R2):have 为记录现有的 {字段: 值},want 为机器想写的 {字段: 值}。
    → (要写的 {字段: 值}:只含现值为空的;不同的 [(字段, 新值, 现值)]:交给调用方写进「待核更新」)。
    想写的值本身为空的不算;现值与新值一样(忽略空白与大小写)的也不算不同。"""
    changes, diffs = {}, []
    for k, v in want.items():
        if is_empty(v):
            continue
        cur = have.get(k)
        if is_empty(cur):
            changes[k] = v
        elif _norm(cur) != _norm(v) and not (isinstance(cur, list) and isinstance(v, list) and set(map(str, cur)) == set(map(str, v))):
            diffs.append((k, v, cur))
    return changes, diffs


_REF = re.compile(r"\[([0-9a-f]{8})\]")


def line_key(line: str) -> tuple[str, str]:
    """列表字段一行的键:(出处线索号,去掉线索号后的规范化文本)。没有线索号的键里线索号为空串。"""
    refs = sorted(set(_REF.findall(line)))
    return ("+".join(refs), _norm(_REF.sub("", line)))


def append_lines(current: str | None, new: list[str]) -> str | None:
    """列表只追加(R2、本体 v1.2):列表类字段存成长文本、一行一项;只加键(出处线索号 + 规范化文本)不在的行,
    已有的行不改不删。→ 新的全文;没有要加的返回 None(调用方不写)。"""
    lines = [ln for ln in str(current or "").splitlines() if ln.strip()]
    keys = {line_key(ln) for ln in lines}
    added = []
    for ln in new:
        ln = " ".join(str(ln or "").split())
        if not ln:
            continue
        k = line_key(ln)
        if k in keys:
            continue
        keys.add(k)
        added.append(ln)
    return "\n".join(lines + added) if added else None


_REVIEW = re.compile(r"^\s*[✗x×]?\s*\[([0-9a-f]{8})\]\s*([^:：]+?)\s*[:：]")


def review_line(lead_no: str, field_name: str, new: object, current: object) -> str:
    """待核更新的一行:「[线索号] 字段:新值(现值:…)」。"""
    def one(v: object) -> str:
        v = "、".join(map(str, v)) if isinstance(v, list) else str(v)
        return " ".join(v.split())[:300]
    return f"[{lead_no}] {field_name}:{one(new)}(现值:{one(current)})"


def append_review(current: str | None, proposals: list[tuple[str, str, Any, Any]]) -> str | None:
    """待核更新只追加:proposals = [(线索号, 字段, 新值, 现值)];键 = 线索号 + 字段,表里已有同键的行(含人驳回、行首 ✗ 的)不再提。
    → 新的全文;没有要加的返回 None。人采纳后改事实、删那一行。"""
    lines = [ln for ln in str(current or "").splitlines() if ln.strip()]
    keys = set()
    for ln in lines:
        m = _REVIEW.match(ln)
        if m:
            keys.add((m[1], _norm(m[2])))
    added = []
    for no, fname, new, cur in proposals:
        k = (no, _norm(fname))
        if k in keys:
            continue
        keys.add(k)
        added.append(review_line(no, fname, new, cur))
    return "\n".join(lines + added) if added else None


class Base:
    """新库的表与字段:按 teable_map.yaml 的表名、本体的属性名找到编号,后台任务按属性编号(如 lead.title)读写,同事改了字段名不坏。
    值的形状:写关联给记录编号(str 或 list[str]),写成员给账号编号;读回来的关联、成员是 {id, title}(或其列表)。"""

    def __init__(self, api: Client, name: str | None = None, onto=None, tmap: dict | None = None):
        from core import ontology as onto_mod
        self.api = api
        self.onto = onto or onto_mod.load()
        self.tmap = tmap or onto_mod.load_map()
        self.name = require_new_base(name or self.tmap["base"])
        self.tz = self.tmap.get("timezone") or "Asia/Shanghai"
        b = api.find_base(self.name)
        if b is None:
            raise TStoreError(f"没有「{self.name}」库:先跑 python -m core.schema")
        self.id = b["id"]
        tables = {t["name"]: t for t in api.tables(self.id)}
        self.t: dict[str, str] = {}
        self.f: dict[str, dict[str, dict]] = {}
        self.missing: set[str] = set()  # teable_map 里 optional: true、库里还没建的表(新加的对象,等跑 schema):后台跳过它们
        for oid, cfg in self.tmap["tables"].items():
            if cfg["table"] not in tables:
                if cfg.get("optional"):
                    self.missing.add(oid)
                    continue
                raise TStoreError(f"新库里没有「{cfg['table']}」表:先跑 python -m core.schema")
            self.t[oid] = tables[cfg["table"]]["id"]
            self.f[oid] = {x["name"]: x for x in api.fields(self.t[oid])}
        self._me: dict | None = None

    # ---------- 字段 ----------
    def field(self, oid: str, prop: str) -> dict:
        """属性编号(title)或字段名(标题)→ 字段。"""
        obj = self.onto.objects[oid]
        key_name = ((self.tmap["tables"].get(oid) or {}).get("key") or {}).get("name")
        if prop == "key" and key_name:
            name = key_name
        else:
            name = obj.props_by_id[prop].name if prop in obj.props_by_id else prop
        if oid in self.missing:
            raise TStoreError(f"新库里没有「{self.tmap['tables'][oid]['table']}」表:先跑 python -m core.schema")
        try:
            return self.f[oid][name]
        except KeyError:
            raise TStoreError(f"「{self.tmap['tables'][oid]['table']}」表里没有字段「{name}」:先跑 python -m core.schema 补字段") from None

    def fid(self, oid: str, prop: str) -> str:
        return self.field(oid, prop)["id"]

    def has(self, oid: str, prop: str) -> bool:
        try:
            self.field(oid, prop)
            return True
        except TStoreError:
            return False

    def get(self, oid: str, rec: dict, prop: str, default=None):
        return (rec.get("fields") or {}).get(self.fid(oid, prop), default)

    def me(self) -> dict:
        if self._me is None:
            self._me = self.api.me()
        return self._me

    # ---------- 读 ----------
    def cond(self, oid: str, prop: str, op: str, value: Any = None) -> dict:
        c: dict = {"fieldId": self.fid(oid, prop), "operator": op}
        if value is not None:
            c["value"] = value
        return c

    @staticmethod
    def filter(*conds: dict, conjunction: str = "and") -> dict:
        return {"conjunction": conjunction, "filterSet": list(conds)}

    def record(self, oid: str, rid: str) -> dict:
        return self.api.record(self.t[oid], rid)

    def rows(self, oid: str, *, filter: dict | None = None, props: list[str] | None = None, view_id: str | None = None) -> list[dict]:
        proj = [self.fid(oid, p) for p in props] if props else None
        return self.api.records(self.t[oid], filter=filter, projection=proj, view_id=view_id)

    # ---------- 写 ----------
    def _vals(self, oid: str, values: dict) -> dict:
        out = {}
        for prop, v in values.items():
            f = self.field(oid, prop)
            if v is None or v == "" or v == []:
                continue
            if f["type"] == "link":
                ids = v if isinstance(v, list) else [v]
                ids = [x["id"] if isinstance(x, dict) else x for x in ids]
                multi = (f.get("options") or {}).get("relationship") in ("manyMany", "oneMany")
                v = [{"id": x} for x in ids] if multi else {"id": ids[0]}
            elif f["type"] == "user":
                ids = v if isinstance(v, list) else [v]
                ids = [x["id"] if isinstance(x, dict) else x for x in ids]
                v = [{"id": x} for x in ids] if (f.get("options") or {}).get("isMultiple") else {"id": ids[0]}
            elif f["type"] == "singleLineText" and isinstance(v, str):
                v = " ".join(v.split())
            elif f["type"] == "checkbox":
                v = bool(v)
            elif f["type"] == "number":
                v = float(v)
            out[f["id"]] = v
        return out

    def create(self, oid: str, rows: list[dict]) -> list[dict]:
        """批量新建:rows = [{属性: 值}]。"""
        if not rows:
            return []
        return self.api.create_records(self.t[oid], [self._vals(oid, r) for r in rows])

    def update(self, oid: str, rid: str, values: dict) -> None:
        vals = self._vals(oid, values)
        if vals:
            self.api.update_records(self.t[oid], [(rid, vals)])

    def update_many(self, oid: str, rows: list[tuple[str, dict]]) -> None:
        rows2 = [(rid, self._vals(oid, v)) for rid, v in rows]
        rows2 = [(rid, v) for rid, v in rows2 if v]
        if rows2:
            self.api.update_records(self.t[oid], rows2)

    def fill(self, oid: str, rec: dict, values: dict) -> tuple[dict, list[tuple[str, Any, Any]]]:
        """只填空地改一条记录(R2):values = {属性: 值}。→ (写了的 {属性: 值}, 不同的 [(字段名, 新值, 现值)])。"""
        have = {p: self.get(oid, rec, p) for p in values}
        changes, diffs = fill_empty(have, values)
        if changes:
            self.update(oid, rec["id"], changes)
        return changes, [(self.field(oid, p)["name"], new, cur) for p, new, cur in diffs]
