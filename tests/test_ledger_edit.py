"""台账更新(界面"更新台账"按钮背后的逻辑):按线索预填、写入与校验、保留注释与写法、来源编号、建议列表的去重、
核对记录,以及把台账改动提交推送到程序仓库。全部离线:台账用测试样例 ZZZ 的副本,git 远端是临时目录里的裸仓库。"""
import shutil
from dataclasses import replace
import subprocess
from datetime import date
from types import SimpleNamespace

import pytest
import yaml

from core import sync
from core.ledger import load_ledger, load_rules
from core.ledger_edit import SRC_GROUP, Update, apply, brief_changes, check_date, grade_of, next_item_id, next_source_id, propose
from core.ledger_auto import (
    MISSING, _name_zh, _segments, create_from_lead, enter_ledger, history, lead_ids, pending, reconcile, should_enter, undo,
)
from core.ledger_auto import run as auto_run
from core.watch.link import build_index, suggest
from core.watch.events import set_stage
from core.watch.outputs import ledger_suggestions, suggestion_pairs
from core.watch.query import set_status
from core.watch.store import Store
from tests.test_watch import FX

TODAY = date(2026, 9, 26)
RULES = load_rules(FX / "rules.yaml")


@pytest.fixture
def led_path(tmp_path):
    d = tmp_path / "ZZZ"
    shutil.copytree(FX / "ZZZ", d)
    return d / "ledger.yaml"


def _sig(sid="a1b2c3d4e5f60718", title="Tender: Construction of irrigation canal", url="https://example.org/tender/9", cat="tender",
         published="2026-09-20", deadlines=(("bid_deadline", "2026-11-30"),), links=(), new=False, project=None):
    return {"id": sid, "title": title, "url": url, "published": published, "first_seen": "2026-09-21T08:00:00+00:00",
            "source": "zz_notices", "source_name": "虚构采购网", "kind": "html_list", "country": "ZZZ",
            "links": [{"item": i, "confidence": "high", "by": "rule"} for i in links],
            "llm": {"relevant": True, "category": cat, "sectors": ["water_supply"], "summary_zh": "虚构公告", "project": project,
                    "funder": "虚构银行", "deadlines": [{"type": t, "date": d, "evidence": "…"} for t, d in deadlines],
                    "ledger_links": [], "new_opportunity": new}}


def _item(path, iid):
    return next(it for it in load_ledger(path, RULES).items if it["id"] == iid)


def test_propose_new_package_milestones_and_stage(led_path):
    upd = propose(_sig(), _item(led_path, "ZZZ-002"), TODAY)
    assert upd.item_id == "ZZZ-002" and upd.stage == "procurement"  # 融资已落实 → 采购进行中
    assert [(m["type"], m["date"], m["status"]) for m in upd.milestones] == [
        ("tender_launch", "2026-09-20", "actual"), ("bid_deadline", "2026-11-30", "planned")]
    assert upd.packages[0]["type"] == "works" and upd.packages[0]["deadline"] == "2026-11-30" and upd.packages[0]["status"] == "open"
    assert upd.source["grade"] == "B" and upd.source["date"] == "2026-09-20" and "a1b2c3d4" in upd.source["note"]
    # 发布日不详时不猜招标发布日,来源日期记"查询"
    upd = propose(_sig(published=None), _item(led_path, "ZZZ-002"), TODAY)
    assert [m["type"] for m in upd.milestones] == ["bid_deadline"] and upd.source["date"] == "2026-09-21 查询"


def test_propose_cites_existing_instead_of_duplicating(led_path):
    # ZZZ-001 已有 2026-11-15 的资格预审截止节点:不再新增,改为在原节点上加引用
    upd = propose(_sig(cat="prequalification", deadlines=(("pq_deadline", "2026-11-15"),)), _item(led_path, "ZZZ-001"), TODAY)
    assert not [m for m in upd.milestones if m["type"] == "pq_deadline"]
    assert {"kind": "milestone", "index": 1, "match": {"type": "pq_deadline", "date": "2026-11-15"}, "set": {}, "cite": True} in upd.edits


def test_apply_writes_and_keeps_file_style(led_path):
    src_path = led_path.with_name("sources.yaml")
    before = led_path.read_text(encoding="utf-8")
    upd = propose(_sig(), _item(led_path, "ZZZ-002"), TODAY)
    upd.ids = {"项目号": "ZZ-2026-01"}
    upd.note = "招标文件免费下载"
    res = apply(led_path, upd, TODAY, RULES)
    assert res.errors == [] and res.source_id == "S501" and res.item_id == "ZZZ-002"
    assert "阶段 financing_signed → procurement" in res.changes and any(c.startswith("新增标段") for c in res.changes)
    after = led_path.read_text(encoding="utf-8")
    assert after.startswith("# 虚构示例台账")  # 注释保留
    # 没改的项目一字不动(包括流式写法与缩进)
    old_1 = before[before.index("  - id: ZZZ-001"):before.index("  - id: ZZZ-002")]
    assert old_1 in after
    led = load_ledger(led_path, RULES)
    it = next(x for x in led.items if x["id"] == "ZZZ-002")
    assert it["stage"] == "procurement" and it["ids"] == {"项目号": "ZZ-2026-01"} and led.as_of == TODAY
    assert it["milestones"][-1] == {"type": "bid_deadline", "date": date(2026, 11, 30), "status": "planned", "src": ["S501"]}
    pk = it["procurement"]["packages"][0]
    assert pk["deadline"] == date(2026, 11, 30) and pk["src"] == ["S501"] and "deadline_type" not in pk  # 投标截止是默认值,不写
    assert it["notes"] == [{"text": "招标文件免费下载", "src": ["S501"]}]
    s = led.sources["S501"]
    assert s["via"] == "primary_read" and s["url"] == "https://example.org/tender/9" and s["grade"] == "B"
    assert f"# {SRC_GROUP}" in src_path.read_text(encoding="utf-8")
    # 第二次登记顺延编号;分组注释只加一次
    res = apply(led_path, Update(item_id="ZZZ-003", source={"title": "另一条", "url": "https://example.org/n/3"}, note="x"), TODAY, RULES)
    assert res.source_id == "S502" and src_path.read_text(encoding="utf-8").count(SRC_GROUP) == 1


def test_apply_edits_existing_and_guards_against_stale_dialog(led_path):
    upd = Update(item_id="ZZZ-001", source={"title": "延期公告", "url": "https://example.org/ext"},
                 edits=[{"kind": "package", "index": 0, "match": {"name": "污水厂设计施工"},
                         "set": {"status": "open", "deadline": "2026-12-20", "deadline_type": "pq_deadline"}, "cite": False},
                        {"kind": "milestone", "index": 0, "match": {"type": "gpn", "date": "2026-04-01"}, "set": {}, "cite": True}])
    res = apply(led_path, upd, TODAY, RULES)
    assert res.errors == []
    it = _item(led_path, "ZZZ-001")
    pk = it["procurement"]["packages"][0]
    assert (pk["status"], pk["deadline"], pk["deadline_type"], pk["src"]) == ("open", date(2026, 12, 20), "pq_deadline", ["S01", "S501"])
    assert list(pk)[-1] == "src"  # 新加的字段插在 src 前面
    assert it["milestones"][0]["src"] == ["S01", "S501"]
    assert any("status planned → open" in c for c in res.changes) and any("加引用 S501" in c for c in res.changes)
    # 弹窗打开后台账被改过(match 对不上):报错,不按位置误改
    before = led_path.read_text(encoding="utf-8")
    upd.edits = [{"kind": "package", "index": 0, "match": {"name": "别的标段"}, "set": {"status": "closed"}}]
    assert "被改过" in apply(led_path, upd, TODAY, RULES).errors[0]
    assert led_path.read_text(encoding="utf-8") == before


