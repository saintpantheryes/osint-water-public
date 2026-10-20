"""测试用的内存版 Teable(HTTP 层,接到 httpx.MockTransport 上),只实现 core.tstore 用到的接口,不联网。

照真实接口的几条要紧行为做:令牌分管理员(所有者)与机器账号(编辑者),编辑者不能建库、建表、建字段;建关联字段会在对方表里
自动生成反向字段(名字是本表名,重名加序号),除非 isOneWay;公式、汇总、查找、最后修改人/时间是计算字段,不能写;公式与汇总
引用的字段编号必须存在;读记录单次最多 1000 行;空值不出现在 fields 里;日期按 Asia/Shanghai 当天零点存成 UTC 时间
(读回来是前一天 16:00Z);最后修改人/时间带 trackedFieldIds 时只在这些字段变了才变;改记录留修改历史。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx

SH = ZoneInfo("Asia/Shanghai")
COMPUTED = {"formula", "rollup", "lastModifiedBy", "lastModifiedTime", "createdTime", "createdBy", "autoNumber", "count"}
TYPES = {"singleLineText", "longText", "number", "checkbox", "date", "singleSelect", "multipleSelect", "user", "attachment",
         "link", *COMPUTED}
SYMMETRIC = {"manyOne": "oneMany", "oneMany": "manyOne", "manyMany": "manyMany", "oneOne": "oneOne"}
MULTI_LINK = {"manyMany", "oneMany"}


class HTTPError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


class FakeTeable:
    def __init__(self):
        # outsider-token:登录了、但不是库协作者的账号(游客演示帐号移出库后的样子):库、表、记录一律 403,只有 /auth/* 能用
        # partner-token:同样是库外账号,但邮箱在伙伴名单里(@bewg.example)→ 伙伴档,读真实库的公开部分
        self.tokens = {"admin-token": ("usrADM", "owner"), "bot-token": ("usrBOT", "editor"), "guest-token": ("usrGST", "viewer"), "outsider-token": ("usrOUT", "none"), "partner-token": ("usrPTN", "none")}
        self.users = {"usrADM": {"id": "usrADM", "name": "Ada", "email": "ada@example.com"},
                      "usrBOT": {"id": "usrBOT", "name": "机器账号", "email": "bot@example.com"},
                      "usrGST": {"id": "usrGST", "name": "游客", "email": "guest@example.com"},
                      "usrOUT": {"id": "usrOUT", "name": "游客", "email": "outsider@example.com"},
                      "usrPTN": {"id": "usrPTN", "name": "北控水务-陈", "email": "Chen@bewg.example"},
                      "usrZS": {"id": "usrZS", "name": "张三", "email": "zs@example.com"}}
        self.spaces = [{"id": "spc1", "name": "水务", "role": "owner"}]
        self.bases: dict[str, dict] = {"bseOLD": {"id": "bseOLD", "name": "水务项目机会", "spaceId": "spc1"}}
        self.tables: dict[str, dict] = {}
        self.fields: dict[str, list[dict]] = {}
        self.views: dict[str, list[dict]] = {}
        self.records: dict[str, list[dict]] = {}
        self.history: dict[str, list[dict]] = {}
        self.comments: list[dict] = []
        self.invitations: list[dict] = []
        self.role_changes: list[tuple[str, str]] = []  # 「账号管理」页改的角色
        self.removed: list[str] = []  # 「账号管理」页移出的协作者
        self.calls: list[tuple[str, str]] = []
        self._n = 0
        self._clock = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)
        self.fail_next: list[int] = []  # 下几次请求先返回这些状态码(测试重试)

    # ---------- 工具 ----------
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def _now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def table_by_name(self, name: str, base_id: str | None = None) -> dict:
        return next(t for t in self.tables.values() if t["name"] == name and (base_id is None or t["baseId"] == base_id))

    def field(self, table_id: str, name_or_id: str) -> dict:
        return next(f for f in self.fields[table_id] if name_or_id in (f["id"], f["name"]))

    def rows(self, table_name: str, base_name: str = "水务项目机会 新版") -> list[dict]:
        """一张表的记录,字段按名字(测试断言用)。"""
        base = next(b for b in self.bases.values() if b["name"] == base_name)
        t = self.table_by_name(table_name, base["id"])
        return [{"id": r["id"], **self._out(t["id"], r, "name")["fields"]} for r in self.records[t["id"]]]

    # ---------- 入口 ----------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        try:
            if self.fail_next:
                code = self.fail_next.pop(0)
                return httpx.Response(code, json={"message": "临时失败"})
            if path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if path == "/docs-json":
                return httpx.Response(200, json=self.openapi())
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
                raise HTTPError(401, "unauthorized")
            self.user, self.role = self.tokens[auth[7:]]
            params = parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query),
                               keep_blank_values=True)
            body = None
            ctype = request.headers.get("content-type", "")
            if request.content and "json" in ctype:
                body = json.loads(request.content)
            out = self.route(method, path.removeprefix("/api"), params, body, request)
            return httpx.Response(200, json=out) if out is not None else httpx.Response(200)
        except HTTPError as e:
            return httpx.Response(e.status, json={"message": e.msg})

    def route(self, method, path, params, body, request):  # noqa: C901 — 路由表
        m = lambda pat: re.fullmatch(pat, path)  # noqa: E731
        if method == "GET" and path == "/auth/user":
            return dict(self.users[self.user])
        if path == "/auth/signout" and method == "POST":
            return None
        if self.role == "none":  # 不是协作者:看不到空间、库、表
            raise HTTPError(403, "no permission")
        if method == "GET" and path == "/space":
            return self.spaces
        if (r := m(r"/space/([^/]+)/base")) and method == "GET":
            return [b for b in self.bases.values() if b["spaceId"] == r.group(1)]
        if path == "/base" and method == "POST":
            self._owner()
            b = {"id": self._id("bse"), "name": body["name"], "spaceId": body["spaceId"]}
            self.bases[b["id"]] = b
            return b
        if (r := m(r"/base/([^/]+)")) and method == "GET":  # 真 Teable 带 role:登录人在这个库里的角色
            b = self.bases.get(r.group(1))
            if not b:
                raise HTTPError(404, "no base")
            return dict(b, role=self.role)
        if (r := m(r"/base/([^/]+)/collaborators")) and method == "GET":  # 库的协作者(库外的 usrOUT / usrPTN 不在)
            roles = {uid: role for uid, role in self.tokens.values()}
            return {"collaborators": [{"type": "user", "userId": u["id"], "userName": u["name"], "email": u["email"], "role": roles.get(u["id"], "editor"), "lastSignTime": "2026-10-19T08:00:00.000Z"}
                                      for u in self.users.values() if roles.get(u["id"], "editor") != "none"]}
        if (r := m(r"/base/([^/]+)/collaborators")) and method == "PATCH":  # 改角色:所有者才行
            self._owner()
            uid, role = body.get("principalId"), body.get("role")
            if uid not in self.users or role not in ("creator", "editor", "commenter", "viewer"):
                raise HTTPError(400, "bad collaborator")
            for tok, (u, _) in list(self.tokens.items()):
                if u == uid:
                    self.tokens[tok] = (u, role)
            self.role_changes.append((uid, role))
            return None
        if (r := m(r"/base/([^/]+)/collaborators")) and method == "DELETE":
            self._owner()
            uid = dict(params).get("principalId")
            if uid not in self.users:
                raise HTTPError(400, "bad collaborator")
            for tok, (u, _) in list(self.tokens.items()):
                if u == uid:
                    self.tokens[tok] = (u, "none")
            self.removed.append(uid)
            return None
        if (r := m(r"/base/([^/]+)/invitation/link")) and method == "GET":
            self._owner()
            return [i for i in self.invitations if i["baseId"] == r.group(1)]
        if (r := m(r"/base/([^/]+)/invitation/link")) and method == "POST":
            self._owner()
            inv = {"invitationId": self._id("inv"), "inviteCode": "code-" + str(self._n), "invitationCode": "code-" + str(self._n), "role": body.get("role"), "baseId": r.group(1),
                   "inviteUrl": f"https://water.example/invite/link?invitationId=inv{self._n}&invitationCode=code-{self._n}"}
            self.invitations.append(inv)
            return inv
        if (r := m(r"/base/([^/]+)/invitation/link/([^/]+)")) and method == "DELETE":
            self._owner()
            self.invitations = [i for i in self.invitations if i["invitationId"] != r.group(2)]
            return None
        if (r := m(r"/base/([^/]+)/table/?")):
            if method == "GET":
                return [t for t in self.tables.values() if t["baseId"] == r.group(1)]
            if method == "POST":
                return self.create_table(r.group(1), body)
        if (r := m(r"/base/([^/]+)/table/([^/]+)")) and method == "DELETE":
            self._owner()
            self.tables.pop(r.group(2))
            return None
        if (r := m(r"/table/([^/]+)/field")):
            if method == "GET":
                return self.fields[r.group(1)]
            if method == "POST":
                self._owner()
                return self.create_field(r.group(1), body)
        if (r := m(r"/table/([^/]+)/field/([^/]+)")) and method == "DELETE":
            self._owner()
            self.fields[r.group(1)] = [x for x in self.fields[r.group(1)] if x["id"] != r.group(2)]
            return None
        if (r := m(r"/table/([^/]+)/field/([^/]+)")) and method == "PATCH":
            self._owner()
            f = self.field(r.group(1), r.group(2))
            if "name" in body and any(x["name"] == body["name"] and x is not f for x in self.fields[r.group(1)]):
                raise HTTPError(400, f"字段名重复:{body['name']}")
            f.update({k: v for k, v in body.items() if k in ("name", "description")})
            return None
        if (r := m(r"/table/([^/]+)/field/([^/]+)/convert")) and method == "PUT":
            self._owner()
            f = self.field(r.group(1), r.group(2))
            self._check_spec(r.group(1), body)
            f.update({k: v for k, v in body.items() if k in ("type", "options", "lookupOptions", "isLookup", "name", "description")})
            return f
        if (r := m(r"/table/([^/]+)/view")):
            if method == "GET":
                return self.views[r.group(1)]
            if method == "POST":
                self._owner()
                v = {"id": self._id("viw"), "name": body["name"], "type": body.get("type", "grid"), "options": body.get("options") or {}}
                self._check_view(r.group(1), v)
                self.views[r.group(1)].append(v)
                return v
        if (r := m(r"/table/([^/]+)/view/([^/]+)/(filter|sort|group)")) and method == "PUT":
            self._owner()
            v = next(x for x in self.views[r.group(1)] if x["id"] == r.group(2))
            v[r.group(3)] = body[r.group(3)]
            return None
        if (r := m(r"/table/([^/]+)/permission")) and method == "GET":  # 真 Teable 的形状:按 table/view/record/field 分组的「动作|布尔」
            w = self.role != "viewer"
            return {"table": {"table|read": True, "table|update": w}, "view": {"view|read": True, "view|update": w},
                    "record": {"record|read": True, "record|create": w, "record|update": w, "record|delete": w, "record|comment": w},
                    "field": {"field|read": True, "field|create": self.role == "owner"}}
        if (r := m(r"/table/([^/]+)/record")):
            if method == "GET":
                return self.list_records(r.group(1), params)
            if method == "POST":
                self._writer()
                return {"records": self.write(r.group(1), body, create=True)}
            if method == "PATCH":
                self._writer()
                return {"records": self.write(r.group(1), body, create=False)}
        if (r := m(r"/table/([^/]+)/aggregation/row-count")) and method == "GET":
            return {"rowCount": len(self._select(r.group(1), params))}
        if (r := m(r"/table/([^/]+)/record/([^/]+)/([^/]+)/uploadAttachment")) and method == "POST":
            self._writer()
            return self.upload(r.group(1), r.group(2), r.group(3), request)
        if (r := m(r"/table/([^/]+)/record/([^/]+)")) and method == "GET":
            rec = next((x for x in self.records.get(r.group(1), []) if x["id"] == r.group(2)), None)
            if rec is None:
                raise HTTPError(404, "record not found")
            return {"id": rec["id"], **self._out(r.group(1), rec, dict(params).get("fieldKeyType", "name"))}
        if (r := m(r"/table/([^/]+)/record/([^/]+)/history")) and method == "GET":
            items = list(reversed(self.history.get(r.group(2), [])))
            return {"historyList": items, "userMap": {h["createdBy"]: self.users[h["createdBy"]] for h in items}, "nextCursor": None}
        if (r := m(r"/comment/([^/]+)/([^/]+)/create")) and method == "POST":
            c = {"id": self._id("com"), "tableId": r.group(1), "recordId": r.group(2), "content": body["content"], "by": self.user}
            self.comments.append(c)
            return c
        raise HTTPError(404, f"no route {method} {path}")

    def _owner(self):
        if self.role != "owner":
            raise HTTPError(403, "编辑者不能改表结构")

    def _writer(self):
        if self.role == "viewer":
            raise HTTPError(403, "查看者不能写记录")

    # ---------- 表、字段、视图 ----------
    def create_table(self, base_id: str, body: dict) -> dict:
        self._owner()
        if any(t["name"] == body["name"] and t["baseId"] == base_id for t in self.tables.values()):
            raise HTTPError(400, f"表名重复:{body['name']}")
        t = {"id": self._id("tbl"), "name": body["name"], "baseId": base_id, "description": body.get("description")}
        self.tables[t["id"]], self.fields[t["id"]], self.records[t["id"]], self.views[t["id"]] = t, [], [], []
        for i, spec in enumerate(body.get("fields") or []):
            f = self.create_field(t["id"], spec)
            f["isPrimary"] = i == 0
        for v in body.get("views") or [{"name": "Grid view", "type": "grid"}]:
            self.views[t["id"]].append({"id": self._id("viw"), "name": v["name"], "type": v.get("type", "grid"), "options": v.get("options") or {}})
        return {**t, "fields": self.fields[t["id"]], "views": self.views[t["id"]]}

    def _check_spec(self, table_id: str, spec: dict) -> None:
        if spec.get("type") not in TYPES:
            raise HTTPError(400, f"不认识的字段类型 {spec.get('type')}")
        ids = {f["id"] for fs in self.fields.values() for f in fs}
        opts = spec.get("options") or {}
        if spec["type"] == "formula":
            for ref in re.findall(r"\{([^}]+)\}", opts.get("expression") or ""):
                if ref not in ids:
                    raise HTTPError(400, f"公式引用的字段不存在:{ref}")
        if spec["type"] in ("lastModifiedBy", "lastModifiedTime"):
            for ref in opts.get("trackedFieldIds") or []:
                if ref not in {f["id"] for f in self.fields[table_id]}:
                    raise HTTPError(400, f"trackedFieldIds 的字段不存在:{ref}")
        lo = spec.get("lookupOptions")
        if spec["type"] == "rollup" or spec.get("isLookup"):
            if not lo or lo.get("foreignTableId") not in self.tables:
                raise HTTPError(400, "lookupOptions.foreignTableId 不存在")
            if spec.get("isConditionalLookup"):  # 条件查找:不经关联字段,靠 filter 关联,filter 必填
                if not lo.get("filter"):
                    raise HTTPError(400, "lookupOptions.filter 必填(条件查找)")
            elif lo.get("linkFieldId") not in {f["id"] for f in self.fields[table_id]}:
                raise HTTPError(400, "lookupOptions.linkFieldId 不在本表")
            if lo.get("lookupFieldId") not in {f["id"] for f in self.fields[lo["foreignTableId"]]}:
                raise HTTPError(400, "lookupOptions.lookupFieldId 不在对方表")
        if spec["type"] == "link" and opts.get("foreignTableId") not in self.tables:
            raise HTTPError(400, "关联的表不存在")

    def create_field(self, table_id: str, spec: dict) -> dict:
        if any(f["name"] == spec.get("name") for f in self.fields[table_id]):
            raise HTTPError(400, f"字段名重复:{spec.get('name')}")
        self._check_spec(table_id, spec)
        f = {"id": self._id("fld"), "isPrimary": False, **json.loads(json.dumps(spec))}
        f["isComputed"] = f["type"] in COMPUTED or bool(f.get("isLookup"))
        if f.get("lookupOptions"):  # 同 Teable 的请求模式:经关联的查找、汇总只收这四项(sort、limit 静默丢掉);条件查找才收 sort、limit
            keep = (("foreignTableId", "lookupFieldId", "filter", "sort", "limit") if f.get("isConditionalLookup")
                    else ("foreignTableId", "linkFieldId", "lookupFieldId", "filter"))
            f["lookupOptions"] = {k: v for k, v in f["lookupOptions"].items() if k in keep}
        if f["type"] == "link":
            o = f["options"]
            ft = o["foreignTableId"]
            o["lookupFieldId"] = next(x["id"] for x in self.fields[ft] if x.get("isPrimary")) if self.fields[ft] else None
            if not o.get("isOneWay"):
                name, n = self.tables[table_id]["name"], 1
                while any(x["name"] == name for x in self.fields[ft]):
                    n += 1
                    name = f"{self.tables[table_id]['name']} {n}"
                sym = {"id": self._id("fld"), "name": name, "type": "link", "isPrimary": False, "isComputed": False,
                       "options": {"relationship": SYMMETRIC[o["relationship"]], "foreignTableId": table_id, "symmetricFieldId": f["id"],
                                   "lookupFieldId": next((x["id"] for x in self.fields[table_id] if x.get("isPrimary")), None)}}
                o["symmetricFieldId"] = sym["id"]
                self.fields[ft].append(sym)
        self.fields[table_id].append(f)
        return f

    def _check_view(self, table_id: str, v: dict) -> None:
        ids = {f["id"] for f in self.fields[table_id]}
        for k, ref in (v.get("options") or {}).items():
            if k.endswith("FieldId") and ref not in ids:
                raise HTTPError(400, f"视图选项 {k} 的字段不存在")

    # ---------- 记录 ----------
    def _key_field(self, table_id: str, key: str, fkt: str) -> dict:
        try:
            return next(f for f in self.fields[table_id] if (f["id"] if fkt == "id" else f["name"]) == key)
        except StopIteration:
            raise HTTPError(400, f"字段不存在:{key}") from None

    def _date(self, v) -> str:
        s = str(v)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = datetime.combine(date.fromisoformat(s), datetime.min.time(), SH)  # 按上海当天零点
            return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _cast(self, table_id: str, f: dict, v, typecast: bool):
        t = f["type"]
        if v is None or v == "" or v == []:
            return None
        if t in ("singleLineText", "longText"):
            return str(v)
        if t == "number":
            return float(v) if typecast else v
        if t == "checkbox":
            return bool(v) or None
        if t == "date":
            return self._date(v)
        if t in ("singleSelect", "multipleSelect"):
            vals = v if isinstance(v, list) else [v]
            choices = f.setdefault("options", {}).setdefault("choices", [])
            for x in vals:
                if not any(c["name"] == x for c in choices):
                    if not typecast or (f.get("options") or {}).get("preventAutoNewOptions"):
                        raise HTTPError(400, f"选项不存在:{f['name']}={x}")
                    choices.append({"name": x})
            return vals if t == "multipleSelect" else vals[0]
        if t == "user":
            vals = v if isinstance(v, list) else [v]
            out = []
            for x in vals:
                uid = x.get("id") if isinstance(x, dict) else x
                u = self.users.get(uid) or next((u for u in self.users.values() if uid in (u["email"], u["name"])), None)
                if not u:
                    raise HTTPError(400, f"用户不存在:{uid}")
                out.append({"id": u["id"], "title": u["name"], "email": u["email"]})
            return out if (f.get("options") or {}).get("isMultiple") else out[0]
        if t == "link":
            vals = v if isinstance(v, list) else [v]
            ft = f["options"]["foreignTableId"]
            have = {r["id"] for r in self.records[ft]}
            ids = []
            for x in vals:
                rid = x.get("id") if isinstance(x, dict) else x
                if rid not in have:
                    raise HTTPError(400, f"关联的记录不存在:{rid}")
                ids.append(rid)
            if f["options"]["relationship"] not in MULTI_LINK and len(ids) > 1:
                raise HTTPError(400, "单条关联只能填一个")
            return ids
        if t == "attachment":
            return v
        raise HTTPError(400, f"不能写的字段类型 {t}")

    def write(self, table_id: str, body: dict, create: bool) -> list[dict]:
        fkt, typecast = body.get("fieldKeyType", "name"), body.get("typecast", False)
        out = []
        for item in body["records"]:
            vals = {}
            for k, v in (item.get("fields") or {}).items():
                f = self._key_field(table_id, k, fkt)
                if f["isComputed"]:
                    raise HTTPError(400, f"字段是计算出来的,不能写:{f['name']}")
                vals[f["id"]] = self._cast(table_id, f, v, typecast)
            if create:
                for fd in self.fields[table_id]:  # 缺省值:建记录时没填的
                    dv = (fd.get("options") or {}).get("defaultValue")
                    if dv is not None and fd["id"] not in vals and not fd["isComputed"]:
                        vals[fd["id"]] = self._cast(table_id, fd, dv, True)
                now = self._now()
                rec = {"id": self._id("rec"), "fields": {}, "createdTime": now, "createdBy": self.user,
                       "lastModifiedTime": now, "lastModifiedBy": self.user}
                self.records[table_id].append(rec)
            else:
                rec = next((r for r in self.records[table_id] if r["id"] == item["id"]), None)
                if rec is None:
                    raise HTTPError(404, f"记录不存在:{item['id']}")
            self._apply(table_id, rec, vals, create)
            out.append({"id": rec["id"], **self._out(table_id, rec, fkt)})
        return out

    def _apply(self, table_id: str, rec: dict, vals: dict, create: bool) -> None:
        now = self._now()
        changed = [fid for fid, v in vals.items() if rec["fields"].get(fid) != v]
        for fid in changed:
            f = self.field(table_id, fid)
            if not create:
                self.history.setdefault(rec["id"], []).append({"fieldId": fid, "createdBy": self.user, "createdTime": now,
                                                              "before": rec["fields"].get(fid), "after": vals[fid]})
            if f["type"] == "link":
                self._relink(table_id, f, rec["id"], rec["fields"].get(fid) or [], vals[fid] or [])
            if vals[fid] is None:
                rec["fields"].pop(fid, None)
            else:
                rec["fields"][fid] = vals[fid]
        if changed:
            rec["lastModifiedTime"], rec["lastModifiedBy"] = now, self.user
            for f in self.fields[table_id]:
                if f["type"] in ("lastModifiedBy", "lastModifiedTime"):
                    tracked = (f.get("options") or {}).get("trackedFieldIds")
                    if not tracked or set(tracked) & set(changed):
                        rec.setdefault("_track", {})[f["id"]] = self.user if f["type"] == "lastModifiedBy" else now

    def _relink(self, table_id: str, f: dict, rid: str, old: list[str], new: list[str]) -> None:
        sym_id = f["options"].get("symmetricFieldId")
        if not sym_id:
            return
        ft = f["options"]["foreignTableId"]
        sym = self.field(ft, sym_id)
        for other in self.records[ft]:
            cur = list(other["fields"].get(sym_id) or [])
            if other["id"] in old and other["id"] not in new and rid in cur:
                cur.remove(rid)
            if other["id"] in new and rid not in cur:
                if sym["options"]["relationship"] not in MULTI_LINK:
                    cur = []
                cur.append(rid)
            if cur:
                other["fields"][sym_id] = cur
            else:
                other["fields"].pop(sym_id, None)

    def upload(self, table_id: str, rid: str, fid: str, request: httpx.Request) -> dict:
        m = re.search(rb'filename="([^"]+)"', request.content)
        name = m.group(1).decode() if m else "file"
        rec = next(r for r in self.records[table_id] if r["id"] == rid)
        att = list(rec["fields"].get(fid) or []) + [{"id": self._id("act"), "name": name, "size": len(request.content),
                                                      "mimetype": "application/octet-stream", "path": f"table/{name}"}]
        self._apply(table_id, rec, {fid: att}, create=False)
        return {"id": rid, **self._out(table_id, rec, "name")}

    # ---------- 读 ----------
    def _title(self, table_id: str, rid: str):
        prim = next((f for f in self.fields[table_id] if f.get("isPrimary")), None)
        rec = next((r for r in self.records[table_id] if r["id"] == rid), None)
        if not prim or not rec:
            return None
        v = self._value(table_id, rec, prim)
        return v if not isinstance(v, (list, dict)) else str(v)

    def _value(self, table_id: str, rec: dict, f: dict):
        if f["type"] == "link":
            ids = rec["fields"].get(f["id"])
            if not ids:
                return None
            ft = f["options"]["foreignTableId"]
            vals = [{"id": i, "title": self._title(ft, i)} for i in ids]
            return vals if f["options"]["relationship"] in MULTI_LINK else vals[0]
        if f["type"] in ("lastModifiedBy", "lastModifiedTime"):
            v = (rec.get("_track") or {}).get(f["id"])
            if f["type"] == "lastModifiedBy" and v:
                u = self.users[v]
                return {"id": u["id"], "title": u["name"], "email": u["email"]}
            return v
        if f["type"] == "rollup" or f.get("isLookup"):
            return self._lookup(table_id, rec, f)
        if f["type"] == "formula":
            return self._formula(table_id, rec, f)
        return rec["fields"].get(f["id"])

    @staticmethod
    def _date_of(v, tz: str) -> date | None:
        """存的日期值(ISO 时间戳或 YYYY-MM-DD)→ 该时区的日期。"""
        if not v:
            return None
        s = str(v)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return date.fromisoformat(s[:10])
        return dt.date() if dt.tzinfo is None else dt.astimezone(ZoneInfo(tz)).date()

    def today(self, tz: str = "Asia/Shanghai") -> date:
        """假接口的"今天":测试可设 fake.today 模拟第二天。"""
        return getattr(self, "_today", None) or datetime.now(ZoneInfo(tz)).date()

    def _formula(self, table_id: str, rec: dict, f: dict):
        """公式基本不在这里算(返回空);只认两种和"今天"有关的写法,好测隔天重算:TODAY(),以及 IF(IS_BEFORE({x}, TODAY()), BLANK(), {x})。"""
        expr = ((f.get("options") or {}).get("expression") or "").strip()
        tz = (f.get("options") or {}).get("timeZone") or "Asia/Shanghai"
        if expr == "TODAY()":
            return self.today(tz).isoformat()
        m = re.fullmatch(r"IF\(IS_BEFORE\(\{(\w+)\}, TODAY\(\)\), BLANK\(\), \{(\w+)\}\)", expr)
        if m and m.group(1) == m.group(2):
            v = rec["fields"].get(m.group(1))
            d = self._date_of(v, tz)
            return v if d is not None and d >= self.today(tz) else None
        return None

    def _lookup(self, table_id: str, rec: dict, f: dict):
        lo = f["lookupOptions"]
        ft = lo["foreignTableId"]
        if f.get("isConditionalLookup"):  # 条件查找:对方表里满足 filter 的行(filter 的值可引用本行的字段 {type: field, fieldId})
            others = [r for r in self.records[ft] if self._match(ft, r, lo["filter"], host=(table_id, rec))]
        else:
            link = self.field(table_id, lo["linkFieldId"])
            others = [r for r in self.records[ft] if r["id"] in (rec["fields"].get(link["id"]) or [])]
            if lo.get("filter"):
                others = [r for r in others if self._match(ft, r, lo["filter"])]
        if lo.get("sort"):
            sf = self.field(ft, lo["sort"]["fieldId"])
            others.sort(key=lambda r: str(self._value(ft, r, sf) or ""), reverse=lo["sort"].get("order") == "desc")
        if lo.get("limit"):
            others = others[: int(lo["limit"])]
        target = self.field(ft, lo["lookupFieldId"])
        vals = [self._value(ft, r, target) for r in others]
        vals = [v for v in vals if v is not None]
        if f.get("isLookup"):
            return vals or None
        expr = (f.get("options") or {}).get("expression", "")
        if expr.startswith("countall"):
            return len(others)
        if not vals:
            return None
        if expr.startswith("min"):
            return min(vals)
        if expr.startswith("max"):
            return max(vals)
        if expr.startswith("sum"):
            return sum(float(v) for v in vals)
        return ", ".join(str(v) for v in vals)

    def _out(self, table_id: str, rec: dict, fkt: str, projection: list[str] | None = None) -> dict:
        fields = {}
        for f in self.fields[table_id]:
            k = f["id"] if fkt == "id" else f["name"]
            if projection and f["id"] not in projection and f["name"] not in projection:
                continue
            v = self._value(table_id, rec, f)
            if v is not None and v != []:
                fields[k] = v
        return {"fields": fields, "createdTime": rec["createdTime"], "lastModifiedTime": rec["lastModifiedTime"],
                "createdBy": rec["createdBy"], "lastModifiedBy": rec["lastModifiedBy"]}

    def _match(self, table_id: str, rec: dict, flt: dict, host: tuple[str, dict] | None = None) -> bool:
        res = []
        for item in flt.get("filterSet") or []:
            if "filterSet" in item:
                res.append(self._match(table_id, rec, item, host))
                continue
            f = self.field(table_id, item["fieldId"])
            v, op, want = self._value(table_id, rec, f), item["operator"], item.get("value")
            if isinstance(want, dict) and want.get("type") == "field":  # 引用本行(host)的字段:条件查找的写法
                if not host:
                    raise HTTPError(400, "filter 里引用了本表的字段,但这不是条件查找")
                want = self._value(host[0], host[1], self.field(host[0], want["fieldId"]))
                want = want.get("title") if isinstance(want, dict) else want
            titles = [x.get("title") for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict)]  # 关联按标题也能比
            if isinstance(v, dict):
                v = v.get("id")
            if isinstance(v, list):
                v = [x.get("id") if isinstance(x, dict) else x for x in v]
            empty = v is None or v == [] or v == ""
            if op == "isEmpty":
                res.append(empty)
            elif op == "isNotEmpty":
                res.append(not empty)
            elif op == "is":
                res.append(v == want or (want is not None and want in titles) or (isinstance(v, list) and want in v)
                           or (want is False and empty))  # 复选框没勾存 null,真 Teable 按「is false」也能查到它们(2026-10-20 线上核过)
            elif op == "isNot":
                res.append(v != want)
            elif op in ("isAnyOf", "hasAnyOf"):
                vs = v if isinstance(v, list) else [v]
                res.append(any(x in (want or []) for x in vs))
            elif op == "isNoneOf":
                vs = v if isinstance(v, list) else [v]
                res.append(not any(x in (want or []) for x in vs))
            elif op == "contains":
                res.append(want is not None and str(want).casefold() in str(v or "").casefold())
            elif op == "doesNotContain":
                res.append(want is None or str(want).casefold() not in str(v or "").casefold())
            elif op in ("isGreater", "isGreaterEqual", "isLess", "isLessEqual"):
                try:
                    x, w = float(v), float(want)
                except (TypeError, ValueError):
                    res.append(False)
                    continue
                res.append({"isGreater": x > w, "isGreaterEqual": x >= w, "isLess": x < w, "isLessEqual": x <= w}[op])
            elif op in ("isBefore", "isAfter", "isOnOrBefore", "isOnOrAfter") and isinstance(want, dict) and want.get("mode"):
                today = self.today(want.get("timeZone") or "Asia/Shanghai")
                anchor = {"today": today, "tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1)}.get(want["mode"])
                if want["mode"] == "exactDate" and want.get("exactDate"):
                    anchor = date.fromisoformat(str(want["exactDate"])[:10])
                d = self._date_of(v, want.get("timeZone") or "Asia/Shanghai")
                res.append(d is not None and anchor is not None
                           and {"isBefore": d < anchor, "isAfter": d > anchor, "isOnOrBefore": d <= anchor, "isOnOrAfter": d >= anchor}[op])
            else:
                res.append(True)  # 其余(日期范围等)在假接口里不过滤
        return all(res) if flt.get("conjunction", "and") == "and" else any(res)

    def _select(self, table_id: str, params: list[tuple[str, str]]) -> list[dict]:
        p = dict(params)
        recs = list(self.records[table_id])
        if p.get("viewId"):
            v = next(x for x in self.views[table_id] if x["id"] == p["viewId"])
            if v.get("filter"):
                recs = [r for r in recs if self._match(table_id, r, v["filter"])]
        if p.get("filter"):
            flt = json.loads(p["filter"])
            recs = [r for r in recs if self._match(table_id, r, flt)]
        if p.get("orderBy"):  # [{fieldId, order: asc|desc}],只按第一个排
            ob = json.loads(p["orderBy"])
            if ob:
                fid, desc = ob[0]["fieldId"], ob[0].get("order") == "desc"
                recs.sort(key=lambda r: (r["fields"].get(fid) is None, str(r["fields"].get(fid) or "")), reverse=desc)
        return recs

    def list_records(self, table_id: str, params: list[tuple[str, str]]) -> dict:
        p = dict(params)
        take, skip = int(p.get("take", 100)), int(p.get("skip", 0))
        if take > 1000:
            raise HTTPError(400, "take 最多 1000")
        projection = [v for k, v in params if k == "projection"]
        recs = self._select(table_id, params)[skip:skip + take]
        return {"records": [{"id": r["id"], **self._out(table_id, r, p.get("fieldKeyType", "name"), projection)} for r in recs]}

    def openapi(self) -> dict:
        """/docs-json 的极简版(能力核验的"看接口说明"用)。"""
        paths = {"/api/table/{tableId}/aggregation/row-count": {"get": {}}, "/api/table/{tableId}/record/{recordId}/history": {"get": {}},
                 "/api/comment/{tableId}/{recordId}/create": {"post": {}}, "/api/base/{baseId}/invitation/link": {"post": {}},
                 "/api/table/{tableId}/record/{recordId}/{fieldId}/uploadAttachment": {"post": {}}}
        schemas = {"IFieldRo": {"properties": {"type": {"enum": sorted(TYPES)}, "lookupOptions": {"properties": {
            "foreignTableId": {}, "linkFieldId": {}, "lookupFieldId": {}, "filter": {}, "sort": {}, "limit": {}}}}},
            "ILastModifiedTimeFieldOptions": {"properties": {"trackedFieldIds": {}}}}
        return {"openapi": "3.0.0", "paths": paths, "components": {"schemas": schemas}}
