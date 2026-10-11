"""监测的状态与线索存储(在数据目录的 watch/ 下,GitHub Actions 里就是私有 data 分支)。

watch/state.json            各信源已见条目(键 → 首见日期与内容指纹)、页面指纹、最近运行与健康状态
watch/signals/YYYY-MM.jsonl 线索,一行一条,按首见月份追加(定时任务写)
watch/submitted/{id}.json   人工提交的线索,一条一个文件(本机界面写;与定时任务写不同的文件,同步时不冲突)
watch/docs/{sha1}.txt       详情页与附件的正文缓存(给模型读,线索里只留摘录);监测页面的上次全文(比对变化用)
watch/triage.yaml           人工处理记录(可选):{线索 id: {status: done|ignore, note: ...}}
watch/ledger_reviewed.yaml  台账核对记录(界面「商机」「待确认」页写):{线索 id: {action: updated|skipped, item, source, at}}
watch/events.jsonl          操作记录(只追加):{id, at, kind: status|stage|note|ledger|owner|next|submit|member, value, note?, item?,
                            source?, due?(next 的到期日), conflict?(表格拉回时覆盖的本机值), by}
                            by 是操作人:本机界面写本机的岗位代号(.env 的 OPERATOR,没设为"本机"),服务器上的界面写登录
                            表格的人的代号,定时任务写 system;加操作人以前的旧记录没有 by
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

MAX_SEEN_PER_SOURCE = 20_000
EVENTS_FILE = "events.jsonl"
TRIAGE_TEMPLATE = """# 人工处理记录。线索号见提醒页最后一列(8 位即可);标记后该线索不再出现在提醒页。
# status: done(已处理)或 ignore(无关、重复);note 随意写处理情况。可在 GitHub 网页上直接编辑本文件。
# 例:
# 9bc845a9: {status: done, note: 已报名;台账 AZE-020 已更新}
# a809d32d: {status: ignore}
"""
REVIEWED_TEMPLATE = """# 台账核对记录(界面「商机」「待确认」页写):线索号 → 处理结果。列在这里的线索不再出现在「待确认」里。
# action: updated 已写进台账(item 项目编号、source 来源编号)/ skipped 核对后不用更新;at 处理日期。
"""
SYSTEM = "system"  # 定时任务(GitHub Actions)写的操作记录的操作人


_operator_of_session = None  # 由界面注入:取当前会话登录的人;没有会话时返回 None(旧网页界面已删,留作后台任务的扩展点)


def set_operator_source(fn) -> None:
    """服务器上一个界面进程同时服务多人:操作人按会话取(fn() → 岗位代号或 None),取不到再看 OPERATOR。"""
    global _operator_of_session
    _operator_of_session = fn


def operator() -> str:
    """操作人:服务器上的界面为当前登录的人(set_operator_source);本机为环境变量 OPERATOR(程序目录 .env 里,
    界面"设置与同步"可填岗位代号);都没有时记"本机"。"""
    who = _operator_of_session() if _operator_of_session else None
    return (who or os.environ.get("OPERATOR", "")).strip() or "本机"


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def signal_id(source_id: str, key: str) -> str:
    return sha1(f"{source_id}|{key}")[:16]


@dataclass
class Item:
    """采集器产出的一条原始条目(公告、新闻、项目记录、页面变化)。"""

    key: str  # 去重键:通常是链接或信源自己的编号
    url: str
    title: str
    published: str | None = None  # YYYY-MM-DD
    text: str = ""  # 摘要或正文摘录
    fields: dict = field(default_factory=dict)  # 结构化字段:公告类型、截止日、项目号、国别……
    lang: str | None = None
    attachments: list[str] = field(default_factory=list)
    change: str = "new"  # new | changed
    fingerprint: str = ""  # 用于变更检测的内容指纹(只对 track_changes 字段或页面正文计算)


class Store:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "watch"
        self.root.mkdir(parents=True, exist_ok=True)
        fp = self.root / "state.json"
        self.state: dict = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {"sources": {}}
        self._ids: set[str] | None = None  # 已入库线索的 id,用到时才读

    # ---------- 已见条目 ----------
    def src(self, source_id: str) -> dict:
        return self.state["sources"].setdefault(source_id, {"seen": {}})

    def is_first_run(self, source_id: str) -> bool:
        return not self.state["sources"].get(source_id, {}).get("last_ok")

    def seen(self, source_id: str, key: str) -> dict | None:
        return self.src(source_id)["seen"].get(key)

    def mark_seen(self, source_id: str, key: str, fingerprint: str, day: str) -> None:
        seen = self.src(source_id)["seen"]
        prev = seen.get(key)
        seen[key] = {"first": prev["first"] if prev else day, "fp": fingerprint}
        if len(seen) > MAX_SEEN_PER_SOURCE:  # 最早见到的先淘汰
            for k in sorted(seen, key=lambda k: seen[k]["first"])[: len(seen) - MAX_SEEN_PER_SOURCE]:
                del seen[k]

    def health(self, source_id: str, **kw) -> None:
        self.src(source_id).update(kw)

    # ---------- 线索 ----------
    def append_signals(self, sigs: list[dict]) -> None:
        if self._ids is not None:
            self._ids.update(s["id"] for s in sigs)
        by_month: dict[str, list[dict]] = {}
        for s in sigs:
            by_month.setdefault(s["first_seen"][:7], []).append(s)
        d = self.root / "signals"
        d.mkdir(exist_ok=True)
        for month, rows in by_month.items():
            with (d / f"{month}.jsonl").open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def put_submitted(self, sig: dict) -> Path:
        d = self.root / "submitted"
        d.mkdir(exist_ok=True)
        fp = d / f"{sig['id']}.json"
        fp.write_text(json.dumps(sig, ensure_ascii=False, indent=1), encoding="utf-8")
        return fp

    def submitted(self, sid: str) -> dict | None:
        fp = self.root / "submitted" / f"{sid}.json"
        return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else None

    def update_signals(self, updated: list[dict]) -> None:
        """改写已入库的线索(如补判模型结果后):人工提交的改其单独文件,其余在首见月份的文件里按 id 替换原行。"""
        by_month: dict[str, dict[str, dict]] = {}
        for s in updated:
            if (self.root / "submitted" / f"{s['id']}.json").exists():
                self.put_submitted(s)
                continue
            by_month.setdefault(s["first_seen"][:7], {})[s["id"]] = s
        for month, rows in by_month.items():
            fp = self.root / "signals" / f"{month}.jsonl"
            if not fp.exists():
                continue
            lines = []
            for line in fp.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    s = json.loads(line)
                    lines.append(json.dumps(rows.get(s["id"], s), ensure_ascii=False))
            tmp = fp.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(fp)

    def signal_ids(self) -> set[str]:
        """已入库线索的 id(判断某条目以前有没有入库)。"""
        if self._ids is None:
            self._ids = {s["id"] for s in self.load_signals()}
        return self._ids

    def load_signals(self, since: date | None = None) -> list[dict]:
        d = self.root / "signals"
        out: dict[str, dict] = {}  # 按 id 去重(万一重复写入,以后写的为准)
        for fp in sorted(d.glob("*.jsonl")):
            if since and fp.stem < since.isoformat()[:7]:
                continue
            for line in fp.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    s = json.loads(line)
                    if not since or s["first_seen"][:10] >= since.isoformat():
                        out[s["id"]] = s
        for fp in sorted((self.root / "submitted").glob("*.json")):
            s = json.loads(fp.read_text(encoding="utf-8"))
            if not since or s["first_seen"][:10] >= since.isoformat():
                out[s["id"]] = s
        return list(out.values())

    def triage(self) -> dict:
        fp = self.root / "triage.yaml"
        if not fp.exists():
            fp.write_text(TRIAGE_TEMPLATE, encoding="utf-8")
        return yaml.safe_load(fp.read_text(encoding="utf-8")) or {}

    # ---------- 台账核对记录 ----------
    def ledger_reviewed(self) -> dict[str, dict]:
        fp = self.root / "ledger_reviewed.yaml"
        raw = (yaml.safe_load(fp.read_text(encoding="utf-8")) or {}) if fp.exists() else {}
        return {str(k): v for k, v in raw.items()}

    def mark_ledger_reviewed(self, sid: str, action: str | None, day: date | None = None, by: str | None = None,
                             event: str | None = None, **info: str) -> None:
        """记下线索的台账核对结果(action=None 即撤销,线索回到"建议更新台账");by 为操作人(缺省本机的);
        操作记录里记 event(缺省同 action,如跟踪即入账建了项目记 created)。"""
        rv = self.ledger_reviewed()
        rv.pop(sid[:8], None)
        if action:
            rv[sid[:8]] = {"action": action, "at": (day or date.today()).isoformat(), **{k: v for k, v in info.items() if v}}
        body = yaml.safe_dump(rv, allow_unicode=True, sort_keys=True) if rv else ""
        (self.root / "ledger_reviewed.yaml").write_text(REVIEWED_TEMPLATE + ("\n" + body if body else ""), encoding="utf-8")
        self.log_event(sid, "ledger", event or action or "undone", by=by, **info)

    # ---------- 操作记录 ----------
    def log_event(self, sid: str, kind: str, value: str = "", note: str = "", by: str | None = None, at: str | None = None,
                  **info: str) -> dict:
        """线索的一次操作(处理状态、跟踪进展、备注、台账核对)追加到 watch/events.jsonl。
        by:操作人,缺省为本机的(operator());at:时间(ISO,带时区),缺省为现在。"""
        rec = {"id": sid, "at": at or datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind, "value": value,
               **({"note": note} if note else {}), **{k: v for k, v in info.items() if v}, "by": by or operator()}
        with (self.root / EVENTS_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def events(self) -> list[dict]:
        """操作记录,按记下的先后;一模一样的行只算一次(两处同时记下同一次操作、合并时留下的重复行)。"""
        fp = self.root / EVENTS_FILE
        out: list[dict] = []
        seen: set[str] = set()
        for line in fp.read_text(encoding="utf-8").splitlines() if fp.exists() else []:
            line = line.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:  # 同步合并留下的坏行等:跳过,不影响其余记录
                continue
        return out

    # ---------- 正文缓存 ----------
    def doc_path(self, url: str) -> Path:
        d = self.root / "docs"
        d.mkdir(exist_ok=True)
        return d / f"{sha1(url)}.txt"

    def save(self) -> None:
        tmp = self.root / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.root / "state.json")