def test_apply_validation_blocks_write_and_preview_does_not_write(led_path):
    src_path = led_path.with_name("sources.yaml")
    before, before_src = led_path.read_text(encoding="utf-8"), src_path.read_text(encoding="utf-8")
    bad = Update(item_id="ZZZ-001", source={"title": "t", "url": "https://example.org/b"},
                 milestones=[{"type": "bid_deadline", "date": "2026-13", "status": "planned"}])
    res = apply(led_path, bad, TODAY, RULES)
    assert res.errors and "日期" in res.errors[0] and res.diff  # 校验不过:给出错误与改动预览,不写
    assert led_path.read_text(encoding="utf-8") == before and src_path.read_text(encoding="utf-8") == before_src
    ok = Update(item_id="ZZZ-001", source={"title": "t", "url": "https://example.org/b"},
                milestones=[{"type": "bid_deadline", "date": "2026-12", "status": "planned"}])
    res = apply(led_path, ok, TODAY, RULES, write=False)
    assert res.errors == [] and "+      - {type: bid_deadline, date: 2026-12, status: planned, src: [S501]}" in res.diff
    assert led_path.read_text(encoding="utf-8") == before
    assert apply(led_path, Update(item_id="ZZZ-009", note="x"), TODAY, RULES).errors == ["台账里没有 ZZZ-009"]
    assert "没有选择" in apply(led_path, Update(item_id="ZZZ-001", source={"title": "t"}), TODAY, RULES).errors[0]
    assert check_date("2027-H2") is None and check_date("2026/10/12")


def test_apply_reuses_registered_url(led_path):
    upd = Update(item_id="ZZZ-004", source={"title": "同一页", "url": "https://example.org/project/1/"}, note="补充")
    res = apply(led_path, upd, TODAY, RULES)
    assert res.errors == [] and res.source_id == "S01" and "引用已登记的来源 S01" in res.changes
    assert "S501" not in load_ledger(led_path, RULES).sources
    # 已登记的来源再"加引用"到已经引用它的节点:什么都不改,明说不用更新
    before = led_path.read_text(encoding="utf-8")
    cite = Update(item_id="ZZZ-001", source={"url": "https://example.org/project/1"},
                  edits=[{"kind": "milestone", "index": 0, "match": {"type": "gpn"}, "set": {}, "cite": True}])
    assert apply(led_path, cite, TODAY, RULES).errors == ["台账里已有这些内容,并且已经引用这条来源(S01),不用更新"]
    assert led_path.read_text(encoding="utf-8") == before


def test_apply_new_item(led_path):
    sig = _sig(sid="ffee0011", title="Water supply for Wu city", cat="gpn", deadlines=(), new=True, project="戊市供水")
    upd = propose(sig, None, TODAY)
    assert upd.new_item["name_zh"] == "戊市供水" and upd.new_item["stage"] == "procurement"
    assert "新项目至少选一个细分领域" in apply(led_path, upd, TODAY, RULES).errors
    upd.new_item["segments"] = ["water_supply"]
    res = apply(led_path, upd, TODAY, RULES)
    assert res.errors == [] and res.item_id == "ZZZ-005"
    it = _item(led_path, "ZZZ-005")
    assert it["name_zh"] == "戊市供水" and it["verification"]["status"] == "unverified"
    assert it["financing"] == [{"funder": "虚构银行", "instrument": "unknown", "status": "reported", "src": ["S501"]}]
    assert it["milestones"] == [{"type": "gpn", "date": date(2026, 9, 20), "status": "actual", "src": ["S501"]}]
    text = led_path.read_text(encoding="utf-8")
    assert "界面新建" in text and "&id" not in text and "*id" not in text  # 当天日期写了两处(as_of、checked_at),不用锚点


def test_ids_and_grades():
    assert next_source_id({"S01": {}, "S419": {}, "R203": {}}) == "S501"
    assert next_source_id({"S501": {}, "S502": {}}) == "S503"
    assert next_item_id([{"id": "AZE-024"}, {"id": "AZE-003"}], "AZE") == "AZE-025"
    assert [grade_of(u, k) for u, k in (("https://www.isdb.org/t/1", "html_list"), ("https://etender.gov.az/x", "html_list"),
                                        ("https://news.az/x", "rss"), ("https://example.org/x", None),
                                        ("https://gov.az.example.com/x", None))] == ["A", "A", "C", "B", "B"]


def test_suggestions_skip_registered_and_reviewed(led_path):
    led = load_ledger(led_path, RULES)
    registered = _sig(sid="11111111aa", url="https://example.org/project/1", links=("ZZZ-001",))  # 网址已是来源 S01
    fresh = _sig(sid="22222222bb", url="https://example.org/tender/new", links=("ZZZ-001",))
    newop = _sig(sid="33333333cc", url="https://example.org/new-op", new=True)
    by_item, new_ops = suggestion_pairs([registered, fresh, newop], led)
    assert [s["id"] for s in by_item["ZZZ-001"]] == ["22222222bb"] and [s["id"] for s in new_ops] == ["33333333cc"]
    by_item, new_ops = suggestion_pairs([registered, fresh, newop], led, {"22222222", "33333333"})
    assert not by_item and not new_ops
    assert ledger_suggestions([registered], led) == ["无。"]


def test_store_ledger_reviewed(tmp_path):
    st = Store(tmp_path)
    st.mark_ledger_reviewed("abcdef0123456789", "skipped", TODAY)
    st.mark_ledger_reviewed("12345678ffff", "updated", TODAY, item="AZE-020", source="S501")
    assert st.ledger_reviewed() == {"abcdef01": {"action": "skipped", "at": "2026-09-26"},
                                    "12345678": {"action": "updated", "at": "2026-09-26", "item": "AZE-020", "source": "S501"}}
    st.mark_ledger_reviewed("abcdef01", None)  # 撤销
    assert list(st.ledger_reviewed()) == ["12345678"]
    assert (tmp_path / "watch" / "ledger_reviewed.yaml").read_text(encoding="utf-8").startswith("# 台账核对记录")


# ---------------------------------------------------------------- 提交到程序仓库


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def no_git_identity(monkeypatch, tmp_path):
    """像没配置过 git 用户名和邮箱的电脑(以及 GitHub 的运行环境):不读全局配置,也不许自动猜身份。"""
    for k in ("EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")


