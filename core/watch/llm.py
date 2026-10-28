"""大模型判定与抽取:是否相关、类别、截止日(附原文依据)、金额、出资方、资格限制、中文摘要、对应台账项目。

- 只处理规则初筛通过的线索;每次运行有调用上限(profile watch.llm.max_calls)。
- 两种接入,profile 的 watch.llm.provider 选:
  - deepseek:DeepSeek 开放平台,OpenAI 格式接口 + JSON 输出 + 思考模式;密钥环境变量 DEEPSEEK_API_KEY。
  - anthropic:Claude(官方 SDK,结构化输出、流式、服务端 fallbacks);密钥 ANTHROPIC_API_KEY 等。
- 没有凭据时整体停用,系统退回纯规则,提醒页会注明;余额不足、密钥无效时本次运行立即停用并注明原因。
- 模型输出一律按下方字段表校验与归一(类别与日期格式等),是"机器建议":进提醒页供人确认,不直接改台账。
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from core import geo
from core.watch.link import item_countries
from core.watch.rules import SECTORS

CATEGORIES = ["tender", "eoi", "prequalification", "gpn", "spn", "award", "financing", "plan", "project_news", "policy", "other"]
DEADLINE_TYPES = ["bid_deadline", "eoi_deadline", "pq_deadline", "rfp_deadline", "other"]
CONFIDENCE = ["high", "medium", "low"]

PROVIDERS = {  # 各接入的默认模型、思考深度、输出上限(含思考)
    "anthropic": {"model": "claude-opus-5", "effort": "medium", "max_tokens": 16_000},
    "deepseek": {"model": "deepseek-v4-pro", "effort": "high", "max_tokens": 32_000},
}
DEFAULT_MODEL = PROVIDERS["anthropic"]["model"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "category": {"type": "string", "enum": CATEGORIES},
        "sectors": {"type": "array", "items": {"type": "string", "enum": SECTORS}},
        "country": _nullable({"type": "string"}),
        "title_zh": {"type": "string"},
        "summary_zh": {"type": "string"},
        "why": {"type": "string"},
        "project": _nullable({"type": "string"}),
        "funder": _nullable({"type": "string"}),
        "amount": _nullable({
            "type": "object",
            "properties": {"value": {"type": "number"}, "currency": {"type": "string"}},
            "required": ["value", "currency"], "additionalProperties": False,
        }),
        "deadlines": {"type": "array", "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": DEADLINE_TYPES}, "date": {"type": "string", "format": "date"},
                           "evidence": {"type": "string"}},
            "required": ["type", "date", "evidence"], "additionalProperties": False,
        }},
        "eligibility": _nullable({"type": "string"}),
        "ledger_links": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "string"}, "confidence": {"type": "string", "enum": CONFIDENCE},
                           "reason": {"type": "string"}},
            "required": ["item", "confidence", "reason"], "additionalProperties": False,
        }},
        "new_opportunity": {"type": "boolean"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "category", "sectors", "country", "title_zh", "summary_zh", "why", "project", "funder", "amount", "deadlines",
                 "eligibility", "ledger_links", "new_opportunity", "evidence"],
    "additionalProperties": False,
}

SYSTEM = """你是水务项目机会监测系统的分析员。使用方是投标人国籍为 {bidder} 的水务工程承包与投资企业,业务覆盖供水、污水、海水淡化、输水与水库、灌溉水利的工程总承包、投资运营与设备供货。

你会收到一条从公开信源采集到的条目(采购公告、新闻、项目页面变化、项目库记录),以及正文或附件摘录。请完成:
1. relevant:它对"{country}的水务项目机会跟踪"是否有用。有用 = 与水务项目(供水、污水、雨水、海水淡化、水库与输水、灌溉水利、水环境治理)的立项、融资、工程/货物/设计施工/咨询公司采购、授标、实施或相关政策有关。
   以下判为 false:其他行业的招标;与水务无关的新闻;水务项目名下但与工程无关的零星采购(个人咨询顾问招聘、宣传/审计/培训/办公用品/车辆等)。
