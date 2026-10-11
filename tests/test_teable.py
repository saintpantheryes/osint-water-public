"""线索与台账同步到 Teable:建表、增量写入、关联、人管字段不覆盖、视图。用内存版的假接口,不联网。"""
from __future__ import annotations

import copy
import shutil
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import cli
from core import teable as tb
from core.ledger import load_ledger, load_overlay
from core.profile import load_profile
from core.watch.events import event_detail, set_stage
from core.watch.query import load_rows, set_status
from core.watch.store import Store

FX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 25)


class FakeTeable:
    """只实现 sync、pull 用到的接口,数据放内存;记录调用便于断言。记录带最后修改时间与修改人(接口返回账号 ID):
    系统写入记为令牌的账号 usrBOT;同事在表里改用 edit()。"""

    def __init__(self):
        self._spaces = [{"id": "spc1", "name": "水务项目机会", "role": "owner"}]
        self._bases: dict[str, dict] = {}
        self._tables: dict[str, dict] = {}       # table_id → {id, name, baseId}
        self._fields: dict[str, list[dict]] = {}  # table_id → [field]
        self._records: dict[str, list[dict]] = {}
        self._views: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self._n = 0
        self.clock = "2026-09-25T00:40:00.000Z"  # 系统写入的时间
        self.users = [{"userId": "usrBOT", "userName": "管理员", "email": "admin@example.com"},
                      {"userId": "usrZS", "userName": "张三", "email": "zs@example.com"},
                      {"userId": "usrLS", "userName": "李四", "email": "ls@example.com"}]
        self.history: dict[str, list[dict]] = {}  # 记录 ID → 每一格的修改(谁、什么时候),edit() 与 update_records() 记
        self.history_page = 20

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def spaces(self):
        return self._spaces

    def bases(self, space_id):
        return [b for b in self._bases.values() if b["spaceId"] == space_id]

    def create_base(self, space_id, name):
        b = {"id": self._id("bse"), "name": name, "spaceId": space_id}
        self._bases[b["id"]] = b
        return b

    def tables(self, base_id):
        return [t for t in self._tables.values() if t["baseId"] == base_id]

    def create_table(self, base_id, name, fields, views=None):
        t = {"id": self._id("tbl"), "name": name, "baseId": base_id}
        self._tables[t["id"]] = t
        self._fields[t["id"]] = [{**f, "id": self._id("fld")} for f in fields]
        self._records[t["id"]] = []
        self._views[t["id"]] = [{**v, "id": self._id("viw")} for v in (views or [{"name": "全部", "type": "grid"}])]
        self.calls.append(("create_table", name, [f["name"] for f in fields]))
        return t

    def fields(self, table_id):
        return self._fields[table_id]

    fail_rename = False

    def rename_table(self, base_id, table_id, name):
        if self.fail_rename:
            raise tb.TeableError("PUT name -> 403: forbidden")
        self._tables[table_id]["name"] = name
        self.calls.append(("rename_table", table_id, name))

    def rename_field(self, table_id, field_id, name):
        if self.fail_rename:
            raise tb.TeableError("PATCH field -> 403: forbidden")
        f = next(x for x in self._fields[table_id] if x["id"] == field_id)
        old, f["name"] = f["name"], name
        for r in self._records[table_id]:  # 同 Teable:记录里按新名字返回
            if old in r["fields"]:
                r["fields"][name] = r["fields"].pop(old)
        self.calls.append(("rename_field", table_id, old, name))

    def delete_record(self, table_id, record_id):
        self._records[table_id] = [r for r in self._records[table_id] if r["id"] != record_id]
        self.calls.append(("delete_record", table_id, record_id))

    def create_field(self, table_id, spec):
        f = {**spec, "id": self._id("fld")}
        self._fields[table_id].append(f)
        self.calls.append(("create_field", table_id, spec["name"]))
        return f

    fail_convert = False

    def convert_field(self, table_id, field_id, spec):
        if self.fail_convert:
            raise tb.TeableError("PUT convert -> 403: forbidden")
        f = next(x for x in self._fields[table_id] if x["id"] == field_id)
        assert spec["type"] == f["type"]  # 只改设置,不改类型
        f["options"] = spec["options"]
        self.calls.append(("convert", table_id, f["name"], spec["options"]))
        return f

    def records(self, table_id, take=500, **params):
        return [dict(r) for r in self._records[table_id]]

    def create_records(self, table_id, records):
        out = []
        for f in records:
            rec = {"id": self._id("rec"), "fields": self._store(table_id, f), "createdBy": "usrBOT", "createdTime": self.clock,
                   "lastModifiedTime": self.clock, "lastModifiedBy": "usrBOT"}
            self._records[table_id].append(rec)
            out.append(rec)
        self.calls.append(("create_records", table_id, len(records)))
        return out

    def update_records(self, table_id, records):
        by = {r["id"]: r for r in self._records[table_id]}
        for rid, f in records:
            self._change(table_id, by[rid], self._store(table_id, f), "usrBOT", self.clock)
        self.calls.append(("update_records", table_id, [f for _, f in records]))

    def collaborators(self, base_id):
        return self.users

    def edit(self, table, key, user="usrZS", at="2026-09-28T02:00:00.000Z", **fields):
        """同事在表里改一条记录(成员字段存成接口返回的样子 {id, title, email})。"""
        rec = self.rows(table)[key]
        names = {u["userId"]: u for u in self.users}
        self._change(self.table(table)["id"], rec, {k: ({"id": v, "title": names[v]["userName"], "email": names[v]["email"]}
                                                        if isinstance(v, str) and v in names else v) for k, v in fields.items()}, user, at)

    def _change(self, table_id, rec, vals, user, at):
        """改记录:值变了的格记进修改历史(同 Teable:只记更新,建记录不记),最后修改人与时间跟着变。"""
        fid = {f["name"]: f["id"] for f in self._fields[table_id]}
        for k, v in vals.items():
            if k in fid and rec["fields"].get(k) != v:
                self.history.setdefault(rec["id"], []).append({"fieldId": fid[k], "createdBy": user, "createdTime": at})
        rec["fields"].update(vals)
        rec.update(lastModifiedTime=at, lastModifiedBy=user)

    fail_history = False

    def record_history(self, table_id, record_id, field_ids=None, cursor=None):
        """同接口:新的在前,按页给,userMap 带显示名与邮箱;fail_history 时像令牌没有权限那样报错。"""
        self.calls.append(("history", record_id))
        if self.fail_history:
            raise tb.TeableError(f"GET /table/{table_id}/record/{record_id}/history -> 403: forbidden")
        items = [h for _, h in sorted(enumerate(self.history.get(record_id, [])), key=lambda x: (x[1]["createdTime"], x[0]), reverse=True)
                 if not field_ids or h["fieldId"] in field_ids]
        start = int(cursor or 0)
        page, end = items[start:start + self.history_page], start + self.history_page
        users = {u["userId"]: {"id": u["userId"], "name": u["userName"], "email": u["email"]} for u in self.users}
        return {"historyList": page, "userMap": {h["createdBy"]: users[h["createdBy"]] for h in page if h["createdBy"] in users},
                "nextCursor": str(end) if end < len(items) else None}

    def add(self, table, user="usrZS", at="2026-09-28T02:00:00.000Z", **fields):
        """同事在表里新加一行(关联传记录 ID:多对一存成 {id, title})。"""
        tid = self.table(table)["id"]
        specs = {f["name"]: f for f in self._fields[tid]}
        vals = {}
        for k, v in fields.items():
            if (specs.get(k) or {}).get("type") == "link":
                vals[k] = {"id": v, "title": "…"}
            elif (specs.get(k) or {}).get("type") == "user":
                u = next(x for x in self.users if x["userId"] == v)
                vals[k] = {"id": v, "title": u["userName"], "email": u["email"]}
            else:
                vals[k] = v
        rec = {"id": self._id("rec"), "fields": vals, "createdBy": user, "createdTime": at, "lastModifiedTime": at, "lastModifiedBy": user}
        self._records[tid].append(rec)
        return rec

    fail_upload = False

    def upload_attachment(self, table_id, record_id, field_id, path):
        if self.fail_upload:
            raise tb.TeableError("POST uploadAttachment -> 500")
        name = next(f["name"] for f in self._fields[table_id] if f["id"] == field_id)
        rec = next(r for r in self._records[table_id] if r["id"] == record_id)
        rec["fields"][name] = (rec["fields"].get(name) or []) + [{"name": Path(path).name, "size": Path(path).stat().st_size}]
        self.calls.append(("upload", table_id, Path(path).name))
        return rec

    def _store(self, table_id, fields):
        """关联字段存成接口返回的样子 [{id, title}]。"""
        out = {}
        for k, v in fields.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
                out[k] = [{"id": x["id"], "title": "…"} for x in v]
            else:
                out[k] = v
        return out

    def views(self, table_id):
        return self._views[table_id]

    def create_view(self, table_id, spec):
        v = {**spec, "id": self._id("viw")}
        self._views[table_id].append(v)
        self.calls.append(("create_view", table_id, spec["name"], spec["type"]))
        return v

    def set_filter(self, table_id, view_id, flt):
        self.calls.append(("filter", view_id, flt))

    def set_sort(self, table_id, view_id, sort_objs):
        self.calls.append(("sort", view_id, sort_objs))

    def set_group(self, table_id, view_id, group):
        self.calls.append(("group", view_id, group))

    # 断言用
    def table(self, name):
        return next(t for t in self._tables.values() if t["name"] == name)

    def rows(self, name):
        return {r["fields"]["编号"]: r for r in self._records[self.table(name)["id"]]}


def _sig(sid, title, links=(), relevant=True, deadline="2026-11-30", country="ZZZ"):
    return {"id": sid, "title": title, "url": f"https://example.org/t/{sid[:4]}", "published": "2026-09-20",
            "first_seen": "2026-09-21T08:00:00+00:00", "source": "zz_notices", "source_name": "虚构采购网", "kind": "html_list",
            "country": country, "links": [{"item": i, "confidence": c, "by": "rule"} for i, c in links],
            "llm": {"relevant": relevant, "category": "tender", "sectors": ["wastewater"], "summary_zh": f"摘要:{title}",
                    "project": "甲市污水厂", "funder": "EBRD", "amount": {"value": 120000000, "currency": "EUR"},
                    "deadlines": [{"type": "bid_deadline", "date": deadline, "evidence": "…"}] if deadline else [],
                    "ledger_links": [], "new_opportunity": False}}


TRACKED = ("ZZZ-001", "ZZZ-002", "ZZZ-003", "ZZZ-004")
FIXTURE = "夹具"  # 夹具里设进展的操作人