@pytest.fixture
def repo(tmp_path, no_git_identity):
    """程序仓库的本机检出 + 作远端的裸仓库。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    shutil.copytree(FX / "ZZZ", work / "countries" / "ZZZ")
    (work / "README.md").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD:main")
    return work, remote


def test_commit_program_commits_only_ledger_files_and_pushes(repo, tmp_path):
    work, remote = repo
    led = work / "countries" / "ZZZ" / "ledger.yaml"
    assert sync.toplevel(led) == work.resolve() and sync.toplevel(tmp_path / "nowhere" / "x.yaml") is None
    (work / "README.md").write_text("本机别的改动\n", encoding="utf-8")
    res = apply(led, Update(item_id="ZZZ-001", source={"title": "t", "url": "https://example.org/c"}, note="n"), TODAY, RULES)
    assert res.errors == []
    r = sync.commit_program(work, [led, led.with_name("sources.yaml")], "台账:ZZZ-001 新增备注")
    assert r.ok, r.output
    assert _git(remote, "log", "-1", "--format=%s", "main") == "台账:ZZZ-001 新增备注"
    changed = _git(remote, "show", "--name-only", "--format=", "main").splitlines()
    assert sorted(changed) == ["countries/ZZZ/ledger.yaml", "countries/ZZZ/sources.yaml"]  # README 的改动不带上
    assert sync.ahead(work) == 0


def test_update_program_pushes_pending_ledger_commit(repo, tmp_path):
    work, remote = repo
    # 另一台电脑先推了一个提交;本机的台账提交没推出去(离线)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "NEW.md").write_text("程序更新\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "程序更新")
    _git(other, "push", "-q", "origin", "HEAD:main")
    led = work / "countries" / "ZZZ" / "ledger.yaml"
    apply(led, Update(item_id="ZZZ-001", source={"title": "t", "url": "https://example.org/d"}, note="n"), TODAY, RULES)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "台账")
    assert sync.ahead(work) == 1
    r = sync.update_program(work)  # 直接快进不行(两边都有新提交):变基后推送
    assert r.ok, r.output
    assert (work / "NEW.md").exists() and sync.ahead(work) == 0
    assert _git(remote, "log", "-1", "--format=%s", "main") == "台账"


def test_data_commit_push_rebases_without_git_identity(tmp_path, no_git_identity):
    """data 分支:别处先推了(定时任务),本机的界面改动变基后推送;本机没配置 git 身份也行。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "data", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(local))
    (seed / "job.md").write_text("定时任务写的\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "watch")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    (local / "triage.yaml").write_text("abcdef01: {status: done}\n", encoding="utf-8")
    r = sync.commit_push(local, "界面:标记")
    assert r.ok, r.output
    assert _git(remote, "log", "-2", "--format=%s", "data").splitlines() == ["界面:标记", "watch"]


def test_data_events_from_two_computers_both_kept(tmp_path, no_git_identity):
    """两台电脑都在操作记录末尾追加了记录:后推的一方变基时两边的记录都保留(不按 -X theirs 只留本机的)。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "data", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "watch").mkdir()
    (seed / "watch" / "events.jsonl").write_text('{"id": "aaaa0000", "value": "base"}\n', encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(d))
    for d, v in ((a, "from-a"), (b, "from-b")):
        with (d / "watch" / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(f'{{"id": "aaaa0000", "value": "{v}"}}\n')
    assert sync.commit_push(a, "界面:A").ok
    r = sync.commit_push(b, "界面:B")
    assert r.ok, r.output
    _git(seed, "pull", "-q", "origin", "data")
    lines = (seed / "watch" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith('"base"}') and sorted(lines[1:]) == ['{"id": "aaaa0000", "value": "from-a"}',
                                                                 '{"id": "aaaa0000", "value": "from-b"}']


def _data_remote(tmp_path):
    """data 分支的远端(裸仓库)+ 一次初始提交(已有一行操作记录与一条自动写入记录)。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "data", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "watch").mkdir()
    for name in ("events.jsonl", "ledger_auto.jsonl"):
        (seed / "watch" / name).write_text('{"id": "aaaa0000", "v": "base"}\n', encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    return remote, seed


def _append(d, v):
    for name in ("events.jsonl", "ledger_auto.jsonl"):
        with (d / "watch" / name).open("a", encoding="utf-8") as f:
            f.write(f'{{"id": "aaaa0000", "v": "{v}"}}\n')


def test_bot_commits_data_twice_and_append_only_files_merge(tmp_path, no_git_identity):
    """定时任务分两次提交 data 分支(监测后、自动入账与同步后);其间本机界面也推了:两个只追加的文件两边的行都保留;
    提交时 data 分支根目录有了 .gitattributes(union),之后不经本机设置的普通克隆同时追加、先后推送也不冲突。"""
    remote, seed = _data_remote(tmp_path)
    bot, ui = tmp_path / "bot", tmp_path / "ui"
    for d in (bot, ui):
        _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(d))
    _append(bot, "bot-watch")
    r1 = sync.commit_data(bot, "watch: 2026-09-28")
    assert r1.ok, r1.output
    assert sync.commit_data(bot, "watch: 2026-09-28 台账自动更新与同步").output == "无变化"
    _append(ui, "ui-mark")
    assert sync.commit_push(ui, "界面:标记").ok  # 本机界面在两次提交之间推了
    _append(bot, "bot-auto")
    r2 = sync.commit_data(bot, "watch: 2026-09-28 台账自动更新与同步")
    assert r2.ok, r2.output
    assert _git(remote, "log", "-3", "--format=%s", "data").splitlines() == [
        "watch: 2026-09-28 台账自动更新与同步", "界面:标记", "watch: 2026-09-28"]
    assert _git(remote, "show", "data:.gitattributes").splitlines() == [
        "watch/events.jsonl merge=union", "watch/ledger_auto.jsonl merge=union", "watch/teable_state.jsonl merge=union"]
    _git(seed, "pull", "-q", "origin", "data")
    for name in ("events.jsonl", "ledger_auto.jsonl"):
        vals = [line.split('"v": "')[1][:-2] for line in (seed / "watch" / name).read_text(encoding="utf-8").splitlines()]
        assert vals[0] == "base" and sorted(vals[1:]) == ["bot-auto", "bot-watch", "ui-mark"], name
    a, b = tmp_path / "a", tmp_path / "b"  # 普通克隆:没有本机 .git/info/attributes,只靠分支里的 .gitattributes
    for d in (a, b):
        _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(d))
    for d, v in ((a, "from-a"), (b, "from-b")):
        _append(d, v)
        _git(d, "add", "-A")
        _git(d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", v)
    _git(a, "push", "-q", "origin", "HEAD:data")
    _git(b, "-c", "user.name=t", "-c", "user.email=t@t", "pull", "-q", "--rebase", "origin", "data")
    _git(b, "push", "-q", "origin", "HEAD:data")
    _git(seed, "pull", "-q", "origin", "data")
    assert {"from-a", "from-b"} <= {line.split('"v": "')[1][:-2]
                                    for line in (seed / "watch" / "events.jsonl").read_text(encoding="utf-8").splitlines()}


def test_commit_data_pushes_a_commit_left_unpushed_by_the_first_step(tmp_path, no_git_identity):
    """第一次提交推送失败(提交留在检出里),第二次即使没有新改动也把它推上去。"""
    remote, _ = _data_remote(tmp_path)
    bot = tmp_path / "bot"
    _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(bot))
    _append(bot, "bot-watch")
    assert not sync.commit_data(bot, "watch: 1", remote=str(tmp_path / "gone.git")).ok  # 远端连不上
    r = sync.commit_data(bot, "watch: 1 台账自动更新与同步", remote=str(remote))
    assert r.ok and r.output != "无变化", r.output
    assert _git(remote, "log", "-1", "--format=%s", "data") == "watch: 1"
    assert sync.commit_data(bot, "watch: 1 再一次", remote=str(remote)).output == "无变化"