2. category:tender 招标/邀请投标;eoi 意向书征集;prequalification 资格预审;gpn 总采购公告;spn 开发银行项目下针对具体合同的特定采购公告;award 授标、短名单或合同签署;financing 贷款/赠款的批准或签署;plan 立项、可研、设计、环评、国家计划、奠基;project_news 项目进展新闻;policy 政策法规;other。
3. sectors:所属领域,可多选:water_supply 供水(含输水、减漏、计量);wastewater 污水(含污泥、再生水);hydraulic 水利(水库、灌溉、渠道、防洪、调水);desalination 海水淡化;stormwater 雨水排涝;environment 水环境治理;other 其他。
   country:项目所在国的 ISO3 三字母代码(如 AZE、UZB);跨多国或看不出来时填 null。
4. 抽取项目名、出资方、金额与币种、截止日(bid/eoi/pq/rfp,每个日期都要有原文依据)、国籍或资格限制(如"仅限成员国")。原文没有的填 null 或空数组,不要推测。日期写成 YYYY-MM-DD。
5. title_zh:原标题的中文直译,不超过 60 个字,只译不评(原题已是中文的照抄);机构与银行用通用中文译名,项目编号、合同号保留原文。
   summary_zh:一句中文,不超过 80 个字,只概括原文说了什么:是什么公告、哪个项目、关键日期或金额;不要写你的判断(如「不属于……」「与……无关」)。
   why:一句话说明你判 relevant 为 true / false 的理由(如「不属于阿塞拜疆水务项目机会」),判断只写在这里。
6. ledger_links:对照下方台账,这条线索对应哪些项目(可多个,说明理由;只填台账里存在的编号;只对应同一国的项目——别国的同类项目,如都是减漏,不算)。都不对应、且确属新的项目机会时 new_opportunity=true。
7. evidence:支撑以上结论的原文片段,保持原语言逐字摘录,每段不超过 200 字。

台账项目({country}):
{ledger}
"""

# DeepSeek 的 JSON 输出只保证是合法 JSON,不保证字段:提示里写明字段与取值,代码再校验
JSON_GUIDE = """
输出格式:只输出一个 JSON 对象,不要任何其他文字。字段与示例:
{"relevant": true, "category": "eoi", "sectors": ["wastewater"], "country": "AZE", "title_zh": "原题中文直译", "summary_zh": "……", "why": "判定理由", "project": "项目名或 null", "funder": "出资方或 null",
 "amount": {"value": 12000000, "currency": "USD"}, "deadlines": [{"type": "eoi_deadline", "date": "2026-10-12", "evidence": "原文片段"}],
 "eligibility": "资格限制或 null", "ledger_links": [{"item": "台账编号", "confidence": "high", "reason": "理由"}],
 "new_opportunity": false, "evidence": ["原文片段"]}
