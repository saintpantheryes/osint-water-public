"""写入层的客户端(core/tstore.py):分页读、按条件查与计数、批量建与改、附件、修改历史、评论、重试;只动新库;
管理员令牌能建表,机器账号令牌不能。用内存版的 Teable(tests/teable_fake.py),不联网。"""
from __future__ import annotations

import pytest

from core import tstore
from tests.teable_fake import FakeTeable


def _client(fake: FakeTeable, token: str = "admin-token") -> tstore.Client:
    return tstore.Client("https://water.example", token, transport=fake.transport, sleep=lambda s: None)


def _table(api: tstore.Client) -> tuple[str, dict]:
    base = api.ensure_new_base("水务项目机会 新版")
    t = api.create_table(base["id"], "试验", [{"name": "名称", "type": "singleLineText"},
                                              {"name": "状态", "type": "singleSelect", "options": {"choices": [{"name": "待处理"}]}},
                                              {"name": "日期", "type": "date"}])
    return t["id"], {f["name"]: f["id"] for f in api.fields(t["id"])}


def test_only_the_new_base_is_touched():
    fake = FakeTeable()
    api = _client(fake)
    with pytest.raises(tstore.TStoreError, match="只动新库"):
        api.ensure_new_base("水务项目机会")
    assert api.ensure_new_base("水务项目机会 新版", create=False) is None
    b = api.ensure_new_base("水务项目机会 新版")
    assert api.ensure_new_base("水务项目机会 新版")["id"] == b["id"]  # 再找不重建
    assert {x["name"] for x in fake.bases.values()} == {"水务项目机会", "水务项目机会 新版"}


def test_pages_filters_counts_and_batches():
    fake = FakeTeable()
    api = _client(fake)
    tid, f = _table(api)
    rows = [{f["名称"]: f"第{i}条", f["状态"]: "已处理" if i % 2 else "待处理"} for i in range(1001)]
    made = api.create_records(tid, rows)
    assert len(made) == 1001 and sum(1 for c in fake.calls if c == ("POST", f"/api/table/{tid}/record")) == 6  # 每批 200
    got = api.records(tid)
    assert len(got) == 1001 and sum(1 for c in fake.calls if c == ("GET", f"/api/table/{tid}/record")) == 2  # 1000 + 1
    flt = {"conjunction": "and", "filterSet": [{"fieldId": f["状态"], "operator": "is", "value": "已处理"}]}
    assert api.count(tid, flt) == 500 and len(api.records(tid, filter=flt)) == 500
    one = api.records(tid, filter={"conjunction": "and", "filterSet": [{"fieldId": f["名称"], "operator": "is", "value": "第7条"}]},
                      projection=[f["名称"]])
    assert [r["fields"] for r in one] == [{f["名称"]: "第7条"}]
    api.update_records(tid, [(one[0]["id"], {f["日期"]: "2026-10-01"})])
    rec = next(r for r in api.records(tid) if r["id"] == one[0]["id"])
    assert rec["fields"][f["日期"]] == "2026-09-30T16:00:00.000Z"  # 日期按上海当天零点存
    assert api.record_history(tid, rec["id"])["historyList"][0]["fieldId"] == f["日期"]


def test_retries_attachments_comments_and_roles():
    fake = FakeTeable()
    api = _client(fake)
    tid, f = _table(api)
    fake.fail_next = [503, 429]
    rec = api.create_records(tid, [{f["名称"]: "甲"}])[0]  # 两次临时失败后成功
    att = api.create_field(tid, {"name": "文件", "type": "attachment"})
    api.upload_attachment(tid, rec["id"], att["id"], "周报.docx", b"docx")
    assert [a["name"] for a in api.records(tid)[0]["fields"][att["id"]]] == ["周报.docx"]
    api.add_comment(tid, rec["id"], "请看", mention_user_id="usrZS")
    assert fake.comments[0]["content"][0]["children"][1] == {"type": "mention", "value": "usrZS"}
    bot = _client(fake, "bot-token")
    with pytest.raises(tstore.TStoreError) as e:
        bot.create_field(tid, {"name": "多余", "type": "singleLineText"})
    assert e.value.status == 403
    bot.create_records(tid, [{f["名称"]: "机器建的"}])  # 编辑者能写记录
    with pytest.raises(tstore.TStoreError, match="401"):
        _client(fake, "wrong").spaces()


def test_credentials_come_from_env_never_from_files(monkeypatch):
    monkeypatch.delenv("TEABLE_URL", raising=False)
    with pytest.raises(tstore.TStoreError, match="TEABLE_URL"):
        tstore.credentials()
    monkeypatch.setenv("TEABLE_URL", "https://water.example")
    monkeypatch.setenv("TEABLE_TOKEN", "bot")
    monkeypatch.delenv("TEABLE_ADMIN_TOKEN", raising=False)
    assert tstore.credentials() == ("https://water.example", "bot")
    with pytest.raises(tstore.TStoreError, match="TEABLE_ADMIN_TOKEN"):
        tstore.credentials(admin=True, prompt=False)