def test_keyed_yaml_files_merge_by_lead_not_by_line(tmp_path, no_git_identity):
    """处理状态、台账核对两个 YAML 文件按线索号合并:定时任务(表格拉回)与本机界面改了相邻的两条线索,两条都保留;
    同一条两边都改了,取正在合入的一方;文件开头的说明保留。"""
    from core.watch.store import REVIEWED_TEMPLATE, TRIAGE_TEMPLATE

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "data", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "watch").mkdir()
    tri = seed / "watch" / "triage.yaml"
    rv = seed / "watch" / "ledger_reviewed.yaml"
    tri.write_text(TRIAGE_TEMPLATE + "\n'40000000': {status: done}\n'70000000': {status: done}\n", encoding="utf-8")
    rv.write_text(REVIEWED_TEMPLATE + "\n'40000000': {action: skipped, at: '2026-09-27'}\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    bot, ui = tmp_path / "bot", tmp_path / "ui"
    for d in (bot, ui):
        _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(d))
    st_ui, st_bot = Store(ui), Store(bot)
    set_status(st_ui, ["60000000aaaa"], "ignore")  # 本机界面:忽略一条
    set_status(st_ui, ["70000000aaaa"], "ignore", "本机")
    st_ui.mark_ledger_reviewed("60000000aaaa", "skipped", TODAY)
    assert sync.commit_push(ui, "界面:忽略").ok
    set_status(st_bot, ["50000000aaaa"], "done", "表格")  # 定时任务(表格拉回):相邻的另一条,和本机也改了的那条
    set_status(st_bot, ["70000000aaaa"], "done", "表格")
    st_bot.mark_ledger_reviewed("50000000aaaa", "skipped", TODAY, by="system")
    r = sync.commit_data(bot, "watch: 拉回")
    assert r.ok, r.output
    _git(seed, "pull", "-q", "origin", "data")
    merged = Store(seed).triage()
    assert merged == {"40000000": {"status": "done"}, "50000000": {"status": "done", "note": "表格"},
                      "60000000": {"status": "ignore"}, "70000000": {"status": "done", "note": "表格"}}
    assert set(Store(seed).ledger_reviewed()) == {"40000000", "50000000", "60000000"}
    assert tri.read_text(encoding="utf-8").startswith(TRIAGE_TEMPLATE) and rv.read_text(encoding="utf-8").startswith(REVIEWED_TEMPLATE)


def test_table_state_survives_a_local_pull_left_uncommitted(tmp_path, no_git_identity):
    """本机跑了拉回、同步状态没提交就打开界面拉取,定时任务也写了:只追加的状态文件两边的行都保留,不留冲突标记。"""
    remote, seed = _data_remote(tmp_path)
    (seed / "watch" / "teable_state.jsonl").write_text('{"at": "2026-09-27T00:00:00.000+00:00", "k": "a", "t": "lead", "v": {}}\n',
                                                         encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "state")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    mac = tmp_path / "mac"
    _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(mac))
    with (mac / "watch" / "teable_state.jsonl").open("a", encoding="utf-8") as f:  # 本机拉回,没提交
        f.write('{"at": "2026-09-28T10:00:00.000+00:00", "k": "b", "t": "lead", "v": {}}\n')
    with (seed / "watch" / "teable_state.jsonl").open("a", encoding="utf-8") as f:  # 定时任务
        f.write('{"at": "2026-09-29T00:31:00.000+00:00", "k": "c", "t": "lead", "v": {}}\n')
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "watch")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    assert sync.pull(mac).ok
    text = (mac / "watch" / "teable_state.jsonl").read_text(encoding="utf-8")
    assert "<<<<<<<" not in text and "=======" not in text
    from core.teable import load_state
    assert set(load_state(Store(mac))["leads"]) == {"a", "b", "c"}


def test_table_pull_rolls_back_an_unpushed_new_project_and_retries(repo, tmp_path, monkeypatch):
    """定时任务里跟踪即入账推送失败:退回那次提交、不记核对与操作记录,下次拉回再建;推上去以后记下,再拉不重复建。"""
    from core import teable as tb
    from core.profile import load_profile
    from tests.test_teable import FakeTeable
    from tests.test_teable import _sig as table_sig

    monkeypatch.delenv("OPERATOR", raising=False)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    work, remote = repo
    base = load_profile(FX / "profile.yaml")
    led = work / "countries" / "ZZZ" / "ledger.yaml"
    prof = replace(base, countries=(replace(base.countries[0], ledger=led, overlay=None),), teable={"base": "水务项目机会", "pull": True})
    st = Store(tmp_path / "data")
    sid = "c0ffee0011223344"
    st.append_signals([table_sig(sid, "Travaux de la STEP de Wu")])
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("线索", sid, 跟踪进展="跟踪中")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # 推不上去
    r = tb.pull(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None, identity=sync.BOT_ID)
    assert "已退回,下次再建" in r["entered"][0] and _git(work, "rev-parse", "HEAD") == head
    assert "ZZZ-005" not in {it["id"] for it in load_ledger(led, RULES).items} and not _git(work, "status", "--porcelain")
    assert [e["kind"] for e in st.events()] == ["stage"] and st.ledger_reviewed() == {}
    assert tb.load_state(st)["leads"][sid]["enter"] == "pending"
    _git(work, "remote", "set-url", "origin", str(remote))
    lead = api.rows("线索")[sid]  # 当天的推送改过这条(系统写的列):最后修改人成了令牌账号,入账仍记设进展的人
    api.update_records(api.table("线索")["id"], [(lead["id"], {"摘要": "推送时更新的摘要"})])
    r = tb.pull(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None, identity=sync.BOT_ID)
    assert "已推送到 GitHub" in r["entered"][0] and "ZZZ-005" in _git(remote, "show", "main:countries/ZZZ/ledger.yaml")
    assert [(e["value"], e["by"]) for e in st.events()] == [("tracking", "teable:张三"), ("created", "teable:张三")]
    assert "enter" not in tb.load_state(st)["leads"][sid]
    assert tb.pull(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)["entered"] == []


