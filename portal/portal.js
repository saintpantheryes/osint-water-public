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
      /* 角色那一问(GET /api/base/{库})要库编号:上次记下的编号先拿来,跟配置、本体同时发,省掉一整轮来回;编号对不上再按配置重问 */
      let bid = ""; try { bid = localStorage.getItem("ow.base") || ""; } catch (e) { /* 隐私模式读不到就照旧 */ }
      const early = bid ? this._baseFetch(bid) : null;
      const [r, o] = await Promise.all([fetch("config.json", { cache: "no-store" }), fetch("ontology.json")]);
      if (!r.ok) throw new Error("读不到 config.json(" + r.status + ")");
      if (!o.ok) throw new Error("读不到 ontology.json(" + o.status + ")");
      this.cfg = await r.json();
      this.onto = await o.json();  /* 本体(docs/ontology/ontology.yaml 转的 JSON):阶段、口径、归组都从这里取,不在脚本里抄 */
      this.tz = this.cfg.timezone || TZ;
      const id = this.cfg.base && this.cfg.base.id;
      if (id) { try { localStorage.setItem("ow.base", id); } catch (e) { /* 记不下也不碍事 */ } }
      if (early && id === bid) this._baseEarly = early;
      await this.role();  /* 登录人在库里的角色:不在库里的(游客)之后所有读表请求改走后台的只读通道 */
      this.followed();  /* 关注的国别:先发出去不等,清单与国别下拉用到时再等(P.followed) */
      if (this._navEarly) this.navFill();  /* 顶栏在读配置之前就先画好了:这时补上「后台数据表格」与帐号区 */
      return this.cfg;
    },
    /* 游客模式:登录人不是库的协作者(GET /api/base/{库} 是 403 / 404)。读表走 api/assistant/guest/*(后台任务进程里的 core/guest.py,
       只放行本体 guest 节里的对象与属性,报告只给定期报告的公开版);不放行的表(跟进记录、成员、报告模板)页面上直接当空表,不发请求;
       写一律不发;「打开表格」「在表格里打开」不显示。库里的查看者、评论者仍直接读表格(他们在表格里本来就看得到)。 */
    get isGuest() { return this._role === "none"; },
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
          throw new Error("游客看不到这部分内容");
        }
        const g2 = await fetch("api/assistant/guest" + path + (qs.toString() ? "?" + qs : ""), { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (g2.status === 401) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
        if (g2.status === 403 || g2.status === 404) throw new Error("游客看不到这部分内容");
        if (!g2.ok) throw new Error(path + " → " + g2.status + " " + (await g2.text()).slice(0, 200));
        return g2.json();
      }
      const r = await fetch("/api" + path + (qs.toString() ? "?" + qs : ""), { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (!r.ok) throw new Error(path + " → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    /* 写:以登录人的身份;关联、人员字段按 config 里的形状(one = {id},many = [{id}]) */
    async me() { if (!this._me) this._me = await this.api("/auth/user"); return this._me || {}; },
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
      const el = document.createElement("div"); el.className = "warn guest-note";
      el.textContent = note || (this.isGuest ? "当前是游客帐号:页面上的线索、商机、国别、时间节点和报告都是编出来的示例数据,用来演示系统怎么用,不是真实内容;不能登记、申请或订阅。"
        : "当前帐号只有查看权限(游客演示帐号):可以浏览全部页面,登记跟进、申请报告、归入和改负责人这些操作不可用。");
      main.insertBefore(el, main.firstChild);
      const SEL = ".form button, .form input, .form select, .form textarea, .batch button, .batch input, .it-act button, .it-act select";
      const off = (root) => { (root.matches && root.matches(SEL) ? [root] : []).concat([...(root.querySelectorAll ? root.querySelectorAll(SEL) : [])]).forEach((x) => { if (!x.disabled) x.disabled = true; }); };
      off(main);
      new MutationObserver((ms) => ms.forEach((m) => { if (m.type === "attributes") off(m.target); else m.addedNodes.forEach((n) => { if (n.nodeType === 1) off(n); }); }))
        .observe(main, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
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
        ["country.html", "国别概况", "一国的指标、机构法规、城市、商机、线索与节点。"],
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
    async account() {
      const el = document.getElementById("acct"); if (!el) return;
      let me = {}, role = "";
      try { [me, role] = await Promise.all([this.me(), this.role()]); } catch (e) { console.warn("读不到登录人", e); }
      const ro = role === "viewer" || role === "commenter" || role === "none";
      const who = this.esc(me.name || me.email || "未登录"), tag = role === "none" ? "游客 · 只读" : ro ? "只读" : (this.ROLE_ZH[role] || "");
      el.innerHTML = '<span class="who" title="' + this.esc(me.email || "") + '">' + who + (tag ? ' <b class="' + (ro ? "ro" : "rw") + '">' + tag + "</b>" : "") + "</span>" +
        (role === "none" ? "" : '<a href="me.html">我的</a>') + '<a href="/setting" target="_blank" rel="noopener">帐号设置</a><a href="#" id="logout">退出登录</a>';
      document.getElementById("logout").onclick = async (e) => {
        e.preventDefault();
        /* 表格的登出接口是 POST /api/auth/signout(线上核过:/api/auth/logout 是 404,所以之前点了没反应) */
        try { await fetch("/api/auth/signout", { method: "POST", credentials: "same-origin" }); } catch (err) { console.warn("退出失败", err); }
        location.href = "/auth/login?redirect=%2Fportal%2F";
      };
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
    /* 忽略的国别(个人设置「忽略国别」,存逗号分隔的国别码):登录人自己忽略的 → ISO3 集合(读一次,各页共用)。地图上这些国别画斜线(hatch*);
       采集不再涉及它们由后台按所有账号的忽略合起来做(A01)。没设过(字段空着)默认忽略中国;一个都不忽略时存「-」,和没设过分开。
       游客 → 默认;表或字段还没建 → 空集合。 */
    IGNORE_DEFAULT: ["CHN"],
    parseIsos(text) { return new Set(String(text || "").split(/[\s,;,;、]+/).filter((x) => /^[A-Za-z]{3}$/.test(x)).map((x) => x.toUpperCase())); },
    ignored() {
      if (!this._ignored) this._ignored = (async () => {
        if (this.isGuest) return new Set(this.IGNORE_DEFAULT);
        if (!this.cfg.tables || !this.cfg.tables.preference || !this.has("preference", "ignored")) return new Set();
        const rec = await this.myPref(false), v = rec && this.get("preference", rec, "ignored");
        return v ? this.parseIsos(v) : new Set(this.IGNORE_DEFAULT);
      })().catch((e) => { console.warn("忽略的国别没读到", e); return new Set(); });
      return this._ignored;
    },
    async saveIgnored(set) {
      const rec = await this.myPref(true); if (!rec) throw new Error("库里还没有「个人设置」表(管理员跑一次建表命令就有)");
      await this.send("PATCH", "/table/" + this.t("preference").id + "/record", { fieldKeyType: "id", typecast: true,
        records: [{ id: rec.id, fields: { [this.f("preference", "ignored")]: Array.from(set).sort().join(",") || "-" } }] });
      this._ignored = Promise.resolve(new Set(set));
    },
    /* 斜线花纹(echarts decal):叠在原来的底色上,颜色深浅照旧看得出。花纹画在地图自己的坐标里,会跟着缩放变粗,
       所以按地图当前每度多少像素换算(hatchDecal),缩放后 hatchFit 重算一次,屏幕上始终是约 1.5 像素的细线、5 像素的间距 */
    HATCH: { symbol: "rect", symbolSize: 1, dashArrayX: [[1, 0]], dashArrayY: [0.3, 1], rotation: -Math.PI / 4, color: "rgba(51,65,85,0.6)" },
    hatchDecal(k) { return k ? Object.assign({}, this.HATCH, { dashArrayY: [1.5 / k, 5 / k] }) : this.HATCH; },
    /* 地图系列(type: "map")的数据:忽略的国别加斜线;数据里没有的也补一项,好画上 */
    hatchData(data, set, decal) {
      if (!set || !set.size) return data;
      decal = decal || this.HATCH;
      const out = data.map((d) => (set.has(d.name) ? Object.assign({}, d, { itemStyle: Object.assign({}, d.itemStyle, { decal }) }) : d));
      const have = new Set(data.map((d) => d.name));  // 数据里没有的国家补一项;底色写明,不然有 visualMap 时颜色是空的,花纹画不出来
      set.forEach((iso) => { if (!have.has(iso)) out.push({ name: iso, value: 0, itemStyle: { areaColor: "#f1f5f9", decal } }); });
      return out;
    },
    /* geo 底图(上面叠城市散点的地图):geo 自己的 regions 画不了花纹,所以叠一层只画忽略国别的地图系列(共用这张 geo);放在 series 最后,
       别的系列的序号不变。叠上以后鼠标落在国家上算这个系列(seriesId "hatch")而不是 geo:页面要响应国家的(趋势机会)传 interactive,
       并用 P.onGeo(p) 判断;不响应的(城市概况)缺省不接鼠标 */
    hatchSeries(set, decal, interactive) {
      return set && set.size ? [Object.assign({ id: "hatch", type: "map", geoIndex: 0 }, interactive ? {} : { silent: true, tooltip: { show: false } }, { data: this.hatchItems(set, decal) })] : [];
    },
    hatchItems(set, decal) { decal = decal || this.HATCH; return Array.from(set).map((iso) => ({ name: iso, itemStyle: { decal } })); },
    onGeo(p) { return !!p && (p.componentType === "geo" || p.seriesId === "hatch"); },
    /* 画好(或重画)地图之后调:按当前缩放把斜线换算成屏幕上的细线;滚轮缩放后再换算。
       base:{ series: () => 地图系列不带斜线的 data }(国别、线索总览的地图),或 { geo: true }(叠了 hatchSeries 的 geo 底图) */
    hatchFit(mc, set, base) {
      if (!mc) return;
      if (!set || !set.size) { mc.__hatch = null; return; }
      mc.__hatch = { set, base, k: 0 };
      const fit = () => {
        const h = mc.__hatch; if (!h || mc.isDisposed()) return;
        const finder = h.base.geo ? { geoIndex: 0 } : { seriesIndex: 0 };
        let a, b, c; try { a = mc.convertToPixel(finder, [0, 0]); b = mc.convertToPixel(finder, [10, 0]); c = mc.convertToPixel(finder, [0, 10]); } catch (e) { return; }
        if (!a || !b || !c) return;
        const k = (Math.abs(b[0] - a[0]) + Math.abs(c[1] - a[1])) / 20;  // 横竖每度像素不一样(纬度方向拉长),取平均
        if (!k || Math.abs(k - h.k) / k < 0.05) return;
        h.k = k;
        const d = this.hatchDecal(k);
        mc.setOption(h.base.geo ? { series: [{ id: "hatch", data: this.hatchItems(h.set, d) }] } : { series: [{ data: this.hatchData(h.base.series(), h.set, d) }] });
      };
      fit();
      if (!mc.__hatchOn) { mc.__hatchOn = true; let t = 0; mc.on("georoam", () => { clearTimeout(t); t = setTimeout(fit, 60); }); }
    },
    /* 提示框里的一行,与地图下方的图例说明 */
    hatchTip(iso, set) { return set && set.has(iso) ? '<br><span style="color:#64748b">已忽略:不再采集这一国的线索</span>' : ""; },
    hatchNote(mapEl, set) {
      if (!mapEl || !set || !set.size || (mapEl.nextElementSibling && mapEl.nextElementSibling.classList.contains("hatch-note"))) return;
      const p = document.createElement("p");
      p.className = "hatch-note muted small";
      p.innerHTML = '<span class="hatch-swatch"></span>斜线:我忽略的国别(' + set.size + ' 个,不再采集),在<a href="me.html">我的</a>里改。';
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
    async ask(q) {
      const r = await fetch("api/assistant/ask", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify({ q }) });
      if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版);其余页面照常可用");
      if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    /* 看一条链接(或一份上传的文件):把用户贴来的网址交给后台(core/link_check.py)读原文、看信源、查库里相关、找旁证、给入库建议;只读不写 */
    async checkLink(url, note, file) {  // file:用户上传的 PDF、Word 或截图(base64),网站不让程序读时用
      const r = await fetch("api/assistant/link", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(file ? { url, note, file } : { url, note }) });
      if (r.status === 401 || r.status === 403) { location.href = "/auth/login?redirect=" + encodeURIComponent(location.pathname + location.search); return null; }
      if (r.status === 502 || r.status === 503 || r.status === 404) throw new Error("助手服务未就绪(后台任务没起来或还没更新到这一版);其余页面照常可用");
      if (!r.ok) throw new Error("助手接口 → " + r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    fail(el, e) { console.error(e); el.innerHTML = '<div class="err">读不到数据:' + this.esc(e && e.message ? e.message : e) + "</div>"; },
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
      [/水风险|用水压力|干旱风险|洪水风险/, (v) => v >= 3, "风险高(≥ 3 分)"],
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
    factHtml(label, value, srcHtml) {
      const [main, year] = this.splitYear(value), bad = this.tone(label, this.num(main));
      return '<div class="fact' + (bad ? " bad" : "") + '"' + (bad ? ' title="' + this.esc(bad) + '"' : "") + ">" + (label ? "<b>" + this.esc(label) + "</b>" : "") +
        '<span class="v"><strong>' + this.esc(main) + "</strong>" + (year ? '<em class="yr">' + this.esc(year) + "</em>" : "") +
        (bad ? '<em class="over">超警戒线:' + this.esc(bad) + "</em>" : "") + "</span>" + (srcHtml || "") + "</div>";
    },
    /* 关键指标卡:items = [{label, value, year?, note?}];数字放大加粗,单位跟在后面小一号;负面的数字标红并写明原因 */
    kpiHtml(items) {
      items = items.filter((x) => x && x.value !== "" && x.value !== null && x.value !== undefined);
      if (!items.length) return "";
      return '<div class="kpis">' + items.map((x) => {
        const [main, y] = this.splitYear(String(x.value)), year = x.year || y;
        const m = main.replace(/[−–]/g, "-").match(/^\s*(-?\d[\d,]*(?:\.\d+)?)\s*(.*)$/);
        const bad = this.tone(x.label, this.num(main));
        const big = m ? '<span class="kn">' + this.esc(main.trim().slice(0, main.trim().length - m[2].length).trim()) + "</span>" + (m[2] ? '<span class="ku">' + this.esc(m[2]) + "</span>" : "") : '<span class="kn kt">' + this.esc(main) + "</span>";
        return '<div class="kpi' + (bad ? " bad" : "") + '"><div class="kv">' + big + "</div>" + '<div class="kl">' + this.esc(x.label) + (year ? '<em class="yr">' + this.esc(year) + "</em>" : "") + "</div>" +
          (bad ? '<div class="kw">超警戒线:' + this.esc(bad) + "</div>" : "") + (x.note ? '<div class="kx">' + this.esc(x.note) + "</div>" : "") + "</div>";
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
      return this.kpiHtml(kp) + (rest.length ? '<div class="facts">' + rest.map(([k, v]) => this.factHtml(k, v)).join("") + "</div>" : "") +
        '<p class="muted small">分数 0–5:4 以上极高,3–4 高。来源:' + this.esc(this.get(oid, rec, "risk_source") || "WRI Aqueduct 4.0 等") + '。模型估计,作趋势参考' + (iso === "CHN" ? "(中国境内不做趋势机会分析)" : ';<a href="trend.html' + (iso ? "?" + new URLSearchParams({ c: iso }) : "") + '">看趋势机会 →</a>') + "</p>";
    },
    /* 已中标项目(本体 V7、V16;2026-10-12 使用方要):国别概况、城市一页最下面的同一块。prop = "country" 或 "city",rid = 那条记录。
       类别、年份可多选筛选,筛选项上的数跟着另一项与搜索走;卡片(份数、合计中标价、最大一笔、中国企业中标)与表只算筛出来的;表按中标时间从新到旧。 */
    async awards(el, prop, rid) {  // prop:country / city(国别、城市页) / winners(供应商页:多一组国别筛选、一列国别)
      const firm = prop === "winners";
      const head = firm ? '<h2 id="awards">中标记录</h2><p class="muted small">这家供应商在开发银行公开中标记录里的全部合同(联合体的合同全额计入每一家)。</p>' :
        '<h2 id="awards">已中标项目</h2><p class="muted small">开发银行公开的中标记录(首批来自世界银行,2019 年以来、10 万美元以上的水务合同)和人工补录的,作报价与竞争对手的参考;不计入线索、商机。' +
        (this.cfg.tables && this.cfg.tables.contractor ? '点供应商看它在各国的全部中标,<a href="contractor.html">全部供应商 →</a>' : "") + "</p>";
      if (!(this.cfg.tables && this.cfg.tables.award) || !this.has("award", prop)) { el.innerHTML = head + '<p class="muted">「已中标项目」表还没建(跑一次建表命令后这里就有)。</p>'; return 0; }
      el.innerHTML = head + '<div class="panel">读取中…</div>';
      const many = (this.t("award").links || {})[prop] === "many", hasW = this.has("award", "winners");
      const rows = await this.records("award", { filter: this.and(many ? this.cond("award", prop, "hasAnyOf", [rid]) : this.cond("award", prop, "is", rid)),
        props: ["name", "name_zh", "sector", "contract_type", "price", "currency", "price_cny", "awarded", "quantity", "winner", "winner_country", "project", "funder", "method", "country", "city", "source", "url"]
          .concat(hasW ? ["winners"] : []) });
      const g = (r, p) => this.get("award", r, p);
      const isoOf = {};  // 供应商页的国别列点进国别概况(那页按国别码开)
      if (firm && rows.length) (await this.records("country", { props: ["key"] })).forEach((c) => { isoOf[c.id] = this.get("country", c, "key"); });
      const year = (r) => (this.day(g(r, "awarded")) || "").slice(0, 4);
      const ctry = (r) => this.text(g(r, "country")) || "";
      rows.sort((a, b) => String(this.day(g(b, "awarded")) || "").localeCompare(String(this.day(g(a, "awarded")) || "")));
      if (!rows.length) { el.innerHTML = head + '<p class="muted">还没有记录。' + (prop === "city" ? "导入时合同名称里写了这座城市的才挂到城市上,其余只在国别概况里;" : "") + "别的来源的中标结果可以在表格「已中标项目」里补一行。</p>"; return 0; }
      const SECT = this.values("lead_sectors"), st = { s: [], y: [], c: [], q: "" };
      const big = (n) => (n >= 1e8 ? (n / 1e8).toFixed(n >= 1e10 ? 0 : 2) + " 亿" : n >= 1e4 ? Math.round(n / 1e4).toLocaleString("zh-CN") + " 万" : Math.round(n).toLocaleString("zh-CN"));
      const curZh = (c) => ({ USD: "美元", EUR: "欧元", CNY: "元" }[c] || (c ? " " + c : ""));
      const cny = (w) => (w >= 1e4 ? (Math.round(w / 100) / 100).toFixed(2) + " 亿元" : Math.round(w).toLocaleString("zh-CN") + " 万元");  // 万元 → 文字
      const words = (r) => [g(r, "name"), g(r, "name_zh"), g(r, "winner"), g(r, "winner_country"), g(r, "project"), firm ? ctry(r) : ""].join(" ").toLowerCase();
      const pass = (r, skip) => (skip === "s" || !st.s.length || st.s.includes(g(r, "sector") || "其他")) && (skip === "y" || !st.y.length || st.y.includes(year(r))) &&
        (skip === "c" || !st.c.length || st.c.includes(ctry(r))) && (!st.q || words(r).includes(st.q.toLowerCase()));
      const years = [...new Set(rows.map(year).filter(Boolean))].sort().reverse();
      const ctrys = firm ? Object.entries(rows.reduce((m, r) => { const c = ctry(r); if (c) m[c] = (m[c] || 0) + 1; return m; }, {})).sort((a, b) => b[1] - a[1]).map((x) => x[0]) : [];
      el.innerHTML = head + '<div class="filters"><div class="frow"><b>类别</b><span class="frow aw-s" style="padding:0"></span></div>' +
        '<div class="frow"><b>年份</b><span class="frow aw-y" style="padding:0"></span></div>' +
        (ctrys.length > 1 ? '<div class="frow"><b>国别</b><span class="frow aw-c" style="padding:0"></span></div>' : "") +
        '<div class="frow"><b>搜索</b><input class="aw-q" placeholder="' + (firm ? "搜名称、项目或国别" : "搜名称、中标单位或国别(如:中国)") + '" size="30" aria-label="搜已中标项目"><span class="muted small aw-n" style="margin-left:auto"></span></div></div>' +
        '<div class="aw-k"></div><div class="panel aw-t"></div>';
      const $ = (c) => el.querySelector(c), PAGE = 20;  // 表一次列 20 份,「再列」往下加
      let show = PAGE;
      const refilter = () => { show = PAGE; render(); };  // 换了筛选从头列
      const render = () => {
        const forS = rows.filter((r) => pass(r, "s")), forY = rows.filter((r) => pass(r, "y")), forC = rows.filter((r) => pass(r, "c")), list = rows.filter((r) => pass(r));
        const cnt = (arr, f) => arr.filter(f).length;
        this.chips($(".aw-s"), [["", "全部 " + forS.length]].concat(SECT.filter((s) => st.s.includes(s) || rows.some((r) => (g(r, "sector") || "其他") === s))
          .map((s) => [s, s + " " + cnt(forS, (r) => (g(r, "sector") || "其他") === s)])), st, "s", refilter, true);
        this.chips($(".aw-y"), [["", "全部 " + forY.length]].concat(years.map((y) => [y, y + " " + cnt(forY, (r) => year(r) === y)])), st, "y", refilter, true);
        if ($(".aw-c")) this.chips($(".aw-c"), [["", "全部 " + forC.length]].concat(ctrys.map((c) => [c, c + " " + cnt(forC, (r) => ctry(r) === c)])), st, "c", refilter, true);
        const sum = list.reduce((s, r) => s + (Number(g(r, "price_cny")) || 0), 0), max = Math.max(0, ...list.map((r) => Number(g(r, "price_cny")) || 0));
        const cn = cnt(list, (r) => /中国/.test(String(g(r, "winner_country") || "")));
        const nC = new Set(list.map(ctry).filter(Boolean)).size;
        $(".aw-k").innerHTML = this.kpiHtml([{ label: firm ? "中标" : "已中标项目", value: list.length + " 份" }, { label: "合计中标价(折合人民币)", value: sum ? cny(sum) : "—" },
          { label: "最大一笔", value: max ? cny(max) : "—" },
          firm ? { label: "涉及国别", value: nC + " 个", note: list.length ? "最近一份 " + (this.day(g(list[0], "awarded")) || "—") : "" } :
            { label: "中国企业中标", value: cn + " 份", note: list.length ? "占 " + Math.round(cn * 100 / list.length) + "%" : "" }]);
        $(".aw-n").textContent = "共 " + rows.length + " 份";
        const place = prop === "country" ? "城市" : firm ? "国别" : "";
        $(".aw-t").innerHTML = !list.length ? '<p class="muted">没有符合条件的已中标项目。</p>' :
          this.tableHtml(["中标时间", "名称", "类别", "合同类型", "中标价", "工程量", firm ? "联合体伙伴" : "中标单位"].concat(place ? [place] : [], ["资金来源", ""]), list.slice(0, show).map((r) => {
            const zh = g(r, "name_zh"), en = g(r, "name") || "", u = g(r, "url");
            const nm = (zh ? "<b>" + this.esc(zh) + '</b><br><span class="muted small">' + this.esc(en) + "</span>" : "<b>" + this.esc(en) + "</b>") +
              (g(r, "project") ? '<br><span class="muted small">项目:' + (u ? '<a href="' + this.esc(u) + '" target="_blank" rel="noopener">' + this.esc(g(r, "project")) + " ↗</a>" : this.esc(g(r, "project"))) + "</span>" : "");
            const p = Number(g(r, "price")), w = Number(g(r, "price_cny"));
            const price = '<span class="nw">' + (p ? "<b>" + this.esc(big(p) + curZh(g(r, "currency"))) + "</b>" : "") + (w ? '<br><span class="muted small">≈ ' + this.esc(cny(w)) + "</span>" : "") + "</span>";
            const ws = hasW ? [].concat(g(r, "winners") || []).filter((x) => x && x.id) : [];  // 有「中标方」关联的,每家点进供应商页
            const link = (x) => '<a href="contractor.html?id=' + encodeURIComponent(x.id) + '">' + this.esc(x.title || "") + "</a>";
            const mates = ws.filter((x) => x.id !== rid);  // 供应商页只列联合体里的其他几家
            const win = firm ? (mates.length ? mates.map(link).join("、") : '<span class="muted">独家中标</span>') :
              (ws.length ? ws.map(link).join("、") : this.esc(g(r, "winner") || "")) + (g(r, "winner_country") ? '<br><span class="muted small">' + this.esc(g(r, "winner_country")) + "</span>" : "");
            const at = (href, f) => href ? this.raw('<a class="nw" href="' + href + '">' + this.esc(this.text(g(r, f))) + "</a>") : this.text(g(r, f)) || "";
            const cid = this.linkId(g(r, "city")), iso = isoOf[this.linkId(g(r, "country"))];
            return [this.raw('<span class="nw">' + this.esc(this.day(g(r, "awarded")) || "") + "</span>"), this.raw(nm), g(r, "sector") || "", g(r, "contract_type") || "", this.raw(price), g(r, "quantity") || "", this.raw(win)]
              .concat(place ? [prop === "country" ? at(cid && "city.html?id=" + encodeURIComponent(cid), "city") : at(iso && "country.html?" + new URLSearchParams({ iso }), "country")] : [], [g(r, "funder") || "", this.raw(this.open("award", r.id))]);
          })) + (list.length > show ? '<p><button type="button" class="chip-btn more aw-more">再列 ' + Math.min(PAGE, list.length - show) + " 份(已列 " + show + " / " + list.length + ")</button></p>" : "");
        const more = $(".aw-more"); if (more) more.onclick = () => { show += PAGE; render(); };
      };
      let tt = 0; $(".aw-q").oninput = () => { clearTimeout(tt); tt = setTimeout(() => { st.q = $(".aw-q").value.trim(); show = PAGE; render(); }, 250); };
      render();
      return rows.length;
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
    tracked(opps) { const t = this.TRACKED; return opps.filter((o) => t.includes(this.get("opportunity", o, "stage"))); },
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
      return this.records("opportunity", { props: ["code", "name_zh", "stage", "country", "owner", "next_milestone", "public_stage", "priority", "next_step", "next_due", "last_followup", "lead_count"] });
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
    zhName(l, n) {
      n = n || 60;
      const first = (v) => String(v || "").trim().split(/\r?\n/)[0].slice(0, n);
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
      return this.raw(this.star("lead", l) + link(main) + '<br><span class="muted small">' + (z.name && title ? this.esc(title) + " · " : "") + this.esc(this.get("lead", l, "key") || "") + (extra ? " · " + this.esc(extra) : "") + "</span>");
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