def _store(tmp_path, tracked=TRACKED):
    """线索库;tracked 里的台账项目设了进展(在跟踪的商机,商机表里只有它们)。a1b2c3d4 编号吻合 ZZZ-001,属于这个商机。"""
    st = Store(tmp_path / "data")
    st.append_signals([
        _sig("a1b2c3d4e5f60718", "Tender: WWTP construction", links=(("ZZZ-001", "high"), ("ZZZ-003", "medium"))),
        _sig("b0112233445566aa", "Road works", relevant=False, deadline=None),
    ])
    (tmp_path / "data" / "watch").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "watch" / "triage.yaml").write_text("b0112233: {status: ignore}\n", encoding="utf-8")
    for i, iid in enumerate(tracked):
        set_stage(st, iid, "tracking", by=FIXTURE, at=f"2026-09-1{i}T08:00:00+00:00")
    return st


def _ops(st):
    """操作记录里除去夹具设的商机进展(测的是同步、拉回记下的)。"""
    return [e for e in st.events() if e.get("by") != FIXTURE]


def test_project_rows_derive_from_ledger():
    prof = load_profile(FX / "profile.yaml")
    rows = {r["编号"]: r for r in tb.project_rows(prof, TODAY)}
    a, b = rows["ZZZ-001"], rows["ZZZ-002"]
    assert a["名称"] == "ZZZ-001 甲市污水处理厂" and a["国别"] == "测试国" and a["台账阶段"] == "采购进行中"
    assert a["领域"] == ["污水"] and a["资金方"] == "EBRD" and a["规模(万美元)"] == 5500  # 5000 万欧元 × 1.1
    assert a["来源数"] == 2 and a["参考项"] is False and a["核实状态"].startswith("已核实")
    assert a["最近节点"].startswith("资格预审截止") and a["最近节点日期"] == "2026-11-15" and a["最晚启动"] < "2026-11-15"
    assert b["参考项"] is True and b["领域"] == ["灌溉"]


def test_lead_rows_take_links_status_and_deadline(tmp_path):
    prof = load_profile(FX / "profile.yaml")
    rows = {r["编号"]: r for r in tb.lead_rows(prof, _store(tmp_path), TODAY)}
    a, b = rows["a1b2c3d4e5f60718"], rows["b0112233445566aa"]
    assert a["所属商机"] == ["ZZZ-001"] and a["挂接"] == "●ZZZ-001、○ZZZ-003"  # 编号吻合的那个;名称相近的只在「挂接」里
    assert a["跟踪进展"] == "跟踪中"  # 线索表的跟踪进展就是所属商机的进展
    assert (a["处理状态"], a["相关"], a["截止日期"], a["截止类型"], a["类别"]) == ("待处理", True, "2026-11-30", "投标截止", "招标")
    assert a["领域"] == ["污水"] and a["信源"] == "虚构采购网" and a["原文"].startswith("https://")
    assert (b["处理状态"], b["相关"], b["所属商机"]) == ("已忽略", False, [])


def test_sync_builds_tables_links_and_views(tmp_path):
    prof = load_profile(FX / "profile.yaml")
    api = FakeTeable()
    r = tb.sync(prof, _store(tmp_path), TODAY, "https://water.example.test", "tok", api=api, log=lambda *_: None)
    assert (r["projects"], r["leads"], r["warnings"]) == (4, 2, [])
    assert [c[1] for c in api.calls if c[0] == "create_table"] == ["商机", "线索", "跟进记录", "报告", "每日摘要"]
    projects, leads = api.rows("商机"), api.rows("线索")
    assert set(projects) == {"ZZZ-001", "ZZZ-002", "ZZZ-003", "ZZZ-004"}
    linked = leads["a1b2c3d4e5f60718"]["fields"]["所属商机"]
    assert [x["id"] for x in linked] == [projects["ZZZ-001"]["id"]]
    # 主字段在前,人管的字段存在,关联指向项目台账表
    names = [f["name"] for f in api.fields(api.table("线索")["id"])]
    assert names[0] == "标题" and {"处理状态", "跟踪进展", "负责人", "下一步", "下一步到期", "备注"} <= set(names)
    link = next(f for f in api.fields(api.table("线索")["id"]) if f["name"] == "所属商机")
    assert link["options"]["foreignTableId"] == api.table("商机")["id"]
    views = [(c[2], c[3]) for c in api.calls if c[0] == "create_view"]
    assert views == [("待处理", "grid"), ("在跟踪", "grid"), ("截止日历", "calendar"), ("已处理", "grid"), ("待分配", "grid"),
                     ("本周到期", "grid"), ("看板", "kanban"), ("按国别", "grid"), ("节点日历", "calendar"), ("优先项目", "grid"),
                     ("在跟踪", "grid"), ("已结束", "grid"), ("看板", "kanban"), ("下一步日历", "calendar"),  # 商机表
                     ("按商机", "grid"), ("跟进日历", "calendar")]  # 跟进记录表
    flt = next(c[2] for c in api.calls if c[0] == "filter")
    assert flt["conjunction"] == "and" and flt["filterSet"][0]["operator"] == "is" and flt["filterSet"][0]["value"] == "待处理"
    by_view = {v["name"]: v for v in api.views(api.table("线索")["id"])}
    flts = {c[1]: c[2] for c in api.calls if c[0] == "filter"}
    week = flts[by_view["本周到期"]["id"]]
    assert week["conjunction"] == "or" and {x["operator"] for x in week["filterSet"]} == {"isWithIn"}
    assert week["filterSet"][0]["value"] == {"mode": "nextNumberOfDays", "numberOfDays": 7, "timeZone": "Asia/Shanghai"}
    assert [x["operator"] for x in flts[by_view["待分配"]["id"]]["filterSet"]] == ["is", "isEmpty"]
    assert by_view["看板"]["options"]["stackFieldId"] == next(f["id"] for f in api.fields(api.table("线索")["id"]) if f["name"] == "跟踪进展")
    # 表格链接(与 Teable"复制记录链接"同一格式)、记录 ID 表(本机界面"在表格里打开"用)
    base = api.table("线索")["baseId"]
    rid = leads["a1b2c3d4e5f60718"]["id"]
    assert leads["a1b2c3d4e5f60718"]["fields"]["表格链接"] == f"https://water.example.test/base/{base}/table/{api.table('线索')['id']}?recordId={rid}"
    links = tb.load_links(Store(tmp_path / "data"))
    assert links["leads"]["a1b2c3d4e5f60718"] == rid and tb.lead_link(links, "a1b2c3d4e5f60718") == leads["a1b2c3d4e5f60718"]["fields"]["表格链接"]
    assert links["tables"] == {n: api.table(n)["id"] for n in ("线索", "商机", "跟进记录", "报告", "每日摘要")}  # 系统里嵌入各表用
    # 跟进记录挂线索、项目(多对一);三张新表默认按日期倒序
    follow = {f["name"]: f for f in api.fields(api.table("跟进记录")["id"])}
    assert list(follow)[0] == "内容" and follow["线索"]["options"] == {"relationship": "manyOne", "foreignTableId": api.table("线索")["id"]}
    sorts = {c[2][0]["fieldId"] for c in api.calls if c[0] == "sort" and c[2][0]["order"] == "desc"}
    newest = {(n, f): next(x["id"] for x in api.fields(api.table(n)["id"]) if x["name"] == f)
              for n, f in (("报告", "日期"), ("每日摘要", "运行"), ("跟进记录", "日期"))}
    assert set(newest.values()) <= sorts