def test_table_pull_project_columns_not_written_are_pulled_again(repo, tmp_path, monkeypatch):
    """商机表里改的优先级:内部叠加写入后校验不过的不写、推送失败的提示原因——都不记下,下次拉回再写;
    推上去以后记下,再拉不重复写。"""
    from core import teable as tb
    from core.profile import load_profile
    from tests.test_teable import FakeTeable

    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    work, remote = repo
    ov = work / "private" / "overlay" / "ZZZ.yaml"
    ov.parent.mkdir(parents=True)
    good = (FX / "overlay.yaml").read_text(encoding="utf-8")

    def commit_overlay(text: str) -> None:
        ov.write_text(text, encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "overlay")
        _git(work, "push", "-q", "origin", "HEAD:main")

    commit_overlay(good.replace("overlay@1", "overlay@0"))  # 有人把 schema 改坏了
    base = load_profile(FX / "profile.yaml")
    prof = replace(base, countries=(replace(base.countries[0], ledger=work / "countries" / "ZZZ" / "ledger.yaml", overlay=ov),),
                   teable={"base": "水务项目机会", "pull": True})
    st = Store(tmp_path / "data")
    set_stage(st, "ZZZ-002", "tracking")  # 在跟踪的商机才在商机表里
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("商机", "ZZZ-002", 优先级="B")

    def pull() -> dict:
        return tb.pull(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None, identity=sync.BOT_ID)

    r = pull()
    assert r["overlay"] == [] and "写入后校验不通过" in r["warnings"][0] and "priority: B" not in ov.read_text(encoding="utf-8")
    assert tb.load_state(st)["projects"]["ZZZ-002"]["v"]["优先级"] is None
    commit_overlay(good)
    _git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # 推不上去
    r = pull()
    assert r["overlay"] == [] and "提交或推送失败(下次拉回再试)" in r["warnings"][0]
    assert tb.load_state(st)["projects"]["ZZZ-002"]["v"]["优先级"] is None
    _git(work, "remote", "set-url", "origin", str(remote))
    assert pull()["overlay"] == ["内部叠加 ZZZ:ZZZ-002 优先级 B"]
    assert yaml.safe_load(_git(remote, "show", "main:private/overlay/ZZZ.yaml"))["items"]["ZZZ-002"] == {"priority": "B"}
    assert tb.load_state(st)["projects"]["ZZZ-002"]["v"]["优先级"] == "B" and pull()["overlay"] == []