没有金额时 amount 为 null;没有截止日时 deadlines 为 []。
category 只能取:""" + ", ".join(CATEGORIES) + """。
sectors 只能取:""" + ", ".join(SECTORS) + """。
deadlines 里的 type 只能取:""" + ", ".join(DEADLINE_TYPES) + """;ledger_links 里的 confidence 只能取:high, medium, low。
"""

USER = """信源:{source}
链接:{url}
标题:{title}
发布日期:{published}
结构化字段:{fields}
规则初筛:{rules}
正文与附件摘录{trunc}:
{text}
"""


@dataclass
class LLMConfig:
    provider: str = "anthropic"  # anthropic | deepseek
    model: str | None = None  # 不设则用该接入的默认模型
    effort: str | None = None  # 思考深度:anthropic low/medium/high/xhigh/max;deepseek low/high/max
    max_calls: int = 60
    max_doc_chars: int = 60_000
    workers: int = 4  # 并发调用数(DeepSeek 思考模式单次约半分钟到一分钟)
    max_tokens: int | None = None
    fallbacks: bool = True  # 仅 anthropic:安全分类器拒绝时由 API 换推荐模型重跑
    base_url: str | None = None  # 仅 deepseek
    thinking: bool = True  # deepseek 的思考模式(判定线索要;助手解析一句提问不要,关掉快十倍)

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"watch.llm.provider={self.provider!r} 不认识,可选:{', '.join(PROVIDERS)}")
        d = PROVIDERS[self.provider]
        self.model = self.model or d["model"]
        self.effort = self.effort or d["effort"]
        self.max_tokens = self.max_tokens or d["max_tokens"]

    @classmethod
    def from_profile(cls, raw: dict | None, **override) -> LLMConfig:
        raw = {**(raw or {}), **override}
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

    @classmethod
    def for_assistant(cls, watch_llm: dict | None, assistant_llm: dict | None, max_tokens: int = 2_000) -> LLMConfig:
        """助手的接入:沿用 watch.llm 的 provider / model / base_url,关思考、浅思考深度、短输出、不限次数;profile 的 assistant.llm 可覆盖任一项。
        max_tokens 是缺省输出上限:解析一句提问 2000 够;看一份文件(逐条列项目、资格要求)要大得多(assistant_server.FILE_MAX_TOKENS)。"""
        base = {k: v for k, v in (watch_llm or {}).items() if k in ("provider", "model", "base_url", "fallbacks")}
        cfg = {**base, "effort": "low", "max_tokens": max_tokens, "max_calls": 10 ** 9, "workers": 4, "thinking": False, **(assistant_llm or {})}
        if cfg.get("provider") == "anthropic" and cfg.get("effort") not in ("low", "medium", "high", "xhigh", "max"):
            cfg["effort"] = "low"
        return cls.from_profile(cfg)


def has_credentials(provider: str = "anthropic") -> bool:
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()  # `ant auth login` 的本地凭据


def ledger_lines(led) -> str:
    if not led or not led.items:
        return "(暂无台账)"
    rows, countries = [], item_countries(led)
    for it in led.items:
        iso = countries.get(it["id"])
        ids = ";".join(f"{k} {v}" for k, v in (it.get("ids") or {}).items())
        funders = "、".join(dict.fromkeys(str(f.get("funder")) for f in it.get("financing") or []))
        names = " / ".join(x for x in [it.get("name_en"), it.get("name_local"), *(it.get("aliases") or [])] if x)
        rows.append(f"- {it['id']} {it['name_zh']}" + (f"|国别 {geo.name_zh(iso)}" if iso else "") + (f"|{names}" if names else "")
                    + (f"|编号 {ids}" if ids else "")
                    + f"|阶段 {it.get('stage')}" + (f"|出资 {funders}" if funders else ""))
    return "\n".join(rows)


# ---------------------------------------------------------------- 输出校验与归一


def _text(v, limit: int = 400) -> str | None:
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none")):
        return None
    return str(v).strip()[:limit]


def _bool(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "是"))


def _iso(v) -> str | None:
    try:
        return date.fromisoformat(str(v).strip()[:10]).isoformat()
    except ValueError:
        return None


def _list(v) -> list:
    """模型偶尔把单个对象或字符串直接放在数组字段里。"""
    if isinstance(v, list):
        return v
    return [v] if isinstance(v, (dict, str)) and v else []


def normalize(data: dict) -> dict:
    """把模型输出整理成下游认得的形状:枚举不认识的归 other/low,日期不是 YYYY-MM-DD 的丢弃,缺的字段补空。"""
    cat = str(data.get("category") or "other").strip().lower()
    amt = data.get("amount")
    amount = None
    if isinstance(amt, dict) and isinstance(amt.get("value"), (int, float)) and _text(amt.get("currency"), 10):
        amount = {"value": float(amt["value"]), "currency": _text(amt["currency"], 10).upper()}
    deadlines = []
    for d in _list(data.get("deadlines")):
        if isinstance(d, dict) and _iso(d.get("date")):
            t = str(d.get("type") or "other").strip().lower()
            deadlines.append({"type": t if t in DEADLINE_TYPES else "other", "date": _iso(d["date"]),
                              "evidence": _text(d.get("evidence")) or ""})
    links = []
    for lk in _list(data.get("ledger_links")):
        if isinstance(lk, dict) and _text(lk.get("item"), 20):
            c = str(lk.get("confidence") or "low").strip().lower()
            links.append({"item": _text(lk["item"], 20).upper(), "confidence": c if c in CONFIDENCE else "low",
                          "reason": _text(lk.get("reason")) or ""})
    sectors = [x for x in dict.fromkeys(str(v).strip().lower() for v in _list(data.get("sectors"))) if x in SECTORS]
    return {
        "relevant": _bool(data.get("relevant")), "category": cat if cat in CATEGORIES else "other",
        "sectors": sectors, "country": geo.iso3(_text(data.get("country"), 80)),
        "title_zh": _text(data.get("title_zh"), 200) or "", "summary_zh": _text(data.get("summary_zh"), 200) or "", "why": _text(data.get("why"), 200) or "",
        "project": _text(data.get("project")),
        "funder": _text(data.get("funder")), "amount": amount, "deadlines": deadlines,
        "eligibility": _text(data.get("eligibility")), "ledger_links": links,
        "new_opportunity": _bool(data.get("new_opportunity")),
        "evidence": [e for e in (_text(x, 300) for x in _list(data.get("evidence"))) if e][:6],
    }


# ---------------------------------------------------------------- DeepSeek 接口


class DeepSeekError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class DeepSeekClient:
    """DeepSeek 开放平台(OpenAI 格式 /chat/completions)。限流、服务端错误、超时重试 2 次(间隔 5、20 秒)。"""

    RETRY_STATUS = (429, 500, 502, 503, 504)
    WAITS = (5, 20)

    def __init__(self, api_key: str, base_url: str = DEEPSEEK_BASE_URL, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        # 非流式请求在推理期间服务端会持续发空行保活,读超时按块计,给足 10 分钟
        self.http = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                 timeout=httpx.Timeout(600, connect=30), transport=transport)
        self.sleep = sleep

    def chat(self, body: dict) -> dict:
        for attempt in range(len(self.WAITS) + 1):
            last = attempt == len(self.WAITS)
            try:
                r = self.http.post("/chat/completions", json=body)
            except httpx.TransportError as e:  # 含超时
                if last:
                    raise DeepSeekError(None, f"连接失败:{type(e).__name__}") from e
                self.sleep(self.WAITS[attempt])
                continue
            if r.status_code in self.RETRY_STATUS and not last:
                self.sleep(self.WAITS[attempt])
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message") or r.text
                except ValueError:
                    msg = r.text
                raise DeepSeekError(r.status_code, str(msg)[:300])
            return r.json()
        raise AssertionError("unreachable")


# ---------------------------------------------------------------- 判定器


def _friendly(provider: str, status: int | None, message: str) -> tuple[str, bool]:
    """→ (提醒页上显示的说明, 是否不必再试:余额不足、密钥无效)。"""
    low = (message or "").lower()
    if (provider == "deepseek" and status == 402) or "credit balance" in low:
        where = "platform.deepseek.com 充值" if provider == "deepseek" else "Claude Console 的 Plans & Billing 充值"
        return f"模型 API 账户余额不足,请到 {where};充值后下次运行自动补判", True
    if status == 401:
        return "模型 API 密钥无效或已过期,请检查 GitHub 仓库 Settings → Secrets 里的密钥", True
    if status == 429:
        return f"模型 API 限流:{message[:160]}", False
    if status is None:
        return message[:200], False
    return f"模型 API HTTP {status}:{message[:200]}", False


@dataclass
class Classifier:
    cfg: LLMConfig
    client: object | None = None
    calls: int = 0
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    errors: list[str] = field(default_factory=list)
    note: str = ""
    fatal: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        if self.client is None and has_credentials(self.cfg.provider):
            if self.cfg.provider == "deepseek":
                self.client = DeepSeekClient(os.environ["DEEPSEEK_API_KEY"], self.cfg.base_url or DEEPSEEK_BASE_URL)
            else:
                import anthropic

                self.client = anthropic.Anthropic()
        if self.client is None:
            key = "DEEPSEEK_API_KEY" if self.cfg.provider == "deepseek" else "ANTHROPIC_API_KEY"
            self.note = f"未配置模型 API 凭据({key}),本次只用规则初筛"

    @property
    def label(self) -> str:
        return f"{self.cfg.model}({'DeepSeek' if self.cfg.provider == 'deepseek' else 'Claude'})"

    @property
    def enabled(self) -> bool:
        return self.client is not None and not self.fatal and len(self.errors) < 5  # 连续出错就停,不拖垮整次运行

    def system(self, country: str, bidder: str, led) -> str:
        return SYSTEM.format(country=country, bidder=bidder or "未设", ledger=ledger_lines(led))

    def _fail(self, status: int | None, message: str) -> None:
        text, fatal = _friendly(self.cfg.provider, status, message)
        with self.lock:
            self.errors.append(text)
            if fatal:
                self.fatal = text

    def _add_usage(self, **kw: int) -> None:
        with self.lock:
            for k, v in kw.items():
                self.usage[k] += v or 0

    def classify_many(self, system: str, jobs: list[tuple[dict, str]]) -> list[dict | None]:
        """并发判定多条线索:jobs = [(线索, 正文)],结果与输入顺序一致。"""
        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as ex:
            return list(ex.map(lambda j: self.classify(system, j[0], j[1]), jobs))

    def classify(self, system: str, sig: dict, doc_text: str) -> dict | None:
        """→ 归一后的判定结果;{"_error": …} 表示这条模型处理不了(不再重试);None 表示没调用或调用失败(下次运行补判)。"""
        with self.lock:  # 预算检查与计数在锁内,并发时不超额
            if not self.enabled:
                return None
            if self.calls >= self.cfg.max_calls:
                self.note = f"本次模型调用已达上限 {self.cfg.max_calls} 次,其余线索先按规则显示,下次运行补判"
                return None
            self.calls += 1
        text = doc_text or sig.get("excerpt") or ""
        trunc = ""
        if len(text) > self.cfg.max_doc_chars:
            trunc = f"(全文 {len(text):,} 字,只送前 {self.cfg.max_doc_chars:,} 字)"
            text = text[: self.cfg.max_doc_chars]
        user = USER.format(source=sig.get("source_name") or sig["source"], url=sig["url"], title=sig["title"],
                           published=sig.get("published") or "未知", fields=json.dumps(sig.get("fields") or {}, ensure_ascii=False),
                           rules=json.dumps(sig.get("rules") or {}, ensure_ascii=False), trunc=trunc, text=text)
        out = self._call(system, user, SCHEMA, JSON_GUIDE)
        if out is None or "_error" in out:
            return out
        data = normalize(out)
        data["_model"] = out.get("_model") or self.cfg.model
        if trunc:
            data["_truncated"] = trunc
        return data

    def write(self, system: str, user: str, fields: dict[str, str]) -> dict | None:
        """让模型按字段写中文文本(报告的分析段落):fields = {字段: 写什么}。→ {字段: 文本};失败返回 None 或 {"_error"}。"""
        with self.lock:
            if not self.enabled:
                return None
            self.calls += 1
        schema = {"type": "object", "properties": {k: {"type": "string"} for k in fields}, "required": list(fields),
                  "additionalProperties": False}
        guide = ("\n输出格式:只输出一个 JSON 对象,不要任何其他文字。字段:\n"
                 + "\n".join(f'- "{k}":{v}' for k, v in fields.items()) + "\n字段值是 Markdown 文本(可用列表),不要再嵌套 JSON。\n")
        out = self._call(system, user, schema, guide)
        if out is None or "_error" in out:
            return out
        return {k: str(out.get(k) or "").strip() for k in fields}

    def _call(self, system: str, user: str, schema: dict, guide: str) -> dict | None:
        return self._deepseek(system, user, guide) if self.cfg.provider == "deepseek" else self._anthropic(system, user, schema)

    def ask(self, system: str, user: str, schema: dict, guide: str) -> dict | None:
        """通用的一问一答(助手的提问解析):按 schema(Claude)/ guide(DeepSeek)要 JSON,返回模型给的 dict(带 _model),
        失败返回 None 或 {"_error"};计次与出错停用的规则同 classify。"""
        with self.lock:
            if not self.enabled:
                return None
            if self.calls >= self.cfg.max_calls:
                return None
            self.calls += 1
        return self._call(system, user, schema, guide)

    def forgive(self) -> None:
        """常驻的助手用:清掉累计的非致命错误,让 enabled 回到 True(致命的——余额不足、密钥无效——不清)。"""
        with self.lock:
            if not self.fatal:
                self.errors.clear()

    # ---- Claude
    def _anthropic(self, system: str, user: str, schema: dict) -> dict | None:
        import anthropic

        kw = {
            "model": self.cfg.model, "max_tokens": self.cfg.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": self.cfg.effort},
        }
        if self.cfg.fallbacks:
            kw.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            with self.client.beta.messages.stream(**kw) as stream:
                resp = stream.get_final_message()
        except anthropic.RateLimitError as e:
            self._fail(429, e.message)
            return None
        except anthropic.APIStatusError as e:
            self._fail(e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            self._fail(None, f"连接失败:{e}")
            return None
        u = resp.usage
        self._add_usage(input=u.input_tokens, output=u.output_tokens, cache_read=getattr(u, "cache_read_input_tokens", 0),
                        cache_write=getattr(u, "cache_creation_input_tokens", 0))
        if resp.stop_reason == "refusal":
            return {"_error": "模型拒绝处理(refusal)", "_model": resp.model}
        if resp.stop_reason == "max_tokens":
            return {"_error": "输出被 max_tokens 截断", "_model": resp.model}
        body = next((b.text for b in resp.content if b.type == "text"), "")
        return self._parse(body, resp.model)

    # ---- DeepSeek
    def _deepseek(self, system: str, user: str, guide: str) -> dict | None:
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system + guide}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.thinking:
            body.update({"thinking": {"type": "enabled"}, "reasoning_effort": self.cfg.effort})
        else:
            body["thinking"] = {"type": "disabled"}
        fin, content, model = None, "", self.cfg.model
        for _ in range(2):  # JSON 输出偶尔返回空内容、推理资源不足会中断:各重试一次
            try:
                resp = self.client.chat(body)
            except DeepSeekError as e:
                self._fail(e.status, e.message)
                return None
            u = resp.get("usage") or {}
            self._add_usage(input=u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0)),
                            cache_read=u.get("prompt_cache_hit_tokens", 0), output=u.get("completion_tokens", 0))
            model = resp.get("model") or model
            choice = (resp.get("choices") or [{}])[0]
            fin, content = choice.get("finish_reason"), ((choice.get("message") or {}).get("content") or "")
            if fin == "length":
                return {"_error": "输出被 max_tokens 截断", "_model": model}
            if fin == "content_filter":
                return {"_error": "内容被模型服务方过滤", "_model": model}
            if fin == "stop" and content.strip():
                break
        else:
            return {"_error": f"模型没有返回内容(finish_reason={fin})", "_model": model}
        return self._parse(content, model)

    @staticmethod
    def _parse(body: str, model: str) -> dict:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {"_error": "输出不是合法 JSON", "_model": model}
        if not isinstance(data, dict):
            return {"_error": "输出不是 JSON 对象", "_model": model}
        data["_model"] = model
        return data
