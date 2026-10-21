"""后台任务共用:运行上下文(表格、profile、今天、模型、本机缓存)、新库里的索引(线索号与网址、商机挂接索引、国别、信源),
采集结果(watch 的线索 dict)→ 线索表一行的转换。

本机缓存(OW_CACHE,缺省 <data_dir>/cache):网页正文与附件文本、各信源已见条目(core.watch.store.Store 的 state.json)。
丢了只会多算一遍:去重按新库里的线索号与原文网址,不会重复建线索。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core import geo, storage, tstore
from core import ontology as onto_mod
from core.profile import Profile
from core.watch.llm import Classifier, LLMConfig
from core.watch.outputs import CAT_ZH, MANUAL, SECTOR_ZH, _llm, category, links_of, relevant, sectors_of
from core.watch.store import Store

# 枚举都从本体取(core.ontology,v2.2 起运行时读;改选项只改 ontology.yaml):判定程序用英文键(taxonomies 里每个值的 code),表里存中文值
CAT = {**onto_mod.codes("lead_category"), "page": onto_mod.codes("lead_category")["other"]}  # 判定的类别 → 本体的类别(page = 页面变化,归「其他」)
DEADLINE_KIND = {onto_mod.code_of("milestone_kind")[v]: v for v in onto_mod.group_values("milestone_kind", "采购截止")}  # 线索里抽出的四种截止(「采购截止」组)
_SOURCE_KIND_CODE = {"html_list": "html_list", "page": "html_list", "links": "html_list", "rss": "rss", "atexo_search": "portal",
                     "wb_procnotices": "api", "wb_projects": "api", "aiib": "api", "ckan": "api", "iati": "api", "gdelt": "api"}  # 信源配置里的 kind → 本体 source_kind 的 code
SOURCE_KIND = {k: onto_mod.codes("source_kind")[c] for k, c in _SOURCE_KIND_CODE.items()}
SOURCE_STATUS = dict(onto_mod.codes("source_status"))
# 线索的类别 → 商机的公开进度(机器只填空;不同的写进待核更新):类别 code → 公开进度 code
_PUBLIC_STAGE_CODE = {"tender": "procurement", "eoi": "procurement", "prequalification": "procurement", "gpn": "procurement", "spn": "procurement",
                      "award": "awarded", "financing": "financing_signed", "plan": "planning"}
PUBLIC_STAGE_OF = {k: onto_mod.codes("public_stage")[c] for k, c in _PUBLIC_STAGE_CODE.items()}
# 线索的领域 → 商机的领域:两套分类体系里 code 相同的那些(水利在商机里分灌溉与防洪,线索分不出,不填)
OPP_SECTOR = {c: v for c, v in onto_mod.codes("opp_sectors").items() if c in onto_mod.codes("lead_sectors")}
EXCERPT_MAX = 20000
LEAD_PROPS = ["key", "url", "status", "judged_by", "origin", "relevant", "opportunity", "suggested_opp", "country", "title", "excerpt"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lead_no(sig_id: str) -> str:
    return sig_id[:8]


@dataclass
class OppIndex:
    """新库里的商机:挂接(编号、别名、名称)与模型提示用。items 的形状与旧台账的项目一致(core.watch.link、core.watch.llm 直接用)。"""
    rows: list[dict] = field(default_factory=list)  # {rid, code, iso, name_zh, name_local, aliases, ids, stage}
    rid_of: dict[str, str] = field(default_factory=dict)  # 商机编号 → 记录编号

    @property
    def items(self) -> list[dict]:
        return [{"id": r["code"], "name_zh": r["name_zh"], "name_en": r["name_local"], "name_local": None, "aliases": r["aliases"],
                 "ids": r["ids"], "stage": r["stage"], "financing": []} for r in self.rows]

    @property
    def countries(self) -> dict[str, str | None]:
        return {r["code"]: r["iso"] for r in self.rows}

    def ledger_like(self, iso: str | None = None) -> SimpleNamespace | None:
        items = [it for it in self.items if not iso or self.countries.get(it["id"]) in (iso, None)]
        return SimpleNamespace(items=items, countries=self.countries) if items else None

    def next_code(self, iso: str) -> str:
        nums = [int(m[1]) for r in self.rows if (m := re.fullmatch(rf"{iso}-(\d+)", r["code"]))]
        return f"{iso}-{(max(nums) + 1 if nums else 1):03d}"

    def add(self, rid: str, code: str, iso: str, name_zh: str, name_local: str | None, aliases: list[str], ids: dict, stage: str) -> None:
        self.rows.append({"rid": rid, "code": code, "iso": iso, "name_zh": name_zh, "name_local": name_local, "aliases": aliases,
                          "ids": ids, "stage": stage})
        self.rid_of[code] = rid


def parse_ids(text: object) -> dict[str, str]:
    """外部编号(长文本,一行一个「标签: 编号」)→ {标签: 编号};没有标签的用序号当标签。"""
    out: dict[str, str] = {}
    for i, ln in enumerate(str(text or "").splitlines()):
        ln = tstore._REF.sub("", ln).strip()
        if not ln:
            continue
        k, _, v = ln.partition(":")
        if not v.strip():
            k, _, v = ln.partition(":")
        if v.strip():
            out[k.strip()] = v.strip()
        else:
            out[f"编号{i + 1}"] = ln
    return out


IGNORE_DEFAULT = frozenset({"CHN"})  # 「忽略国别」没设过(空着)、库里也没有「默认」行时,默认忽略中国;一个都不忽略的存「-」
IGNORE_DEFAULT_ROW = "默认"  # 个人设置里名称为「默认」、账号空着的一行 = 新账号(没设过忽略国别的账号)的默认忽略国别(使用方 2026-10-21:默认只看清单国别)


def parse_isos(text: object) -> set[str]:
    """「USA, CAN;mex」→ {"USA", "CAN", "MEX"}:逗号、分号、空白都算分隔,只认三个字母的码。"""
    return {x.upper() for x in re.split(r"[\s,;,;、]+", str(text or "")) if re.fullmatch(r"[A-Za-z]{3}", x)}


def is_ignore_default_row(b, rec: dict) -> bool:
    """个人设置的「默认」行:名称是「默认」且账号空着(页面建的账号行都带账号)。"""
    if str(b.get("preference", rec, "name") or "").strip() != IGNORE_DEFAULT_ROW:
        return False
    return not b.has("preference", "user") or not b.get("preference", rec, "user")


@dataclass
class Ctx:
    api: tstore.Client
    base: tstore.Base
    profile: Profile
    today: date
    cache: Store  # 本机缓存:网页正文、已见条目
    clf: Classifier | None = None
    clf_light: Classifier | None = None  # 译名之类的简单活:同一模型关思考、浅深度、短输出,比 clf 便宜得多
    log: Any = print
    _countries: dict[str, str] | None = None  # ISO3 → 记录编号
    _iso_of: dict[str, str] | None = None  # 记录编号 → ISO3
    _sources: dict[str, dict] | None = None  # 信源编号 → 记录
    _opps: OppIndex | None = None
    _ignored: set[str] | None = None  # 忽略国别 ISO3(个人设置,所有账号合起来)
    _fx: Any = None  # 当日汇率(core.fx.Rates),用到时才取

    @property
    def fx(self):
        if self._fx is None:
            from core import fx as fx_mod
            self._fx = fx_mod.Rates(cache_dir=self.cache.root.parent / "fx", today=self.today, log=self.log)
        return self._fx

    # ---------- 国别 ----------
    def _load_countries(self) -> None:
        self._countries, self._iso_of = {}, {}
        for r in self.base.rows("country", props=["name_zh"] + ["ISO3"]):
            iso = self.base.get("country", r, "ISO3")
            if iso:
                self._countries[str(iso).upper()] = r["id"]
                self._iso_of[r["id"]] = str(iso).upper()

    def country_rid(self, iso: str | None, create: bool = True) -> str | None:
        """国别的记录编号;新国别出现时建一行(A11:国名、ISO3、大洲与区域;市场分级「其他」由人改)。"""
        if not iso:
            return None
        iso = str(iso).upper()
        if self._countries is None:
            self._load_countries()
        assert self._countries is not None and self._iso_of is not None
        if iso in self._countries or not create:
            return self._countries.get(iso)
        if iso not in geo.countries():
            return None
        rec = self.base.create("country", [{"name_zh": geo.name_zh(iso), "ISO3": iso,
                                            "region": " · ".join(x for x in (geo.continent(iso), geo.region(iso)) if x) or None,
                                            "tier": "其他"}])[0]
        self._countries[iso] = rec["id"]
        self._iso_of[rec["id"]] = iso
        self.log(f"  ▸ 新国别 {iso} {geo.name_zh(iso)}:国别表加了一行")
        return rec["id"]

    def iso_of(self, rid: str | None) -> str | None:
        if self._iso_of is None:
            self._load_countries()
        assert self._iso_of is not None
        return self._iso_of.get(rid or "")

    def tier_of(self, iso: str) -> str | None:
        rid = self.country_rid(iso, create=False)
        if not rid:
            return None
        recs = self.base.rows("country", filter=self.base.filter(self.base.cond("country", "ISO3", "is", iso)), props=["tier"])
        return self.base.get("country", recs[0], "tier") if recs else None

    def ignored(self) -> set[str]:
        """个人设置里任何一个账号忽略的国别(ISO3,「忽略国别」存逗号分隔的国别码):采集不再涉及(A01)。
        没设过的账号(空着)、还没有个人设置的,按「默认」行算(名称「默认」、账号空着;管理员在「我的」页设),
        没有「默认」行则按默认忽略中国;表或字段还没建 → 空。「默认」行本身不算一个账号(有人明确恢复了的国别,默认不拦它)。"""
        if self._ignored is None:
            b = self.base
            out: set[str] = set()
            if "preference" not in b.missing and b.has("preference", "ignored"):
                rows = b.rows("preference", props=["name", "user", "ignored"])
                default: set[str] = set(IGNORE_DEFAULT)
                accounts = []
                for r in rows:
                    if is_ignore_default_row(b, r):
                        v = b.get("preference", r, "ignored")
                        default = parse_isos(v) if v else set(IGNORE_DEFAULT)
                    else:
                        accounts.append(r)
                for r in accounts:
                    v = b.get("preference", r, "ignored")
                    out |= parse_isos(v) if v else default
                if not accounts:
                    out |= default
            self._ignored = out
        return self._ignored

    # ---------- 信源 ----------
    def sources(self) -> dict[str, dict]:
        """信源表:信源编号 → 记录(抓不抓、抓得怎样在表里;怎么抓在 git 的 watch.yaml)。"""
        if self._sources is None:
            self._sources = {}
            for r in self.base.rows("source"):
                k = self.base.get("source", r, "信源编号")
                if k:
                    self._sources[str(k)] = r
        return self._sources

    def source_rid(self, sid: str, name: str | None = None, kind: str | None = None, scope: str | None = None,
                   url: str | None = None, enabled: bool = True) -> str:
        """信源的记录编号;表里没有的建一行(新加进 watch.yaml 的信源、人工上传)。"""
        rows = self.sources()
        if sid in rows:
            return rows[sid]["id"]
        rec = self.base.create("source", [{"name": name or sid, "信源编号": sid, "kind": kind, "scope": scope, "url": url, "enabled": enabled}])[0]
        rows[sid] = rec
        self.log(f"  ▸ 信源 {sid} 不在表里:加了一行(启用 {'是' if enabled else '否'})")
        return rec["id"]

    def source_enabled(self, sid: str) -> bool | None:
        r = self.sources().get(sid)
        return None if r is None else bool(self.base.get("source", r, "enabled"))

    def source_status(self, sid: str, status: str, when: str | None = None) -> None:
        """机器自己的运行状态(R2 的例外):最近采集、最近状态直接更新。"""
        r = self.sources().get(sid)
        if r:
            self.base.update("source", r["id"], {"last_run": when or now_iso(), "last_status": SOURCE_STATUS.get(status, status)})
            self._sources[sid] = self.base.record("source", r["id"]) or r

    # ---------- 商机 ----------
    def opps(self, reload: bool = False) -> OppIndex:
        if self._opps is None or reload:
            idx = OppIndex()
            b = self.base
            for r in b.rows("opportunity", props=["code", "name_zh", "name_local", "aliases", "external_ids", "country", "stage"]):
                code = b.get("opportunity", r, "code")
                if not code:
                    continue
                idx.add(r["id"], str(code), self.iso_of(tstore.link_id(b.get("opportunity", r, "country"))) or "",
                        str(b.get("opportunity", r, "name_zh") or code), b.get("opportunity", r, "name_local"),
                        [x.strip() for x in str(b.get("opportunity", r, "aliases") or "").splitlines() if x.strip()],
                        parse_ids(b.get("opportunity", r, "external_ids")), str(b.get("opportunity", r, "stage") or "建议"))
            self._opps = idx
        return self._opps

    # ---------- 线索 ----------
    def lead_keys(self) -> tuple[dict[str, str], dict[str, str]]:
        """新库里已有的线索:(线索号 → 记录编号, 原文网址 → 记录编号)。去重按这两个(后台无状态)。"""
        nos, urls = {}, {}
        for r in self.base.rows("lead", props=["key", "url"]):
            no, u = self.base.get("lead", r, "key"), self.base.get("lead", r, "url")
            if no:
                nos[str(no)] = r["id"]
            if u:
                urls[str(u).strip().rstrip("/")] = r["id"]
        return nos, urls


def cache_root(profile: Profile) -> Path:
    """后台的本机缓存目录(网页正文、已见条目在 watch/ 下,汇率在 fx/ 下)。"""
    return Path(os.environ.get("OW_CACHE") or (profile.data_dir or storage.DATA_DIR) / "cache")


TRANSLATE_MAX_TOKENS = 8_000  # 一批 40 个名称的中文译名约两三千 token,留足余量


def connect(profile: Profile, today: date | None = None, use_llm: bool = True, log=print, api: tstore.Client | None = None,
            base: tstore.Base | None = None, cache_dir: Path | None = None) -> Ctx:
    """连上新库(机器账号的 TEABLE_TOKEN),准备模型与本机缓存。"""
    if api is None:
        url, token = tstore.credentials()
        api = tstore.Client(url, token)
    base = base or tstore.Base(api)
    cache = Store(cache_dir or cache_root(profile))
    cfg = (profile.watch or {}).get("llm")
    clf = Classifier(LLMConfig.from_profile(cfg)) if use_llm else None
    light = Classifier(LLMConfig.for_assistant(cfg, {"max_tokens": TRANSLATE_MAX_TOKENS})) if use_llm else None
    return Ctx(api=api, base=base, profile=profile, today=today or date.today(), cache=cache, clf=clf, clf_light=light, log=log)


# ---------- 采集结果 → 线索行 ----------

def judged_by(sig: dict) -> str | None:
    """判定方式(A03、R8):上传的为不判定;模型判过的为模型;规则初筛没过(没送模型)的为规则;送了模型没结果的留空(下次补判)。"""
    if sig.get("source") == MANUAL:
        return "不判定"
    if _llm(sig):
        return "模型"
    return None if (sig.get("rules") or {}).get("candidate") else "规则"


def lead_values(ctx: Ctx, sig: dict, source_rid: str | None, country_rid: str | None) -> dict:
    """watch 的线索 dict → 线索表的 {属性: 值}(关联给记录编号;所属与建议所属由 suggest 填)。"""
    x = _llm(sig) or {}
    amt = x.get("amount")
    amount = f"{amt['value']:,.0f} {amt['currency']}" if amt else (sig.get("fields") or {}).get("estimate")
    cat = category(sig)
    b = ctx.base
    size: dict = {}
    if amt and b.has("lead", "amount_value") and b.has("lead", "currency"):  # 投资规模 + 货币(币种不在选项里的留空);折合人民币由 A21 补
        choices = {c["name"] for c in (b.field("lead", "currency").get("options") or {}).get("choices") or []}
        cur = str(amt.get("currency") or "").upper()
        size = {"amount_value": amt["value"], "currency": cur if cur in choices else None}
    return {
        "title": (sig.get("title") or sig.get("url") or lead_no(sig["id"]))[:500], "key": lead_no(sig["id"]), "url": sig.get("url"),
        "source": source_rid, "origin": "用户上传" if sig.get("source") == MANUAL else "机器采集",
        "published": sig.get("published"), "found_at": sig.get("first_seen") or now_iso(),
        "excerpt": (sig.get("excerpt") or "")[:EXCERPT_MAX] or None,
        "relevant": True if relevant(sig) else None, "category": CAT.get(cat) if judged_by(sig) else None,
        "sectors": [SECTOR_ZH[k] for k in sectors_of(sig) if k in SECTOR_ZH] if judged_by(sig) else None,
        "country": country_rid, "summary_zh": x.get("summary_zh") or None, "funder": x.get("funder"), "project_name": x.get("project"),
        "amount": amount, "judged_by": judged_by(sig), "status": "待处理", **size,
    }


def deadlines_of(sig: dict) -> list[tuple[str, str, str]]:
    """线索给出的日期 → [(日期, 节点类型, 说明)]:模型抽的在前;信源结构化字段的截止日模型没抽到时补上(类型「其他」,说明「截止」)。"""
    out = [(d["date"], DEADLINE_KIND.get(d.get("type"), "其他"), (d.get("evidence") or "")[:200]) for d in (_llm(sig) or {}).get("deadlines") or []]
    fd = (sig.get("fields") or {}).get("deadline")
    if fd and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fd)) and all(fd != d for d, _, _ in out):
        out.append((str(fd), "其他", "截止"))
    return out


def best_link(sig: dict) -> tuple[str | None, bool]:
    """线索的挂接建议 → (最可能的商机编号, 是否可靠)。可靠 = 编号吻合(规则 high)、监测配置指定、或模型读原文确认(llm high)。"""
    links = links_of(sig)
    if not links:
        return None, False
    top = links[0]
    reliable = top.get("by") == "config" or top.get("confidence") == "high"
    return top["item"], reliable