def test_reconcile_restores_records_of_auto_writes_whose_data_commit_failed(repo, tmp_path, monkeypatch):
    """对账(F26):自动写入推上了 main,当天 data 分支没提交成功,记录与操作记录都丢了——下次运行开始时按提交补记为待过目
    (状态"记录缺失",新增了什么从提交前后的台账算出,撤销照样按内容删);再对账不重复补;当时的记录后来又合过来时只列一条;
    撤销过的(来源已不在台账里)不补。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    work, remote = repo
    led = work / "countries" / "ZZZ" / "ledger.yaml"
    prof = SimpleNamespace(rules=str(FX / "rules.yaml"), countries=[SimpleNamespace(iso3="ZZZ", ledger=str(led))])
    st = Store(tmp_path / "data")
    orig2 = _item(led, "ZZZ-002")
    good = _sig(sid="aaaa1111bbbb2222", url="https://example.org/tender/auto1", links=("ZZZ-002",))
    later = {**_sig(sid="5555eeee6666ffff", url="https://example.org/tender/auto2", links=("ZZZ-003",), deadlines=(("bid_deadline", "2027-03-10"),)),
             "first_seen": "2026-09-22T08:00:00+00:00"}
    st.append_signals([good, later])
    done, warns = auto_run(prof, st, TODAY, push=True)
    assert warns == [] and len(done) == 2 and reconcile(prof, st, RULES) == []  # 记录都在:不补
    lost = (st.root / "ledger_auto.jsonl").read_text(encoding="utf-8")
    (st.root / "ledger_auto.jsonl").unlink()  # 那天 data 分支没提交成功
    (st.root / "events.jsonl").unlink()
    got = reconcile(prof, st, RULES)
    assert [(r["id"], r["item"], r["source"], r["sha"], r["status"]) for r in got] == [
        (d["id"], d["item"], d["source"], d["sha"], MISSING) for d in reversed(done)]  # 新的提交在前
    for r in got:
        d = next(x for x in done if x["id"] == r["id"])
        assert {k: sorted(map(tuple, r["added"][k])) for k in ("milestones", "packages")} == \
               {k: sorted(map(tuple, d["added"][k])) for k in ("milestones", "packages")} and r["added"]["stage"] == d["added"]["stage"]
        assert r["title"] == d["title"] and r["url"] == d["url"] and r["changes"]
    assert [(e["value"], e["by"]) for e in st.events()] == [("auto", "system")] * 2 and MISSING in st.events()[0]["note"]
    assert reconcile(prof, st, RULES) == [] and len(history(st)) == 2  # 再对账不重复补
    assert [r["status"] for r in pending(st)] == [MISSING, MISSING]
    with (st.root / "ledger_auto.jsonl").open("a", encoding="utf-8") as f:  # 当时的记录后来又合过来了
        f.write(lost)
    assert [r.get("status") for r in pending(st)] == [None, None]
    rec = next(r for r in history(st) if r["item"] == "ZZZ-002" and r.get("status") == MISSING)
    assert undo(st, rec, led, TODAY, RULES).ok and _item(led, "ZZZ-002") == orig2  # 按补记的"新增了什么"撤销干净
    (st.root / "ledger_auto.jsonl").unlink()
    assert [r["item"] for r in reconcile(prof, st, RULES)] == ["ZZZ-003"]  # 撤销过的不补


def test_table_pull_commits_new_project_and_overlay_to_main_as_bot(repo, tmp_path, monkeypatch):
    """表格拉回(定时任务):表里设了跟踪进展的线索跟踪即入账,商机表里改的优先级写进内部叠加——各一次提交,用系统身份推送到 main。"""
    from core import teable as tb
    from core.profile import load_profile
    from tests.test_teable import FakeTeable
    from tests.test_teable import _sig as table_sig

    monkeypatch.delenv("OPERATOR", raising=False)
    work, remote = repo
    ov = work / "private" / "overlay" / "ZZZ.yaml"
    ov.parent.mkdir(parents=True)
    shutil.copy(FX / "overlay.yaml", ov)
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "overlay")
    _git(work, "push", "-q", "origin", "HEAD:main")
    base = load_profile(FX / "profile.yaml")
    c = replace(base.countries[0], ledger=work / "countries" / "ZZZ" / "ledger.yaml", overlay=ov)
    prof = replace(base, countries=(c,), teable={"base": "水务项目机会", "pull": True})
    st = Store(tmp_path / "data")
    st.append_signals([table_sig("c0ffee0011223344", "Travaux de la STEP de Wu")])
    set_stage(st, "ZZZ-001", "tracking")  # 在跟踪的商机才在商机表里
    api = FakeTeable()
    tb.sync(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None)
    api.edit("线索", "c0ffee0011223344", 跟踪进展="跟踪中")
    api.edit("商机", "ZZZ-001", 优先级="B")
    r = tb.pull(prof, st, TODAY, "https://x", "t", api=api, log=lambda *_: None, identity=sync.BOT_ID)
    assert r["warnings"] == [] and "已推送到 GitHub" in r["entered"][0]
    log = _git(remote, "log", "-2", "--format=%an|%s", "main").splitlines()
    assert log[0].startswith("osint-water-bot|内部叠加:ZZZ") and log[1].startswith("osint-water-bot|台账:新建项目 ZZZ-005")
    assert "priority: B" in _git(remote, "show", "main:private/overlay/ZZZ.yaml") and sync.ahead(work) == 0


def test_auto_ledger_writes_reliable_links_commits_pushes_and_undo(repo, tmp_path):
    """挂接可靠、模型判为相关的线索自动写进台账:来源标"系统自动写入";每条线索一次提交并推送;记录与操作记录都有;
    名称相近(○)、模型没判相关、人标了"不用更新"的不写;没有新内容可写的记为"不用更新"(系统核对);再跑一遍不重复写;
    撤销按内容删掉那次写入的(先写的也能撤,后写的挨着同一处不冲突),线索以后不再自动写。"""
    work, remote = repo
    led = work / "countries" / "ZZZ" / "ledger.yaml"
    prof = SimpleNamespace(rules=str(FX / "rules.yaml"), countries=[SimpleNamespace(iso3="ZZZ", ledger=str(led))])
    st = Store(tmp_path / "data")
    orig2, orig3 = _item(led, "ZZZ-002"), _item(led, "ZZZ-003")
    good = _sig(sid="aaaa1111bbbb2222", url="https://example.org/tender/auto1", links=("ZZZ-002",))
    later = {**_sig(sid="5555eeee6666ffff", url="https://example.org/tender/auto2", links=("ZZZ-003",), deadlines=(("bid_deadline", "2027-03-10"),)),
             "first_seen": "2026-09-22T08:00:00+00:00"}
    weak = {**_sig(sid="cccc3333dddd4444", url="https://example.org/tender/weak"),
            "links": [{"item": "ZZZ-002", "confidence": "medium", "by": "rule"}]}
    notrel = _sig(sid="eeee5555ffff6666", url="https://example.org/tender/nr", links=("ZZZ-002",))
    notrel["llm"]["relevant"] = False
    skipped = _sig(sid="1111aaaa2222bbbb", url="https://example.org/tender/sk", links=("ZZZ-002",))
    news = _sig(sid="3333cccc4444dddd", url="https://example.org/news/1", cat="project_news", published=None, deadlines=(),
                links=("ZZZ-002",))
    st.append_signals([good, later, weak, notrel, skipped, news])
    st.mark_ledger_reviewed(skipped["id"], "skipped", TODAY)
    done, warns = auto_run(prof, st, TODAY, push=True)
    assert warns == [] and [d["id"] for d in done] == [good["id"], later["id"]]
    assert st.ledger_reviewed()["3333cccc"]["action"] == "skipped" and "系统核对" in st.ledger_reviewed()["3333cccc"]["note"]
    src_id = done[0]["source"]
    srcs = yaml.safe_load(led.with_name("sources.yaml").read_text(encoding="utf-8"))["sources"]
    assert srcs[src_id]["via"] == "auto_extract" and "aaaa1111" in srcs[src_id]["note"]
    assert _git(remote, "log", "-1", "--format=%s", "main") == done[1]["message"] and len(done[0]["sha"]) == 40
    item = _item(led, "ZZZ-002")
    assert item["stage"] == "procurement" and ("bid_deadline", "2026-11-30") in [(m["type"], str(m["date"])) for m in item["milestones"]]
    assert _item(led, "ZZZ-003")["milestones"][0]["src"] == [done[1]["source"]]  # 已有的节点只加引用
    assert [e["value"] for e in st.events() if e["kind"] == "ledger"] == ["skipped", "skipped", "auto", "auto"]
    assert [e["by"] for e in st.events() if e["kind"] == "ledger"][1:] == ["system"] * 3  # 系统核对与自动写入记为系统
    assert [r["id"] for r in pending(st)] == [later["id"], good["id"]]
    assert auto_run(prof, st, TODAY, push=True)[0] == []  # 原文网址已登记为来源:不重复写
    first = next(r for r in pending(st) if r["id"] == good["id"])
    r = undo(st, first, led, TODAY, RULES)  # 先写的这条:后一条的来源挨着它登记,按提交还原会冲突,按内容撤销不会
    assert r.ok, r.output
    assert _item(led, "ZZZ-002") == orig2  # 节点、标段、阶段都回到写入之前
    srcs = yaml.safe_load(led.with_name("sources.yaml").read_text(encoding="utf-8"))["sources"]
    assert src_id not in srcs and done[1]["source"] in srcs and _item(led, "ZZZ-003") != orig3
    assert _git(remote, "log", "-1", "--format=%s", "main").startswith("撤销台账自动更新:ZZZ-002")
    assert [x["id"] for x in pending(st)] == [later["id"]]
    assert "删去来源" in undo(st, pending(st)[0], led, TODAY, RULES).output and _item(led, "ZZZ-003") == orig3
    assert auto_run(prof, st, TODAY, push=True)[0] == []  # 撤销过的线索记为不用更新,不再自动写


def _portal_sig(**kw):
    """像摩洛哥公共采购门户的线索:门户公告号在网址里,采购方、地点、预估金额、招标编号在结构化字段里。"""
    sig = _sig(sid="9999aaaa8888bbbb", title="Travaux de réalisation de la STEP de la ville de Wu",
               url="https://www.marchespublics.gov.ma/index.php?page=entreprise.EntrepriseDetailConsultation&refConsultation=1234567&orgAcronyme=x1y",
               cat="tender")
    sig.update(source="ma_pmmp", kind="atexo_search",
               fields={"reference": "12/2026", "buyer": "SOCIETE REGIONALE MULTISERVICES WU", "place": "WU", "estimate": "48,516,000 MAD"})
    sig["llm"].update(sectors=["wastewater"], summary_zh="ZZZ戊市多服务公司招标建设戊市活性污泥污水处理厂,投标截止2026-11-30。")
    for k, v in kw.items():
        (sig["llm"] if k in ("category", "summary_zh", "sectors") else sig)[k] = v
    return sig


def test_create_from_lead_builds_project_that_later_leads_link_to(led_path):
    """跟踪即入账:按线索新建项目——中文名取模型摘要里"招什么"的那句,来源与核实方式标"系统自动写入";门户公告号写进编号,
    以后同一公告的更正、结果按编号挂上;通用的招标编号(12/2026)只写在标段备注里,不拿来挂接。"""
    prof = SimpleNamespace(rules=str(FX / "rules.yaml"), countries=[SimpleNamespace(iso3="ZZZ", ledger=str(led_path))])
    lp, res = create_from_lead(prof, _portal_sig(), TODAY)
    assert lp == led_path and res.errors == [] and res.item_id == "ZZZ-005", res
    it = _item(led_path, "ZZZ-005")
    assert it["name_zh"] == "建设戊市活性污泥污水处理厂" and it["segments"] == ["wastewater"] and it["stage"] == "procurement"
    assert it["ids"] == {"门户": "refConsultation=1234567"} and it["owner"] == {"业主": "SOCIETE REGIONALE MULTISERVICES WU"}
    assert it["location"] == {"detail": "执行地点 WU"} and it["verification"]["method"] == "auto_extract"
    pk = it["procurement"]["packages"][0]
    assert pk["value"] == "预估 48,516,000 MAD" and pk["note"] == "招标编号 12/2026" and str(pk["deadline"]) == "2026-11-30"
    srcs = yaml.safe_load(led_path.with_name("sources.yaml").read_text(encoding="utf-8"))["sources"]
    assert srcs[res.source_id]["via"] == "auto_extract" and "跟踪进展" in srcs[res.source_id]["note"]
    idx = build_index(load_ledger(led_path, RULES))
    assert suggest("Rectificatif ... refConsultation=1234567&orgAcronyme=x1y", idx, "ZZZ")[0]["item"] == "ZZZ-005"
    assert not [x for x in suggest("AO n° 12/2026 ailleurs", idx, "ZZZ") if x["item"] == "ZZZ-005"]


def test_create_from_lead_refuses_news_and_countries_without_ledger(led_path):
    prof = SimpleNamespace(rules=str(FX / "rules.yaml"), countries=[SimpleNamespace(iso3="ZZZ", ledger=str(led_path))])
    before = led_path.read_text(encoding="utf-8")
    lp, why = create_from_lead(prof, _portal_sig(category="project_news"), TODAY)
    assert lp is None and "动态" in why
    lp, why = create_from_lead(prof, _portal_sig(country="MAR"), TODAY)
    assert lp is None and "还没有国别台账" in why
    lp, why = create_from_lead(prof, _portal_sig(sectors=["other"]), TODAY)
    assert lp is None and "细分领域" in why
    assert led_path.read_text(encoding="utf-8") == before


def test_enter_ledger_rules_shared_by_ui_and_table(led_path, tmp_path, monkeypatch):
    """跟踪即入账(本机界面、表格拉回共用一处规则):挂接可靠(●)、写进过台账、原文已登记为来源的不另建;
    只有名称相近(○)的照样建,说明里提醒核对是否重复;建了记核对记录与操作记录 ledger created(by 为设进展的人)。"""
    monkeypatch.delenv("OPERATOR", raising=False)
    prof = SimpleNamespace(rules=str(FX / "rules.yaml"), countries=[SimpleNamespace(iso3="ZZZ", ledger=str(led_path))])
    st = Store(tmp_path / "data")
    assert should_enter("", "tracking") and should_enter("dropped", "won") and should_enter(None, "bid")
    assert not should_enter("tracking", "preparing") and not should_enter("", "lost") and not should_enter("bid", "")
    assert "还没有国别台账" in enter_ledger(prof, st, _portal_sig(country="MAR"), TODAY).msg
    high = {**_portal_sig(), "links": [{"item": "ZZZ-001", "confidence": "high", "by": "rule"}]}
    assert "ZZZ-001(挂接可靠),不另建" in enter_ledger(prof, st, high, TODAY).msg
    reg = {**_portal_sig(), "url": yaml.safe_load(led_path.with_name("sources.yaml").read_text(encoding="utf-8"))["sources"]["S01"]["url"]}
    assert "已登记为台账来源 S01" in enter_ledger(prof, st, reg, TODAY).msg
    assert "动态" in enter_ledger(prof, st, _portal_sig(category="project_news"), TODAY).msg
    assert st.events() == [] and len(load_ledger(led_path, RULES).items) == 4  # 以上都没建、没记
    weak = {**_portal_sig(), "links": [{"item": "ZZZ-003", "confidence": "medium", "by": "rule"}]}
    msg = enter_ledger(prof, st, weak, TODAY, by="teable:张三").msg
    assert msg.startswith("已按线索在台账新建项目 ZZZ-005") and "已保存在本机" in msg and "ZZZ-003,请核对" in msg
    assert st.ledger_reviewed()[weak["id"][:8]] == {"action": "updated", "at": TODAY.isoformat(), "item": "ZZZ-005", "source": "S501"}
    assert [(e["kind"], e["value"], e["item"], e["by"]) for e in st.events()] == [("ledger", "created", "ZZZ-005", "teable:张三")]
    assert enter_ledger(prof, st, weak, TODAY).msg == "已在台账 ZZZ-005" and len(load_ledger(led_path, RULES).items) == 5


@pytest.mark.parametrize("summary,country,expect", [
    ("摩洛哥水利工程局就塞夫鲁省RIBAT EL KHEIR大坝液压机械和机电设备安装工程公开招标,预算约2.6274亿迪拉姆。", "MAR",
     "塞夫鲁省RIBAT EL KHEIR大坝液压机械和机电设备安装工程"),
    ("摩洛哥国家电力和饮用水办公室招标:丹吉尔及周边饮用水安全项目El Hachef水处理厂扩建1标,投标截止2026-10-15。", "MAR",
     "丹吉尔及周边饮用水安全项目El Hachef水处理厂扩建1标"),
    ("摩洛哥卡萨布兰卡-塞塔特大区多重服务公司招标,强化Amabretane污水泵站并以廊道替换Ilham污水泵站,投标截止2026-10-09。", "MAR",
     "强化Amabretane污水泵站并以廊道替换Ilham污水泵站"),
    ("摩洛哥贝尼迈拉勒-海尼夫拉大区项目执行机构发布公开招标,建设阿济拉勒省Sidi Ali Ben Brahim大坝,估算5213万迪拉姆。", "MAR",
     "建设阿济拉勒省Sidi Ali Ben Brahim大坝"),
    ("阿塞拜疆苏姆盖特污水处理厂项目(欧佩克基金贷款17052PA)发布咨询公司意向书征集,服务含设计审查,截止2026年9月16日。", "AZE",
     "苏姆盖特污水处理厂项目"),
    ("伊斯兰开发银行阿塞拜疆卡拉巴赫灌溉渠重建项目(AZE 1028)发布咨询公司意向书征集,截止2026-10-12。", "AZE", "卡拉巴赫灌溉渠重建项目"),
    ("阿塞拜疆国家水资源局就巴尔古沙德恰伊水库二期灌溉干管和配水管建设(第2部分)发布招标,投标截止2026-10-14。", "AZE",
     "巴尔古沙德恰伊水库二期灌溉干管和配水管建设"),
    ("摩洛哥卡萨布兰卡-塞塔特大区多服务公司招标建设半地下蓄水池,地点贝雷希德,投标截止2026-10-13。", "MAR", "建设半地下蓄水池(地点贝雷希德)"),
    ("", "MAR", "Titre original"),
])
def test_name_zh_from_summary(summary, country, expect):
    assert _name_zh({"country": country, "title": "Titre original", "llm": {"summary_zh": summary}}) == expect


def test_brief_changes_cuts_between_items():
    ch = ["登记来源 S501", "新增标段:" + "x" * 150 + "(截止 2026-09-30)", "新增节点:投标截止 2026-09-30"]
    assert brief_changes(ch[:1]) == "登记来源 S501"
    assert brief_changes(ch) == "登记来源 S501 等 3 处" and brief_changes(ch, 400) == ";".join(ch)


def test_segments_and_ids_from_lead():
    def seg(title, sectors):
        return _segments({"title": title, "llm": {"sectors": sectors}})
    assert seg("Travaux de protection de la ville de Taza contre les inondations", ["hydraulic"]) == ["flood"]
    assert seg("CONSTRUCTION DU BARRAGE SIDI ALI BEN BRAHIM", ["hydraulic"]) == ["reservoir"]
    assert seg("Qız Qalası su anbarından Magistral Suvarma Kanalının Tikintisi", ["hydraulic"]) == ["irrigation"]
    assert seg("Travaux du réseau de distribution du Saiss III", ["hydraulic", "water_supply", "other"]) == ["irrigation", "water_supply"]
    assert lead_ids({"source": "wb_notices", "url": "https://x/y", "fields": {"project_id": "P175747"}}) == {"世行": "P175747"}
    assert lead_ids({"source": "ebrd", "url": "https://x/y", "fields": {"project_id": "P175747"}}) == {}


def test_pull_ledger_updates_only_when_remote_changed_ledgers(repo, tmp_path):
    """界面启动时:远端只改了台账就拉下来(定时任务自动写入的台账立即可见);远端改了程序代码不拉,提示去更新程序。"""
    work, remote = repo
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(other))
    who = ("-c", "user.name=t", "-c", "user.email=t@t")
    fp = other / "countries" / "ZZZ" / "ledger.yaml"
    fp.write_text(fp.read_text(encoding="utf-8").replace("as_of:", "# 自动更新\nas_of:", 1), encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, *who, "commit", "-q", "-m", "台账自动更新")
    _git(other, "push", "-q", "origin", "HEAD:main")
    r = sync.pull_ledger_updates(work)
    assert r.ok and "已拉取台账更新" in r.output, r.output
    assert "# 自动更新" in (work / "countries" / "ZZZ" / "ledger.yaml").read_text(encoding="utf-8")
    assert sync.pull_ledger_updates(work) == sync.Result(True, "")  # 没有新改动时不提示
    (other / "private" / "overlay").mkdir(parents=True)  # 表格里改的项目优先级拉回内部叠加:也直接拉下来
    (other / "private" / "overlay" / "ZZZ.yaml").write_text("items: {ZZZ-001: {priority: A}}\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, *who, "commit", "-q", "-m", "内部叠加")
    _git(other, "push", "-q", "origin", "HEAD:main")
    assert sync.pull_ledger_updates(work).ok and (work / "private" / "overlay" / "ZZZ.yaml").exists()
    (other / "private" / "team.yaml").write_text("members: []\n", encoding="utf-8")  # 代号表改了:服务器上 5 分钟内就用新的
    _git(other, "add", "-A")
    _git(other, *who, "commit", "-q", "-m", "代号表")
    _git(other, "push", "-q", "origin", "HEAD:main")
    assert sync.pull_ledger_updates(work).ok and (work / "private" / "team.yaml").exists()
    (other / "tool.py").write_text("x = 1\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, *who, "commit", "-q", "-m", "代码")
    _git(other, "push", "-q", "origin", "HEAD:main")
    r = sync.pull_ledger_updates(work)
    assert not r.ok and "程序有更新" in r.output and not (work / "tool.py").exists()


@pytest.mark.parametrize("origin,expect", [
    ("https://github.com/owner/repo.git", "https://github.com/owner/repo/edit/main/countries/ZZZ/ledger.yaml"),
    ("git@github.com:owner/repo.git", "https://github.com/owner/repo/edit/main/countries/ZZZ/ledger.yaml"),
    ("https://user:secret@github.com/owner/repo", "https://github.com/owner/repo/edit/main/countries/ZZZ/ledger.yaml"),
    ("https://gitlab.example.com/owner/repo.git", None),
])
def test_github_url(repo, origin, expect):
    work, _ = repo
    _git(work, "remote", "set-url", "origin", origin)
    assert sync.github_url(work, work / "countries" / "ZZZ" / "ledger.yaml") == expect
    assert ("secret" in (sync.origin_url(work) or "")) == ("secret" in origin)  # 克隆 data 分支要用原样地址,不遮凭据


def test_git_output_masks_credentials(repo, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)  # 连不上时会稍等重试
    work, _ = repo
    _git(work, "remote", "set-url", "origin", "https://user:secret@127.0.0.1:9/owner/repo.git")
    r = sync.push_program(work)  # 连不上:报错信息里的网址不带凭据
    assert not r.ok and "secret" not in r.output


def test_data_pull_never_leaves_a_rebase_half_done(tmp_path, no_git_identity):
    """变基解不开(本机改了、远端删了同一个文件):拉取失败,退回拉取以前——本机提交还在、没有半截的变基,之后照常提交、推送。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "data", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "views.yaml").write_text("a: {}\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(local))
    _git(seed, "rm", "-q", "views.yaml")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "删掉")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    (local / "views.yaml").write_text("a: {}\nb: {}\n", encoding="utf-8")
    _git(local, *sync.APP_ID, "commit", "-qam", "界面:保存筛选方案")
    (local / "note.txt").write_text("还没提交的改动\n", encoding="utf-8")
    r = sync.pull(local)
    assert not r.ok
    assert not sync.rebasing(local)
    assert _git(local, "log", "-1", "--format=%s") == "界面:保存筛选方案" and (local / "note.txt").exists()
    assert (local / "views.yaml").read_text(encoding="utf-8") == "a: {}\nb: {}\n"