def test_second_sync_is_incremental_and_keeps_human_fields(tmp_path):
    prof = load_profile(FX / "profile.yaml")
    st = _store(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    lead = api.rows("线索")["a1b2c3d4e5f60718"]
    lead["fields"].update({"处理状态": "已处理", "备注": "同事在表里写的", "负责人": {"id": "usr1"}, "下一步": "约见业主"})
    api.calls.clear()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert not [c for c in api.calls if c[0] in ("create_table", "create_field", "create_records", "create_view")]
    assert not [c for c in api.calls if c[0] == "update_records"]  # 数据没变就一条也不改
    # 数据变了(日期推后 → 距最晚启动变)只改变了的字段,人管的字段原样保留
    tb.sync(prof, st, date(2026, 10, 5), "https://x", "t", api=api, log=lambda *_: None)
    ups = [c for c in api.calls if c[0] == "update_records"]
    assert ups and all(set(f) <= {"距最晚启动(天)", "台账更新提醒"} for f in ups[0][2])
    lead = api.rows("线索")["a1b2c3d4e5f60718"]["fields"]
    assert (lead["处理状态"], lead["备注"], lead["负责人"], lead["下一步"]) == ("已处理", "同事在表里写的", {"id": "usr1"}, "约见业主")


def test_profile_teable_section_defaults(tmp_path):
    off = {"internal_reports": False, "notify_owner": False, "server_sync": False}
    assert load_profile(FX / "profile.yaml").teable == {"base": "水务项目机会", "pull": False, **off}  # 没写这一节
    fp = tmp_path / "profile.yaml"
    head = "name: t\ncountries:\n  - {iso2: ZZ, iso3: ZZZ, name_en: T, tier: focus}\n"
    fp.write_text(head + "teable: {pull: true}\n", encoding="utf-8")
    assert load_profile(fp).teable == {"base": "水务项目机会", "pull": True, **off}  # 只写一项,其余取缺省
    fp.write_text(head + "teable: {base: ' 测试库 ', notify_owner: yes}\n", encoding="utf-8")
    assert load_profile(fp).teable == {"base": "测试库", "pull": False, "internal_reports": False, "notify_owner": True,
                                       "server_sync": False}
    fp.write_text(head + "teable: {server_sync: true}\n", encoding="utf-8")
    assert load_profile(fp).teable["server_sync"] is True  # 表格只由服务器上的系统同步


def test_owner_notify_is_switched_on_once_when_the_profile_asks(tmp_path):
    """teable.notify_owner:缺省不动「负责人」的通知;打开时两张表的负责人字段各改一次(只改设置),已打开的不再改;
    改不成只提示,同步照常。"""
    prof = load_profile(FX / "profile.yaml")
    st = _store(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert not [c for c in api.calls if c[0] == "convert"]
    on = replace(prof, teable={**prof.teable, "notify_owner": True})
    api.fail_convert = True
    r = tb.sync(on, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert [w.split("(")[0] for w in r["warnings"]] == ["线索 的负责人通知没打开", "商机 的负责人通知没打开"] and r["leads"] == 2
    api.fail_convert = False
    tb.sync(on, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    conv = [(c[1], c[2], c[3]) for c in api.calls if c[0] == "convert"]
    assert conv == [(api.table(n)["id"], "负责人", {"isMultiple": False, "shouldNotify": True}) for n in ("线索", "商机")]
    api.calls.clear()
    tb.sync(on, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert not [c for c in api.calls if c[0] == "convert"]


def test_sync_finds_base_named_in_profile(tmp_path):
    prof = replace(load_profile(FX / "profile.yaml"), teable={"base": "测试库", "pull": False})
    api = FakeTeable()
    old = api.create_base("spc1", "测试库")
    r = tb.sync(prof, _store(tmp_path), TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert r["base"] == old["id"] and [b["name"] for b in api._bases.values()] == ["测试库"]  # 按名找到已有的库,不另建


NEW_LEAD = "c0ffee0011223344"  # 没有可靠挂接的线索:在表里设了进展就跟踪即入账


def _pull_env(tmp_path, team: str | None = None):
    """可写的台账与内部叠加副本 + 线索库(含一条没挂接的新线索);team:岗位代号表 team.yaml 的内容。"""
    shutil.copytree(FX / "ZZZ", tmp_path / "ZZZ")
    shutil.copy(FX / "overlay.yaml", tmp_path / "overlay.yaml")
    if team:
        (tmp_path / "team.yaml").write_text(team, encoding="utf-8")
    base = load_profile(FX / "profile.yaml")
    c = replace(base.countries[0], ledger=tmp_path / "ZZZ" / "ledger.yaml", overlay=tmp_path / "overlay.yaml")
    prof = replace(base, countries=(c,), teable={"base": "水务项目机会", "pull": True}, path=tmp_path / "profile.yaml")
    st = _store(tmp_path)
    st.append_signals([_sig(NEW_LEAD, "Travaux de la STEP de Wu", deadline="2026-12-01")])
    return prof, st


def _pull(prof, st, api):
    return tb.pull(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)


def test_pull_brings_colleagues_changes_back_as_events(tmp_path, monkeypatch):
    """反向同步:推送时记下人管字段的初值;同事在表里改了处理状态与备注、负责人、下一步与到期、跟踪进展(跟踪即入账),
    项目的优先级、负责人、内部备注——拉回写成操作记录(操作人 teable:<代号或显示名>,时间取表格的修改时间)、处理状态与内部叠加;
    邮箱不存;连拉两次第二次没有新记录;本机与表格都改了同一字段时以表格为准并注明。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path, team="members:\n  - {code: 部门甲-01, teable: 张三}\n")
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    state = tb.load_state(st)
    assert state["leads"]["b0112233445566aa"]["v"]["处理状态"] == "已忽略"
    assert state["projects"]["ZZZ-001"]["v"] == {**dict.fromkeys(tb.PROJECT_HUMAN), "进展": "跟踪中", "优先级": "A"}  # 建记录时写进的
    n0 = len(st.events())
    r = _pull(prof, st, api)
    assert (r["events"], r["baseline"], r["leads"], r["projects"]) == (0, 0, 3, 4) and len(st.events()) == n0  # 推送后没人改:无记录
    lead = "a1b2c3d4e5f60718"
    api.edit("线索", lead, 处理状态="已处理", 备注="已转市场部", 负责人="usrZS", 下一步="约见业主", 下一步到期="2026-10-14T16:00:00.000Z")
    api.edit("线索", NEW_LEAD, user="usrLS", at="2026-09-28T03:30:00.000Z", 跟踪进展="跟踪中")
    api.edit("商机", "ZZZ-001", user="usrLS", 优先级="B", 负责人="usrLS", 内部备注="集团重点")
    r = _pull(prof, st, api)
    assert st.triage()[lead[:8]] == {"status": "done", "note": "已转市场部"}
    ev = [(e["id"], e["kind"], e["value"], e["by"], e["at"]) for e in st.events()]
    zs = ("teable:部门甲-01", "2026-09-28T02:00:00+00:00")  # 张三在代号表里:记代号
    assert (lead, "status", "done", *zs) in ev and (lead, "owner", "部门甲-01", *zs) in ev and (lead, "next", "约见业主", *zs) in ev
    assert next(e for e in st.events() if e["kind"] == "next")["due"] == "2026-10-15"  # 上海日期
    assert (NEW_LEAD, "stage", "tracking", "teable:李四", "2026-09-28T03:30:00+00:00") in ev  # 李四不在代号表里:记显示名
    assert (NEW_LEAD, "ledger", "created", "teable:李四") in [x[:4] for x in ev] and r["entered"][0].startswith("线索 c0ffee00:已按线索")
    assert st.ledger_reviewed()[NEW_LEAD[:8]]["item"] == "ZZZ-005" and "ZZZ-005" in {it["id"] for it in load_ledger(prof.countries[0].ledger).items}
    ov = load_overlay(tmp_path / "overlay.yaml")["items"]["ZZZ-001"]
    assert (ov["priority"], ov["owner"], ov["note"], ov["business_line"]) == ("B", "李四", "集团重点", "EPC")  # 原有的判断保留
    assert r["overlay"] and "@" not in (st.root / "teable_state.jsonl").read_text(encoding="utf-8")  # 邮箱不存
    assert "@" not in (st.root / "events.jsonl").read_text(encoding="utf-8")
    rows = {x["id"]: x for x in load_rows(st, TODAY)}
    assert (rows[lead]["owner"], rows[lead]["next_step"], rows[lead]["next_due"], rows[NEW_LEAD]["stage"]) == (
        "部门甲-01", "约见业主", "2026-10-15", "tracking")
    n = len(st.events())
    assert _pull(prof, st, api)["events"] == 0 and len(st.events()) == n  # 幂等:连拉两次第二次没有新记录
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)  # 推送不覆盖人管字段,新项目出现在项目表
    assert api.rows("线索")[lead]["fields"]["处理状态"] == "已处理" and "ZZZ-005" in api.rows("商机")
    assert [x["id"] for x in api.rows("线索")[NEW_LEAD]["fields"]["所属商机"]] == [api.rows("商机")["ZZZ-005"]["id"]]
    assert _pull(prof, st, api)["events"] == 0
    # 冲突:本机把它改成忽略,表格里同事又改回待处理 → 以表格为准,操作记录注明本机原来的值
    set_status(st, [lead], "ignore", "本机看是重复的")
    api.edit("线索", lead, at="2026-09-29T01:00:00.000Z", 处理状态="待处理")
    r = _pull(prof, st, api)
    assert r["conflicts"] == [lead[:8]] and lead[:8] not in st.triage() and tb.load_state(st)["last_pull"]["conflicts"] == [lead[:8]]
    last = st.events()[-1]
    assert (last["kind"], last["value"], last["conflict"]) == ("status", "new", "本机为「已忽略」")
    assert event_detail(last).endswith("本机也改过(本机为「已忽略」),以表格为准")


def test_first_pull_brings_earlier_colleague_edits_and_odd_values_are_reported(tmp_path, monkeypatch):
    """还没有同步状态(早期推送建的记录):第一次拉回时,建好后只有系统动过的只记下现值;有同事改过的按系统现值比,
    改了的拉回;只改备注的记一笔(已处理的处理备注跟着改);认不出的选项值提示、不拉回;修改人是账号 ID 时按协作者换成显示名。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    (st.root / "teable_state.jsonl").unlink()
    api.edit("线索", "a1b2c3d4e5f60718", 处理状态="已处理")  # 第一次拉回之前同事就改了:照样拉回
    r = _pull(prof, st, api)
    assert (r["events"], r["baseline"]) == (1, 6) and st.triage()["a1b2c3d4"]["status"] == "done"
    assert [(e["kind"], e["value"], e["by"]) for e in _ops(st)] == [("status", "done", "teable:张三")]
    assert _pull(prof, st, api)["events"] == 0
    n = len(st.events())
    api.edit("线索", "b0112233445566aa", 备注="重复公告")  # 已忽略的线索只改了备注
    api.edit("线索", NEW_LEAD, 跟踪进展="意向不明")
    r = _pull(prof, st, api)
    assert st.triage()["b0112233"] == {"status": "ignore", "note": "重复公告"}
    assert [(e["kind"], e["note"], e["by"]) for e in st.events()[n:]] == [("note", "表格里的备注改为:重复公告", "teable:张三")]
    assert r["warnings"] == ["线索 c0ffee00 的跟踪进展「意向不明」认不出,没拉回"]


def test_each_field_is_attributed_to_whoever_changed_it(tmp_path, monkeypatch):
    """几个人先后改同一条线索:操作人与时间按字段取自记录历史(谁、什么时候改的这一格),不都记在最后改的人名下;
    历史分页时往后翻;代号表照样换成代号;下一步与到期取两格里后改的那次。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path, team="members:\n  - {code: 部门甲-01, teable: 张三}\n")
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    lead = "a1b2c3d4e5f60718"
    api.edit("线索", lead, at="2026-09-28T01:00:00.000Z", 处理状态="已处理")
    api.edit("线索", lead, user="usrLS", at="2026-09-28T02:00:00.000Z", 负责人="usrLS", 下一步="约见业主")
    api.edit("线索", lead, user="usrLS", at="2026-09-28T02:10:00.000Z", 下一步到期="2026-10-14T16:00:00.000Z")
    api.edit("线索", lead, at="2026-09-28T03:00:00.000Z", 结果原因="业主改用国内招标")  # 张三最后改:整条记录的最后修改人
    api.history_page = 2
    r = _pull(prof, st, api)
    ev = {e["kind"]: (e["by"], e["at"]) for e in _ops(st)}
    assert ev == {"status": ("teable:部门甲-01", "2026-09-28T01:00:00+00:00"), "owner": ("teable:李四", "2026-09-28T02:00:00+00:00"),
                  "next": ("teable:李四", "2026-09-28T02:10:00+00:00"), "note": ("teable:部门甲-01", "2026-09-28T03:00:00+00:00")}
    assert r["warnings"] == [] and len([c for c in api.calls if c[0] == "history"]) == 3  # 5 格的历史每页 2 条:翻到第 3 页


def test_without_record_history_changes_go_to_the_last_modifier(tmp_path, monkeypatch):
    """记录历史查不了(老版本、令牌没有权限、出错):操作人与时间按整条记录的最后修改人与时间,提示一次,这次拉回不再查。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    lead = "a1b2c3d4e5f60718"
    api.edit("线索", lead, at="2026-09-28T01:00:00.000Z", 处理状态="已处理")
    api.edit("线索", lead, user="usrLS", 负责人="usrLS")
    api.edit("线索", NEW_LEAD, 下一步="问一下业主")
    api.fail_history = True
    r = _pull(prof, st, api)
    assert {(e["id"], e["kind"], e["by"], e["at"]) for e in _ops(st)} == {
        (lead, "status", "teable:李四", "2026-09-28T02:00:00+00:00"), (lead, "owner", "teable:李四", "2026-09-28T02:00:00+00:00"),
        (NEW_LEAD, "next", "teable:张三", "2026-09-28T02:00:00+00:00")}
    assert len(r["warnings"]) == 1 and r["warnings"][0].startswith("表格的记录历史查不了,这次的操作人按整条记录的最后修改人记(GET")
    assert len([c for c in api.calls if c[0] == "history"]) == 1


def _clock(monkeypatch):
    """同步状态的时间(tb._now)换成可控的钟:每调一次走一分钟,从 2026-09-28 00:00 UTC 起。"""
    t = [datetime(2026, 9, 28, tzinfo=timezone.utc)]

    def now():
        t[0] += timedelta(minutes=1)
        return t[0].isoformat(timespec="milliseconds")
    monkeypatch.setattr(tb, "_now", now)
    return t


def test_after_restore_aligns_records_synced_since_the_backup(tmp_path, monkeypatch):
    """表格从备份恢复:备份以后同步过的记录,人管字段改回上次同步时的值,不写操作记录,下次拉回没有"改回去"的假记录;
    原负责人已不在库里的,同步状态改成表格现在的值并列出来。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    clock = _clock(monkeypatch)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    backup_at = clock[0] + timedelta(seconds=30)  # 备份:此刻表格的样子(连同修改历史)
    snap = copy.deepcopy((api._records, api.history))
    lead = "a1b2c3d4e5f60718"
    api.edit("线索", lead, user="usrLS", 处理状态="已处理", 跟踪进展="跟踪中", 负责人="usrLS", 下一步="约见业主",
             下一步到期="2026-10-14T16:00:00.000Z")
    api.edit("商机", "ZZZ-001", 优先级="B")
    _pull(prof, st, api)  # 备份以后拉回了这些改动
    n, tri = len(st.events()), st.triage()[lead[:8]]
    api._records, api.history = copy.deepcopy(snap)  # 表格从备份恢复
    api.users = [u for u in api.users if u["userId"] != "usrLS"]  # 李四之后离开了库
    r = tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    assert (r["leads"], r["projects"]) == (1, 1) and r["kept"] == ["线索 a1b2c3d4:原负责人已不在库里,表格里保留现在的负责人,请核对"]
    f = api.rows("线索")[lead]["fields"]
    assert (f["处理状态"], f["跟踪进展"], f["下一步"], f["下一步到期"], f.get("负责人")) == ("已处理", "跟踪中", "约见业主", "2026-10-15", None)
    assert api.rows("商机")["ZZZ-001"]["fields"]["优先级"] == "B"
    assert len(st.events()) == n and st.triage()[lead[:8]] == tri  # 对齐不写操作记录
    r = _pull(prof, st, api)
    assert r["events"] == 0 and len(st.events()) == n and st.triage()[lead[:8]] == tri  # 下次拉回没有改回去的假记录
    assert load_overlay(tmp_path / "overlay.yaml")["items"]["ZZZ-001"]["priority"] == "B"


def _restored(tmp_path, monkeypatch):
    """同步 → 备份(连同修改历史)→ 李四在表里改了线索 → 拉回 → 表格从备份恢复。→ (prof, st, api, 备份时间, 线索)"""
    monkeypatch.delenv("OPERATOR", raising=False)
    clock = _clock(monkeypatch)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    backup_at = clock[0] + timedelta(seconds=30)
    snap = copy.deepcopy((api._records, api.history))
    lead = "a1b2c3d4e5f60718"
    api.edit("线索", lead, user="usrLS", 处理状态="已处理", 下一步="约见业主")
    _pull(prof, st, api)
    api._records, api.history = copy.deepcopy(snap)
    return prof, st, api, backup_at, lead


def test_after_restore_never_overwrites_edits_made_after_the_restore(tmp_path, monkeypatch):
    """恢复以后、对齐之前同事又改了(或对齐重跑、时间填早了):记录历史里晚于备份的格不改回,留给拉回照常拉。"""
    prof, st, api, backup_at, lead = _restored(tmp_path, monkeypatch)
    api.edit("线索", lead, at="2026-10-01T02:00:00.000Z", 下一步="下周二去现场")  # 恢复以后张三改的
    r = tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    f = api.rows("线索")[lead]["fields"]
    assert (f["处理状态"], f["下一步"]) == ("已处理", "下周二去现场") and r["fields"] == 1
    assert r["kept"] == ["线索 a1b2c3d4:下一步恢复以后在表格里改过,不改回(下次拉回照常拉)"]
    api.edit("线索", lead, at="2026-10-01T03:00:00.000Z", 处理状态="已忽略")  # 对齐以后又改,再跑一次对齐:不动
    r = tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    assert (r["fields"], api.rows("线索")[lead]["fields"]["处理状态"]) == (0, "已忽略")
    n = len(st.events())
    _pull(prof, st, api)
    assert [(e["kind"], e["by"]) for e in st.events()[n:]] == [("status", "teable:张三"), ("next", "teable:张三")]


def test_after_restore_leaves_fields_the_system_did_not_learn_after_the_backup(tmp_path, monkeypatch):
    """备份以后的同步状态行里,推送写回时没碰的格沿用旧值:这些格不改回(备份里同事改的值照常拉回)。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    clock = _clock(monkeypatch)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    lead = "a1b2c3d4e5f60718"
    api.edit("线索", lead, user="usrLS", at="2026-09-27T12:00:00.000Z", 下一步="约见业主")  # 备份以前同事改的,系统还没拉回
    backup_at = clock[0] + timedelta(seconds=30)
    snap = copy.deepcopy((api._records, api.history))
    set_status(st, [lead], "done", "本机处理了")
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)  # 只推送(拉回没跑):写回处理状态,下一步沿用旧值
    api._records, api.history = copy.deepcopy(snap)
    r = tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    f = api.rows("线索")[lead]["fields"]
    assert (f["处理状态"], f["下一步"], r["fields"]) == ("已处理", "约见业主", 2)  # 处理状态与备注改回写回后的值,下一步不动
    _pull(prof, st, api)
    assert [(e["kind"], e["value"], e["by"]) for e in st.events() if e["kind"] == "next"] == [("next", "约见业主", "teable:李四")]


def test_after_restore_refuses_without_members_or_history_and_reports_gaps(tmp_path, monkeypatch):
    """读不到库成员或记录历史:不改,报错(稍后重跑)。备份以后拉回过、表格里没了的跟进,只数这些;
    备份以后才有的线索(表格里没了)列出来,负责人、下一步要重填。"""
    prof, st, api, backup_at, lead = _restored(tmp_path, monkeypatch)

    def no_members(base_id):
        raise tb.TeableError("GET collaborators -> 500")
    with monkeypatch.context() as m:
        m.setattr(api, "collaborators", no_members)
        with pytest.raises(tb.TeableError, match="读不到库的成员"):
            tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    api.fail_history = True
    with pytest.raises(tb.TeableError, match="读不到记录历史"):
        tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    api.fail_history = False
    assert api.rows("线索")[lead]["fields"]["处理状态"] == "待处理"  # 报错的两次都没改表格
    tb.append_state(st, [{"at": "2026-09-27T00:00:00.000+00:00", "t": "follow", "k": "recOLD"},   # 备份以前拉回、后来被删的:不算
                         {"at": "2099-01-01T00:00:00.000+00:00", "t": "follow", "k": "recNEW"},   # 备份以后拉回的:表格里没了
                         {"at": "2099-01-01T00:00:00.000+00:00", "t": "lead", "k": "f00d000011112222",
                          "v": {**dict.fromkeys(tb.LEAD_HUMAN), "负责人": "usrZS"}, "s": {}}])
    r = tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    assert (r["follows_lost"], r["refill"], r["fields"]) == (1, ["f00d0000"], 2)
    assert "recNEW" in tb.load_state(st)["flost"]  # 拉回时不当作删除,推送时按系统里的重建


def test_after_restore_leaves_records_not_synced_since_the_backup(tmp_path, monkeypatch):
    """备份比上次同步新(同事在上次同步以后改的在备份里):不动,下次拉回照常拉,操作人照常记;备份时间没带时区的不认。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    clock = _clock(monkeypatch)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("线索", NEW_LEAD, 下一步="问一下业主")
    backup_at = clock[0] + timedelta(seconds=30)
    api.calls.clear()
    r = tb.after_restore(prof, st, backup_at, "https://x", "t", api=api, log=lambda *_: None)
    assert (r["leads"], r["projects"], r["fields"]) == (0, 0, 0) and not [c for c in api.calls if c[0] == "update_records"]
    assert _pull(prof, st, api)["events"] == 1 and st.events()[-1]["by"] == "teable:张三"
    with pytest.raises(ValueError):
        tb.after_restore(prof, st, datetime(2026, 10, 5, 3, 30), "https://x", "t", api=api, log=lambda *_: None)
    assert cli.teable(prof, TODAY, "https://x", "t", after_restore="2026-10-05 03:30") == 1  # 命令行:认不出的时间不动表格


def test_fields_unreadable_is_reported_and_falls_back(tmp_path, monkeypatch):
    """读不到线索表的字段(查记录历史要用字段 ID):同样按整条记录的最后修改人记,并提示,不悄悄退回。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("线索", "a1b2c3d4e5f60718", at="2026-09-28T01:00:00.000Z", 处理状态="已处理")
    api.edit("线索", "a1b2c3d4e5f60718", user="usrLS", 负责人="usrLS")

    def broken(table_id):
        raise tb.TeableError("GET /table/x/field -> 500")
    monkeypatch.setattr(api, "fields", broken)
    r = _pull(prof, st, api)
    assert {(e["kind"], e["by"]) for e in _ops(st)} == {("status", "teable:李四"), ("owner", "teable:李四")}
    assert r["warnings"] == ["表格的记录历史查不了,这次的操作人按整条记录的最后修改人记(读不到线索表的字段:GET /table/x/field -> 500)"]
    assert not [c for c in api.calls if c[0] == "history"]


def test_project_changes_that_could_not_be_written_are_pulled_again(tmp_path):
    """项目的人管列没写进内部叠加(国别没配 overlay 文件、写坏了、推送失败)时提示原因,state 不前移,下次拉回再试。"""
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("商机", "ZZZ-002", 优先级="B")
    no_overlay = replace(prof, countries=(replace(prof.countries[0], overlay=None),))
    for _ in range(2):
        r = _pull(no_overlay, st, api)
        assert r["overlay"] == [] and "ZZZ-002 的国别没有配置内部叠加文件" in r["warnings"][0]
        assert tb.load_state(st)["projects"]["ZZZ-002"]["v"]["优先级"] is None
    assert _pull(prof, st, api)["overlay"] == ["内部叠加 ZZZ:ZZZ-002 优先级 B"]
    assert load_overlay(tmp_path / "overlay.yaml")["items"]["ZZZ-002"] == {"priority": "B"}
    assert tb.load_state(st)["projects"]["ZZZ-002"]["v"]["优先级"] == "B" and _pull(prof, st, api)["overlay"] == []


class FlakyTeable(FakeTeable):
    """更新记录时出错一次(推送建好一批记录后中途失败)。"""

    fail_update = True

    def update_records(self, table_id, records):
        if self.fail_update:
            self.fail_update = False
            raise tb.TeableError("PATCH -> 502")
        super().update_records(table_id, records)


def test_state_survives_a_push_that_fails_after_creating_records(tmp_path, monkeypatch):
    """推送建好新记录后中途出错:建好的已记下初值;之后同事改了它,照样拉回(不会当成"第一次见到"吞掉)。
    没记下过的记录(那次的同步状态没保存上),建好后有人改过的,按推送时会写的系统值比;没人动过的只记现值。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FlakyTeable()
    api.fail_update = False
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    _pull(prof, st, api)
    st.append_signals([_sig("d00d00aa11223344", "Works at Wu", deadline="2026-12-05")])  # 新线索 + 另一条要更新
    api.rows("线索")["a1b2c3d4e5f60718"]["fields"]["摘要"] = "表里被改乱的摘要"
    api.fail_update = True
    with pytest.raises(tb.TeableError):
        tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert "d00d00aa11223344" in tb.load_state(st)["leads"]  # 建好的那批已记下
    api.edit("线索", "d00d00aa11223344", 处理状态="已忽略", 备注="重复")
    _pull(prof, st, api)
    assert st.triage()["d00d00aa"] == {"status": "ignore", "note": "重复"}
    # 同步状态丢了一条(推送那次的数据提交没成功):有人改过的按系统值比,拉回;没人动过的只记现值
    lines = (st.root / "teable_state.jsonl").read_text(encoding="utf-8").splitlines()
    (st.root / "teable_state.jsonl").write_text("\n".join(x for x in lines if '"k": "a1b2c3d4e5f60718"' not in x
                                                          and '"k": "c0ffee0011223344"' not in x) + "\n", encoding="utf-8")
    api.edit("线索", "a1b2c3d4e5f60718", 处理状态="已处理")
    r = _pull(prof, st, api)
    assert st.triage()["a1b2c3d4"]["status"] == "done" and r["baseline"] == 1  # c0ffee00 没人动过:只记现值


def test_duplicate_rows_are_reported_and_do_not_flip_flop(tmp_path, monkeypatch):
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    rec = api.rows("线索")["a1b2c3d4e5f60718"]
    copy = {**rec, "id": "recCOPY", "fields": {**rec["fields"], "处理状态": "已忽略"}, "lastModifiedTime": "2026-09-26T00:00:00.000Z"}
    api._records[api.table("线索")["id"]].append(copy)  # 同事复制了一行(较早改过)
    api.edit("线索", "a1b2c3d4e5f60718", 处理状态="已处理")  # 原来那行最近改过
    r = _pull(prof, st, api)
    assert "编号 a1b2c3d4e5f60718 有不止一条记录" in r["warnings"][0] and st.triage()["a1b2c3d4"]["status"] == "done"
    for _ in range(2):
        assert _pull(prof, st, api)["events"] == 0  # 不来回改


def test_conflict_only_when_this_computer_changed_it_since_last_sync(tmp_path, monkeypatch):
    """"本机也改过"只算上次同步以后本机改的:本机改了、推送时已写回表格的,之后表格再改不算冲突;
    上次同步以后两边都改了才算,以表格为准,那条记录的时间记为这次拉回,排在本机那次之后。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    lead = "a1b2c3d4e5f60718"
    set_status(st, [lead], "ignore")  # 本机改了,推送时写回表格
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert api.rows("线索")[lead]["fields"]["处理状态"] == "已忽略"
    api.edit("线索", lead, at="2020-01-02T00:00:00.000Z", 处理状态="已处理")
    r = _pull(prof, st, api)
    assert r["conflicts"] == [] and st.triage()[lead[:8]]["status"] == "done" and "conflict" not in st.events()[-1]
    set_status(st, [lead], "new")  # 本机恢复待处理,表格里同事又改成已忽略:两边都改了
    api.edit("线索", lead, at="2020-01-03T00:00:00.000Z", 处理状态="已忽略")
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)  # 推送不覆盖表格里改过的
    assert api.rows("线索")[lead]["fields"]["处理状态"] == "已忽略"
    r = _pull(prof, st, api)
    last = st.events()[-1]
    assert r["conflicts"] == [lead[:8]] and last["conflict"] == "本机为「待处理」" and last["at"] > "2020-01-04"
    assert st.triage()[lead[:8]]["status"] == "ignore"
    api.calls.clear()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert not [f for c in api.calls if c[0] == "update_records" for f in c[2] if "处理状态" in f]  # 以表格为准后不再写回


def test_push_writes_back_local_changes_the_table_did_not_touch(tmp_path, monkeypatch):
    """推送的写回:本机改了处理状态与备注、进展,表格自上次同步没人改过的写回表格;写回后拉回不重复记;
    表格里同事改过的字段不覆盖;负责人、下一步只在表格里设,不写回。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    a, b = "a1b2c3d4e5f60718", "b0112233445566aa"
    set_status(st, [a], "done", "本机已核对")
    from core.watch.events import set_owner, set_stage
    set_stage(st, NEW_LEAD, "tracking")
    set_owner(st, NEW_LEAD, "部门甲-01")
    set_status(st, [b], "new")  # 本机把已忽略的恢复待处理,但表格里同事已经改成已处理
    api.edit("线索", b, 处理状态="已处理")
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    rows = api.rows("线索")
    assert (rows[a]["fields"]["处理状态"], rows[a]["fields"]["备注"]) == ("已处理", "本机已核对")
    assert rows[NEW_LEAD]["fields"]["跟踪进展"] == "跟踪中" and not rows[NEW_LEAD]["fields"].get("负责人")
    assert rows[b]["fields"]["处理状态"] == "已处理" and r["written_back"] == 2
    assert rows[a]["lastModifiedBy"] == "usrBOT"
    n = len(st.events())
    r = _pull(prof, st, api)  # 写回的两条不当作表格改动;b 两边都改了:以表格为准
    assert r["conflicts"] == [b[:8]] and [(e["id"], e["value"]) for e in st.events()[n:]] == [(b, "done")]
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["written_back"] == 0
    assert _pull(prof, st, api)["events"] == 0


def test_local_change_still_written_back_after_a_pull_of_another_field(tmp_path, monkeypatch):
    """本机改了处理状态还没推送,同事在表里改了同一条的进展:拉回只拉进展,处理状态的基准不动,推送时照样写回。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    a = "a1b2c3d4e5f60718"
    set_status(st, [a], "done")
    api.edit("线索", a, 跟踪进展="放弃")
    r = _pull(prof, st, api)
    assert r["conflicts"] == [] and [(e["id"], e["kind"], e["value"]) for e in st.events()[-1:]] == [("ZZZ-001", "stage", "dropped")]
    # 写回:线索的处理状态;线索属于 ZZZ-001,在线索上改的进展是商机的进展,商机表跟着改
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["written_back"] == 2
    assert (api.rows("线索")[a]["fields"]["处理状态"], api.rows("线索")[a]["fields"]["跟踪进展"]) == ("已处理", "放弃")
    assert api.rows("商机")["ZZZ-001"]["fields"]["进展"] == "放弃"
    assert _pull(prof, st, api)["events"] == 0


def test_records_without_sync_state_get_local_changes_written_back(tmp_path, monkeypatch):
    """早期推送建的记录(没有同步状态):建好后只有系统动过的,拉回只记基准,本机建好后改的推送时写回;
    有同事改过、本机建好后也改过同一条的,拉回以表格为准并注明冲突。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    (st.root / "teable_state.jsonl").unlink()
    a = "a1b2c3d4e5f60718"
    set_status(st, [a], "done")  # 建好后本机改的
    from core.watch.events import set_stage
    set_stage(st, NEW_LEAD, "tracking")  # 建好后本机设了进展,同事又在表里改了这条的备注
    api.edit("线索", NEW_LEAD, 备注="同事的备注")
    b = "b0112233445566aa"
    set_status(st, [b], "new")  # 建好后本机把已忽略的恢复待处理,同事又在表里改了这条的备注
    api.edit("线索", b, 备注="同事看过")
    r = _pull(prof, st, api)
    assert r["conflicts"] == [b[:8], NEW_LEAD[:8]] and not [e for e in st.events() if e["id"] == a and e["by"].startswith("teable:")]
    assert (st.events()[-1]["kind"], st.events()[-1]["value"], st.events()[-1]["conflict"]) == ("stage", "", "本机为「跟踪中」")
    assert [(e["kind"], e["value"], e.get("conflict")) for e in st.events() if e["id"] == b][-1] == ("status", "ignore", "本机为「待处理」")
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert api.rows("线索")[a]["fields"]["处理状态"] == "已处理" and r["written_back"] == 1
    assert _pull(prof, st, api)["events"] == 0


def test_project_priority_and_note_follow_the_overlay_when_the_table_did_not_change(tmp_path, monkeypatch):
    """项目表的优先级、内部备注:建记录时写进内部叠加里的值(A/B/C 以外的不写,提示);内部叠加改了、表格没人改过的写回;
    没有同步状态、表格里还空着的(早期推送不写这几列)拉回记基准后写回;表格与本机都改了:以表格为准写进内部叠加并提示。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    ov = tmp_path / "overlay.yaml"
    ov.write_text(ov.read_text(encoding="utf-8") + "  ZZZ-003: {priority: 2}\n", encoding="utf-8")  # 没换成 A/B/C 的
    api = FakeTeable()
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    rows = api.rows("商机")
    assert rows["ZZZ-001"]["fields"]["优先级"] == "A" and not rows["ZZZ-003"]["fields"].get("优先级")
    assert "内部叠加里 ZZZ-003 的优先级「2」不是 A/B/C,表格里不显示" in r["warnings"]
    tb.write_overlay(prof, {"ZZZ-001": {"priority": "B", "note": "本机的备注"}}, TODAY, warns=[])  # 本机改了内部叠加
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert (api.rows("商机")["ZZZ-001"]["fields"]["优先级"], api.rows("商机")["ZZZ-001"]["fields"]["内部备注"]) == ("B", "本机的备注")
    assert r["written_back"] == 1
    (st.root / "teable_state.jsonl").unlink()  # 早期推送建的、表格里还空着的项目
    api.rows("商机")["ZZZ-002"]["fields"]["优先级"] = None
    tb.write_overlay(prof, {"ZZZ-002": {"priority": "C"}}, TODAY, warns=[])
    assert _pull(prof, st, api)["overlay"] == []
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["written_back"] == 1
    assert api.rows("商机")["ZZZ-002"]["fields"]["优先级"] == "C"
    api.edit("商机", "ZZZ-001", 优先级="C")  # 两边都改了
    tb.write_overlay(prof, {"ZZZ-001": {"priority": "A"}}, TODAY, warns=[])
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert api.rows("商机")["ZZZ-001"]["fields"]["优先级"] == "C"
    r = _pull(prof, st, api)
    assert load_overlay(ov)["items"]["ZZZ-001"]["priority"] == "C" and "商机 ZZZ-001 的优先级本机也改过(本机为「A」),以表格为准" in r["warnings"]


def test_early_state_lines_are_completed_without_writing(tmp_path, monkeypatch):
    """早期的同步状态行(线索的 s 没有 note,项目行没有 s):不知道本机改没改的项不写回,补记现值作基准;之后本机再改的照常写回。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    a = "a1b2c3d4e5f60718"
    set_status(st, [a], "done", "早先的处理备注")
    (st.root / "teable_state.jsonl").unlink()  # 只留早期格式的行
    old = [{"at": "2026-09-28T00:00:00.000+00:00", "t": "lead", "k": a, "s": {"status": "done", "stage": ""},
            "v": {**dict.fromkeys(tb.LEAD_HUMAN), "处理状态": "待处理"}},
           {"at": "2026-09-28T00:00:00.000+00:00", "t": "project", "k": "ZZZ-002", "v": dict.fromkeys(tb.PROJECT_HUMAN)}]
    tb.append_state(st, old)
    tb.write_overlay(prof, {"ZZZ-002": {"priority": "A"}}, TODAY, warns=[])
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert r["written_back"] == 0 and not api.rows("线索")[a]["fields"].get("备注") and not api.rows("商机")["ZZZ-002"]["fields"].get("优先级")
    state = tb.load_state(st)
    assert state["leads"][a]["s"]["note"] == "早先的处理备注" and state["projects"]["ZZZ-002"]["s"] == {"priority": "A", "note": "", "stage": "tracking"}
    set_status(st, [a], "done", "新的处理备注")
    tb.write_overlay(prof, {"ZZZ-002": {"priority": "B"}}, TODAY, warns=[])
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["written_back"] == 2
    assert (api.rows("线索")[a]["fields"]["备注"], api.rows("商机")["ZZZ-002"]["fields"]["优先级"]) == ("新的处理备注", "B")


def test_overlay_values_are_written_so_they_read_back_the_same(tmp_path):
    """写进 overlay 的文字按 PyYAML 读回来要一样:no、Yes、1:20 这类加引号,多行的保留换行;说明里不写负责人与备注的内容。"""
    prof, _ = _pull_env(tmp_path)
    lines, written = tb.write_overlay(prof, {"ZZZ-001": {"owner": "no", "note": "第一行\n第二行", "priority": "A"},
                                             "ZZZ-003": {"note": "1:20"}}, TODAY, warns=[])
    items = load_overlay(tmp_path / "overlay.yaml")["items"]
    assert (items["ZZZ-001"]["owner"], items["ZZZ-001"]["note"], items["ZZZ-003"]["note"]) == ("no", "第一行\n第二行", "1:20")
    assert written == {"ZZZ-001", "ZZZ-003"} and lines == ["内部叠加 ZZZ:ZZZ-001 负责人、内部备注、优先级 A;ZZZ-003 内部备注"]


def test_load_team_maps_display_names_to_codes(tmp_path):
    prof = replace(load_profile(FX / "profile.yaml"), path=tmp_path / "profile.yaml")
    assert tb.load_team(prof) == {}
    (tmp_path / "team.yaml").write_text("members:\n  - {code: A1, teable: 张三}\n  - {code: '', teable: 李四}\n  - 王五\n", encoding="utf-8")
    assert tb.load_team(prof) == {"张三": "A1"}


def test_norm_and_diff_tolerate_api_value_shapes():
    assert tb._same("2026-11-30T00:00:00.000Z", "2026-11-30") and tb._same("2026-11-29T16:00:00.000Z", "2026-11-30")
    assert tb._same([{"id": "rec1", "title": "x"}], [{"id": "rec1"}]) and not tb._same([{"id": "rec1"}], [{"id": "rec2"}])
    assert tb._same(["污水", "供水"], ["供水", "污水"]) and tb._same(None, False) and tb._same(None, "") and tb._same(5500, 5500.0)
    assert tb.diff({"a": 1, "备注": "x"}, {"a": 2, "备注": "y", "b": None}, protect={"备注"}) == {"a": 2}


def test_field_specs_use_known_types():
    types = {"singleLineText", "longText", "user", "checkbox", "multipleSelect", "singleSelect", "date", "number", "link"}
    for spec in tb.project_fields() + tb.lead_fields("tblX"):
        assert spec["type"] in types, spec
        if spec["type"] in ("singleSelect", "multipleSelect"):
            assert all(isinstance(c["name"], str) for c in spec["options"]["choices"])
    assert tb.project_fields({"国别": ["测试国"]})[2]["options"]["choices"] == [{"name": "测试国"}]


def test_follow_records_become_follow_ups_on_the_opportunity(tmp_path, monkeypatch):
    """跟进记录表里新加的行拉回写成商机的跟进(kind follow:内容、判断与依据、跟进日期):关联商机的记在商机上;只关联线索的,
    记在线索所属的商机上,线索还没有商机的记在线索上。填了进展的改商机的进展,判断为放弃又没填进展的进展记为放弃;下一步记下一步。
    操作人记「人」(没填记建这行的账号),时间取建这行的时间;每行只拉一次;没关联的先不拉并提示,关联上以后再拉;内容与判断都空的不拉。
    推送时商机表写出当前判断与最近跟进。"""
    from core import opportunity as op
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path, team="members:\n  - {code: 部门甲-01, teable: 张三}\n")
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    a = "a1b2c3d4e5f60718"
    opps = api.rows("商机")
    lead_rec, new_rec = api.rows("线索")[a]["id"], api.rows("线索")[NEW_LEAD]["id"]
    api.add("跟进记录", user="usrLS", at="2026-09-27T09:00:00.000Z", 内容="约见了业主", 日期="2026-09-26T16:00:00.000Z", 人="usrLS",
            线索=lead_rec, 下一步="报价", 下一步到期="2026-10-14T16:00:00.000Z")  # 只关联线索:a1b2 属于 ZZZ-001
    api.add("跟进记录", 内容="与设计院交流", 商机=opps["ZZZ-001"]["id"], 判断="重点推进", 判断依据="业主确认 11 月发标")  # 没填人:建这行的账号
    api.add("跟进记录", 内容="问了业主,还在可研", 线索=new_rec)  # 线索还没有商机:记在线索上
    api.add("跟进记录", 内容="资金没落实", 商机=opps["ZZZ-002"]["id"], 判断="放弃")  # 判断放弃、没填进展:进展记为放弃
    api.add("跟进记录", 内容="买了标书", 商机=opps["ZZZ-003"]["id"], 进展="准备投标")
    loose = api.add("跟进记录", 内容="没关联的")
    api.add("跟进记录", 内容="  ", 线索=lead_rec)
    r = _pull(prof, st, api)
    ev = [(e["id"], e["kind"], e["value"], e.get("note"), e["by"], e["at"]) for e in _ops(st)]
    li, zs, at0 = ("teable:李四", "2026-09-27T09:00:00+00:00"), "teable:部门甲-01", "2026-09-28T02:00:00+00:00"
    assert ("ZZZ-001", "follow", "", "约见了业主", *li) in ev and ("ZZZ-001", "next", "报价", None, *li) in ev
    f1 = next(e for e in st.events() if e.get("note") == "约见了业主")
    assert f1["day"] == "2026-09-27" and next(e for e in st.events() if e["kind"] == "next")["due"] == "2026-10-15"  # 上海日期
    assert ("ZZZ-001", "follow", "focus", "与设计院交流", zs, at0) in ev
    assert (NEW_LEAD, "follow", "", "问了业主,还在可研", zs, at0) in ev
    assert ("ZZZ-002", "follow", "drop", "资金没落实", zs, at0) in ev and ("ZZZ-002", "stage", "dropped", None, zs, at0) in ev
    assert ("ZZZ-003", "stage", "preparing", None, zs, at0) in ev
    assert len(ev) == 8 and r["warnings"] == [f"跟进记录 {loose['id']} 没有关联商机或线索,先不拉回(关联上以后下次再拉)"]
    assert op.judgment_text(op.judgments(st.events())["ZZZ-001"]) == "重点推进(2026-09-28):业主确认 11 月发标"
    assert _pull(prof, st, api)["events"] == 0  # 每行只拉一次
    copy = {**api.rows("线索")[a], "id": "recCOPY", "lastModifiedTime": "2026-09-20T00:00:00.000Z"}  # 表里复制出来的重复行
    api._records[api.table("线索")["id"]].append(copy)
    loose["fields"]["线索"] = {"id": "recCOPY", "title": "…"}  # 关联到重复行:按编号认
    assert _pull(prof, st, api)["events"] == 1 and (st.events()[-1]["id"], st.events()[-1]["note"]) == ("ZZZ-001", "没关联的")
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)  # 商机表:当前判断、最近跟进、进展
    rows = api.rows("商机")
    assert rows["ZZZ-001"]["fields"]["当前判断"] == "重点推进(2026-09-28):业主确认 11 月发标"
    assert rows["ZZZ-001"]["fields"]["最近跟进"].endswith("没关联的")
    assert (rows["ZZZ-002"]["fields"]["进展"], rows["ZZZ-003"]["fields"]["进展"]) == ("放弃", "准备投标")


def _frows(api):
    """跟进记录表的行(按建的先后)。"""
    return api._records[api.table("跟进记录")["id"]]


def test_follow_ups_recorded_in_the_system_are_pushed_once(tmp_path, monkeypatch):
    """系统里记的跟进(商机页的跟进与判断、带说明的进展,含所属线索上的)推到跟进记录表:一条一行,关联商机,记在线索上的同时
    关联线索,「人」写表格显示名(代号换回显示名);系统代记的(auto、system)不推;再推不重复;拉回不把推上去的行当新行;
    表格里已有一模一样的行(早先拉回、没记 ref 的)认下,不再建。商机页的「跟进与判断」就是这些行。"""
    from core import opportunity as op
    from core.watch.events import add_follow, add_note
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path, team="members:\n  - {code: 部门甲-01, teable: 张三}\n")
    a = "a1b2c3d4e5f60718"
    set_stage(st, "ZZZ-002", "tracking", "发给伙伴打听进展", by="teable:李四", at="2026-09-30T15:05:31+00:00")  # 商机页设进展写了说明
    set_stage(st, a, "tracking", "在询问有无适格的伙伴", by="部门甲-01", at="2026-09-30T08:07:16+00:00")  # 线索上(属于 ZZZ-001)
    add_follow(st, "ZZZ-001", "与业主通话", "focus", "资金已到位", "2026-09-29", by="部门甲-01", stage="preparing", step="报价",
               due="2026-10-15")
    add_note(st, a, "表格里的备注改为:x", by="teable:张三", auto=True)  # 系统代记的:不推
    add_note(st, "ZZZ-003", "跟踪即入账:…", by="system")
    add_follow(st, "ZZZ-003", "早先拉回的", "watch", day="2026-09-20", by="teable:李四")  # 上一版拉回的:没记 ref,表里已有
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    old = next(x for x in _frows(api) if x["fields"].get("内容") == "早先拉回的")
    api._records[api.table("跟进记录")["id"]] = [old]  # 只留上一版拉回的那行(像上一版那样:系统里记的没推过)
    (st.root / "teable_state.jsonl").write_text("".join(
        x + "\n" for x in (st.root / "teable_state.jsonl").read_text(encoding="utf-8").splitlines() if '"fpush"' not in x), encoding="utf-8")
    tb.append_state(st, [{"at": "2026-09-21T00:00:00.000+00:00", "t": "follow", "k": old["id"]}])
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    opps, leads = api.rows("商机"), api.rows("线索")
    got = {x["fields"]["内容"]: x["fields"] for x in _frows(api)}
    assert r["follows"] == 3 and set(got) == {"早先拉回的", "发给伙伴打听进展", "在询问有无适格的伙伴", "与业主通话"}
    f = got["与业主通话"]
    assert f["商机"] == {"id": opps["ZZZ-001"]["id"]} and "线索" not in f and f["人"] == "张三"
    assert (f["日期"], f["判断"], f["判断依据"], f["进展"], f["下一步"], f["下一步到期"]) == (
        "2026-09-29", "重点推进", "资金已到位", "准备投标", "报价", "2026-10-15")
    g = got["在询问有无适格的伙伴"]
    assert g["商机"] == {"id": opps["ZZZ-001"]["id"]} and g["线索"] == {"id": leads[a]["id"]} and g["进展"] == "跟踪中"
    assert (g["日期"], g["人"], g.get("判断")) == ("2026-09-30", "张三", None)
    assert (got["发给伙伴打听进展"]["日期"], got["发给伙伴打听进展"]["人"]) == ("2026-09-30", "李四")  # 北京时间 23:05,还是 30 日
    n = len(st.events())
    assert _pull(prof, st, api)["events"] == 0 and len(st.events()) == n  # 推上去的、认下的行都不当新行拉回
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["follows"] == 0 and len(_frows(api)) == 4
    assert [e["note"] for e in op.work_log("ZZZ-001", st.events(), ["a1b2c3d4"])] == ["在询问有无适格的伙伴", "与业主通话"]
    assert op.judgment_text(op.judgments(st.events(), {"ZZZ-001": ["a1b2c3d4"]})["ZZZ-001"]) == "重点推进(2026-09-29):资金已到位"


def test_follow_records_edited_or_deleted_in_the_table_change_the_system(tmp_path, monkeypatch):
    """表格里改了对上的行(推上去的或拉回过的):系统里那条撤回,换成改后的一条(带 ref),改了进展的改商机的进展;
    删掉的行:系统里那条撤回,不再重建;商机表的最近跟进跟着变。整张表空着时不当作删除,推送时按系统里的重建。"""
    from core import opportunity as op
    from core.watch.events import add_follow
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    add_follow(st, "ZZZ-001", "与业主通话", "continue", day="2026-09-29", by="部门甲-01")
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    ft = api.table("跟进记录")["id"]
    pushed = _frows(api)[0]
    api.add("跟进记录", 内容="买了标书", 商机=api.rows("商机")["ZZZ-003"]["id"], 判断="继续跟进")
    _pull(prof, st, api)
    bought = _frows(api)[1]
    assert [e["note"] for e in op.work_log("ZZZ-003", st.events())] == ["买了标书"]
    api._change(ft, pushed, {"内容": "与业主通话,确认 11 月发标", "判断": "重点推进", "进展": "准备投标"}, "usrLS", "2026-10-01T02:00:00.000Z")
    r = _pull(prof, st, api)
    log = op.work_log("ZZZ-001", st.events(), ["a1b2c3d4"])
    assert [(e["kind"], e["note"], e["value"], e["ref"], e["by"], e["day"]) for e in log] == [
        ("follow", "与业主通话,确认 11 月发标", "focus", pushed["id"], "teable:李四", "2026-09-29")]
    assert r["follows_edited"] == 1 and tb.book_of(prof, st).stage("ZZZ-001", st.events())["value"] == "preparing"
    assert _pull(prof, st, api)["events"] == 0  # 改后的已对上
    api.delete_record(ft, bought["id"])
    r = _pull(prof, st, api)
    assert r["follows_deleted"] == 1 and op.work_log("ZZZ-003", st.events()) == [] and st.events()[-1]["kind"] == "retract"
    assert _pull(prof, st, api)["events"] == 0  # 不再重复撤回
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["follows"] == 0  # 删掉的不重建,改后的不重复建
    assert [x["fields"]["内容"] for x in _frows(api)] == ["与业主通话,确认 11 月发标"]
    assert api.rows("商机")["ZZZ-001"]["fields"]["最近跟进"].endswith("与业主通话,确认 11 月发标")
    api._records[ft] = []  # 整张表空着:不当作删除,推送时重建
    r = _pull(prof, st, api)
    assert r["follows_deleted"] == 0 and any("跟进记录表是空的" in w for w in r["warnings"])
    assert tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["follows"] == 1
    assert _pull(prof, st, api)["events"] == 0 and len(op.work_log("ZZZ-001", st.events(), ["a1b2c3d4"])) == 1


def test_lead_moved_to_another_opportunity_relinks_its_follow_rows(tmp_path, monkeypatch):
    """记在线索上的跟进随线索走:线索并入别的商机后,推送时把那一行的「商机」改过来。"""
    from core.watch.events import add_follow, set_member
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    a = "a1b2c3d4e5f60718"
    add_follow(st, a, "线索上的跟进", by="部门甲-01")
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert _frows(api)[0]["fields"]["商机"] == {"id": api.rows("商机")["ZZZ-001"]["id"]}
    set_member(st, a, "ZZZ-002")
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert r["follows"] == 0 and _frows(api)[0]["fields"]["商机"] == {"id": api.rows("商机")["ZZZ-002"]["id"]}


def test_result_reason_is_pulled_as_note(tmp_path, monkeypatch):
    """线索的「结果原因」(中标、未中标、放弃时填)拉回记一笔;早期的同步状态行没有这一项,不当作改动。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    lines = (st.root / "teable_state.jsonl").read_text(encoding="utf-8").replace(', "结果原因": null', "")  # 早期格式
    (st.root / "teable_state.jsonl").write_text(lines, encoding="utf-8")
    assert '"结果原因"' not in lines
    n = len((st.root / "teable_state.jsonl").read_text(encoding="utf-8").splitlines())
    assert _pull(prof, st, api)["events"] == 0 and len((st.root / "teable_state.jsonl").read_text(encoding="utf-8").splitlines()) == n  # 没拉到改动:不记
    api.edit("线索", NEW_LEAD, 跟踪进展="未中标", 结果原因="报价高于中标价 8%")
    _pull(prof, st, api)
    assert [(e["kind"], e["value"], e.get("note")) for e in _ops(st)] == [("stage", "lost", None), ("note", "", "结果原因:报价高于中标价 8%")]


def test_reports_uploaded_once_and_linked_from_daily_summary(tmp_path, monkeypatch):
    """推送时:数据目录 reports/weekly、monthly 下的 Word 报告按名称传到「报告」表——缺省只传不含内部信息的(周报、月报公开版),
    月报内部版、国别动态、线索简报在 profile 打开 teable.internal_reports 才传;内容没变的不重传,重新生成过的换新文件,
    没传成的下次再传;Word 的临时文件、认不出日期的周次不出错。监测摘要(watch/summary.json)每次运行写成「每日摘要」一行,
    这次传上去的报告挂在这一行上。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    root = st.root.parent
    files = {"reports/weekly/ZZZ_周报_2026-W39.docx": b"PK weekly v1", "reports/weekly/~$Z_周报_2026-W39.docx": b"lock",
             "reports/weekly/ZZZ_周报_2027-W53.docx": b"PK odd week", "reports/monthly/线索简报_2026-09-26.docx": b"PK brief",
             "reports/monthly/国别报告_测试国_2026-09-26.docx": b"PK country", "reports/monthly/ZZZ_项目机会分析_2026-09-28.docx": b"PK internal",
             "reports/monthly/ZZZ_项目机会分析_2026-09-28_公开版.docx": b"PK public", "reports/monthly/别的_2026-09-26.docx": b"PK other"}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    run1 = '{"date": "2026-09-28", "at": "2026-09-28T00:43:00+00:00", "new_relevant": 3, "due_14d": 2, "autoledger": 1, ' \
           '"source_errors": ["虚构采购网:error 403"]}'
    (st.root / "summary.json").write_text(run1, encoding="utf-8")
    api = FakeTeable()
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    reps = {x["fields"]["名称"]: x["fields"] for x in api._records[api.table("报告")["id"]]}
    assert set(reps) == {"ZZZ_周报_2026-W39", "ZZZ_周报_2027-W53", "ZZZ_项目机会分析_2026-09-28_公开版"} and r["reports"] == sorted(reps)
    assert (reps["ZZZ_周报_2026-W39"]["类型"], reps["ZZZ_周报_2026-W39"]["日期"]) == ("周报", "2026-09-27")
    assert reps["ZZZ_周报_2027-W53"].get("日期") is None and reps["ZZZ_项目机会分析_2026-09-28_公开版"]["类型"] == "月报"
    assert reps["ZZZ_周报_2026-W39"]["文件"] == [{"name": "ZZZ_周报_2026-W39.docx", "size": 12}]
    rows = {x["fields"]["运行"]: x["fields"] for x in api._records[api.table("每日摘要")["id"]]}
    day = rows["2026-09-28 08:43"]  # 北京时间
    assert (day["日期"], day["新增相关线索"], day["14天内截止"], day["自动入账"], day["信源异常"]) == ("2026-09-28", 3, 2, 1, "虚构采购网:error 403")
    assert {x["id"] for x in day["报告"]} == {x["id"] for x in api._records[api.table("报告")["id"]]}
    ups = len([c for c in api.calls if c[0] == "upload"])
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)  # 再推送:内容没变不重传,摘要里的报告不丢
    assert len([c for c in api.calls if c[0] == "upload"]) == ups and len(api._records[api.table("每日摘要")["id"]]) == 1
    assert len({x["fields"]["运行"]: x for x in api._records[api.table("每日摘要")["id"]]}["2026-09-28 08:43"]["fields"]["报告"]) == 3
    # 周一的完整版替换周中手动出的同名周报;同一天又跑了一次监测:另起一行,前一次的数字不丢
    (root / "reports/weekly/ZZZ_周报_2026-W39.docx").write_bytes(b"PK weekly v2 full")
    (st.root / "summary.json").write_text(run1.replace("00:43", "09:10").replace('"new_relevant": 3', '"new_relevant": 0'), encoding="utf-8")
    api.fail_upload = True
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert r["reports"] == [] and "报告 ZZZ_周报_2026-W39 没传上去(下次再传)" in r["warnings"][-1]
    api.fail_upload = False
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    week = {x["fields"]["名称"]: x["fields"] for x in api._records[api.table("报告")["id"]]}["ZZZ_周报_2026-W39"]
    assert r["reports"] == ["ZZZ_周报_2026-W39"] and week["文件"] == [{"name": "ZZZ_周报_2026-W39.docx", "size": 17}]
    rows = {x["fields"]["运行"]: x["fields"] for x in api._records[api.table("每日摘要")["id"]]}
    assert (rows["2026-09-28 08:43"]["新增相关线索"], rows["2026-09-28 17:10"]["新增相关线索"]) == (3, 0)
    week_id = next(x["id"] for x in api._records[api.table("报告")["id"]] if x["fields"]["名称"] == "ZZZ_周报_2026-W39")
    assert [x["id"] for x in rows["2026-09-28 17:10"]["报告"]] == [week_id]
    inside = replace(prof, teable={**prof.teable, "internal_reports": True})  # profile 打开 teable.internal_reports 才传含内部信息的
    assert tb.sync(inside, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["reports"] == [
        "ZZZ_项目机会分析_2026-09-28", "国别报告_测试国_2026-09-26", "线索简报_2026-09-26"]


def test_table_links_ignore_url_spelling_but_follow_a_new_domain(tmp_path):
    """定时任务与本机填的表格地址写法略有不同(大小写、末尾斜杠):表格链接与记录 ID 表都不来回改;
    换了域名(迁移主机):下次推送时表格链接与记录 ID 表跟着改成新地址,只改这一列。"""
    prof = load_profile(FX / "profile.yaml")
    st = _store(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://water.example.test", "t", api=api, log=lambda *_: None)
    fp = st.root / "teable_links.json"
    before = fp.read_text(encoding="utf-8")
    api.calls.clear()
    tb.sync(prof, st, TODAY, "HTTPS://Water.Example.Test/", "t", api=api, log=lambda *_: None)
    assert not [c for c in api.calls if c[0] == "update_records"] and fp.read_text(encoding="utf-8") == before
    tb.sync(prof, st, TODAY, "https://water.newcorp.test/", "t", api=api, log=lambda *_: None)
    ups = [f for c in api.calls if c[0] == "update_records" and c[1] == api.table("线索")["id"] for f in c[2]]
    assert ups and all(set(f) == {"表格链接"} and f["表格链接"].startswith("https://water.newcorp.test/base/") for f in ups)
    assert all(r["fields"]["表格链接"].startswith("https://water.newcorp.test/base/") for r in api.rows("线索").values())
    assert tb.load_links(st)["url"] == "https://water.newcorp.test"


def test_report_or_summary_errors_do_not_stop_the_rest(tmp_path):
    """报告表或每日摘要出错:记成警告,线索与台账照样推送,记录 ID 表照样写,跟进记录表照样建。"""
    prof = load_profile(FX / "profile.yaml")
    st = _store(tmp_path)
    api = FakeTeable()
    create = api.create_table

    def failing(base_id, name, fields, views=None):
        if name == "报告":
            raise tb.TeableError("POST /base/…/table/ -> 500")
        return create(base_id, name, fields, views)

    api.create_table = failing
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert r["warnings"][-1].startswith("报告或每日摘要没写成(下次再试)") and len(api.rows("线索")) == 2
    assert set(tb.load_links(st)["leads"]) == set(api.rows("线索")) and "跟进记录" in {t["name"] for t in api._tables.values()}
    assert set(tb.load_links(st)["tables"]) == {"线索", "商机", "跟进记录"}  # 报告、每日摘要没建成:不记
    api.create_table = create
    (st.root / "summary.json").write_text('{"date": "2026-09-28", "source_errors": 5}', encoding="utf-8")  # 摘要写坏了
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert "报告" in {t["name"] for t in api._tables.values()} and r["warnings"][-1].startswith("报告或每日摘要没写成")


def _old_base(api, tracked_rows=("ZZZ-001", "ZZZ-002", "ZZZ-003")):
    """上一版建的库:「项目台账」表(阶段、阶段说明,没有进展等列)、线索表的「台账项目」、跟进记录的「项目」。"""
    base = api.create_base("spc1", "水务项目机会")
    old_p = [{**f, "name": {"台账阶段": "阶段", "台账阶段说明": "阶段说明"}.get(f["name"], f["name"])} for f in tb.project_fields()
             if f["name"] not in ("进展", "当前判断", "最近跟进", "所属线索", "最近截止", "下一步", "下一步到期")]
    pt = api.create_table(base["id"], "项目台账", old_p)
    api.create_records(pt["id"], [{"名称": f"{k} 旧名", "编号": k, "阶段": "采购进行中", "优先级": "A" if k == "ZZZ-001" else None}
                                  for k in tracked_rows])
    lt = api.create_table(base["id"], "线索", [{**f, "name": "台账项目"} if f["name"] == "所属商机" else f for f in tb.lead_fields(pt["id"])])
    api.create_table(base["id"], "跟进记录", [{**f, "name": "项目"} if f["name"] == "商机" else f
                                            for f in tb.follow_fields(lt["id"], pt["id"]) if f["name"] not in ("判断", "判断依据", "进展")])
    return pt, lt


def test_old_project_table_becomes_the_opportunity_table(tmp_path):
    """第一次推送新版:「项目台账」表改名为「商机」(同一张表),旧字段改名(阶段 → 台账阶段、线索表的台账项目 → 所属商机、
    跟进记录的项目 → 商机),不在跟踪的行删掉,在跟踪的行补上进展(新加的列还空着,写系统现值);以后再推送不再改名、删行。"""
    prof = load_profile(FX / "profile.yaml")
    st = _store(tmp_path, tracked=("ZZZ-001",))
    api = FakeTeable()
    pt, lt = _old_base(api)
    tb.append_state(st, [tb._project_line("ZZZ-001", {"优先级": "A", "负责人": None, "内部备注": None}, {"priority": "A", "note": ""})])
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert r["warnings"] == [] and r["projects"] == 1
    assert {t["name"] for t in api._tables.values()} == {"商机", "线索", "跟进记录", "报告", "每日摘要"} and api.table("商机")["id"] == pt["id"]
    pnames = [f["name"] for f in api.fields(pt["id"])]
    assert {"台账阶段", "台账阶段说明", "进展", "当前判断", "下一步"} <= set(pnames) and "阶段" not in pnames
    rows = api.rows("商机")
    assert set(rows) == {"ZZZ-001"} and (rows["ZZZ-001"]["fields"]["进展"], rows["ZZZ-001"]["fields"]["台账阶段"]) == ("跟踪中", "采购进行中")
    assert len([c for c in api.calls if c[0] == "delete_record"]) == 2  # ZZZ-002、ZZZ-003 不在跟踪:删掉
    lnames, fnames = {f["name"] for f in api.fields(lt["id"])}, {f["name"] for f in api.fields(api.table("跟进记录")["id"])}
    assert "所属商机" in lnames and "台账项目" not in lnames and {"商机", "判断", "判断依据", "进展"} <= fnames and "项目" not in fnames
    assert [x["id"] for x in api.rows("线索")["a1b2c3d4e5f60718"]["fields"]["所属商机"]] == [rows["ZZZ-001"]["id"]]
    assert tb.load_links(st)["tables"]["商机"] == pt["id"]
    api.calls.clear()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert not [c for c in api.calls if c[0] in ("rename_table", "rename_field", "delete_record", "create_table", "create_field")]


def test_when_renaming_is_refused_new_table_and_fields_are_made_and_reported(tmp_path):
    """改名做不成(令牌权限、接口):照样按新名字建「商机」表、加字段,提示旧的不再同步、请在表格里删掉;拉回在第一次推送新版之前
    照样从原来的「项目台账」表拉。"""
    prof = load_profile(FX / "profile.yaml")
    st = _store(tmp_path, tracked=("ZZZ-001",))
    api = FakeTeable()
    pt, _ = _old_base(api)
    api.fail_rename = True
    r = tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert [w.split(",")[0] for w in r["warnings"]] == ["表「项目台账」没能改名为「商机」", "线索 的字段「台账项目」没能改名为「所属商机」",
                                                          "跟进记录 的字段「项目」没能改名为「商机」"]
    assert api.table("商机")["id"] != pt["id"] and set(api.rows("商机")) == {"ZZZ-001"}


def test_pull_reads_the_old_project_table_before_the_first_new_push(tmp_path, monkeypatch):
    """定时任务先拉回再推送:新版第一次拉回时表还叫「项目台账」,照样拉(同事改的优先级写进内部叠加)。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    _old_base(api)
    tb.append_state(st, [tb._project_line("ZZZ-002", {"优先级": None, "负责人": None, "内部备注": None}, {"priority": "", "note": ""})])
    api.edit("项目台账", "ZZZ-002", 优先级="B")
    r = _pull(prof, st, api)
    assert r["projects"] == 3 and r["overlay"] == ["内部叠加 ZZZ:ZZZ-002 优先级 B"]


def test_opportunity_progress_and_next_step_are_kept_on_the_opportunity(tmp_path, monkeypatch):
    """商机表里改进展、下一步:记在商机上(操作人按格取自记录历史);清除进展即不再跟踪,下次推送商机表里删掉这一行。
    线索表里在属于商机的线索上改跟踪进展,改的是商机的进展,商机表跟着改;系统里改的进展,表格没人动过的写回商机表。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof, st = _pull_env(tmp_path)
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("商机", "ZZZ-002", user="usrLS", 进展="已投标", 下一步="等开标", 下一步到期="2026-10-20T16:00:00.000Z")
    api.edit("商机", "ZZZ-004", 进展=None)
    r = _pull(prof, st, api)
    ev = [(e["id"], e["kind"], e["value"], e["by"]) for e in _ops(st)]
    assert ev == [("ZZZ-002", "stage", "bid", "teable:李四"), ("ZZZ-002", "next", "等开标", "teable:李四"),
                  ("ZZZ-004", "stage", "", "teable:张三")] and r["conflicts"] == []
    assert next(e for e in st.events() if e["kind"] == "next")["due"] == "2026-10-21"
    assert _pull(prof, st, api)["events"] == 0
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    assert set(api.rows("商机")) == {"ZZZ-001", "ZZZ-002", "ZZZ-003"}  # ZZZ-004 不再跟踪
    api.edit("线索", "a1b2c3d4e5f60718", at="2026-09-29T01:00:00.000Z", 跟踪进展="准备投标")  # a1b2 属于 ZZZ-001
    _pull(prof, st, api)
    last = st.events()[-1]
    assert (last["id"], last["kind"], last["value"], last["note"]) == ("ZZZ-001", "stage", "preparing", "在线索 a1b2c3d4 上改的")
    set_stage(st, "ZZZ-003", "won")  # 系统里(商机页)改的
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    rows = api.rows("商机")
    assert (rows["ZZZ-001"]["fields"]["进展"], rows["ZZZ-003"]["fields"]["进展"]) == ("准备投标", "中标")
    assert _pull(prof, st, api)["events"] == 0  # 写回的不当作表格改动
