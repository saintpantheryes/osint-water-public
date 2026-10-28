/* 页面的公用脚本(架构方案第 5 节):读 config.json 里的编号,带着登录表格的 cookie 直接调表格接口;一律 fieldKeyType=id;
   不整表下载(按条件 filter、按窗口取、计数用 aggregation/row-count)。
   首页、日历、国别概况只读;商机工作台与线索处理页会以登录人的身份写(新建跟进、报告申请,改阶段、归属等),权限仍由表格管。 */
(function () {
  "use strict";
  const TZ = "Asia/Shanghai";
  const P = {
    cfg: null,
    tz: TZ,
    onto: null,
    async load() {
      /* 角色那一问(GET /api/base/{库})要库编号:上次记下的编号先拿来,跟配置、本体同时发,省掉一整轮来回;编号对不上再按配置重问。
         顶栏先画的页面在 nav() 里已经发出去了(连同「这是谁」那一问),这里不再发第二遍 */
      this._askEarly();
      const bid = this._baseEarlyId;
      const early = this._baseEarly;
      const [r, o] = await Promise.all([fetch("config.json", { cache: "no-store" }), fetch("ontology.json")]);
      if (!r.ok) throw new Error("读不到 config.json(" + r.status + ")");
      if (!o.ok) throw new Error("读不到 ontology.json(" + o.status + ")");
      this.cfg = await r.json();
      this.onto = await o.json();  /* 本体(docs/ontology/ontology.yaml 转的 JSON):阶段、口径、归组都从这里取,不在脚本里抄 */
      this.tz = this.cfg.timezone || TZ;
      const id = this.cfg.base && this.cfg.base.id;
      if (id) { try { localStorage.setItem("ow.base", id); } catch (e) { /* 记不下也不碍事 */ } }
      if (!(early && id === bid)) this._baseEarly = null;  /* 记下的编号对不上(换了库):早发的那一问作废,按配置重问 */
      await this.role();  /* 登录人在库里的角色:不在库里的(游客)之后所有读表请求改走后台的只读通道 */
      this.followed();  /* 关注的国别:先发出去不等,清单与国别下拉用到时再等(P.followed) */
      if (this._navEarly) this.navFill();  /* 顶栏在读配置之前就先画好了:这时补上「后台数据表格」与帐号区 */
      return this.cfg;
    },
    /* 游客模式:登录人不是库的协作者(GET /api/base/{库} 是 403 / 404)。读表走 api/assistant/guest/*(后台任务进程里的 core/guest.py,
       只放行本体 guest 节里的对象与属性,报告只给定期报告的公开版);不放行的表(跟进记录、成员、报告模板)页面上直接当空表,不发请求;
       写一律不发;「打开表格」「在表格里打开」不显示。库里的查看者、评论者仍直接读表格(他们在表格里本来就看得到)。 */
    get isGuest() { return this._role === "none"; },
    /* 伙伴帐号(本体 partner 节):也是库外的登录人,但邮箱在私有 team.yaml 的伙伴名单里;读的是真实库的公开部分(通道的 policy 答 tier: partner)。
       商机跟进这一组(商机总览、商机工作台、日历)与线索处理对伙伴不开放:菜单里不列,直接打开只给一句说明。 */
    get isPartner() { return this.isGuest && !!(this.gpol && this.gpol.tier === "partner"); },
    get outsiderName() { return this.isPartner ? "伙伴帐号" : "游客帐号"; },
    PARTNER_CLOSED: ["opps.html", "opp.html", "calendar.html", "inbox.html"],
    gpol: null,
    _guestEntry(path) {
      const m = /^\/table\/([^/]+)\//.exec(path);
      if (!m) return null;
      const oid = Object.keys(this.cfg.tables).find((k) => this.cfg.tables[k].id === m[1]);
      return { oid, allowed: !!(oid && this.gpol && this.gpol.objects && this.gpol.objects[oid]) };
    },
    /* 本体:分类体系的值(按本体顺序)、英文键 ↔ 中文值、口径、归组 */
    taxonomy(tid) { const t = (this.onto.taxonomies || {})[tid]; if (!t) throw new Error("本体里没有分类体系 " + tid); return t; },
    values(tid) { return this.taxonomy(tid).groups.flatMap((g) => g.values.map((v) => v.v)); },
    valueDefs(tid) { return this.taxonomy(tid).groups.flatMap((g) => g.values.map((v) => Object.assign({ group: g.name }, v))); },
    glossary(key) { const g = (this.onto.glossary || {})[key]; if (!g) throw new Error("本体里没有口径 " + key); return g; },
    grouping(gid) { const g = (this.onto.groupings || {})[gid]; if (!g) throw new Error("本体里没有归组 " + gid); return g; },
    /* 对象 / 属性的中文名(页面文案也跟着本体走) */
    object(oid) { return (this.onto.objects || []).find((o) => o.id === oid) || null; },
    propDef(oid, pid) { const o = this.object(oid); return o ? (o.properties.find((p) => p.id === pid) || null) : null; },
    propName(oid, pid) { const p = this.propDef(oid, pid); return p ? p.name : pid; },
    /* 语义层(本体 semantic):助手能算的指标(评标天数、中标价 / 招标估价……)与算法(平均、中位数、合计) */
    measure(id) { return ((this.onto.semantic || {}).measures || []).find((m) => m.id === id) || null; },
    aggName(id) { const a = ((this.onto.semantic || {}).aggs || []).find((x) => x.id === id); return a ? a.name : id; },
    t(oid) { const t = this.cfg.tables[oid]; if (!t) throw new Error("配置里没有表 " + oid); return t; },
    f(oid, prop) {
      const t = this.t(oid);
      const id = (prop === "key" && t.key) || t.props[prop] || t.fields[prop];
      if (!id) throw new Error("配置里「" + t.name + "」没有字段 " + prop);
      return id;
    },
    has(oid, prop) { const t = this.t(oid); return !!((prop === "key" && t.key) || t.props[prop] || t.fields[prop]); },
    view(oid, name) { return this.t(oid).views[name] || null; },
    choices(oid, prop) { return (this.t(oid).choices || {})[prop] || []; },
    async api(path, params) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params || {})) {
        if (v === undefined || v === null) continue;
        if (Array.isArray(v) && v.every((x) => typeof x !== "object")) v.forEach((x) => qs.append(k, x));  // projection 这类重复参数
        else qs.append(k, typeof v === "object" ? JSON.stringify(v) : String(v));  // filter、orderBy 是 JSON
      }
      if (this.isGuest && path.startsWith("/table/")) {
        const g = this._guestEntry(path);
        if (!g.allowed) {  /* 不对游客开放的表:当空表,不发请求 */
          if (/\/aggregation\/row-count$/.test(path)) return { rowCount: 0 };
          if (/\/record$/.test(path)) return { records: [] };
          throw new Error(this.outsiderName + "看不到这部分内容");
        }
        const g2 = await fetch("api/assistant/guest" + path + (qs.toString() ? "?" + qs : ""), { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (g2.status === 401) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
        if (g2.status === 403 || g2.status === 404) throw new Error(this.outsiderName + "看不到这部分内容");
        if (!g2.ok) throw new Error(path + " → " + g2.status + " " + (await g2.text()).slice(0, 200));
        return g2.json();
      }
      const r = await fetch("/api" + path + (qs.toString() ? "?" + qs : ""), { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (!r.ok) throw new Error(path + " → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    /* 写:以登录人的身份;关联、人员字段按 config 里的形状(one = {id},many = [{id}]) */
    /* 「这是谁」只问一次:几处同时要(帐号区、线索处理页……)共用同一个请求 */
    me() {
      if (!this._mep) this._mep = this.api("/auth/user").then((m) => m || {}).catch((e) => { this._mep = null; throw e; });  /* 问失败的下次再问 */
      return this._mep;
    },
    /* 顶栏一画好就先问「这是谁」与登录角色(角色那一问要库编号,用上次记下的),不等配置与本体:登录人的名字几百毫秒就能出来,
       不用等配置、本体(几百 KB)、角色一串问完再问。编号对不上的,load() 作废这一问、按配置重问 */
    _askEarly() {
      if (!this._mep) this.me().catch(() => {});
      if (this._baseEarly === undefined) {
        let bid = ""; try { bid = localStorage.getItem("ow.base") || ""; } catch (e) { /* 隐私模式读不到就照旧 */ }
        this._baseEarlyId = bid;
        this._baseEarly = bid ? this._baseFetch(bid) : null;
      }
    },
    /* 登录人在库里的角色(owner/creator/editor/commenter/viewer);不在库里(403 / 404)记 "none" = 游客;问不到就空 */
    ROLE_ZH: { owner: "所有者", creator: "创建者", editor: "编辑者", commenter: "评论者", viewer: "查看者", none: "游客" },
    _baseFetch(id) { return fetch("/api/base/" + id, { credentials: "same-origin", headers: { Accept: "application/json" } }); },
    async role() {
      if (this._role === undefined) {
        try {
          const r = this.cfg && this.cfg.base ? await (this._baseEarly || this._baseFetch(this.cfg.base.id)) : null;
          if (!r) this._role = "";
          else if (r.status === 401) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); this._role = ""; }
          else if (r.status === 403 || r.status === 404) { this._role = "none"; document.documentElement.classList.add("is-guest"); }
          else if (!r.ok) this._role = "";
          else this._role = ((await r.json()) || {}).role || "";
        } catch (e) { console.warn("读不到角色", e); this._role = ""; }
        if (this._role === "none" && !this.gpol) {
          try { const p = await fetch("api/assistant/guest/policy", { credentials: "same-origin", headers: { Accept: "application/json" } }); this.gpol = p.ok ? await p.json() : { objects: {} }; }
          catch (e) { console.warn("读不到游客范围", e); this.gpol = { objects: {} }; }
        }
      }
      return this._role;
    },
    /* 登录人在库里能不能写:查看者、评论者只能看;游客(不在库里)只能看公开内容。问不到角色就当能写,照旧由表格在写入时拒绝。 */
    async canWrite() { const r = await this.role(); return !(r === "viewer" || r === "commenter" || r === "none"); },
    /* 只读访客:页面顶部提示一行,写入区块里的按钮和输入框一律禁用,之后动态加出来的也禁用。游客的提示说清只看公开内容。 */
    guest(note) {
      document.body.classList.add("guest");
      const main = document.querySelector("main") || document.body;
      const el = document.createElement("div"); el.className = "note n-info guest-note";
      const scope = this.isPartner && this.gpol && Array.isArray(this.gpol.countries) ? "只看这些国家:" + (this.gpol.countries.map((c) => c.name).join("、") || "(没有)") + "。" : "";
      el.textContent = note || (this.isPartner ? (((this.gpol && this.gpol.note) || "当前是伙伴帐号:只看公开内容(真实数据);商机跟进、我方资料、内部备注与内部版报告不开放;不能登记、申请或订阅。") + scope)
        : this.isGuest ? "当前是游客帐号:页面上的线索、商机、国别、时间节点和报告都是编出来的示例数据,用来演示系统怎么用,不是真实内容;不能登记、申请或订阅。"
        : "当前帐号只有查看权限(游客演示帐号):可以浏览全部页面,登记跟进、申请报告、归入和改负责人这些操作不可用。");
      main.insertBefore(el, main.firstChild);
      const SEL = ".form button, .form input, .form select, .form textarea, .batch button, .batch input, .it-act button, .it-act select";
      const off = (root) => { (root.matches && root.matches(SEL) ? [root] : []).concat([...(root.querySelectorAll ? root.querySelectorAll(SEL) : [])]).forEach((x) => { if (!x.disabled) x.disabled = true; }); };
      off(main);
      new MutationObserver((ms) => ms.forEach((m) => { if (m.type === "attributes") off(m.target); else m.addedNodes.forEach((n) => { if (n.nodeType === 1) off(n); }); }))
        .observe(main, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    },
    /* 对伙伴不开放的页面(商机总览、商机工作台、日历、线索处理):正文换成一句说明,别的不画。页面在 load() 之后调:if (P.isPartner) return P.partnerOnly(); */
    partnerOnly() {
      const main = document.querySelector("main") || document.body;
      main.innerHTML = '<div class="note n-info guest-note">这一页是商机跟进与线索处理,伙伴帐号不开放。可以看的:项目线索的总览与分析、国别与城市概况、趋势机会、法规、公开版报告。</div><p><a class="btn" href="overview.html">去线索总览 →</a></p>';
      document.body.classList.add("guest");
      return null;
    },
    val(oid, prop, v) {
      if (v === undefined || v === null || v === "") return undefined;
      const kind = (this.t(oid).links || {})[prop];
      if (!kind) return v;
      const ids = (Array.isArray(v) ? v : [v]).map((x) => (x && typeof x === "object" ? x.id : x)).filter(Boolean);
      if (!ids.length) return undefined;
      return kind === "many" ? ids.map((id) => ({ id })) : { id: ids[0] };
    },
    fieldsOf(oid, values) { const out = {}; for (const [prop, v] of Object.entries(values)) { const x = this.val(oid, prop, v); if (x !== undefined) out[this.f(oid, prop)] = x; } return out; },
    async send(method, path, body) {
      if (this.isGuest) throw new Error("游客帐号不能写入");
      const r = await fetch("/api" + path, { method, credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) });
      if (r.status === 401 || r.status === 403) throw new Error(r.status === 401 ? "登录已过期,请刷新页面重新登录" : "你在表格里没有写入权限(找管理员把角色改成「编辑者」)");
      if (!r.ok) throw new Error(path + " → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    /* 传一个文件到记录的附件字段(追加),以登录人的身份 */
    async upload(oid, rid, prop, file) {
      if (this.isGuest) throw new Error("游客帐号不能写入");
      const fd = new FormData(); fd.append("file", file, file.name);
      const r = await fetch("/api/table/" + this.t(oid).id + "/record/" + rid + "/" + this.f(oid, prop) + "/uploadAttachment", { method: "POST", credentials: "same-origin", body: fd });
      if (r.status === 401 || r.status === 403) throw new Error(r.status === 401 ? "登录已过期,请刷新页面重新登录" : "你在表格里没有写入权限(找管理员把角色改成「编辑者」)");
      if (!r.ok) throw new Error("上传 → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    async create(oid, values) {
      const r = await this.send("POST", "/table/" + this.t(oid).id + "/record", { fieldKeyType: "id", typecast: true, records: [{ fields: this.fieldsOf(oid, values) }] });
      return ((r || {}).records || [])[0] || null;
    },
    async patch(oid, rid, values) {
      return this.send("PATCH", "/table/" + this.t(oid).id + "/record", { fieldKeyType: "id", typecast: true, records: [{ id: rid, fields: this.fieldsOf(oid, values) }] });
    },
    /* 条件 */
    cond(oid, prop, operator, value) { const c = { fieldId: this.f(oid, prop), operator }; if (value !== undefined) c.value = value; return c; },
    and(...conds) { return { conjunction: "and", filterSet: conds.filter(Boolean) }; },
    or(...conds) { return { conjunction: "or", filterSet: conds.filter(Boolean) }; },
    onDay(d) { return { mode: "exactDate", exactDate: d + "T00:00:00.000Z", timeZone: this.tz }; },
    between(oid, prop, from, to) { return [this.cond(oid, prop, "isOnOrAfter", this.onDay(from)), this.cond(oid, prop, "isOnOrBefore", this.onDay(to))]; },
    /* 读 */
    async records(oid, opt) {
      opt = opt || {};
      const t = this.t(oid), take = Math.min(opt.take || 1000, 1000), out = [];
      const one = (skip) => this.api("/table/" + t.id + "/record", {
        fieldKeyType: "id", take, skip, filter: opt.filter, viewId: opt.viewId, orderBy: opt.orderBy,
        projection: (opt.props || []).filter((p) => this.has(oid, p)).map((p) => this.f(oid, p)),  /* 配置里还没有的字段(表结构没更新)跳过 */
      });
      const first = await one(0);
      if (!first) return out;
      out.push(...(first.records || []));
      const cap = opt.take || Infinity;
      if (out.length < take || out.length >= cap) return out.slice(0, cap);
      /* 第一页满了(城市表三四千行):后面的页每次四页同时取,不再一页等一页;遇到不满的一页就停 */
      for (let skip = take; ; skip += 4 * take) {
        const n = Math.min(4, Math.ceil((cap - skip) / take));
        const pages = await Promise.all(Array.from({ length: n }, (_, i) => one(skip + i * take)));
        for (const page of pages) {
          const recs = (page && page.records) || [];
          out.push(...recs);
          if (recs.length < take || out.length >= cap) return out.slice(0, cap);
        }
      }
    },
    async count(oid, filter, viewId) {
      const r = await this.api("/table/" + this.t(oid).id + "/aggregation/row-count", { filter, viewId });
      return r ? Number(r.rowCount || 0) : 0;
    },
    get(oid, rec, prop) { return this.has(oid, prop) ? (rec.fields || {})[this.f(oid, prop)] : undefined; },
    /* 投资规模:「1.5 亿 USD(≈ 10.8 亿元)」这样的一句;没填的空串 */
    sizeText(oid, rec) {
      const v = this.get(oid, rec, oid === "lead" ? "amount_value" : "size_value"), cur = this.get(oid, rec, "currency"), cny = this.get(oid, rec, oid === "lead" ? "amount_cny" : "size_cny");
      const big = (n) => (n >= 1e8 ? (n / 1e8).toFixed(n >= 1e9 ? 0 : 1) + " 亿" : n >= 1e4 ? (n / 1e4).toFixed(n >= 1e6 ? 0 : 1) + " 万" : String(n));
      if (v === undefined || v === null || v === "") return cny ? "≈ " + big(cny * 1e4) + " 元" : "";
      return big(Number(v)) + " " + (cur || "") + (cny ? "(≈ " + big(cny * 1e4) + " 元)" : "");
    },
    /* 值 → 文字 */
    text(v) {
      if (v === null || v === undefined) return "";
      if (Array.isArray(v)) return v.map((x) => this.text(x)).join("、");
      if (typeof v === "object") return v.title || v.name || v.id || "";
      if (typeof v === "boolean") return v ? "是" : "否";
      return String(v);
    },
    linkId(v) { if (!v) return null; if (Array.isArray(v)) v = v[0]; return v && typeof v === "object" ? v.id : null; },
    linkIds(v) { if (!v) return []; if (!Array.isArray(v)) v = [v]; return v.map((x) => (x && typeof x === "object" ? x.id : null)).filter(Boolean); },
    day(v) {
      if (!v) return "";
      const s = String(v);
      if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
      const d = new Date(s);
      if (isNaN(d)) return s.slice(0, 10);
      return new Intl.DateTimeFormat("sv-SE", { timeZone: this.tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
    },
    today() { return this.day(new Date().toISOString()); },
    addDays(d, n) { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); },
    esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
    /* 附件下载地址:表格存的签名地址可能带着生成时的网址(换域名前的旧网址打不开),只取路径,在本站打开。 */
    fileUrl(a) {
      const u = String((a && (a.presignedUrl || a.url)) || "");
      if (!u) return "#";
      try { const x = new URL(u, location.origin); if (x.pathname.startsWith("/api/attachments/")) return x.pathname + x.search + x.hash; } catch (e) { /* 原样用 */ }
      return u;
    },
    /* 在表格里打开:/base/{库}/{表}/{视图}?recordId={记录} */
    recordUrl(oid, rid) { if (this.isGuest) return oid === "opportunity" ? this.oppUrl(rid) : "#"; const t = this.t(oid); return "/base/" + this.cfg.base.id + "/" + t.id + "/" + (t.views["全部"] || "") + "?recordId=" + encodeURIComponent(rid); },
    tableUrl(oid, viewName) { if (this.isGuest) return "#"; const t = this.t(oid); return "/base/" + this.cfg.base.id + "/" + t.id + "/" + (t.views[viewName || "全部"] || ""); },
    open(oid, rid, label) { if (this.isGuest) return ""; return '<a class="open" href="' + this.recordUrl(oid, rid) + '" target="_blank" rel="noopener">' + this.esc(label || "在表格里打开") + "</a>"; },
    /* 菜单(各页入口只在这一处):首页 + 四组(工作原理、市场背景、项目线索、商机跟进)。每页 [页面, 名称, 一句话说明];"@table" 是表格本身(新窗口打开,游客不显示)。
       组的第一页是点组名进去的那一页;一句话说明是页签的提示,工作流程页的「各页是干什么的」也读它。商机工作台从商机总览点进;「我的」「我方资料」在帐号区,都不进菜单。 */
    MENU: [
      { name: "首页", href: "index.html", desc: "提问入口:一句话查台账。" },
      { name: "工作原理", items: [
        ["how.html", "工作流程", "线索到报告的主线、每天的节奏、人和机器各做什么、红线。"],
        ["ontology.html", "本体", "系统里有哪些对象、属性、口径与规则(只读)。"],
        ["sources.html", "信源", "机器每天去哪些网站收集线索、最近采得怎样;可停用或启用。"],
        ["@table", "后台数据表格", "在表格里直接看、改全部原始数据。"],
      ] },
      { name: "市场背景", items: [
        ["country.html", "国别概况", "一国的指标、机构法规、城市与水务片区、历史资料与中标、商机、线索与节点。"],
        ["city.html", "城市概况", "规模以上城市的人口、面积、分布,与关联的商机、线索。"],
        ["trend.html", "趋势机会", "按水风险、人均 GDP 与开发性贷款推算哪里缺水又有钱建:方向参考,不是具体项目。"],
      ] },
      { name: "项目线索", items: [
        ["overview.html", "线索总览", "全局数字、漏斗、地图、最新报告。"],
        ["analysis.html", "线索分析", "每日简报与线索分析图表。"],
        ["inbox.html", "线索处理", "确认线索归属,决定 AI 推荐的商机跟不跟,可批量处理。"],
      ] },
      { name: "商机跟进", items: [
        ["opps.html", "商机总览", "商机列表、下一步已逾期、待核更新;点一行进商机工作台。"],
        ["calendar.html", "日历", "按月看节点、跟进到期与报告;可导出、订阅。"],
        ["reports.html", "报告", "历史报告、要一份报告、报告模板。"],
      ] },
    ],
    /* 平铺的页面清单 [页面, 名称](不含表格本身) */
    get NAV() { return this.MENU.flatMap((g) => (g.items ? g.items.filter(([h]) => h !== "@table").map(([h, n]) => [h, n]) : [[g.href, g.name]])); },
    /* 窄屏(手机):所有页面都用首页那种「站名 + 菜单按钮」的顶栏,页面链接与帐号区收进菜单卡片;筛选区缺省收起、每行横向滑动 */
    get narrow() { return !!(window.matchMedia && window.matchMedia("(max-width: 760px)").matches); },
    /* 页面骨架。电脑上的内页:顶栏平铺「首页」与四个组名,点组名进这一组的第一页,当前页所在的组标蓝;
       顶栏下面一排页签是这一组的各页,当前页标出,鼠标停在页签上显示那一句说明(手机上横着滑,前面带组名)。
       首页与手机:顶栏只露出站名与「菜单」按钮,点开是分四栏的菜单卡片(手机上四组上下排)。首页、「我的」不在组里,没有页签 */
    nav(active, collapsed) {
      /* 各页紧跟在 <div id="nav"> 后面的一小段脚本里调(不等读配置与角色),读表那几秒顶栏和页签已经在了,不会先露出没有顶栏的版式、读完再跳成带页签的:
         这时「后台数据表格」先藏着(还不知道是不是游客)、帐号区空着,load() 读完角色由 navFill() 补上。读完配置以后才调的照旧一次画全 */
      this._navEarly = !this.cfg;
      if (this._navEarly) { this._askEarly(); setTimeout(() => this.account(), 0); }  /* 名字先出来,角色、「后台数据表格」读完配置再补 */
      const table = this.cfg ? "/base/" + this.cfg.base.id : "/space";
      const link = ([h, n, d]) => {
        const tip = d ? ' title="' + this.esc(d) + '"' : "";
        if (h === "@table") return this.isGuest ? "" : '<a class="table" href="' + table + '" target="_blank" rel="noopener"' + tip + (this.cfg ? "" : " hidden") + ">" + n + " ↗</a>";
        return '<a href="' + h + '"' + (active === h ? ' class="on" aria-current="page"' : "") + tip + ">" + n + "</a>";
      };
      const group = this.MENU.find((g) => g.items && g.items.some(([h]) => h === active));
      const tabs = group ? '<nav class="pagetabs' + (group.items.length > 4 ? " many" : "") + '" aria-label="' + group.name + '"><div class="in"><span class="gname">' + group.name + "</span>" + group.items.map(link).join("") + "</div></nav>" : "";
      if (this.cfg) setTimeout(() => this.navFill(), 0);
      else if (this.narrow && document.readyState === "loading") {  // 手机上的筛选区也不等读表:页面一边解析一边收成一行(收在画出来之前,不会先展开再缩回去)
        const mo = new MutationObserver(() => this.compactFilters());
        mo.observe(document.documentElement, { childList: true, subtree: true });
        document.addEventListener("DOMContentLoaded", () => { mo.disconnect(); this.compactFilters(); }, { once: true });
      }
      setTimeout(() => {  // 页签一行放不下(很窄的手机)时,把当前页的页签滑进来
        const row = document.querySelector(".pagetabs .in"), on = row && row.querySelector("a.on");
        if (on) row.scrollLeft += Math.max(0, on.getBoundingClientRect().right - row.getBoundingClientRect().right + 8);
      }, 0);
      if (!collapsed && this.narrow) collapsed = true;
      if (collapsed) document.body.classList.add("menu-page");
      if (!collapsed) {
        const bar = this.MENU.map((g) => (g.items ? '<a href="' + g.items[0][0] + '"' + (g === group ? ' class="on"' : "") + ">" + g.name + "</a>" : link([g.href, g.name]))).join("");
        return '<header class="top"><a class="brand" href="index.html" aria-label="首页"><img src="logo.png" alt="" class="logo"></a><nav class="groups" aria-label="栏目">' + bar + '</nav><div class="acct" id="acct"></div></header>' + tabs;
      }
      /* 「菜单」:点开后整页磨砂变暗(.menu-veil),右上角弹出白色大圆角卡片;点 ×、点卡片外或按 Esc 关闭 */
      setTimeout(() => {
        const b = document.getElementById("menuBtn"), m = document.getElementById("menu"), veil = document.getElementById("menuVeil"), x = document.getElementById("menuClose");
        if (!b || !m) return;
        const set = (on) => { m.classList.toggle("open", on); veil.classList.toggle("open", on); b.setAttribute("aria-expanded", on ? "true" : "false"); document.body.classList.toggle("menu-open", on); };
        b.onclick = () => set(!m.classList.contains("open"));
        x.onclick = () => set(false); veil.onclick = () => set(false);
        document.addEventListener("keydown", (e) => { if (e.key === "Escape") set(false); });
      }, 0);
      const card = this.MENU.map((g) => (g.items
        ? '<section class="mgrp' + (g === group ? " on" : "") + '"><h3>' + g.name + "</h3>" + g.items.map(link).join("") + "</section>"
        : '<div class="mtop">' + link([g.href, g.name]) + "</div>")).join("");
      return '<header class="top hero-top"><a class="brand" href="index.html"><img src="logo.png" alt="" class="logo"><span class="brand-zh">全球水务开源情报</span><span class="brand-en">Global Water OSINT</span></a>' +
        '<button type="button" class="menu-btn" id="menuBtn" aria-expanded="false" aria-controls="menu">菜单</button></header>' +
        '<div class="menu-veil" id="menuVeil"></div><nav class="menu" id="menu" aria-label="页面"><button type="button" class="menu-close" id="menuClose" aria-label="关闭菜单">×</button>' + card + '<div class="acct" id="acct"></div></nav>' + tabs;
    },
    /* 补全先画好的顶栏(读完配置与角色以后):「后台数据表格」游客拿掉、别人露出来,登录人与角色填进帐号区,手机上收起筛选区 */
    navFill() {
      document.querySelectorAll("#nav a.table").forEach((a) => { if (this.isGuest) a.remove(); else { a.href = "/base/" + this.cfg.base.id; a.hidden = false; } });
      if (this.isPartner) document.querySelectorAll("#nav a[href]").forEach((a) => {  /* 伙伴:菜单里不列商机总览、日历、线索处理;顶栏「商机跟进」组名改指报告页(这一组伙伴只剩报告) */
        if (!this.PARTNER_CLOSED.includes(a.getAttribute("href"))) return;
        if (a.parentElement && a.parentElement.classList.contains("groups")) a.setAttribute("href", "reports.html"); else a.remove();
      });
      setTimeout(() => this.account(), 0);
      setTimeout(() => this.compactFilters(), 0);
    },
    /* 手机上的筛选区:每个 .filters 顶上加一行「筛选 ▾ + 当前选了什么」,缺省收起(只占一行),点开才展开;选了的条件变化时摘要跟着变 */
    compactFilters() {
      if (!this.narrow) return;
      document.querySelectorAll(".filters").forEach((box) => {
        if (box.querySelector(".frow-head")) return;
        const head = document.createElement("div"); head.className = "frow frow-head";
        head.innerHTML = '<button type="button" class="chip-btn ftoggle" aria-expanded="false">筛选 ▾</button><span class="fsum muted small"></span>';
        box.insertBefore(head, box.firstChild);
        box.classList.add("collapsed");
        const sum = () => {
          const on = [...box.querySelectorAll(".chip-btn.on")].map((b) => b.textContent.trim()).filter((t) => t && !/^全部/.test(t));
          const picked = [...box.querySelectorAll("select")].map((s) => s.selectedIndex > 0 ? s.options[s.selectedIndex].textContent.trim() : "").filter(Boolean);
          const typed = [...box.querySelectorAll("input[type=text], input:not([type])")].map((i) => i.value.trim()).filter(Boolean);
          const all = on.concat(picked, typed);
          const txt = all.length ? all.join("、") : "全部", el = head.querySelector(".fsum");
          if (el.textContent !== txt) el.textContent = txt;  // 没变就不写:写了会再触发观察器
        };
        head.querySelector(".ftoggle").onclick = () => { const open = box.classList.toggle("collapsed") === false; head.querySelector(".ftoggle").textContent = open ? "筛选 ▴" : "筛选 ▾"; head.querySelector(".ftoggle").setAttribute("aria-expanded", open ? "true" : "false"); };
        new MutationObserver(sum).observe(box, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
        box.addEventListener("change", sum); box.addEventListener("input", sum);
        sum();
      });
    },
    /* 帐号区(平铺导航的右端 / 菜单卡片的底部):登录人、在库里的角色、「帐号设置」(表格的设置页)、「退出登录」。游客帐号标成「游客 · 只读」。 */
    /* 名字一回来就先画(角色还没问到时不带角色标签、不带「我的」);角色回来再画一遍补上。顶栏先画的页面这里会进两次:
       nav() 一次(只等名字)、load() 读完角色后 navFill() 一次(名字与角色都齐) */
    async account() {
      const el = document.getElementById("acct"); if (!el) return;
      let me = {};
      try { me = await this.me(); } catch (e) { console.warn("读不到登录人", e); }
      if (!me) return;  /* 没登录:api() 已转去登录页 */
      const paint = (role) => {
        const ro = role === "viewer" || role === "commenter" || role === "none";
        const who = this.esc(me.name || me.email || "未登录"), tag = role === "none" ? (this.isPartner ? "伙伴 · 只读" : "游客 · 只读") : ro ? "只读" : (this.ROLE_ZH[role] || "");
        /* 「帐号设置」直接落在表格设置里的个人资料页(改名、改密码在那一页;/setting 本身落在访问令牌页,同事会懵) */
        el.innerHTML = '<span class="who" title="' + this.esc(me.email || "") + '">' + who + (tag ? ' <b class="' + (ro ? "ro" : "rw") + '">' + tag + "</b>" : "") + "</span>" +
          (role === "none" || role === undefined ? "" : '<a href="me.html">我的</a>') + (role === "owner" || role === "creator" ? '<a href="accounts.html">账号管理</a>' : "") + '<a href="/setting/account" target="_blank" rel="noopener">帐号设置</a><a href="#" id="logout">退出登录</a>';
        document.getElementById("logout").onclick = async (e) => {
          e.preventDefault();
          /* 表格的登出接口是 POST /api/auth/signout(线上核过:/api/auth/logout 是 404,所以之前点了没反应) */
          try { await fetch("/api/auth/signout", { method: "POST", credentials: "same-origin" }); } catch (err) { console.warn("退出失败", err); }
          location.href = "/auth/login?redirect=%2Fportal%2F";
        };
      };
      if (!this.cfg) { paint(this._role); return; }  /* 配置还没读完:角色问不了(要库编号),先只画名字;navFill() 会再来一次 */
      let role = "";
      try { role = await this.role(); } catch (e) { console.warn("读不到角色", e); }
      paint(role);
    },
    /* 「我的」:登录人在「个人设置」表里的那一行(关注国别、日历订阅;A24)。库里没建这张表 → null;游客 → null;
       create 为真且还没有行时建一行(名称 = 登录人显示名,账号 = 登录人)。别人的行页面不读不改。 */
    async myPref(create) {
      if (this.isGuest || !(this.cfg.tables && this.cfg.tables.preference)) return null;
      const me = await this.me(); if (!me.id) return null;
      const rows = await this.records("preference", { filter: this.and(this.cond("preference", "user", "is", me.id)) });
      if (rows.length) return rows[0];
      if (!create) return null;
      const rec = await this.create("preference", { name: me.name || me.email || "我", user: me.id });
      return rec ? (await this.records("preference", { filter: this.and(this.cond("preference", "user", "is", me.id)) }))[0] || rec : null;
    },
    /* 关注的国别(个人设置「关注国别」):清单里这些国家的线索、商机排在最前、带 ★,国别下拉把它们放在最上面一组。
       follow = { ids: 国别记录编号, names: 国名 };游客、表没建 → 空。followed() 读一次,各处共用 */
    follow: { ids: new Set(), names: new Set() },
    followed() {
      if (!this._followP) this._followP = (async () => {
        if (this.isGuest || !this.cfg.tables || !this.cfg.tables.preference) return this.follow;
        const rec = await this.myPref(false);
        const v = rec ? this.get("preference", rec, "countries") || [] : [];
        this.follow = { ids: new Set(this.linkIds(v)), names: new Set(v.map((x) => x && x.title).filter(Boolean)) };
        return this.follow;
      })().catch((e) => { console.warn("关注的国别没读到", e); return this.follow; });
      return this._followP;
    },
    isFollowed(oid, rec) { return this.follow.ids.size > 0 && this.follow.ids.has(oid === "country" ? rec.id : this.linkId(this.get(oid, rec, "country"))); },
    star(oid, rec) { return this.isFollowed(oid, rec) ? '<span class="star" title="关注的国别">★</span> ' : ""; },
    /* 关注国家的排前面,其余顺序不变 */
    followFirst(rows, oid) { if (!this.follow.ids.size) return rows; const a = [], b = []; rows.forEach((r) => (this.isFollowed(oid, r) ? a : b).push(r)); return a.concat(b); },
    /* 忽略的国别(个人设置「忽略国别」,存逗号分隔的国别码):登录人自己忽略的 → ISO3 集合(读一次,各页共用)。地图上这些国别涂成灰色(hatch*);
       采集不再涉及它们由后台按所有账号的忽略合起来做(A01)。没设过(字段空着)的账号按「默认」行算(个人设置里名称「默认」、账号空着的一行,
       库的所有者 / 创建者在「我的」页点「设为新账号的默认」写它;使用方 2026-10-21:默认只看清单国别);没有「默认」行时默认忽略中国。
       一个都不忽略时存「-」,和没设过分开。游客 → 默认;表或字段还没建 → 空集合。 */
    IGNORE_DEFAULT: ["CHN"],
    IGNORE_DEFAULT_ROW: "默认",
    /* 「默认」行(没有则 null):名称「默认」且账号空着 */
    async ignoreDefaultRec() {
      if (this.isGuest || !this.cfg.tables || !this.cfg.tables.preference || !this.has("preference", "ignored")) return null;
      const rows = await this.records("preference", { filter: this.and(this.cond("preference", "name", "is", this.IGNORE_DEFAULT_ROW)) });
      return rows.find((r) => !this.get("preference", r, "user")) || null;
    },
    /* 新账号的默认忽略国别:「默认」行的集合;没有「默认」行 → 只忽略中国。{ set, custom: 是不是「默认」行定的 } */
    ignoreDefault() {
      if (!this._ignDef) this._ignDef = (async () => {
        const rec = await this.ignoreDefaultRec(), v = rec && this.get("preference", rec, "ignored");
        return v ? { set: this.parseIsos(v), custom: true } : { set: new Set(this.IGNORE_DEFAULT), custom: false };
      })().catch((e) => { console.warn("默认忽略国别没读到", e); return { set: new Set(this.IGNORE_DEFAULT), custom: false }; });
      return this._ignDef;
    },
    /* 对比参照国别(glossary.compare_country,使用方 2026-10-20):中国不是市场,只在城市概况里作城市化进程的参照——国别筛选里叫「中国(对比)」,默认不列不计数,
       选了才列;城市详情的「历史影像」自动挑一座人口相近的中国城市并排看。不受忽略国别影响;别的页面照旧不算它 */
    COMPARE: new Set(["CHN"]),
    cmpLabel(name) { return name + "(对比)"; },
    parseIsos(text) { return new Set(String(text || "").split(/[\s,;,;、]+/).filter((x) => /^[A-Za-z]{3}$/.test(x)).map((x) => x.toUpperCase())); },
    ignored() {
      if (!this._ignored) this._ignored = (async () => {
        if (this.isGuest) return new Set(this.IGNORE_DEFAULT);
        if (!this.cfg.tables || !this.cfg.tables.preference || !this.has("preference", "ignored")) return new Set();
        const rec = await this.myPref(false), v = rec && this.get("preference", rec, "ignored");
        return v ? this.parseIsos(v) : new Set((await this.ignoreDefault()).set);
      })().catch((e) => { console.warn("忽略的国别没读到", e); return new Set(); });
      return this._ignored;
    },
    async saveIgnored(set) {
      const rec = await this.myPref(true); if (!rec) throw new Error("库里还没有「个人设置」表(管理员跑一次建表命令就有)");
      await this.send("PATCH", "/table/" + this.t("preference").id + "/record", { fieldKeyType: "id", typecast: true,
        records: [{ id: rec.id, fields: { [this.f("preference", "ignored")]: Array.from(set).sort().join(",") || "-" } }] });
      this._ignored = Promise.resolve(new Set(set));
    },
    /* 把一个集合写成「默认」行(没有就建):所有者 / 创建者在「我的」页点「设为新账号的默认」 */
    async saveIgnoreDefault(set) {
      const val = Array.from(set).sort().join(",") || "-";
      const rec = await this.ignoreDefaultRec();
      if (rec) await this.send("PATCH", "/table/" + this.t("preference").id + "/record", { fieldKeyType: "id", typecast: true, records: [{ id: rec.id, fields: { [this.f("preference", "ignored")]: val } }] });
      else await this.create("preference", { name: this.IGNORE_DEFAULT_ROW, ignored: val });
      this._ignDef = Promise.resolve({ set: new Set(set), custom: true });
    },
    /* 忽略国别涂灰:直接给这几项填底色(areaColor),不用 echarts 的花纹(decal)。花纹是画布图案,有的浏览器拖动地图时整张图画不出来。
       有 visualMap(线索多少的蓝色)的地图,visualMap 会盖掉单项底色,所以忽略的国别 value 写「-」(不参与配色),线索数挪到 leads,提示框照常显示。
       灰色不带蓝调,和最浅的蓝 #dbeafe 也分得开 */
    IGNORED_COLOR: "#c4bfba",
    /* 地图系列(type: "map")的数据:忽略的国别涂灰;数据里没有的也补一项,好画上 */
    hatchData(data, set) {
      if (!set || !set.size) return data;
      const itemStyle = { areaColor: this.IGNORED_COLOR };
      const out = data.map((d) => (set.has(d.name) ? Object.assign({}, d, { value: "-", leads: d.value || 0, itemStyle: Object.assign({}, d.itemStyle, itemStyle) }) : d));
      const have = new Set(data.map((d) => d.name));
      set.forEach((iso) => { if (!have.has(iso)) out.push({ name: iso, value: "-", leads: 0, itemStyle }); });
      return out;
    },
    /* geo 底图(上面叠城市散点的地图):忽略的国别放进 geo 的 regions 涂灰(放在页面自己的 regions 前面,页面的高亮优先) */
    hatchRegions(set) { return set ? Array.from(set).map((iso) => ({ name: iso, itemStyle: { areaColor: this.IGNORED_COLOR } })) : []; },
    onGeo(p) { return !!p && p.componentType === "geo"; },
    /* 提示框里的一行,与地图下方的图例说明 */
    hatchTip(iso, set) { return set && set.has(iso) ? '<br><span class="muted">已忽略:不再采集这一国的线索</span>' : ""; },
    /* 忽略国别的数据默认不列、不算(使用方 2026-10-15:灰色国别不再采集,城市概况、线索页面里也别再出现):页面上一行说明 + 「显示 / 不显示」。
       el 是 p.ig-note;parts 是 [「3 条线索」…](为 0 的别传);shown = 现在已露出;toggle = 点按钮时 */
    igNote(el, parts, shown, toggle) {
      if (!el) return;
      if (!parts.length) { el.innerHTML = ""; return; }
      el.innerHTML = (shown ? "已显示忽略国别(地图上灰色,不再采集)的 " + parts.join("、") + "。" : "忽略国别(地图上灰色,不再采集)的 " + parts.join("、") + "没算进来。") +
        ' <button type="button" class="btn quiet">' + (shown ? "不显示" : "显示") + "</button>";
      el.querySelector("button").onclick = toggle;
    },
    /* 过期线索归档(本体 lead.archived,A49):截止已过、或发布超过一年没归入商机的线索由后台标「已归档」;线索页面与助手默认不算它们(网址 ar=1 露出),
       商机工作台照旧全列(已归档的在有效线索下面)。表里还没这个字段(没跑建表)时不加条件、不显示说明行 */
    ARC_WHY: "截止已过或发布超过一年没归入商机",
    arcCond(show) { return !show && this.has("lead", "archived") ? this.cond("lead", "archived", "is", false) : null; },
    arcNote(el, n, shown, toggle) {
      if (!el) return;
      if (!n || !this.has("lead", "archived")) { el.innerHTML = ""; return; }
      const k = Number(n).toLocaleString("zh-CN");
      el.innerHTML = (shown ? "已显示归档(" + this.ARC_WHY + ")的 " + k + " 条线索。" : "已归档(" + this.ARC_WHY + ")的 " + k + " 条线索没算进来。") +
        ' <button type="button" class="btn quiet">' + (shown ? "不显示" : "显示") + "</button>";
      el.querySelector("button").onclick = toggle;
    },
    /* 地图联动(2026-10-21,使用方:选国别或城市时地图缩放并居中当前选择项):
       每个国家的经纬度范围从 world.geo.json 算一次;一国有好几块(岛)时取面积最大的那块,免得跨 180° 经线的俄罗斯、斐济算成整个地球 */
    _geoBox: null,
    geoBoxes(geo) {
      const box = {}, W = [180, -180, 90, -90];
      const ring = (r) => { let a = 0; const b = [180, -180, 90, -90];
        for (let i = 0; i < r.length; i++) { const [x, y] = r[i], [px, py] = r[(i + 1) % r.length]; a += x * py - px * y;
          if (x < b[0]) b[0] = x; if (x > b[1]) b[1] = x; if (y < b[2]) b[2] = y; if (y > b[3]) b[3] = y; }
        return { a: Math.abs(a), b }; };
      (geo.features || []).forEach((f) => {
        const g = f.geometry, polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
        let best = null;
        polys.forEach((p) => { const r = ring(p[0] || []); if (!best || r.a > best.a) best = r; W[0] = Math.min(W[0], r.b[0]); W[1] = Math.max(W[1], r.b[1]); W[2] = Math.min(W[2], r.b[2]); W[3] = Math.max(W[3], r.b[3]); });
        if (best) box[f.properties.name] = best.b;
      });
      this._geoBox = { box, world: W };
    },
    /* 某个国家在这张图上该用的视角 {center, zoom}:整个国家刚好放进图里、居中;iso 空 = 全图 */
    geoView(mc, iso) {
      const gb = this._geoBox; if (!gb || !mc) return null;
      const W = gb.world, w = mc.getWidth(), h = mc.getHeight();
      if (!iso) return { center: [(W[0] + W[1]) / 2, (W[2] + W[3]) / 2], zoom: 1.1 };
      const b = gb.box[iso]; if (!b || !w || !h) return null;
      const s = Math.min(w / (W[1] - W[0]), h / ((W[3] - W[2]) * 0.75));  // zoom 1 时整幅世界地图的比例(像素/度;纬度按 0.75 压扁)
      const z = Math.min(w / (Math.max(0.5, b[1] - b[0]) * s), h / (Math.max(0.5, b[3] - b[2]) * 0.75 * s)) * 0.8;  // 四周留两成空
      return { center: [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2], zoom: Math.max(1.3, Math.min(40, z)) };
    },
    /* 现在的视角(拖过、滚过轮之后的):kind = "geo"(底图上叠散点)或 "series"(type: "map" 的系列) */
    mapView(mc, kind) {
      const o = mc && mc.getOption(), m = o && (kind === "geo" ? o.geo && o.geo[0] : o.series && o.series[0]);
      const d = this.geoView(mc, "");
      return { center: (m && m.center) || (d && d.center) || [0, 0], zoom: (m && m.zoom) || 1.1 };
    },
    /* 平滑地飞到一个视角(半秒多;缩放按对数插值,远近看着匀速);再来一次就中断上一次 */
    mapFly(mc, kind, to, ms) {
      if (!mc || !to) return;
      const from = this.mapView(mc, kind), dur = ms || 650, t0 = performance.now();
      const put = (v) => mc.setOption(kind === "geo" ? { geo: { center: v.center, zoom: v.zoom } } : { series: [{ center: v.center, zoom: v.zoom }] });
      if (mc._fly) cancelAnimationFrame(mc._fly);
      const step = (now) => {
        const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
        put({ center: [from.center[0] + (to.center[0] - from.center[0]) * e, from.center[1] + (to.center[1] - from.center[1]) * e],
          zoom: Math.exp(Math.log(from.zoom) + (Math.log(to.zoom) - Math.log(from.zoom)) * e) });
        mc._fly = k < 1 ? requestAnimationFrame(step) : 0;
      };
      mc._fly = requestAnimationFrame(step);
    },
    hatchNote(mapEl, set) {
      if (!mapEl || !set || !set.size || (mapEl.nextElementSibling && mapEl.nextElementSibling.classList.contains("hatch-note"))) return;
      const p = document.createElement("p");
      p.className = "hatch-note muted small";
      p.innerHTML = '<span class="hatch-swatch"></span>灰色:我忽略的国别(' + set.size + ' 个,不再采集),在<a href="me.html">我的</a>里改。';
      mapEl.insertAdjacentElement("afterend", p);
    },
    /* 日历订阅列表属性:一行一个「名称 | 链接」 */
    subsOf(rec) { return String(this.get("preference", rec, "subscriptions") || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const i = l.indexOf("|"); return i < 0 ? { name: l, url: "" } : { name: l.slice(0, i).trim(), url: l.slice(i + 1).trim() }; }); },
    async saveSub(name, url) {
      const rec = await this.myPref(true); if (!rec) throw new Error("库里还没有「个人设置」表(管理员跑一次建表命令就有)");
      const subs = this.subsOf(rec).filter((x) => x.url !== url); subs.push({ name: name || "日历订阅", url });
      await this.patch("preference", rec.id, { subscriptions: subs.map((x) => x.name.replace(/\|/g, "/") + " | " + x.url).join("\n") });
      return subs.length;
    },
    /* 「助手」页:把一句提问送给后台的助手接口(portal.caddy 转到后台任务进程里的 core/assistant_server.py),拿回查询计划;
       接口只做理解,数据仍由本页带着登录 cookie 向表格查。这不是写表格的操作。 */
    /* 「助手」页答案下面的「答对了」「答错了」(本体 ask_log,A45):经助手接口记进表格「助手提问记录」(查看者帐号也能反馈) */
    async feedback(data) {
      const r = await fetch("api/assistant/feedback", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(data) });
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪,反馈没记上");
      return r.json();
    },
    async ask(q, prev, fix) {  // prev:上一问的计划(接着问时后台沿用它的对象、条件与指标,见 core/assistant.py 的 plan);fix:答错了以后按说明当场重答 {key, note, understood}
      const r = await fetch("api/assistant/ask", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(Object.assign({ q }, prev ? { prev } : {}, fix ? { fix } : {})) });
      if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版);其余页面照常可用");
      if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    /* 浏览器说的「网络断了」(iPhone 上是 Load failed,Chrome 是 Failed to fetch,Firefox 是 NetworkError…):认出来好用中文解释 */
    netError(e) { return !!(e && /Load failed|Failed to fetch|NetworkError|network connection was lost|request timed out|cancelled|abort/i.test(e.message || String(e))); },
    /* 看一条链接(或一份上传的文件):把用户贴来的网址交给后台(core/link_check.py)读原文、看信源、查库里相关、找旁证、给入库建议;只读不写。
       2026-10-16:模型加检索常要一分钟,手机浏览器一条请求等不了这么久(iPhone 60 秒就断,报「读不到数据:Load failed」),
       所以先让后台起任务、马上拿编号,再隔两三秒取一次进度;取的时候断了就再取(手机锁屏、切到微信回来接着取),结果在后台留半小时。
       onWait(阶段, 已过秒数, 刚断过) 每次取到进度叫一次,给页面写「模型分析中(已 25 秒)」 */
    async checkLink(url, note, file, onWait) {  // file:用户上传的 PDF、Word 或截图(base64),网站不让程序读时用
      const started = Date.now(), body = JSON.stringify(file ? { url, note, file, job: true } : { url, note, job: true });
      const check = async (r) => {
        if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
        if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版);其余页面照常可用");
        if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
        return r.json();
      };
      let first;
      for (let i = 0; ; i++) {  // 传文件这一步断了(手机网络不稳)自动再传一次,再不行才报
        try { first = await check(await fetch("api/assistant/link", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body })); break; }
        catch (e) { if (i >= 1 || !this.netError(e)) throw new Error(this.netError(e) ? (file ? "文件没传上去(网络断了一下),请再发一次" : "网络断了一下,请再问一次") : e.message); await new Promise((ok) => setTimeout(ok, 1500)); }
      }
      if (!first || !first.job) return first;  // 旧版后台不认 job,直接给结果
      return this.pollJob(first.job, started, onWait);
    },
    /* 后台任务(看链接、帮我复核)的进度与结果:隔两三秒取一次 GET /link/{编号};断了就再取(手机锁屏、切到微信回来接着取) */
    async pollJob(jobId, started, onWait) {
      const check = async (r) => {
        if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
        if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版);其余页面照常可用");
        if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
        return r.json();
      };
      const wait = (ms) => new Promise((ok) => {  // 等 ms 毫秒;页面从后台回到前台(手机解锁、切回来)就不等了,马上去取
        const t = setTimeout(done, ms);
        function done() { clearTimeout(t); document.removeEventListener("visibilitychange", vis); ok(); }
        function vis() { if (document.visibilityState === "visible") done(); }
        document.addEventListener("visibilitychange", vis);
      });
      let fails = 0;
      for (;;) {
        const secs = Math.round((Date.now() - started) / 1000);
        await wait(secs < 30 ? 2000 : 3000);
        try {
          const j = await check(await fetch("api/assistant/link/" + encodeURIComponent(jobId), { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" }));
          if (!j || j.done || !j.ok) return j;
          fails = 0;
          if (onWait) onWait(j.stage || "", Math.round((Date.now() - started) / 1000), false);
        } catch (e) {
          if (!this.netError(e)) throw e;
          if (++fails > 40) throw new Error("网络一直连不上(断了 " + fails + " 次),请换个网络再试;结果后台会留半小时,再发一次同样的内容就能看到");
          if (onWait) onWait("", Math.round((Date.now() - started) / 1000), true);
        }
      }
    },
    /* 复核按钮(本体 review,A47 / A48;2026-10-19 使用方要的):看链接结果里「要核实」每条的 已复核 / 忽略此项、「库里线索」每条的 是同一项目 / 不是同一项目,
       经助手接口记进表格「助手复核记录」并按全部点选重算投标评估;「帮我复核」是后台任务(助手自己查、请模型给结论),编号同看链接的取法 */
    async review(data) {
      const r = await fetch("api/assistant/review", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(data) });
      if (r.status === 401) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (r.status === 403) throw new Error("游客帐号不能复核");
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版),没记上");
      if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    async reviewHelp(data, onWait) {
      const started = Date.now();
      const r = await fetch("api/assistant/review/help", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(data) });
      if (r.status === 401) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (r.status === 403) throw new Error("游客帐号不能复核");
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版)");
      if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
      const first = await r.json();
      if (!first || !first.job) return first;
      return this.pollJob(first.job, started, onWait);
    },
    /* 法规问题(助手认作「法规」、要按条文作答的):后台(core/law.py)按国别与关键词查条文,配了模型的按查到的条文作答;只读 */
    async lawAsk(q, plan) {
      const r = await fetch("api/assistant/law", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ q, law: plan.law, countries: (plan.filters.countries || []).map((c) => c.iso3) }) });
      if (r.status === 401) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (r.status === 403) throw new Error("游客帐号不能查法规条文");
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版);其余页面照常可用");
      if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    fail(el, e) {  // 出错提示要说人话:浏览器自己的「Load failed」「Failed to fetch」换成中文说明,原话留在控制台
      console.error(e);
      const msg = this.netError(e) ? "网络连接断了一下(手机锁屏、切到别的应用或网络不稳都会这样);请再试一次" : (e && e.message ? e.message : e);
      el.innerHTML = '<div class="note n-bad">读不到数据:' + this.esc(msg) + "</div>";
    },
    tableHtml(head, rows) {
      if (!rows.length) return '<p class="muted">(无)</p>';
      return "<table><thead><tr>" + head.map((h) => "<th>" + this.esc(h) + "</th>").join("") + "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => "<td>" + (c && c.__html !== undefined ? c.__html : this.esc(c)) + "</td>").join("") + "</tr>").join("") + "</tbody></table>";
    },
    raw(html) { return { __html: html }; },
    /* 指标的呈现(使用方 2026-10-09:关键指标加粗放大放最前,负面指标标红):
       num 取文字开头的数(认 −、–、千分位);tone 按指标名与数值判断是不是「负面」(门槛只写在 TONES 这一处);
       factHtml 一行「名称 值(年份) 来源」,值加粗、年份变小、负面标红并写明「超警戒线:门槛」(2026-10-10 起不用 ▼,免得误读);kpiHtml 一排大号关键指标卡 */
    TONES: [
      [/实际增速|GDP 增速/, (v) => v < 0, "负增长"], [/通胀|CPI/, (v) => v >= 10, "通胀 ≥ 10%"], [/政府总债务/, (v) => v >= 70, "债务 ≥ 70% GDP"],
      [/净借贷/, (v) => v <= -5, "赤字 ≥ 5% GDP"], [/经常账户/, (v) => v <= -5, "逆差 ≥ 5% GDP"], [/外债\/GNI/, (v) => v >= 60, "外债 ≥ 60% GNI"],
      [/直接投资净流入/, (v) => v < 0, "净流出"], [/人口增速/, (v) => v < 0, "人口减少"],
      [/基本饮水|基本卫生/, (v) => v < 80, "覆盖率 < 80%"], [/安全管理饮水|安全管理卫生/, (v) => v < 50, "覆盖率 < 50%"],
      [/淡水压力/, (v) => v >= 75, "取水 ≥ 75% 可再生量"], [/无安全饮用水|无卫生设施/, (v) => v >= 20, "≥ 20% 人口"], [/未处理污水/, (v) => v >= 50, "≥ 50%"],
      [/水风险|用水压力|干旱风险|洪水风险/, (v) => v >= 3 && v <= 5, "风险高(≥ 3 分)"],  // 分数只在 0–5;超过 5 的是「有水风险数据的城市 3,116」这类个数,不是分数
      [/营商环境总分|供水得分|公共采购得分|纠纷解决得分/, (v) => v < 50, "低于 50 分,偏弱"], [/企业供水可靠/, (v) => v < 80, "不到 80%,缺水的企业多"],
      [/授标天数/, (v) => v > 180, "超过 180 天,授标慢"], [/回款天数/, (v) => v > 90, "超过 90 天,回款慢"],
    ],
    num(text) {
      const m = String(text == null ? "" : text).replace(/[−–]/g, "-").match(/^\s*(-?\d[\d,]*(?:\.\d+)?)/);
      return m ? Number(m[1].replace(/,/g, "")) : null;
    },
    tone(label, v) {
      if (v === null || v === undefined || isNaN(v)) return null;
      const r = this.TONES.find(([re]) => re.test(label));
      return r && r[1](v) ? r[2] : null;
    },
    /* 「226 亿美元(2025)」→ ["226 亿美元", "2025"] */
    splitYear(text) { const m = String(text || "").match(/^(.*?)\s*[((](\d{4}(?:-\d{2})?)[))]\s*$/); return m ? [m[1], m[2]] : [String(text || ""), ""]; },
    /* 标签(界面规范:全站一种胶囊,颜色只按含义分):tone = ""(灰,一般属性) | "info"(进行中) | "ok"(正面、已核实) | "warn"(提醒、要核实) | "bad"(负面、出错) */
    tagHtml(text, tone, title) { return '<span class="tag' + (tone ? " t-" + tone : "") + '"' + (title ? ' title="' + this.esc(title) + '"' : "") + ">" + this.esc(text) + "</span>"; },
    /* 我方阶段的标签:颜色按本体 taxonomies.stage 的编码定(不在这里写阶段名):建议(suggested)提醒色、中标(won)正面色、其余在跟踪的进行中色、已结束和不跟进灰色 */
    stageOf(code) { return (this.taxonomy("stage").groups.flatMap((g) => g.values).find((v) => v.code === code) || {}).v || ""; },  // 阶段名按本体编码取(页面脚本里不写阶段名)
    stageTag(s) {
      s = s || this.STAGES[0];
      const code = (this.taxonomy("stage").groups.flatMap((g) => g.values).find((v) => v.v === s) || {}).code;
      return this.tagHtml(s, code === "suggested" ? "warn" : code === "won" ? "ok" : this.TRACKED.includes(s) ? "info" : "");
    },
    /* 名称-数值行(全站只有这一种写法,样式在 portal.css「名称-数值行」;2026-10-14 起各页不再各拼各的 <div><b><span>,免得样式互相串):
       第一行「名称 | 值 年份 来源」,值太长就在自己那一栏里换行;下面一行是警示(红/橙),再下面一行是灰色说明,都在值那一栏,不挤值也不压到隔壁。
       值是数或短词的加粗;html 的值(链接、成段说明)用正常粗细。
       opts:字符串 = 来源(HTML,旧写法);或 { src: 来源 HTML, note: 说明文字, html: true 表示 value 已是 HTML(链接等), tone: "bad" | "warn", why: 警示原文 }。
       不给 tone 的按 TONES 自动判断负面,警示写「超警戒线:门槛」 */
    factHtml(label, value, opts) {
      const o = typeof opts === "string" ? { src: opts } : opts || {};
      const [main, year] = o.html ? [String(value), ""] : this.splitYear(value);
      const auto = o.tone || o.html ? null : this.tone(label, this.num(main)), tone = o.tone || (auto ? "bad" : "");
      const why = o.why || (auto ? "超警戒线:" + auto : "");
      return '<div class="fact' + (tone ? " is-" + tone : "") + '">' + '<b class="fact-k">' + this.esc(label || "") + "</b>" +
        '<span class="fact-v">' + (o.html ? "<span>" + main + "</span>" : "<strong>" + this.esc(main) + "</strong>") + (year ? '<em class="yr">' + this.esc(year) + "</em>" : "") + (o.src || "") + "</span>" +
        (why ? '<span class="fact-w">' + this.esc(why) + "</span>" : "") + (o.note ? '<span class="fact-n">' + this.esc(o.note) + "</span>" : "") + "</div>";
    },
    /* 一组名称-数值行:rows = [[名称, 值, opts?], …],值为空的不列;一行都没有返回空串 */
    factsHtml(rows) {
      rows = rows.filter((r) => r && r[1] !== undefined && r[1] !== null && r[1] !== "");
      return rows.length ? '<div class="facts">' + rows.map(([k, v, o]) => this.factHtml(k, v, o)).join("") + "</div>" : "";
    },
    /* 指标卡(界面规范:全站数字卡片只这一种,2026-10-14 起国别、城市、线索总览等顶上那排数也用它):
       items = [{label, value, year?, note?, tone?, why?, href?, attrs?}];数字放大加粗,单位跟在后面小一号;
       负面(按 TONES 自动判断,或 tone: "bad";tone: false 不判断,用于名字里带指标词的个数)数字标红、下面写原因;tone: "warn" 是琥珀色提醒(待处理这类有积压的数);
       有 href 的整张卡可点(attrs 是附加的属性串,如 data-i、target) */
    kpiHtml(items) {
      items = items.filter((x) => x && x.value !== "" && x.value !== null && x.value !== undefined);
      if (!items.length) return "";
      /* 分几栏按张数定,不让最后一张单独落一行:6 张以内一排放下,再多就分两排;窄一点(≤1000px)5 张以上分两排;手机两栏,单出来的那张占满一行 */
      const n = items.length, cols = n <= 6 ? n : Math.ceil(n / 2), colsMd = n <= 5 ? n : Math.ceil(n / 2);
      return '<div class="kpis" style="--cols:' + cols + ";--cols-md:" + colsMd + '">' + items.map((x) => {
        const [main, y] = this.splitYear(String(x.value)), year = x.year || y;
        const m = main.replace(/[−–]/g, "-").match(/^\s*(约\s*)?(-?\d[\d,]*(?:\.\d+)?\+?(?:\s?\/\s?\d[\d,]*)?)\s*(.*)$/);  // 「约 1.18 美元/m³」:约 小字在前;「200+」「13 / 98」整个放大
        const auto = x.tone || x.tone === false ? null : this.tone(x.label, this.num(m ? m[2] : main)), tone = x.tone || (auto ? "bad" : ""), why = x.why || (auto ? "超警戒线:" + auto : "");
        const big = m ? (m[1] ? '<span class="ku">约</span>' : "") + '<span class="kn">' + this.esc(m[2]) + "</span>" + (m[3] ? '<span class="ku">' + this.esc(m[3]) + "</span>" : "") : '<span class="kn kt">' + this.esc(main) + "</span>";
        const tag = x.href ? "a" : "div";
        return "<" + tag + ' class="kpi' + (tone ? " is-" + tone : "") + '"' + (x.href ? ' href="' + this.esc(x.href) + '"' : "") + (x.attrs ? " " + x.attrs : "") + '><div class="kv">' + big + "</div>" +
          '<div class="kl">' + this.esc(x.label) + (year ? '<em class="yr">' + this.esc(year) + "</em>" : "") + "</div>" +
          (why ? '<div class="kw">' + this.esc(why) + "</div>" : "") + (x.note ? '<div class="kx">' + this.esc(x.note) + "</div>" : "") + "</" + tag + ">";
      }).join("") + "</div>";
    },
    /* 水风险与经济(城市、国别共用;glossary.risk_level 的读法):有数的项才列,一项都没有返回空串 */
    /* 「← 返回」:从站内哪一页点进来就回哪一页(带着那一页的筛选),直接打开的才用默认去处 */
    backLink(el, href, label) {
      const NAMES = { "trend.html": "趋势机会", "country.html": "国别概况", "city.html": "城市清单", "overview.html": "线索总览", "analysis.html": "线索分析",
        "leads.html": "线索处理", "opps.html": "商机跟进", "opp.html": "商机", "": "首页", "index.html": "首页" };
      let ref = null;
      try { ref = document.referrer ? new URL(document.referrer) : null; } catch (e) { ref = null; }
      const dir = location.pathname.replace(/[^/]*$/, "");  // 页面所在目录(线上是 /portal/)
      const page = ref && ref.origin === location.origin && ref.pathname.startsWith(dir) && !ref.pathname.slice(dir.length).includes("/") ? ref.pathname.slice(dir.length) : null;
      const same = ref && ref.pathname === location.pathname && ref.search === location.search;
      if (page !== null && !same && NAMES[page] !== undefined && !(page === "city.html" && /[?&]id=/.test(ref.search))) {
        el.href = ref.pathname + ref.search;
        el.textContent = "← 返回" + NAMES[page];
        el.onclick = (e) => { if (history.length > 1) { e.preventDefault(); history.back(); } };
      } else { el.href = href; el.textContent = "← " + label; el.onclick = null; }
    },
    /* 一个国家在「市场背景」三页之间互相跳:国别概况 · 城市概况 · 趋势机会(中国不做趋势分析,不给这一项);here 是当前页,不重复给 */
    marketLinks(iso, name, here) {
      if (!iso) return "";
      const L = [["country.html?" + new URLSearchParams({ iso }), "国别概况"], ["city.html?" + new URLSearchParams({ c: iso }), "城市概况"]];
      if (iso !== "CHN") L.push(["trend.html?" + new URLSearchParams({ c: iso }), "趋势机会"]);
      return '<span class="mlinks">' + (name ? "<b>" + this.esc(name) + "</b>" : "") + L.filter(([u]) => !here || !u.startsWith(here)).map(([u, l]) => '<a href="' + u + '">' + l + " →</a>").join("") + "</span>";
    },
    /* 维基百科链接(国别、城市共用,使用方 2026-10-26「只保留中文的维基百科链接」):一个「维基百科 ↗」,开中文条目;没有中文条目的才开英文条目。字段还没建或没填时返回空串 */
    wikiLinks(oid, rec) {
      const zh = this.has(oid, "wiki_zh") ? this.get(oid, rec, "wiki_zh") : "", en = this.has(oid, "wiki_en") ? this.get(oid, rec, "wiki_en") : "";
      if (!zh && !en) return "";
      return '<span class="wikis"><a class="wiki" href="' + this.esc(zh || en) + '" target="_blank" rel="noopener" title="' + (zh ? "中文维基百科" : "没有中文条目,开英文维基百科") + '">维基百科 ↗</a></span>';
    },
    riskFacts(oid, rec, iso) {
      const lv = (x) => (x >= 4 ? "极高" : x >= 3 ? "高" : x >= 2 ? "中到高" : x >= 1 ? "低到中" : "低");
      const n = (p) => { const v = this.get(oid, rec, p); return v === undefined || v === null || v === "" ? null : Number(v); };
      const sc = (p) => { const v = n(p); return v === null ? "" : v.toFixed(1) + " 分(" + lv(v) + ")"; };
      const pc = (p) => { const v = n(p); return v === null ? "" : v.toFixed(1) + "%"; };
      const rows = [["总体水风险", sc("water_risk")], ["用水压力", sc("water_stress")], ["2030 年用水压力", sc("water_stress_2030")], ["2050 年用水压力", sc("water_stress_2050")],
        ["干旱风险", sc("drought")], ["河流洪水风险", sc("flood")], ["无安全饮用水", pc("no_drinking")], ["无卫生设施", pc("no_sanitation")], ["未处理污水", pc("untreated_ww")],
        ["人均 GDP(PPP)", n("gdp_pc") === null ? "" : Math.round(n("gdp_pc")).toLocaleString("zh-CN") + " 美元"],
        ["世行涉水项目(2020-10 至 2024)", n("wb_water_n") === null ? "" : n("wb_water_n") + " 个" + (n("wb_water_n") ? ",承诺额 " + Math.round(n("wb_water_musd") || 0).toLocaleString("zh-CN") + " 百万美元" : "")]]
        .filter(([, v]) => v);
      if (!rows.length) return "";
      /* 最要紧的三项放大放前:总体水风险(没有就用水压力)、人均 GDP、世行涉水项目;其余在下面逐行列 */
      const lead = rows.find(([k]) => k === "总体水风险") || rows.find(([k]) => k === "用水压力");
      const top = [lead, rows.find(([k]) => k === "人均 GDP(PPP)"), rows.find(([k]) => k.startsWith("世行涉水项目"))].filter(Boolean);
      const kp = top.map(([k, v]) => k.startsWith("世行") ? { label: "世行涉水项目", value: v.split(",")[0], note: v.split(",")[1] || "2020-10 至 2024" } : { label: k, value: v.replace(/\(.*\)$/, ""), note: (v.match(/\((.*)\)$/) || [])[1] ? "等级:" + v.match(/\((.*)\)$/)[1] : "" });
      const rest = rows.filter((r) => !top.includes(r));
      return this.kpiHtml(kp) + this.factsHtml(rest) +
        '<p class="muted small">分数 0–5:4 以上极高,3–4 高。来源:' + this.esc(this.get(oid, rec, "risk_source") || "WRI Aqueduct 4.0 等") + '。模型估计,作趋势参考' + (iso === "CHN" ? "(中国境内不做趋势机会分析)" : ';<a href="trend.html' + (iso ? "?" + new URLSearchParams({ c: iso }) : "") + '">看趋势机会 →</a>') + "</p>";
    },
    /* 营商环境(世界银行 B-READY;本体 V7「营商环境」页签,2026-10-14 使用方要):rec 是一整条国别记录。
       四张大号指标卡(总分、供水得分、公共采购得分、商业水价)→ 投标与供水的几项 → 细项分供水、采购两栏;这一期没覆盖的国家写明,不显示成 0 */
    BREADY_URL: "https://www.worldbank.org/en/businessready",
    BREADY_BAD: /外企参标或授标受限|外企须|专留给本地企业/,  // 这几条细项答「是」反倒是坏事(对我们投标不利),标红
    bready(rec, name) {
      const src = '<p class="muted small">来源:<a href="' + this.BREADY_URL + '" target="_blank" rel="noopener">世界银行 Business Ready(B-READY)↗</a>' +
        "。分数 0–100,专家问卷加企业调查,测的是这国最大的商业城市;总分是三大支柱(监管框架、公共服务、运行效率)的平均。世行不给国家排名,这里也只看分数和含义。</p>";
      if (!this.has("country", "bready_edition")) return '<p class="empty">「营商环境」的字段还没建(跑一次建表命令后就有)。</p>';
      const g = (p) => this.get("country", rec, p), n = (p) => { const v = g(p); return v === undefined || v === null || v === "" ? null : Number(v); };
      if (n("bready_total") === null) return '<div class="panel"><p><b>世界银行 B-READY 这一期没有覆盖' + this.esc(name || "这个国家") + "。</b></p>" +
        '<p class="muted">2025 年这一期评了 101 个经济体(2024 年 50 个),世行计划扩大到约 180 个;新一期出来、数据文件换掉后这里自动补上。</p></div>' + src;
      const sc = (p) => (n(p) === null ? "" : n(p).toFixed(1) + " 分"), day = (p) => (n(p) === null ? "" : Math.round(n(p)) + " 天");
      const cur = g("water_tariff_cur") || "", usd = n("water_tariff_usd"), loc = n("water_tariff");
      const kp = [{ label: "营商环境总分", value: sc("bready_total"), note: "三大支柱平均" }, { label: "供水得分", value: sc("bready_water"), note: "水价监管、漏损目标、接水与供水可靠性" },
        { label: "公共采购得分", value: sc("bready_procure"), note: "招投标规则、电子采购、授标与付款" },
        usd !== null ? { label: "商业水价", value: "约 " + usd.toFixed(2) + " 美元/m³", note: (loc !== null ? loc.toLocaleString("zh-CN", { maximumFractionDigits: 3 }) + " " + cur + "/m³," : "") + "水费 + 污水费,每月 1000 m³ 的商业用户" }
          : { label: "商业水价", value: "未公布", note: "这一期没有可用的数" }];
      const fb = g("foreign_bid") || "", fbTone = /全部/.test(fb) ? "bad" : /部分|说法不一/.test(fb) ? "warn" : "";
      const rows = [["外企参标限制", fb || "—", { tone: fbTone, why: fbTone === "bad" ? "所有公共招标都对外企设限" : fbTone ? (/说法不一/.test(fb) ? "专家看法不一,投标前要核实" : "部分招标对外企设限,投标前要核实") : "" }],
        ["大型工程授标天数", day("award_days"), { note: "国际招标门槛以上的工程,从发标到授标(专家估计)" }], ["政府合同回款天数", day("gov_pay_days"), { note: "企业收到政府合同款的平均天数" }],
        ["企业供水可靠比例", n("water_reliable") === null ? "" : n("water_reliable").toFixed(1) + "%", { note: "没遇到缺水的企业占比" }], ["接水天数", day("water_conn_days"), { note: "企业新接供水的天数(中位数)" }],
        ["纠纷解决得分", sc("bready_dispute"), { note: "商事纠纷解决" }]];
      const items = String(g("bready_items") || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
      const col = (grp) => {
        const li = items.filter((x) => x.startsWith("[" + grp + "]")).map((x) => {
          const t = x.replace(/^\[[^\]]+\]\s*/, ""), i = t.lastIndexOf(":"), k = i > 0 ? t.slice(0, i) : t, v = i > 0 ? t.slice(i + 1) : "";
          const bad = this.BREADY_BAD.test(k) && v && !/^否/.test(v) && !/^说法不一/.test(v);
          return '<li' + (bad ? ' class="bad"' : "") + "><span>" + this.esc(k) + '</span><b class="' + (/^否/.test(v) ? "no" : "yes") + '">' + this.esc(v || "") + (bad ? '<em class="over">对外企投标不利</em>' : "") + "</b></li>";
        });
        return li.length ? '<ul class="bitems">' + li.join("") + "</ul>" : '<p class="muted">(无)</p>';
      };
      return this.kpiHtml(kp) +
        '<div class="panel">' + this.factsHtml(rows) + "</div>" +
        '<div class="bcols"><div class="panel"><h3>供水细项</h3>' + col("供水") + '</div><div class="panel"><h3>公共采购细项</h3>' + col("采购") + "</div></div>" +
        src.replace("</p>", "数据:" + this.esc(g("bready_edition") || "") + "。</p>");
    },
    /* 已中标项目(本体 V7、V16;2026-10-12 使用方要):国别概况、城市一页最下面的同一块。prop = "country" 或 "city",rid = 那条记录。
       类别、合同类型、承包模式、金额(折合人民币分档)、年份可多选筛选(2026-10-13 使用方要合同类型、金额筛选与排序),筛选项上的数跟着别的项与搜索走;
       卡片(份数、合计中标价、最大一笔、中国企业中标)与表只算筛出来的;表默认按中标时间从新到旧,可改按最早、金额最高、金额最低排。
       v2.21(2026-10-13):多一组「主体工程」筛选(污水处理厂、供水厂、管网……);筛出来的有招标估价、评标天数、投标家数的,再加一排卡片(中位数,写明按几份算),
       表里中标价下面写招标估价与比例,中标时间下面写评标天数与投标家数。 */
    /* 已中标项目的几项过程数据(v2.21):招标估价(本国采购网才有)、评标天数、投标家数。首页助手的清单与这里的表同一写法 */
    awRatio(r) { const p = Number(this.get("award", r, "price")), e = this.has("award", "estimate") ? Number(this.get("award", r, "estimate")) : 0; return p > 0 && e > 0 ? p / e * 100 : null; },
    awEstimateLine(r) {
      const k = this.awRatio(r);
      return k === null ? "" : '<br><span class="muted small nw" title="中标价 ÷ 招标估价(同一币种)">招标估价的 ' + Math.round(k) + "%</span>";
    },
    awEvalLine(r) {
      const n = (p) => { const v = this.has("award", p) ? this.get("award", r, p) : null; return v === null || v === undefined || v === "" ? null : Number(v); };
      const d = n("eval_days"), b = n("bidders"), t = [d !== null ? "评标 " + d + " 天" : "", b !== null ? b + " 家投标" : ""].filter(Boolean);
      return t.length ? '<br><span class="muted small nw">' + this.esc(t.join(" · ")) + "</span>" : "";
    },
    awExtraKpis(list) {  // 有数据的才出卡片;中位数,写明按几份算(其余的来源不公布)
      const med = (a) => { const s = a.slice().sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
      const num = (r, p) => { const v = this.has("award", p) ? this.get("award", r, p) : null; return v === null || v === undefined || v === "" ? null : Number(v); };
      const ks = list.map((r) => this.awRatio(r)).filter((v) => v !== null), ds = list.map((r) => num(r, "eval_days")).filter((v) => v !== null), bs = list.map((r) => num(r, "bidders")).filter((v) => v !== null);
      const out = [];
      if (ks.length) out.push({ label: "中标价 / 招标估价(中位数)", value: Math.round(med(ks)) + "%", note: "按 " + ks.length + " 份有估价的算,低于估价的 " + ks.filter((v) => v < 100).length + " 份" });
      if (ds.length) out.push({ label: "评标天数(中位数)", value: Math.round(med(ds)) + " 天", note: "按 " + ds.length + " 份算" });
      if (bs.length) out.push({ label: "投标家数(中位数)", value: (Math.round(med(bs) * 10) / 10) + " 家", note: "按 " + bs.length + " 份算" });
      return this.kpiHtml(out);
    },
    async awards(el, prop, rid) {  // prop:country / city(国别、城市页) / winners(供应商页:多一组国别筛选、一列国别)
      const firm = prop === "winners";
      const head = firm ? '<h2 id="awards">中标记录</h2><p class="muted small">这家供应商在公开中标记录里的全部合同(联合体的合同全额计入每一家)。</p>' :
        '<h2 id="awards">已中标项目</h2><p class="muted small">公开的中标记录(世界银行、亚洲开发银行、欧洲复兴开发银行贷款里 10 万美元以上的水务合同;摩洛哥、吉尔吉斯斯坦另有本国公共采购网的结果,摩洛哥 100 万迪拉姆以上、吉尔吉斯斯坦 10 万美元以上)和人工补录的,作报价与竞争对手的参考;不计入线索、商机。' +
        (this.cfg.tables && this.cfg.tables.contractor ? '点供应商看它在各国的全部中标,<a href="contractor.html">全部供应商 →</a>' : "") + "</p>";
      if (!(this.cfg.tables && this.cfg.tables.award) || !this.has("award", prop)) { el.innerHTML = head + '<p class="empty">「已中标项目」表还没建(跑一次建表命令后这里就有)。</p>'; return 0; }
      el.innerHTML = head + '<div class="panel">读取中…</div>';
      const many = (this.t("award").links || {})[prop] === "many", hasW = this.has("award", "winners");
      const rows = await this.records("award", { filter: this.and(many ? this.cond("award", prop, "hasAnyOf", [].concat(rid)) : Array.isArray(rid) ? this.cond("award", prop, "isAnyOf", rid) : this.cond("award", prop, "is", rid)),
        props: ["name", "name_zh", "sector", "contract_type", "price", "currency", "price_cny", "awarded", "quantity", "winner", "winner_country", "project", "funder", "method", "country", "city", "source", "url"]
          .concat(hasW ? ["winners"] : [], ["buyer", "delivery", "asset", "estimate", "eval_days", "bidders"].filter((x) => this.has("award", x))) });
      const g = (r, p) => this.get("award", r, p);
      const isoOf = {};  // 供应商页的国别列点进国别概况的「已中标项目」页签(那页按国别码开)
      if (firm && rows.length) (await this.records("country", { props: ["key"] })).forEach((c) => { isoOf[c.id] = this.get("country", c, "key"); });
      const year = (r) => (this.day(g(r, "awarded")) || "").slice(0, 4);
      const ctry = (r) => this.text(g(r, "country")) || "";
      rows.sort((a, b) => String(this.day(g(b, "awarded")) || "").localeCompare(String(this.day(g(a, "awarded")) || "")));
      if (!rows.length) { el.innerHTML = head + '<p class="empty">还没有记录。' + (prop === "city" ? "导入时合同名称里写了这座城市的才挂到城市上,其余只在国别概况里;" : "") + "别的来源的中标结果可以在表格「已中标项目」里补一行。</p>"; return 0; }
      const st = { s: [], a: [], ct: [], d: [], m: [], y: [], c: [], q: "", o: "new" };
      const big = (n) => (n >= 1e8 ? (n / 1e8).toFixed(n >= 1e10 ? 0 : 2) + " 亿" : n >= 1e4 ? Math.round(n / 1e4).toLocaleString("zh-CN") + " 万" : Math.round(n).toLocaleString("zh-CN"));
      const curZh = (c) => ({ USD: "美元", EUR: "欧元", CNY: "元" }[c] || (c ? " " + c : ""));
      const cny = (w) => (w >= 1e4 ? (Math.round(w / 100) / 100).toFixed(2) + " 亿元" : Math.round(w).toLocaleString("zh-CN") + " 万元");  // 万元 → 文字
      const words = (r) => [g(r, "name"), g(r, "name_zh"), g(r, "winner"), g(r, "winner_country"), g(r, "project"), g(r, "buyer"), firm ? ctry(r) : ""].join(" ").toLowerCase();
      /* 金额按折合人民币(万元)分五档;没折算的(没有金额或汇率)单列,数对得上 */
      const BANDS = [["100 万元以下", 0, 100], ["100–500 万", 100, 500], ["500–2,000 万", 500, 2000], ["2,000 万–1 亿", 2000, 1e4], ["1 亿元以上", 1e4, Infinity]];
      const band = (r) => { const w = Number(g(r, "price_cny")) || 0; return w ? (BANDS.find(([, lo, hi]) => w >= lo && w < hi) || BANDS[0])[0] : "未折算"; };
      /* 各筛选项:取值、全部选项(本体的顺序,只列有的;空着的单列一项,让各项的数加起来等于全部) */
      const DIMS = [
        ["s", "类别", (r) => g(r, "sector") || "其他", this.values("lead_sectors")],
        ["a", "主体工程", (r) => g(r, "asset") || "未分", this.has("award", "asset") ? this.values("award_asset").concat(["未分"]) : []],
        ["ct", "合同类型", (r) => g(r, "contract_type") || "未标", this.values("award_contract_type").concat(["未标"])],
        ["d", "承包模式", (r) => g(r, "delivery") || "货物、咨询等", this.has("award", "delivery") ? this.values("award_delivery").concat(["货物、咨询等"]) : []],
        ["m", "金额", band, BANDS.map((b) => b[0]).concat(["未折算"])],
        ["y", "年份", year, [...new Set(rows.map(year).filter(Boolean))].sort().reverse()],
        ["c", "国别", ctry, firm ? Object.entries(rows.reduce((m, r) => { const c = ctry(r); if (c) m[c] = (m[c] || 0) + 1; return m; }, {})).sort((a, b) => b[1] - a[1]).map((x) => x[0]) : []],
      ].map(([k, label, f, all]) => ({ k, label, f, opts: all.filter((v) => rows.some((r) => f(r) === v)) })).filter((x) => x.opts.length > (x.k === "c" ? 1 : 0));
      const TIPS = { a: "按合同名称归的主要工程:水厂(污水处理厂、供水厂、海水淡化厂)、管网与配套、其他", d: "合同怎么包:施工、设计施工(EPC)、DBO(建了还管运营)、BOT/PPP(承包方投资)、运营;货物、咨询服务等不按这个分", m: "按中标价折合人民币分档" };
      const pass = (r, skip) => DIMS.every((x) => x.k === skip || !st[x.k].length || st[x.k].includes(x.f(r))) && (!st.q || words(r).includes(st.q.toLowerCase()));
      const SORTS = [["new", "最新"], ["old", "最早"], ["hi", "金额最高"], ["lo", "金额最低"]];
      const day = (r) => String(this.day(g(r, "awarded")) || ""), wan = (r) => Number(g(r, "price_cny")) || 0;
      const order = (list) => {  // 没有金额的排在最后
        if (st.o === "old") return list.slice().sort((a, b) => (day(a) || "9").localeCompare(day(b) || "9"));
        if (st.o === "hi" || st.o === "lo") return list.slice().sort((a, b) => (!wan(a) - !wan(b)) || (st.o === "hi" ? wan(b) - wan(a) : wan(a) - wan(b)));
        return list;  // rows 已按中标时间从新到旧
      };
      el.innerHTML = head + '<div class="filters">' + DIMS.map((x) => '<div class="frow"><b' + (TIPS[x.k] ? ' title="' + this.esc(TIPS[x.k]) + '"' : "") + ">" + x.label +
        '</b><span class="frow aw-' + x.k + '"></span></div>').join("") +
        '<div class="frow"><b>排序</b><span class="frow aw-o"></span></div>' +
        '<div class="frow"><b>搜索</b><input class="aw-q" placeholder="' + (firm ? "搜名称、项目或国别" : "搜名称、中标单位或国别(如:中国)") + '" size="30" aria-label="搜已中标项目"><span class="muted small aw-n push"></span></div></div>' +
        '<div class="aw-k"></div><div class="panel aw-t"></div>';
      const $ = (c) => el.querySelector(c), PAGE = 20;  // 表一次列 20 份,「再列」往下加
      let show = PAGE;
      const refilter = () => { show = PAGE; render(); };  // 换了筛选、排序从头列
      this.chips($(".aw-o"), SORTS, st, "o", refilter);
      const render = () => {
        const list = order(rows.filter((r) => pass(r)));
        DIMS.forEach((x) => { const base = rows.filter((r) => pass(r, x.k));
          this.chips($(".aw-" + x.k), [["", "全部 " + base.length]].concat(x.opts.map((v) => [v, v + " " + base.filter((r) => x.f(r) === v).length])), st, x.k, refilter, true); });
        const sum = list.reduce((s, r) => s + (Number(g(r, "price_cny")) || 0), 0), max = Math.max(0, ...list.map((r) => Number(g(r, "price_cny")) || 0));
        const cn = list.filter((r) => /中国/.test(String(g(r, "winner_country") || ""))).length;
        const nC = new Set(list.map(ctry).filter(Boolean)).size;
        $(".aw-k").innerHTML = this.kpiHtml([{ label: firm ? "中标" : "已中标项目", value: list.length + " 份" }, { label: "合计中标价(折合人民币)", value: sum ? cny(sum) : "—" },
          { label: "最大一笔", value: max ? cny(max) : "—" },
          firm ? { label: "涉及国别", value: nC + " 个", note: list.length ? "最近一份 " + (this.day(g(list[0], "awarded")) || "—") : "" } :
            { label: "中国企业中标", value: cn + " 份", note: list.length ? "占 " + Math.round(cn * 100 / list.length) + "%" : "" }]);
        $(".aw-k").insertAdjacentHTML("beforeend", this.awExtraKpis(list));
        $(".aw-n").textContent = "共 " + rows.length + " 份";
        const place = prop === "country" || Array.isArray(rid) ? "城市" : firm ? "国别" : "";
        $(".aw-t").innerHTML = !list.length ? '<p class="empty">没有符合条件的已中标项目。</p>' :
          this.tableHtml(["中标时间", "名称", "类别", "合同类型", "中标价", "工程量", firm ? "联合体伙伴" : "中标单位"].concat(place ? [place] : [], ["资金来源", ""]), list.slice(0, show).map((r) => {
            const zh = g(r, "name_zh"), en = g(r, "name") || "", u = g(r, "url");
            const nm = (zh ? "<b>" + this.esc(zh) + '</b><br><span class="muted small">' + this.esc(en) + "</span>" : "<b>" + this.esc(en) + "</b>") +
              (g(r, "project") ? '<br><span class="muted small">项目:' + (u ? '<a href="' + this.esc(u) + '" target="_blank" rel="noopener">' + this.esc(g(r, "project")) + " ↗</a>" : this.esc(g(r, "project"))) + "</span>" : "") +
              (g(r, "buyer") ? '<br><span class="muted small">采购方:' + this.esc(g(r, "buyer")) + "</span>" : "") +
              (!g(r, "project") && u ? ' <a class="small" href="' + this.esc(u) + '" target="_blank" rel="noopener">公告原文 ↗</a>' : "");  // 本国采购网的记录没有所属项目,链到公告
            const p = Number(g(r, "price")), w = Number(g(r, "price_cny"));
            const price = '<span class="nw">' + (p ? "<b>" + this.esc(big(p) + curZh(g(r, "currency"))) + "</b>" : "") + (w ? '<br><span class="muted small">≈ ' + this.esc(cny(w)) + "</span>" : "") + "</span>" + this.awEstimateLine(r);
            const ws = hasW ? [].concat(g(r, "winners") || []).filter((x) => x && x.id) : [];  // 有「中标方」关联的,每家点进供应商页
            const link = (x) => '<a href="contractor.html?id=' + encodeURIComponent(x.id) + '">' + this.esc(x.title || "") + "</a>";
            const mates = ws.filter((x) => x.id !== rid);  // 供应商页只列联合体里的其他几家
            const win = firm ? (mates.length ? mates.map(link).join("、") : '<span class="muted">独家中标</span>') :
              (ws.length ? ws.map(link).join("、") : this.esc(g(r, "winner") || "")) + (g(r, "winner_country") ? '<br><span class="muted small">' + this.esc(g(r, "winner_country")) + "</span>" : "");
            const at = (href, f) => href ? this.raw('<a class="nw" href="' + href + '">' + this.esc(this.text(g(r, f))) + "</a>") : this.text(g(r, f)) || "";
            const cid = this.linkId(g(r, "city")), iso = isoOf[this.linkId(g(r, "country"))];
            const dv = g(r, "delivery"), ct = this.esc(g(r, "contract_type") || "") + (dv && !["施工", "运营"].includes(dv) ? '<br><span class="tag" title="承包模式">' + this.esc(dv) + "</span>" : "");
            return [this.raw('<span class="nw">' + this.esc(this.day(g(r, "awarded")) || "") + "</span>" + this.awEvalLine(r)), this.raw(nm), g(r, "sector") || "", this.raw(ct), this.raw(price), g(r, "quantity") || "", this.raw(win)]
              .concat(place ? [place === "城市" ? at(cid && "city.html?id=" + encodeURIComponent(cid), "city") : at(iso && "country.html?" + new URLSearchParams({ iso, t: "awards" }), "country")] : [], [g(r, "funder") || "", this.raw(this.open("award", r.id))]);
          })) + (list.length > show ? '<p><button type="button" class="chip-btn more aw-more">再列 ' + Math.min(PAGE, list.length - show) + " 份(已列 " + show + " / " + list.length + ")</button></p>" : "");
        const more = $(".aw-more"); if (more) more.onclick = () => { show += PAGE; render(); };
      };
      let tt = 0; $(".aw-q").oninput = () => { clearTimeout(tt); tt = setTimeout(() => { st.q = $(".aw-q").value.trim(); show = PAGE; render(); }, 250); };
      render();
      return rows.length;
    },
    /* 综述(研究资料里「综述」那一行的 summary,Markdown;2026-10-13 使用方嫌排版单调、出处是一串字符):
       开头「来源简称 / 文件简称:缩写=全称;…」那段不在正文里显示,收进文末「出处」;全称写成 [全称](网址) 的能点开原文。
       正文括号里的「缩写 p14」→ 带序号的小角标(序号按第一次引用排),点角标弹出全称、页码和「打开原文第 14 页」;只写「p23」的算前一个出处;
       括号外的「缩写 p7 的口径」→ 全称 + 角标。「## N. 标题」分节,节名排成目录;「- 标签:内容」标签单列加粗、内容一句一行;
       「要点速览」一节(每条以 **数值** 开头)画成指标卡,前面带「!」的标红(说明里写清它坏在哪);标题带「推断」的一节换底色,条目开头的「推断:」去掉。
       uid 区分同一页上的几份综述(角标、出处的锚点)。 */
    digestHtml(src, uid) {
      uid = "dg-" + String(uid || "x").replace(/[^\w-]/g, "");
      const esc = (s) => this.esc(s), by = {}, srcs = [], notes = [];
      const add = (k, desc, url) => { k = k.trim(); if (!by[k]) by[k] = { k, desc: "", url: "", pages: [], n: 0 }; if (desc && !by[k].desc) by[k].desc = desc; if (url && !by[k].url) by[k].url = url; return by[k]; };
      const lines = String(src || "").split(/\r?\n/);
      /* 1. 出处表:「缩写=全称」用「;」或「,」隔开,几个缩写可以「/」并列(全称也「/」并列且各段像个名字时一一对上,不然共用) */
      const li = lines.findIndex((l) => /(来源简称|文件简称)[:：]/.test(l));
      if (li >= 0) {
        const l = lines[li], m = l.match(/^(.*?)(?:来源简称|文件简称)[:：]\s*(.*)$/), KEY = /(?:^|[;；,，]\s*)([^;；,，。=\s（）()\[\]]+(?:\/[^;；,，。=\s（）()\[\]]+)*)=/g;
        if (m[1].trim()) notes.push(m[1].trim());
        const body = m[2], hits = [...body.matchAll(KEY)];
        hits.forEach((h, i) => {
          let d = body.slice(h.index + h[0].length, i + 1 < hits.length ? hits[i + 1].index : body.length).replace(/[;；,，]\s*$/, "").trim();
          if (i + 1 === hits.length) { const j = d.search(/。(?![^\[]*\])/); if (j >= 0) { const rest = d.slice(j + 1).trim(); if (rest) notes.push(rest); d = d.slice(0, j); } }
          const lk = d.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/), desc = lk ? lk[1] : d, url = lk ? lk[2] : "";
          const ks = h[1].split("/"), ds = desc.split("/");
          ks.forEach((k, n) => add(k, ks.length > 1 && ds.length === ks.length && ds.every((x) => x.length >= 4) ? ds[n] : desc, url));
        });
        lines.splice(li, 1);
      }
      /* 2. 引用:先把带页码出现过的缩写都记下(括号里只写缩写、没写页码的也能认) */
      const PG = "p\\d+(?:\\s*[-–]\\s*\\d+)?(?:表\\d+)?";
      const PART = new RegExp("^(.*?)\\s*(" + PG + "(?:\\s*[、/]\\s*" + PG + ")*)$");
      const parts = (g) => g.split(/[;；，]|,(?!\d{3})/).map((p) => p.trim()).filter(Boolean).map((p) => {
        const m = p.match(PART); return m ? { k: m[1].trim(), pages: m[2].split(/\s*[、/]\s*/).map((x) => x.slice(1).replace(/\s*[-–]\s*/, "–").replace(/表\d+$/, "")) } : { text: p };
      });
      const GROUP = /[（(]([^（）()]*)[）)]/g;
      lines.forEach((l) => { for (const g of l.matchAll(GROUP)) parts(g[1]).forEach((p) => { if (p.pages && p.k && /^[^\s]{2,}$/.test(p.k)) add(p.k); }); });
      const known = (k) => by[k] || (/^[一-鿿]{1,6}[A-Z]{2,}[A-Za-z-]*\d{2,4}$/.test(k) ? add(k) : null);
      let last = null;
      const mark = (s, pages) => {
        if (!s.n) { srcs.push(s); s.n = srcs.length; }
        (pages || []).forEach((p) => { if (!s.pages.includes(p)) s.pages.push(p); });
        last = s;
        const tip = (s.desc || s.k) + (pages && pages.length ? " · PDF 第 " + pages.join("、") + " 页" : "");
        return '<a href="#' + uid + "-s" + s.n + '" data-u="' + uid + '" data-s="' + s.n + '" data-p="' + esc((pages || []).join(",")) + '" title="' + esc(tip) + '">' + s.n + "</a>";
      };
      const sup = (as) => as.length ? '<sup class="sref">' + as.join("") + "</sup>" : "";
      /* 正文一段 → HTML:括号里的出处变角标,别的文字转义后加粗 **…**、带单位的数字加重 */
      const NUM = /(\d[\d,]*(?:\.\d+)?(?:\s?[–-]\s?\d[\d,]*(?:\.\d+)?)?\s?(?:%|‰|(?:亿|万|百万|千)?\s?(?:美元|欧元|人民币|元|迪拉姆|马纳特|索姆|MAD|AZN|KGS|BCM|Mm3|Mm³|m3|m³|km³|km|公里|公顷|户|人|座|个|家|处|吨|小时|天)|亿|万|百万))/g;
      const plain = (t) => esc(t).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(NUM, '<span class="dn">$1</span>');
      const keysRe = () => { const ks = Object.keys(by).filter((k) => by[k].desc).sort((a, b) => b.length - a.length).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")); return ks.length ? new RegExp("(" + ks.join("|") + ")\\s*(" + PG + "(?:\\s*[、/]\\s*" + PG + ")*)", "g") : null; };
      const inline = (t) => {  // 括号外的「缩写 p7」→ 全称 + 角标
        const re = keysRe(); if (!re) return plain(t);
        let out = "", i = 0;
        for (const m of t.matchAll(re)) { const s = by[m[1]]; out += plain(t.slice(i, m.index)) + esc(s.desc) + sup([mark(s, m[2].split(/\s*[、/]\s*/).map((x) => x.slice(1).replace(/\s*[-–]\s*/, "–")))]); i = m.index + m[0].length; }
        return out + plain(t.slice(i));
      };
      const rich = (t) => {
        let out = "", i = 0;
        for (const g of t.matchAll(GROUP)) {
          const ps = parts(g[1]), cite = ps.some((p) => p.pages) || ps.every((p) => p.text && known(p.text));
          if (!cite) continue;
          const keep = [], as = [];
          ps.forEach((p) => {
            if (p.pages) { const s = p.k ? (by[p.k] || add(p.k)) : last; if (s) as.push(mark(s, p.pages)); else keep.push(p.k + " p" + p.pages.join("、p")); }
            else if (known(p.text)) as.push(mark(known(p.text)));
            else keep.push(p.text);
          });
          out += inline(t.slice(i, g.index)).replace(/\s+$/, "") + (keep.length ? "（" + inline(keep.join("，")) + "）" : "") + sup(as);
          i = g.index + g[0].length;
        }
        return out + inline(t.slice(i));
      };
      /* 3. 分块:节(## …)、条目(- …,缩进的是子条目)、段落 */
      const secs = []; let sec = null, intro = [];
      const stack = [];
      lines.forEach((l) => {
        if (!l.trim()) return;
        const h = l.match(/^#{1,4}\s+(.*)$/), it = l.match(/^(\s*)[-*]\s+(.*)$/);
        if (h && /^#\s/.test(l)) return;  // 一级标题(综述本身的名字)上面已经有了
        if (h) { sec = { title: h[1].trim(), items: [] }; secs.push(sec); stack.length = 0; return; }
        const into = sec ? sec.items : intro;
        if (it) {
          const lv = Math.floor(it[1].replace(/\t/g, "  ").length / 2), node = { text: it[2].trim(), kids: [] };
          while (stack.length && stack[stack.length - 1].lv >= lv) stack.pop();
          (stack.length ? stack[stack.length - 1].node.kids : into).push(node);
          stack.push({ lv, node });
        } else { stack.length = 0; into.push({ text: l.trim(), para: true }); }
      });
      const label = (t) => {  // 「标签:内容」→ [标签, 内容];标签不长于 24 个字(不算括号里的出处)、不含句号
        const i = t.search(/[:：]/); if (i <= 0) return null;
        const k = t.slice(0, i), v = t.slice(i + 1).trim();
        return k.replace(GROUP, "").length <= 24 && !/[。;；]/.test(k) ? [k, v] : null;
      };
      const sentences = (t) => t.split(/(?<=。)(?![）)])/).map((x) => x.trim()).filter(Boolean);
      const sub = (kids) => kids.length ? '<ul class="dg-sub">' + kids.map((n) => { const kv = label(n.text);
        return "<li>" + (kv && kv[1] ? "<b>" + rich(kv[0]) + "</b>：" + rich(kv[1]) : rich(n.text)) + sub(n.kids) + "</li>"; }).join("") + "</ul>" : "";
      const item = (n, infer) => {
        if (n.para) return /^\*\*[^*]+\*\*[:：]?$/.test(n.text) ? '<h5 class="dg-h5">' + rich(n.text.replace(/[:：]$/, "")) + "</h5>" : '<p class="dg-p">' + rich(n.text) + "</p>";
        const t = infer ? n.text.replace(/^推断[:：]\s*/, "") : n.text, kv = infer ? null : label(t);
        const body = (v) => sentences(v).map((s) => "<p>" + rich(s) + "</p>").join("");
        return kv ? '<div class="dg-it"><div class="dg-k">' + rich(kv[0]) + '</div><div class="dg-v">' + body(kv[1]) + sub(n.kids) + "</div></div>"
          : '<div class="dg-it dg-wide"><div class="dg-v">' + body(t) + sub(n.kids) + "</div></div>";
      };
      const kpi = (n) => {  // 「!**数值** 说明」→ 指标卡
        const m = n.text.match(/^(!?)\*\*([^*]+)\*\*\s*(.*)$/), v = m[2].trim(), x = v.match(/^(约|近|超过|≤|<|>)?\s*(-?\d[\d,]*(?:\.\d+)?(?:\s?[–-]\s?\d[\d,]*(?:\.\d+)?)?)\s*(.*)$/);
        const big = x ? (x[1] ? '<span class="ku">' + esc(x[1]) + "</span>" : "") + '<span class="kn">' + esc(x[2]) + "</span>" + (x[3] ? '<span class="ku">' + esc(x[3]) + "</span>" : "") : '<span class="kn kt">' + esc(v) + "</span>";
        return '<div class="kpi' + (m[1] ? " is-bad" : "") + '"><div class="kv">' + big + '</div><div class="kl">' + rich(m[3]) + "</div></div>";
      };
      const head = (s, i) => { const m = s.title.match(/^(\d+)[.、．]\s*(.*)$/); return { no: m ? m[1] : "", name: (m ? m[2] : s.title).replace(/[（(](?:以下为)?推断[^（）()]*[）)]/, "").trim(), infer: /推断/.test(s.title), id: uid + "-c" + i }; };
      const hs = secs.map(head);
      const html = secs.map((s, i) => {
        const h = hs[i], cards = /要点|速览|关键指标/.test(h.name) && s.items.length && s.items.every((n) => !n.para && /^!?\*\*[^*]+\*\*/.test(n.text));
        return '<section class="dg-sec' + (h.infer ? " infer" : "") + (cards ? " dg-kpi" : "") + '" id="' + h.id + '"><h4>' + (h.no ? '<span class="dg-no">' + esc(h.no) + "</span>" : "") + esc(h.name) +
          (h.infer ? '<span class="tag t-warn">推断</span><span class="muted small">本系统据资料推断,不是文件原文</span>' : "") + "</h4>" +
          (cards ? '<div class="kpis">' + s.items.map(kpi).join("") + "</div>" : s.items.map((n) => item(n, h.infer)).join("")) + "</section>";
      }).join("");
      const toc = hs.length > 2 ? '<nav class="dg-toc">' + hs.map((h) => '<a href="#' + h.id + '">' + (h.no ? "<b>" + esc(h.no) + "</b> " : "") + esc(h.name) + "</a>").join("") + "</nav>" : "";
      const rest = Object.values(by).filter((s) => !s.n && s.desc);  // 出处表里列了、正文没引用的也列上
      const all = srcs.concat(rest);
      const pnum = (p) => parseInt(p, 10) || 0;
      const srcHtml = all.length ? '<details class="dg-src" id="' + uid + '-src"><summary>出处:' + all.length + " 份文件" + (notes.length ? "(" + esc(notes.join(" ").replace(/。$/, "")) + ")" : "") + "</summary><ol>" +
        all.map((s) => '<li id="' + uid + "-s" + (s.n || "") + '"' + (s.n ? "" : ' class="muted"') + '><span class="dg-sn">' + (s.n || "·") + "</span>" + (s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.desc || s.k) + " ↗</a>" : esc(s.desc || s.k)) +
          ' <span class="muted small">' + esc(s.k) + (s.pages.length ? " · 引用 PDF 第 " + s.pages.slice().sort((a, b) => pnum(a) - pnum(b)).join("、") + " 页" : s.n ? "" : " · 正文未直接引用") + "</span></li>").join("") + "</ol></details>" : "";
      (this._dg = this._dg || {})[uid] = srcs;
      this.srefOn();
      return '<div class="dg">' + (intro.length ? intro.map((n) => item(n)).join("") : "") + toc + html + srcHtml + "</div>";
    },
    /* 角标点开:小框里是全称、页码、「打开原文第 N 页」(PDF 链接带 #page=N)和「全部出处」;点别处关掉 */
    srefOn() {
      if (this._srefOn) return; this._srefOn = true;
      document.addEventListener("click", (e) => {
        const t = e.target, a = t.closest && t.closest(".sref a"), pop = document.querySelector(".sref-pop");
        if (pop && t.closest(".sref-pop")) { if (t.closest("a.all")) { const d = document.getElementById(t.closest("a.all").getAttribute("href").slice(1)); if (d) d.open = true; pop.remove(); } return; }
        if (pop) pop.remove();
        if (!a) return;
        e.preventDefault();
        const s = ((this._dg || {})[a.dataset.u] || [])[Number(a.dataset.s) - 1]; if (!s) return;
        const pg = (a.dataset.p || "").split(",").filter(Boolean), p1 = pg.length ? parseInt(pg[0], 10) : 0;
        const el = document.createElement("div"); el.className = "sref-pop";
        el.innerHTML = '<div><b>' + s.n + "</b> " + this.esc(s.desc || s.k) + "</div>" + '<div class="muted small">' + this.esc(s.k) + (pg.length ? " · PDF 第 " + this.esc(pg.join("、")) + " 页" : "") + "</div>" +
          '<div class="small">' + (s.url ? '<a href="' + this.esc(s.url + (p1 ? "#page=" + p1 : "")) + '" target="_blank" rel="noopener">打开原文' + (p1 ? "第 " + p1 + " 页" : "") + " ↗</a> · " : "") + '<a class="all" href="#' + a.dataset.u + '-src">全部出处</a></div>';
        document.body.appendChild(el);
        const r = a.getBoundingClientRect(), w = el.offsetWidth;
        el.style.left = Math.max(8, Math.min(r.left + window.scrollX - 12, window.scrollX + document.documentElement.clientWidth - w - 8)) + "px";
        el.style.top = (r.bottom + window.scrollY + 6) + "px";
      });
    },
    /* 水务年度大事记(本体 V7、V16;2026-10-13 使用方要):国别概况「水务历史资料」页签最上面、城市一页里的同一块。
       水务事件(所属国别 / 所在城市 = 它)+ 已中标项目里每年最大的三份(页面现算,不存);按年从新到旧,类别可多选筛选,筛选项上的数跟着走。
       水务片区页(V20)传 prop = "city"、rid = 片区里几座城市的编号数组。→ 条数 */
    async chronicle(el, prop, rid) {
      const many = Array.isArray(rid), byCity = prop === "country" || many;
      const on = (oid) => many ? this.cond(oid, prop, "isAnyOf", rid.length ? rid : ["__none__"]) : this.cond(oid, prop, "is", rid);
      const head = '<h2 id="chronicle">水务大事记</h2><p class="muted small">项目批准与关闭来自世界银行项目库;机构改革、规划、水价、工程投运摘自世行等机构文件原文(出处写到页码);' +
        "「中标」是已中标项目里每年最大的三份。人工可在表格「水务事件」里补。</p>";
      const hasE = this.cfg.tables && this.cfg.tables.water_event && this.has("water_event", prop);
      const hasA = this.cfg.tables && this.cfg.tables.award && this.has("award", prop);
      if (!hasE && !hasA) { el.innerHTML = head + '<p class="empty">「水务事件」表还没建(跑一次建表命令后这里就有)。</p>'; return 0; }
      el.innerHTML = head + '<div class="panel">读取中…</div>';
      const RAW = "日期原文";
      const [evs, aws] = await Promise.all([
        hasE ? this.records("water_event", { filter: this.and(on("water_event")), props: ["day", RAW, "title", "kind", "city", "source", "url"] }) : [],
        hasA ? this.records("award", { filter: this.and(on("award")), props: ["name", "name_zh", "price_cny", "awarded", "winner", "winner_country", "city", "url"] }) : [],
      ]);
      const ge = (r, p) => this.get("water_event", r, p), ga = (r, p) => this.get("award", r, p);
      const cny = (w) => (w >= 1e4 ? (Math.round(w / 100) / 100).toFixed(2) + " 亿元" : Math.round(w).toLocaleString("zh-CN") + " 万元");
      const items = evs.map((r) => {
        const raw = String(ge(r, RAW) || "").trim(), day = this.day(ge(r, "day")) || "";
        return { y: (raw || day).slice(0, 4), when: raw || day, kind: ge(r, "kind") || "其他", title: ge(r, "title") || "", src: ge(r, "source") || "", url: ge(r, "url") || "",
          city: byCity ? { id: this.linkId(ge(r, "city")), name: this.text(ge(r, "city")) } : null, open: r.id, oid: "water_event" };
      });
      const byYear = {};  // 已中标项目:每年按折合人民币取最大的三份
      aws.forEach((r) => { const d = this.day(ga(r, "awarded")) || "", w = Number(ga(r, "price_cny")) || 0; if (d && w) (byYear[d.slice(0, 4)] = byYear[d.slice(0, 4)] || []).push({ r, d, w }); });
      Object.values(byYear).forEach((arr) => arr.sort((a, b) => b.w - a.w).slice(0, 3).forEach(({ r, d, w }) => items.push({
        y: d.slice(0, 4), when: d, kind: "中标", title: (ga(r, "winner") || "") + (ga(r, "winner_country") ? "(" + ga(r, "winner_country") + ")" : "") + " 中标「" + (ga(r, "name_zh") || ga(r, "name") || "") + "」," + cny(w),
        src: "已中标项目", url: ga(r, "url") || "", city: byCity ? { id: this.linkId(ga(r, "city")), name: this.text(ga(r, "city")) } : null, open: r.id, oid: "award" })));
      if (!items.length) { el.innerHTML = head + '<p class="empty">还没有记录。' + (prop === "city" ? "原文里写明了" + (many ? "片区里某座城市" : "这座城市") + "的大事才挂到城市上,其余在国别概况里。" : "") + "</p>"; return 0; }
      items.sort((a, b) => b.when.localeCompare(a.when));
      const KINDS = this.values("event_kind").concat(["中标"]).filter((k) => items.some((x) => x.kind === k));
      const st = { k: [] }, YEARS = 12;
      let all = false;
      el.innerHTML = head + '<div class="filters"><div class="frow"><b>类别</b><span class="frow ch-k"></span><span class="muted small ch-n push"></span></div></div><div class="panel ch-t"></div>';
      const $ = (c) => el.querySelector(c);
      const render = () => {
        this.chips($(".ch-k"), [["", "全部 " + items.length]].concat(KINDS.map((k) => [k, k + " " + items.filter((x) => x.kind === k).length])), st, "k", render, true);
        const list = items.filter((x) => !st.k.length || st.k.includes(x.kind));
        const years = [...new Set(list.map((x) => x.y))];
        const shown = all ? years : years.slice(0, YEARS);
        $(".ch-n").textContent = list.length + " 件 · " + (years.length > 1 ? years[years.length - 1] + "–" + years[0] + " 年" : years.length ? years[0] + " 年" : "");
        $(".ch-t").innerHTML = !list.length ? '<p class="empty">没有这一类的大事。</p>' : '<div class="chron">' + shown.map((y) => '<div class="cy"><div class="cyl">' + this.esc(y) + "</div><ul>" +
          list.filter((x) => x.y === y).map((x) => {
            const when = x.when.length > 7 ? '<span class="cw">' + this.esc(x.when.slice(5, 10)) + "</span>" : x.when.length > 4 ? '<span class="cw">' + Number(x.when.slice(5, 7)) + " 月</span>" : "";  // 只到月的写「12 月」
            const city = x.city && x.city.id ? ' <a class="muted small" href="city.html?id=' + encodeURIComponent(x.city.id) + '">' + this.esc(x.city.name) + "</a>" : "";
            const src = x.src ? '<span class="muted small cs">' + (x.url ? '<a href="' + this.esc(x.url) + '" target="_blank" rel="noopener">' + this.esc(x.src) + " ↗</a>" : this.esc(x.src)) + "</span>" : "";
            return '<li><span class="ck ck-' + (["项目批准", "项目完成", "机构改革", "规划与政策", "水价调整", "工程投运", "中标"].indexOf(x.kind) + 1) + '">' + this.esc(x.kind) + "</span>" + when +
              '<span class="ct">' + this.esc(x.title) + city + "</span>" + src + "</li>";
          }).join("") + "</ul></div>").join("") + "</div>" +
          (years.length > shown.length ? '<p><button type="button" class="chip-btn more ch-more">列出更早的 ' + (years.length - shown.length) + " 年(" + years[years.length - 1] + "–" + years[shown.length] + ")</button></p>" : "");
        const more = $(".ch-more"); if (more) more.onclick = () => { all = true; render(); };
      };
      render();
      return items.length;
    },
    /* 研究资料(本体 V7):国别概况「水务历史资料」页签里,综述展开在前,其余按类型筛选、按发布日期从新到旧列表。→ 份数 */
    async studies(el, rid) {
      const head = '<h2 id="studies">研究资料</h2><p class="muted small">世界银行等机构公开的项目评估、完成报告、环境社会评估与行业研究,点标题看原文;综述是本系统读这些资料后整理的要点(每个数字注明出处页码)。人工可在表格「研究资料」里补别的来源。</p>';
      if (!(this.cfg.tables && this.cfg.tables.study)) { el.innerHTML = head + '<p class="empty">「研究资料」表还没建(跑一次建表命令后这里就有)。</p>'; return 0; }
      el.innerHTML = head + '<div class="panel">读取中…</div>';
      const rows = await this.records("study", { filter: this.and(this.cond("study", "country", "is", rid)), props: ["name", "name_zh", "kind", "publisher", "published", "language", "project", "url", "summary", "note"] });
      const g = (r, p) => this.get("study", r, p);
      rows.sort((a, b) => String(this.day(g(b, "published")) || "").localeCompare(String(this.day(g(a, "published")) || "")));
      if (!rows.length) { el.innerHTML = head + '<p class="empty">这个国家还没有研究资料。</p>'; return 0; }
      const sums = rows.filter((r) => g(r, "kind") === "综述"), docs = rows.filter((r) => g(r, "kind") !== "综述");
      const KINDS = this.values("study_kind").filter((k) => k !== "综述" && docs.some((r) => (g(r, "kind") || "") === k));
      const st = { k: [] };
      el.innerHTML = head + sums.map((r) => '<details class="sec dg-wrap" open><summary>' + this.esc(g(r, "name_zh") || g(r, "name") || "综述") + ' <span class="muted small">' + this.esc(this.day(g(r, "published")) || "") +
        " · 点数字后面的小角标看出处页码</span> " + this.open("study", r.id, "↗") + '</summary><div class="panel">' + this.digestHtml(g(r, "summary"), r.id) + "</div></details>").join("") +
        (docs.length ? '<div class="filters"><div class="frow"><b>类型</b><span class="frow sd-k"></span></div></div><div class="panel sd-t"></div>' : "");
      if (!docs.length) return rows.length;
      const $ = (c) => el.querySelector(c);
      const render = () => {
        this.chips($(".sd-k"), [["", "全部 " + docs.length]].concat(KINDS.map((k) => [k, k + " " + docs.filter((r) => g(r, "kind") === k).length])), st, "k", render, true);
        const list = docs.filter((r) => !st.k.length || st.k.includes(g(r, "kind")));
        $(".sd-t").innerHTML = this.tableHtml(["发布", "标题", "类型", "相关项目", "机构", "语言", ""], list.map((r) => {
          const zh = g(r, "name_zh"), en = g(r, "name") || "", u = g(r, "url");
          const t = (s) => u ? '<a href="' + this.esc(u) + '" target="_blank" rel="noopener">' + this.esc(s) + " ↗</a>" : this.esc(s);
          return [this.raw('<span class="nw">' + this.esc(this.day(g(r, "published")) || "") + "</span>"), this.raw(zh ? "<b>" + t(zh) + '</b><br><span class="muted small">' + this.esc(en) + "</span>" : "<b>" + t(en) + "</b>"),
            g(r, "kind") || "", g(r, "project") || "", g(r, "publisher") || "", g(r, "language") || "", this.raw(this.open("study", r.id))];
        }));
      };
      render();
      return rows.length;
    },
    /* 法规(本体 v2.24):国别概况「机构与法规」页签下半部、助手的法规答案、法规全文页共用。
       排放限值按「法规 + 口径 + 排到哪里 + 适用范围」分块,口径不同的数不放在一起比;出处是带序号的小角标(.lref),
       点开看法规、条号、页码、条文中文与原文,以及到法规全文页那一条的链接。待核实(机器摘的、人还没对过原文)的标出。 */
    hasLaw() { return !!(this.cfg.tables && this.cfg.tables.law); },
    async lawData(rids) {  // 国别记录编号 → {laws, limits, terms, clause: {条文记录编号: {...}}};表没建 → null
      const T = this.cfg.tables || {};
      if (!T.law) return null;
      const on = (oid) => (rids && rids.length ? this.and(rids.length === 1 ? this.cond(oid, "country", "is", rids[0]) : this.cond(oid, "country", "isAnyOf", rids)) : undefined);
      const [laws, limits, terms] = await Promise.all([
        this.records("law", { filter: on("law"), props: ["key", "name", "name_orig", "kind", "number", "issued", "status", "version", "language", "country", "url", "source", "clause_count"] }),
        T.discharge_limit ? this.records("discharge_limit", { filter: on("discharge_limit"), props: ["key", "param", "param_orig", "value", "value_text", "unit", "basis", "receiving", "scope", "sampling", "country", "law", "clause", "verified", "note"] }) : [],
        T.local_term ? this.records("local_term", { filter: on("local_term"), props: ["key", "concept", "term_orig", "term_zh", "definition", "definition_orig", "relation", "difference", "country", "law", "clause", "verified"] }) : [],
      ]);
      const ids = [...new Set(limits.map((r) => this.linkId(this.get("discharge_limit", r, "clause"))).concat(terms.map((r) => this.linkId(this.get("local_term", r, "clause")))).filter(Boolean))];
      const clause = {}, lawOf = {}; laws.forEach((r) => { lawOf[r.id] = r; });
      this._lc = this._lc || {};
      const need = ids.filter((id) => !this._lc[id]);
      for (let i = 0; i < need.length; i += 40) {  // 条文按记录编号一条条取(引用的通常十几条),同时发
        const got = await Promise.all(need.slice(i, i + 40).map((id) => this.api("/table/" + this.t("law_clause").id + "/record/" + id, { fieldKeyType: "id" }).catch(() => null)));
        got.forEach((r) => { if (r && r.id) this._lc[r.id] = r; });
      }
      ids.forEach((id) => { const r = this._lc[id]; if (!r) return; const g = (p) => this.get("law_clause", r, p), lw = lawOf[this.linkId(g("law"))];
        clause[id] = { id, label: g("label") || "", page: g("page") || "", seq: g("seq"), text: g("text") || "", zh: g("text_zh") || "", law: lw ? this.get("law", lw, "name") : this.text(g("law")),
          lawKey: lw ? this.get("law", lw, "key") : "", url: lw ? this.get("law", lw, "url") : "" }; });
      return { laws, limits, terms, clause, lawOf };
    },
    /* 出处角标:同一份答案里按出现先后编号(nums 由调用方给一个空对象,整份答案共用) */
    lref(c, nums) {
      if (!c) return "";
      if (!nums[c.id]) nums[c.id] = Object.keys(nums).length + 1;
      (this._lref = this._lref || {})[c.id] = c;
      this.lrefOn();
      return '<sup class="sref lref"><a href="' + this.esc(this.lawUrl(c.lawKey, c.seq)) + '" data-c="' + this.esc(c.id) + '" title="' + this.esc(c.law + " " + c.label) + '">' + nums[c.id] + "</a></sup>";
    },
    lawUrl(key, seq) { return "law.html?" + new URLSearchParams({ k: key || "" }) + (seq ? "#c" + seq : ""); },
    lrefOn() {
      if (this._lrefOn) return; this._lrefOn = true;
      document.addEventListener("click", (e) => {
        const t = e.target, a = t.closest && t.closest(".lref a"), pop = document.querySelector(".sref-pop.lpop");
        if (pop && t.closest(".sref-pop.lpop")) { const tg = t.closest(".lp-tg"); if (tg) { e.preventDefault(); pop.querySelectorAll(".lp-tx").forEach((x) => x.classList.toggle("hide")); tg.textContent = tg.textContent === "看原文" ? "看中文" : "看原文"; } return; }
        if (pop) pop.remove();
        if (!a) return;
        e.preventDefault();
        const c = (this._lref || {})[a.dataset.c]; if (!c) return;
        const cut = (s) => (s.length > 700 ? s.slice(0, 700) + "……" : s);
        const el = document.createElement("div"); el.className = "sref-pop lpop";
        el.innerHTML = "<div><b>" + this.esc(c.law) + "</b> " + this.esc(c.label) + (c.page ? ' <span class="muted small">原文第 ' + this.esc(c.page) + " 页</span>" : "") + "</div>" +
          (c.zh ? '<div class="lp-tx small">' + this.esc(cut(c.zh)).replace(/\n/g, "<br>") + "</div>" : "") +
          '<div class="lp-tx small' + (c.zh ? " hide" : "") + '">' + this.esc(cut(c.text)).replace(/\n/g, "<br>") + (c.zh ? "" : '<br><span class="muted">(中文译文后台正在补)</span>') + "</div>" +
          '<div class="small"><a href="' + this.esc(this.lawUrl(c.lawKey, c.seq)) + '">看这一条和全文 →</a>' + (c.zh ? ' · <a href="#" class="lp-tg">看原文</a>' : "") +
          (c.url ? ' · <a href="' + this.esc(c.url + (c.page && /\.pdf/i.test(c.url) ? "#page=" + parseInt(c.page, 10) : "")) + '" target="_blank" rel="noopener">原文出处 ↗</a>' : "") + "</div>";
        document.body.appendChild(el);
        const r = a.getBoundingClientRect(), w = el.offsetWidth;
        el.style.left = Math.max(8, Math.min(r.left + window.scrollX - 12, window.scrollX + document.documentElement.clientWidth - w - 8)) + "px";
        el.style.top = (r.bottom + window.scrollY + 6) + "px";
      });
    },
    /* 一个限值写成一句:≤ 120 mg/L、≥ 70%(去除率)、6.5–8.5;原文照抄的(区间、带条件)用原文 */
    limitText(r) {
      const g = (p) => this.get("discharge_limit", r, p), u = g("unit") || "", vt = String(g("value_text") || "").trim(), v = g("value");
      const num = v === null || v === undefined || v === "" ? "" : String(Number(v)).replace(/\.0+$/, "");
      if (vt && !/^[\d.,]+$/.test(vt)) return vt + (u && !vt.includes(u) ? " " + u : "");
      const op = g("basis") === "去除率下限" ? "≥ " : g("param") === "pH" ? "" : "≤ ";
      return num ? op + num + (u ? " " + u : "") : vt;
    },
    /* 一国的排放限值 → html;nums 是整份答案共用的角标编号;params 只看这几个指标(空 = 全部) */
    lawLimitsHtml(d, rid, nums, params) {
      const g = (r, p) => this.get("discharge_limit", r, p), order = this.values("effluent_param");
      let rows = d.limits.filter((r) => !rid || this.linkId(g(r, "country")) === rid);
      const want = (params || []).length ? rows.filter((r) => params.includes(g(r, "param"))) : rows;
      if (want.length) rows = want;
      if (!rows.length) return "";
      const blocks = {}, groups = {};  // 同一部法规同一口径、同一去处:适用范围有三种以上(如河湖按用途分级)的排成一张对照表,否则按适用范围分开列
      rows.forEach((r) => { const k = [this.linkId(g(r, "law")), g(r, "basis") || "", g(r, "receiving") || ""].join("|"); (groups[k] = groups[k] || []).push(r); });
      Object.entries(groups).forEach(([k, rs]) => {
        const scopes = [...new Set(rs.map((r) => g(r, "scope") || ""))];
        if (scopes.length >= 3) blocks[k + "|*"] = rs; else rs.forEach((r) => { const kk = k + "|" + (g(r, "scope") || ""); (blocks[kk] = blocks[kk] || []).push(r); });
      });
      const pend = rows.filter((r) => g(r, "verified") !== "已核实").length;
      const src = (r) => d.clause[this.linkId(g(r, "clause"))] ? '<span class="nw">' + this.esc(d.clause[this.linkId(g(r, "clause"))].label) + "</span>" + this.lref(d.clause[this.linkId(g(r, "clause"))], nums) : '<span class="muted small">' + this.esc(this.text(g(r, "clause")) || "") + "</span>";
      const chk = (rs) => rs.every((r) => g(r, "verified") === "已核实") ? '<span class="tag t-ok nw">已核实</span>' : '<span class="tag t-warn nw" title="机器从条文摘出,人还没对过原文">待核实</span>';
      const pname = (r) => g(r, "param") === "其他" && g(r, "param_orig") ? "<b>" + this.esc(g(r, "param_orig")) + "</b>" : "<b>" + this.esc(g(r, "param")) + "</b>" + (g(r, "param_orig") && g(r, "param_orig") !== g(r, "param") ? ' <span class="muted small">' + this.esc(g(r, "param_orig")) + "</span>" : "");
      return Object.entries(blocks).map(([k, rs]) => {
        const [lid, basis, recv, scope] = k.split("|"), lw = d.lawOf[lid], samp = [...new Set(rs.map((r) => g(r, "sampling")).filter(Boolean))];
        rs.sort((a, b) => order.indexOf(g(a, "param")) - order.indexOf(g(b, "param")));
        const head = '<div class="lw-bh"><b>' + this.esc(lw ? this.get("law", lw, "name") : "(法规)") + "</b>" + (lw ? ' <a class="small" href="' + this.esc(this.lawUrl(this.get("law", lw, "key"))) + '">全文 →</a>' : "") +
          '<div class="lw-tags">' + [["口径", basis], ["排到", recv], ["适用", scope === "*" ? "" : scope], ["取样", samp.join(";")]].filter((x) => x[1]).map(([a, b]) => '<span class="tag' + (a === "口径" && basis === "受纳水体允许浓度" ? " t-warn" : "") + '">' + a + ":" + this.esc(b) + "</span>").join("") + "</div></div>";
        if (scope === "*") {
          const scopes = [...new Set(rs.map((r) => g(r, "scope") || ""))], byP = {};
          rs.forEach((r) => { const p = g(r, "param") + "|" + (g(r, "param_orig") || ""); (byP[p] = byP[p] || []).push(r); });
          return head + this.tableHtml(["指标", ...scopes.map((x) => x || "(未注明)"), "出处", ""], Object.values(byP).map((ps) => [
            this.raw(pname(ps[0])),
            ...scopes.map((sc) => { const r = ps.find((x) => (g(x, "scope") || "") === sc); return r ? this.raw('<b class="nw">' + this.esc(this.limitText(r)) + "</b>") : this.raw('<span class="muted">不规定</span>'); }),
            this.raw([...new Set(ps.map(src))].join(" ")), this.raw(chk(ps))]));
        }
        return head + this.tableHtml(["指标", "限值", "出处", ""], rs.map((r) => [
          this.raw(pname(r)),
          this.raw('<b class="nw">' + this.esc(this.limitText(r)) + "</b>" + (g(r, "note") ? '<br><span class="muted small">' + this.esc(g(r, "note")) + "</span>" : "")),
          this.raw(src(r)), this.raw(chk([r]))]));
      }).join("") + (pend ? '<p class="muted small">标「待核实」的 ' + pend + " 项是从条文摘出、还没人对过原文的;在表格「排放限值」里对过后改成「已核实」。</p>" : "");
    },
    /* 一国的本国定义 → html(concepts 只看这几个通用概念,空 = 全部) */
    lawTermsHtml(d, rid, nums, concepts) {
      const g = (r, p) => this.get("local_term", r, p);
      let rows = d.terms.filter((r) => !rid || this.linkId(g(r, "country")) === rid);
      const want = (concepts || []).length ? rows.filter((r) => concepts.includes(g(r, "concept"))) : rows;
      if ((concepts || []).length) rows = want;
      if (!rows.length) return "";
      const REL = { "相同": "", "更窄": " t-warn", "更宽": " t-warn", "部分重叠": " t-warn", "没有对应": " t-bad" };
      return '<div class="lw-terms">' + rows.map((r) => {
        const c = d.clause[this.linkId(g(r, "clause"))];
        return '<div class="lw-term"><div><b>' + this.esc(g(r, "concept")) + "</b>" + (g(r, "term_zh") || g(r, "term_orig") ? " → 本国说法「" + this.esc(g(r, "term_zh") || "") + "」" +
          (g(r, "term_orig") ? ' <span class="muted small">' + this.esc(g(r, "term_orig")) + "</span>" : "") : "") +
          (g(r, "relation") ? ' <span class="tag' + (REL[g(r, "relation")] || "") + '">和通用说法:' + this.esc(g(r, "relation")) + "</span>" : "") +
          (g(r, "verified") === "已核实" ? "" : ' <span class="tag t-warn" title="机器从条文摘出,人还没对过原文">待核实</span>') + "</div>" +
          "<div>" + this.esc(g(r, "definition") || "") + (c ? this.lref(c, nums) : "") + "</div>" +
          (g(r, "difference") ? '<div class="muted small">差别:' + this.esc(g(r, "difference")) + "</div>" : "") + "</div>";
      }).join("") + "</div>";
    },
    /* 一国的法规清单 → html(按类别;名称点进全文页) */
    lawListHtml(d, rid) {
      const g = (r, p) => this.get("law", r, p), rows = d.laws.filter((r) => !rid || this.linkId(g(r, "country")) === rid);
      if (!rows.length) return "";
      const order = this.values("law_kind");
      rows.sort((a, b) => (order.indexOf(g(a, "kind")) - order.indexOf(g(b, "kind"))) || String(this.day(g(b, "issued")) || "").localeCompare(String(this.day(g(a, "issued")) || "")));
      return this.tableHtml(["类别", "法规", "文号与颁布", "现行状态", "条文", ""], rows.map((r) => [g(r, "kind") || "",
        this.raw('<a href="' + this.esc(this.lawUrl(g(r, "key"))) + '"><b>' + this.esc(g(r, "name") || "") + "</b></a>" + (g(r, "name_orig") ? '<br><span class="muted small">' + this.esc(g(r, "name_orig")) + "</span>" : "") +
          (g(r, "version") ? '<br><span class="muted small">收的版本:' + this.esc(g(r, "version")) + "</span>" : "")),
        this.raw(this.esc(g(r, "number") || "") + (g(r, "issued") ? '<br><span class="muted small">' + this.esc(this.day(g(r, "issued"))) + "</span>" : "")),
        this.raw(g(r, "status") === "已废止" ? '<span class="num-bad">已废止</span>' : this.esc(g(r, "status") || "")),
        this.raw((Number(g(r, "clause_count")) ? '<span class="nw">' + Number(g(r, "clause_count")) + " 条</span>" : '<span class="muted small nw">原文未取到</span>') + (g(r, "language") ? '<br><span class="muted small">' + this.esc(g(r, "language")) + "</span>" : "")),
        this.raw((g(r, "url") ? '<a class="small" href="' + this.esc(g(r, "url")) + '" target="_blank" rel="noopener">原文 ↗</a> ' : "") + this.open("law", r.id, "↗"))]));
    },
    /* 国别概况「机构与法规」页签下半部:排放标准、本国定义、法规原文;→ 收了几部法规 */
    async lawSection(el, rid, name) {
      if (!this.hasLaw()) { el.innerHTML = ""; return 0; }
      el.innerHTML = '<p class="muted">读取法规…</p>';
      const d = await this.lawData([rid]), nums = {};
      const lim = this.lawLimitsHtml(d, rid, nums), terms = this.lawTermsHtml(d, rid, nums), list = this.lawListHtml(d, rid);
      const sec = (label, html, open, note) => '<details class="sec"' + (open ? " open" : "") + "><summary>" + label + '</summary><div class="panel">' + (note ? '<p class="muted small">' + note + "</p>" : "") + html + "</div></details>";
      el.innerHTML = !d.laws.length ? sec("法规原文", '<p class="empty">还没收' + this.esc(name) + "的法规原文:上面的法规简表只有名称和链接。要收的话把原文网址告诉管理员加进法规清单。</p>", true) :
        sec("排放标准", lim || '<p class="muted">收了' + this.esc(name) + "的 " + d.laws.length + " 部法规原文,还没摘出排放限值(可能这几部法规里没有,或在别的部令、技术规范里)。</p>", true,
          "从法规原文摘出的限值;同一国不同口径(出水浓度上限、去除率、受纳水体允许浓度)分开列,不能直接比。角标点开看条文。") +
        sec("本国定义", terms || '<p class="empty">还没摘出本国定义。</p>', true, "通用概念(「行业术语」表里的词)在这国法规里怎么定义,和通用说法差在哪;助手回答这国的问题时按这里的定义。") +
        sec("法规原文", list, true, "点名称看全文(条文中文与原文对照);中文由后台逐批译,没译到的先显示原文。");
      return d.laws.length;
    },
    /* 平铺筛选(一组按钮,点亮一个;multi 时可多选,一个没选就点亮「全部」(值为空的那个)) */
    chips(el, opts, state, key, onChange, multi) {
      const cur = state[key];
      el.innerHTML = opts.map(([v, label]) => { const on = multi ? (v === "" ? !(cur || []).length : (cur || []).includes(v)) : (cur || "") === String(v); return '<button type="button" class="chip-btn' + (on ? " on" : "") + '" data-v="' + this.esc(v) + '">' + this.esc(label) + "</button>"; }).join("");
      el.querySelectorAll(".chip-btn").forEach((b) => { b.onclick = () => {
        const v = b.dataset.v;
        if (multi) { const arr = new Set(state[key] || []); if (v === "") arr.clear(); else if (arr.has(v)) arr.delete(v); else arr.add(v); state[key] = [...arr]; }
        else state[key] = v;
        this.chips(el, opts, state, key, onChange, multi); onChange();
      }; });
    },
    /* 出处角标:正文里的 [8位线索号] → 带序号的小角标,点开是那条线索;〔R012〕是国别成稿的条目号,只提示不跳转 */
    async leadsByKeys(keys) {
      const out = {}, want = [...new Set(keys)].filter((k) => !this._leadCache || !(k in this._leadCache));
      this._leadCache = this._leadCache || {};
      for (let i = 0; i < want.length; i += 40) {
        const part = want.slice(i, i + 40);
        const rows = await this.records("lead", { filter: this.or(...part.map((k) => this.cond("lead", "key", "is", k))), props: ["key", "title", "url"] });
        rows.forEach((r) => { this._leadCache[this.get("lead", r, "key")] = { id: r.id, title: this.get("lead", r, "title") || "", url: this.get("lead", r, "url") || "" }; });
        part.forEach((k) => { if (!(k in this._leadCache)) this._leadCache[k] = null; });
      }
      keys.forEach((k) => { out[k] = this._leadCache[k]; });
      return out;
    },
    citeKeys(texts) { const ks = []; texts.forEach((t) => { String(t || "").replace(/\[([0-9a-f]{8})\]/g, (m, k) => { ks.push(k); return m; }); }); return [...new Set(ks)]; },
    cites(txt, numOf, info) {
      numOf = numOf || {}; info = info || {};
      return this.esc(txt).replace(/\s*\[([0-9a-f]{8})\]/g, (m, k) => {
        const n = numOf[k] || "?", l = info[k];
        const tip = l ? "出处线索:" + l.title : "出处线索 " + k + "(表里没找到)";
        return l ? '<a class="cite" href="' + this.recordUrl("lead", l.id) + '" target="_blank" rel="noopener" title="' + this.esc(tip) + '">' + n + "</a>" : '<sup class="cite" title="' + this.esc(tip) + '">' + n + "</sup>";
      }).replace(/\s*〔(R\d+)〕/g, '<sup class="cite" title="引自国别成稿的第 $1 条">$1</sup>');
    },
    citeNote(numOf, info) {
      const ks = Object.keys(numOf); if (!ks.length) return "";
      return '<p class="muted small">角标是出处线索(点开看原文):' + ks.sort((a, b) => numOf[a] - numOf[b]).map((k) => { const l = info[k]; return (l ? '<a href="' + this.recordUrl("lead", l.id) + '" target="_blank" rel="noopener">' : "<span>") + numOf[k] + " " + this.esc((l && l.title ? l.title : k).slice(0, 40)) + (l ? "</a>" : "</span>"); }).join(" · ") + "</p>";
    },
    /* 我方阶段的全部值与顺序(本体 taxonomies.stage;「观望」是跟进的判断,不是阶段);商机总览、国别页的排序都按它 */
    get STAGES() { return this.values("stage"); },
    /* 资金来源归组(本体 groupings.funders,运行时读):出资方文本包含任一样式即归该组,大小写不分 */
    get FUNDERS() { if (!this._funders) { this._funders = {}; this.grouping("funders").groups.forEach((g) => { this._funders[g.v] = g.patterns.map(String); }); } return this._funders; },
    get FUNDER_OTHER() { return this.grouping("funders").other; },
    get FUNDER_EMPTY() { return this.grouping("funders").empty; },
    funderOptions() { return Object.keys(this.FUNDERS).concat([this.FUNDER_OTHER, this.FUNDER_EMPTY]); },
    /* 某组 → 线索表「出资方」的条件(嵌套在 and 里的一组) */
    funderCond(oid, prop, group) {
      if (!group) return null;
      if (group === this.FUNDER_EMPTY) return this.cond(oid, prop, "isEmpty");
      const all = Object.values(this.FUNDERS).flat();
      if (group === this.FUNDER_OTHER) return this.and(this.cond(oid, prop, "isNotEmpty"), ...all.map((x) => this.cond(oid, prop, "doesNotContain", x)));
      return this.or(...(this.FUNDERS[group] || []).map((x) => this.cond(oid, prop, "contains", x)));
    },
    /* 一段文字里的出资方属于哪些组(商机「资金」每行开头的出资方;页面内筛选用) */
    funderGroups(text) {
      const t = String(text || ""); if (!t.trim()) return [this.FUNDER_EMPTY];
      const heads = t.split(/\r?\n/).map((l) => l.replace(/^\s*\[[0-9a-f]{8}\]\s*/, "").split(" · ")[0].toLowerCase()).filter(Boolean);
      const out = new Set();
      heads.forEach((h) => { let hit = false; Object.entries(this.FUNDERS).forEach(([g, ps]) => { if (ps.some((x) => h.includes(x.toLowerCase()))) { out.add(g); hit = true; } }); if (!hit) out.add(this.FUNDER_OTHER); });
      return [...out];
    },
    /* 区域(使用方的经营区域,国别表「经营区域」):国别 → 区域名;没填的归「未划区」 */
    get REGION_EMPTY() { return this.grouping("regions").empty; },
    regionOf(c) { return String(this.get("country", c, "market") || "").trim() || this.REGION_EMPTY; },
    oppUrl(rid) { return "opp.html?id=" + encodeURIComponent(rid); },
    oppLink(o) { return '<a href="' + this.oppUrl(o.id) + '">' + this.esc((this.get("opportunity", o, "code") || "") + " " + (this.get("opportunity", o, "name_zh") || "")) + "</a>"; },
    toast(msg, bad) { let el = document.getElementById("toast"); if (!el) { el = document.createElement("div"); el.id = "toast"; document.body.appendChild(el); } el.textContent = msg; el.className = bad ? "bad show" : "show"; clearTimeout(this._tt); this._tt = setTimeout(() => { el.className = ""; }, bad ? 6000 : 2500); },
    /* 跟踪中的商机 = 我方阶段 ∈ 跟踪中、准备投标、已投标、中标(本体 glossary.tracked):线索总览的卡片与漏斗、日历、商机总览都用这一个口径 */
    get TRACKED() { return this.glossary("tracked").stages; },
    tracked(opps) { const t = this.TRACKED; return opps.filter((o) => t.includes(this.get("opportunity", o, "stage")) && !this.isArchived(o)); },
    /* 已立项的商机(glossary.decided):人定过跟不跟的;AI 推荐、不跟进的留在「线索处理」那一步。两个口径都不含已归档的(glossary.archived_opp) */
    get DECIDED() { return this.glossary("decided").stages; },
    decided(opps) { const t = this.DECIDED; return opps.filter((o) => t.includes(this.get("opportunity", o, "stage")) && !this.isArchived(o)); },
    /* 已归档的商机(本体 opportunity.archived,A50 放弃跟踪):商机跟进各页、首页与助手默认不算,说明行「已归档 N 个商机 · 显示」(网址 ar=1)点开才算;
       商机工作台点开仍看全部。表里还没这个字段(没跑建表)时谁都不算已归档、不显示说明行。读商机时 props 带上 OPP_ARC 才认得出来 */
    OPP_ARC: ["archived", "archive_reason", "archived_at"],
    isArchived(o) { return this.get("opportunity", o, "archived") === true; },
    archivedOpps(opps) { return opps.filter((o) => this.isArchived(o)); },
    oppArcNote(el, n, shown, toggle) {
      if (!el) return;
      if (!n || !this.has("opportunity", "archived")) { el.innerHTML = ""; return; }
      el.innerHTML = (shown ? "已显示归档(放弃跟踪)的 " + n + " 个商机。" : "已归档(放弃跟踪)的 " + n + " 个商机没算进来。") +
        ' <button type="button" class="btn quiet">' + (shown ? "不显示" : "显示") + "</button>";
      el.querySelector("button").onclick = toggle;
    },
    /* 线索 → 商机(线索处理、线索总览共用一套):归入已有商机,线索记为「已处理」;或用这条线索新建商机(编号 国别ISO-序号,阶段取「跟踪中的商机」口径的第一档即跟踪中、负责人是登录人、出处挂这条线索)再归入 */
    nextOppCode(opps, iso) { let m = 0; opps.forEach((o) => { const x = String(this.get("opportunity", o, "code") || "").match(new RegExp("^" + iso + "-(\\d+)$")); if (x) m = Math.max(m, Number(x[1])); }); return iso + "-" + String(m + 1).padStart(3, "0"); },
    leadToOpp(leadId, oppId) { return this.patch("lead", leadId, { opportunity: oppId, status: "已处理" }); },
    async newOppFromLead(leadId, { name, cid, iso, opps }) {
      const me = await this.me();
      const rec = await this.create("opportunity", { code: this.nextOppCode(opps, iso), name_zh: name, country: cid, stage: this.TRACKED[0], owner: me && me.id, sources: [leadId] });
      if (!rec) throw new Error("新建商机没成功");
      await this.leadToOpp(leadId, rec.id);
      return rec;
    },
    async opps() {
      return this.records("opportunity", { props: ["code", "name_zh", "stage", "country", "owner", "next_milestone", "public_stage", "priority", "next_step", "next_due", "last_followup", "lead_count"].concat(this.OPP_ARC) });
    },
    /* 线索的中文名称(与 core/zh_name.py 同一规则,词表共用 zh_terms.json):项目名 → 中文摘要首行 → 从原题挑认得的词拼成「要点」→ 都没有为空。
       要点不是翻译,只说明这条线索讲什么;原题照样给出。 */
    LEDGER_PREFIX: /^台账出处\s*[(（][^)）]*[)）]\s*[::]\s*/,
    fold(s) { return String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[’‘ʼ]/g, "'"); },
    async zhTerms() {
      if (this._zh) return this._zh;
      let raw = { terms: {}, max: 6 };
      try { raw = await (await fetch("zh_terms.json")).json(); } catch (e) { console.warn("zh_terms.json 读不到", e); }
      const esc = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const list = Object.entries(raw.terms || {}).sort((a, b) => b[0].length - a[0].length).map(([t, zh]) => {
        if (/^[A-Z&0-9][A-Z&0-9-]{1,5}$/.test(t)) return { re: new RegExp("(?<!\\p{L})" + esc(t) + "(?!\\p{L})", "gu"), zh, cased: true };
        return { re: new RegExp("(?<!\\p{L})" + esc(this.fold(t)) + (t.length <= 4 ? "(?!\\p{L})" : ""), "giu"), zh, cased: false };
      });
      this._zh = { list, max: Number(raw.max) || 6 };
      return this._zh;
    },
    keyPoints(title) {
      if (!this._zh) return "";
      const s = String(title || "").trim().split(/\r?\n/)[0].replace(this.LEDGER_PREFIX, ""), folded = this.fold(s), covered = [], hits = [];
      for (const { re, zh, cased } of this._zh.list) {
        re.lastIndex = 0; let m;
        while ((m = re.exec(cased ? s : folded))) {
          const a = m.index, b = a + m[0].length;
          if (!covered.some(([x, y]) => x < b && a < y)) { covered.push([a, b]); hits.push([a, zh]); }
          if (!m[0].length) re.lastIndex++;
        }
      }
      const out = []; hits.sort((x, y) => x[0] - y[0]).forEach(([, zh]) => { if (!out.includes(zh)) out.push(zh); });
      return out.slice(0, this._zh.max).join("、");
    },
    /* 清单取名(使用方 2026-10-28「标题逻辑很混乱」):先用「中文标题」(原题的中文直译,A03 写、老线索后台补译),没有时才退到项目名 / 摘要 / 要点 */
    LEAD_TITLE_PROPS: ["key", "title", "url", "project_name", "summary_zh"],
    leadTitleProps() { return this.LEAD_TITLE_PROPS.concat(this.has("lead", "title_zh") ? ["title_zh"] : []); },
    zhName(l, n) {
      n = n || 60;
      const first = (v) => String(v || "").trim().split(/\r?\n/)[0].slice(0, n);
      if (this.has("lead", "title_zh") && first(this.get("lead", l, "title_zh"))) return { name: first(this.get("lead", l, "title_zh")), how: "译名" };
      if (first(this.get("lead", l, "project_name"))) return { name: first(this.get("lead", l, "project_name")), how: "项目名" };
      if (first(this.get("lead", l, "summary_zh"))) return { name: first(this.get("lead", l, "summary_zh")), how: "摘要" };
      const kp = this.keyPoints(this.get("lead", l, "title"));
      return kp ? { name: kp.slice(0, n), how: "要点" } : { name: "", how: "" };
    },
    /* 清单里的标题格:中文名称为主(要点加灰标),原题与线索号小字 */
    titleCell(l, extra) {
      const z = this.zhName(l), url = this.get("lead", l, "url"), title = String(this.get("lead", l, "title") || "").split("\n")[0].slice(0, 90);
      const main = z.name ? this.esc(z.name) + (z.how === "要点" ? ' <span class="tag" title="没有中文摘要,这是从原题挑出认得的词;配好模型键后会有真正的中文摘要">要点</span>' : "") : this.esc(title);
      const link = (h) => url ? '<a href="' + this.esc(url) + '" target="_blank" rel="noopener">' + h + "</a>" : "<span>" + h + "</span>";
      return this.raw(this.star("lead", l) + link(main) + '<br><span class="muted small">' + (z.name && title && title !== z.name ? this.esc(title) + " · " : "") + this.esc(this.get("lead", l, "key") || "") + (extra ? " · " + this.esc(extra) : "") + "</span>");
    },
    /* 清单里的「截止」格(本体 lead.deadline):招标等要动作的线索才有;过了标红「已过」;表里还没这一列时退到「最近截止」(含 TODAY 的汇总,过了就空) */
    deadlineProps() { return this.has("lead", "deadline") ? ["deadline"] : this.has("lead", "nearest_deadline") ? ["nearest_deadline"] : []; },
    deadlineCell(l) {
      const d = this.day(this.has("lead", "deadline") ? this.get("lead", l, "deadline") : this.get("lead", l, "nearest_deadline"));
      if (!d) return "";
      return d < this.today() ? this.raw('<span class="nw">' + this.tagHtml("已过 " + d, "bad", "截止日期已过") + "</span>") : this.raw('<b class="nw">' + this.esc(d) + "</b>");
    },
    /* 「转为商机」(线索总览、线索分析共用;线索处理另有自己的卡片):没归入的线索、能写的帐号出按钮,展开后可新建商机并归入,或归入已有商机;已归入的显示商机名,点开商机页。
       ctx = { opps, countries, canWrite };bindConvert(container, rows, ctx, col) 给容器里的按钮挂事件,col 是「所属商机」这一列的序号 */
    oppName(o) { return (this.get("opportunity", o, "code") || "") + " " + (this.get("opportunity", o, "name_zh") || ""); },
    convertCell(l, ctx) {
      const oid = this.linkId(this.get("lead", l, "opportunity"));
      if (oid) { const o = (ctx.opps || []).find((x) => x.id === oid); return this.raw('<a href="' + this.oppUrl(oid) + '">' + this.esc(o ? this.oppName(o) : this.text(this.get("lead", l, "opportunity"))) + "</a>"); }
      return this.raw(ctx.canWrite ? '<button type="button" class="conv" data-l="' + this.esc(l.id) + '">转为商机</button>' : "");
    },
    bindConvert(box, rows, ctx, col) {
      const byId = {}; rows.forEach((l) => { byId[l.id] = l; });
      box.querySelectorAll("button.conv").forEach((b) => { b.onclick = () => this.convertRow(b, byId[b.dataset.l], ctx, col); });
    },
    convertRow(btn, l, ctx, col) {
      const P = this, tr = btn.closest("tr"), open = tr.nextElementSibling;
      if (open && open.classList.contains("convrow")) { open.remove(); return; }
      const cid = P.linkId(P.get("lead", l, "country")), c = (ctx.countries || []).find((x) => x.id === cid), iso = c && P.get("country", c, "key");
      const live = (ctx.opps || []).filter((o) => (P.get("opportunity", o, "stage") || P.stageOf("suggested")) !== P.stageOf("dropped"));
      const same = live.filter((o) => P.linkId(P.get("opportunity", o, "country")) === cid), others = live.filter((o) => P.linkId(P.get("opportunity", o, "country")) !== cid);
      const opts = (arr) => arr.map((o) => '<option value="' + o.id + '">' + P.esc(P.oppName(o)) + "</option>").join("");
      const row = document.createElement("tr"); row.className = "convrow";
      row.innerHTML = '<td colspan="' + tr.children.length + '"><div class="convbox"><button type="button" class="primary" data-a="new">新建商机并归入</button>' +
        '<select data-a="pick"><option value="">归入已有商机…</option>' + (same.length ? '<optgroup label="' + P.esc(c ? P.get("country", c, "name_zh") || iso : "同国") + '">' + opts(same) + "</optgroup>" : "") + (others.length ? '<optgroup label="其他国家">' + opts(others) + "</optgroup>" : "") + "</select>" +
        '<button type="button" data-a="x">取消</button></div></td>';
      tr.after(row);
      const done = (o, msg) => { tr.children[col].innerHTML = '<a href="' + P.oppUrl(o.id) + '">' + P.esc(P.oppName(o)) + "</a>"; row.remove(); P.toast(msg); };
      const busy = (on) => row.querySelectorAll("button, select").forEach((e) => { e.disabled = on; });
      row.querySelector('[data-a="x"]').onclick = () => row.remove();
      row.querySelector('[data-a="pick"]').onchange = async (e) => {
        const o = ctx.opps.find((x) => x.id === e.target.value); if (!o) return;
        busy(true);
        try { await P.leadToOpp(l.id, o.id); done(o, "已归入 " + (P.get("opportunity", o, "code") || "")); } catch (err) { P.toast(err.message, true); busy(false); }
      };
      row.querySelector('[data-a="new"]').onclick = async () => {
        if (!iso) { P.toast("这条线索没有国别,先在表格里填国别", true); return; }
        const name = prompt("新商机的名称(可改):", String(P.get("lead", l, "project_name") || P.zhName(l).name || P.get("lead", l, "title") || "").split("\n")[0]);
        if (name === null || !name.trim()) return;
        busy(true);
        try { const rec = await P.newOppFromLead(l.id, { name: name.trim(), cid, iso, opps: ctx.opps }); ctx.opps.push(rec); done(rec, "已新建商机 " + (P.get("opportunity", rec, "code") || "") + " 并归入,阶段跟踪中、负责人是你"); }
        catch (err) { P.toast(err.message, true); busy(false); }
      };
    },
    /* 清单全屏(使用方 2026-10-28):面板右上角「全屏」,点了面板铺满窗口、再点或按 Esc 退出;标题跟着进去 */
    fullscreenable(panel, titleEl) {
      if (!panel || panel.querySelector(".fs-bar")) return;
      const bar = document.createElement("div"); bar.className = "fs-bar";
      bar.innerHTML = '<span class="fs-title"></span><button type="button" class="btn quiet fs-btn" title="铺满整个窗口">全屏</button>';
      panel.prepend(bar);
      const btn = bar.querySelector(".fs-btn"), ttl = bar.querySelector(".fs-title");
      const set = (on) => { panel.classList.toggle("fs", on); document.body.classList.toggle("has-fs", on); btn.textContent = on ? "退出全屏" : "全屏"; ttl.textContent = on && titleEl ? titleEl.textContent : ""; };
      btn.onclick = () => set(!panel.classList.contains("fs"));
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && panel.classList.contains("fs")) set(false); });
      panel._fsRefresh = () => { if (panel.classList.contains("fs") && titleEl) ttl.textContent = titleEl.textContent; };
    },
    /* 中文按拼音排(国别下拉等);拼音首字母用于下拉里的 A、B、C 分组 */
    zhCmp(a, b) {
      const C = P._col || (P._col = (() => { try { return new Intl.Collator("zh-CN-u-co-pinyin"); } catch (e) { return { compare: (x, y) => (x < y ? -1 : x > y ? 1 : 0) }; } })());
      return C.compare(String(a || ""), String(b || ""));
    },
    pyInitial(s) {
      const c = String(s || "").trim().charAt(0), B = "阿八嚓哒妸发旮哈讥咔垃妈拏噢妑七呥仨他屲夕丫帀", L = "ABCDEFGHJKLMNOPQRSTWXYZ";
      if (/[a-z]/i.test(c)) return c.toUpperCase();
      if (!/[\u3400-\u9fff]/.test(c)) return "#";
      let k = -1; for (let i = 0; i < B.length; i++) if (this.zhCmp(c, B[i]) >= 0) k = i;
      return k < 0 ? "#" : L[k];
    },
    /* 往 <select> 里填国别:按中文名拼音排,按首字母分组。items = [[value, 名称, 显示文字?], ...] */
    /* 关注的国别放在最上面一组「★ 关注」;关注还没读到时先按拼音填,读到后补一次 */
    fillCountrySelect(sel, items, noFollow) {
      const fill = () => {
        const keep = sel.value;
        sel.querySelectorAll("optgroup").forEach((g) => g.remove());
        const list = items.filter((x) => x[0]).slice().sort((a, b) => this.zhCmp(a[1], b[1]));
        const fav = (v, name) => !noFollow && (this.follow.ids.has(v) || this.follow.names.has(name));
        const add = (label, xs) => { if (!xs.length) return; const grp = document.createElement("optgroup"); grp.label = label; sel.appendChild(grp);
          xs.forEach(([v, name, text]) => { const op = document.createElement("option"); op.value = v; op.textContent = text || name; grp.appendChild(op); }); };
        add("★ 关注", list.filter(([v, name]) => fav(v, name)));
        let cur = "", run = [];
        list.filter(([v, name]) => !fav(v, name)).forEach((x) => { const k = this.pyInitial(x[1]); if (k !== cur) { add(cur, run); cur = k; run = []; } run.push(x); });
        add(cur, run);
        if (keep) sel.value = keep;
      };
      fill();
      if (!noFollow && !this.follow.ids.size && this._followP) this._followP.then((f) => { if (f.ids.size) fill(); });
    },
    async countries() {
      const rows = await this.records("country", { props: ["name_zh", "key", "tier", "lead_count", "opp_count", "region", "market"] });
      rows.sort((a, b) => (Number(this.get("country", b, "lead_count")) || 0) - (Number(this.get("country", a, "lead_count")) || 0));
      return rows;
    },
  };
  window.P = P;
})();