def test_rebase_left_by_a_killed_process_is_cleaned_up(tmp_path, no_git_identity):
    """变基中途进程被停掉(变基目录还在,没有 REBASE_HEAD):下次拉取先退回,本机提交还在,不一直卡着。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "data", str(remote))
    seed, local = tmp_path / "seed", tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    _git(tmp_path, "clone", "-q", "-b", "data", str(remote), str(local))
    (seed / "a.txt").write_text("remote\n", encoding="utf-8")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "远端")
    _git(seed, "push", "-q", "origin", "HEAD:data")
    (local / "a.txt").write_text("local\n", encoding="utf-8")
    _git(local, *sync.APP_ID, "commit", "-qam", "界面:本机")
    subprocess.run(["git", *sync.APP_ID, "pull", "-q", "--rebase", "origin", "data"], cwd=local, capture_output=True)  # 冲突停下
    assert sync.rebasing(local)
    (local / ".git" / "REBASE_HEAD").unlink(missing_ok=True)  # 像进程被停掉:只剩变基目录
    sync.pull(local)
    assert not sync.rebasing(local)
    assert _git(local, "log", "--format=%s", "-3").splitlines()[0] in ("界面:本机", "远端")
    assert "界面:本机" in _git(local, "log", "--all", "--format=%s")
