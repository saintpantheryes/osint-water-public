# core/

采集适配器、归一表、快照与差异、健康检查、报告渲染。**只处理公开数据,只读 profile 提供的参数。**

P0 计划的五张表:`country`(含 `wb_country_code`)· `country_indicator`(长表:iso3, indicator, year, value, source, is_forecast, fetched_at)· `project`(宽表,主键 (source, source_id);含 funder、window[主权/非主权/PPP 机构]、stage、金额原值/币种/原单位、key_date、source_url、fetched_at、first_seen_at、extras JSON)· `notice` · `run_log`。

存储:`data/raw/{source}/{date}/*.json.gz`(可重放)→ `data/snapshots/{date}/{source}.parquet`(归一后、过滤前的全集)→ 报告查询用进程内 DuckDB。SQLite 只在将来前端需要写操作时引入。数据目录默认为仓库 `data/`,可用环境变量 `OSINT_WATER_DATA` 或 profile 的 `data_dir` 改;私有 profile 用 `data/private/`,不覆盖 demo 的快照。

适配器门槛:相关记录 <50 条且月变动 <5 条的源,只做"下载、哈希、变了提醒",归一化由人工维护 CSV;同时在线的非正式接口适配器为 0(本仓库不接入非正式接口)。

健康检查(防"HTTP 200 但内容错"):行数带(±30% 或为 0 报警)、数据内新鲜度(取数据里的最大日期,不看文件元数据)、金丝雀记录、带过滤请求断言"过滤后总数 < 不过滤总数"、每国返回数断言。

项目机会台账(`core/ledger.py`):一国一份人工维护的 YAML(私有配置仓的 `countries/{ISO3}/ledger.yaml` + `sources.yaml`,由 profile 指定路径),core 负责校验(字段、枚举、日期、来源引用、流式写法里被英文逗号切断的文本)与派生——投标资格(`reference/procurement_rules.yaml` × `profile.bidder.nationality`)、下一节点、最晚启动日期(节点 − `profile.lead_times`)、过期提醒;快照为 `opportunity`(一项一行)与 `opportunity_milestone`(长表)两表。

国别报告(`core/report.py`):分析单位是国别,正文以项目为粒度——本期要点、国别背景(最近一期 `country_indicator` 快照里的世行 WDI 与 IMF 指标,未 ingest 则不出)、台账总表、时间窗口、活动、建议动作(内部)、投标资格规则、项目卡片、待核实清单、来源登记(只列被引用的来源)。Markdown 为源,pandoc 出 Word(A4 横向、中文字体、表格列宽按内容分配)。`--public` 只含公开事实。

采集纪律补充:对方响应慢(超时)可重试一次;403/429 立即停止、不重试。WDI 单个指标仍超时则跳过该指标并在日志提示,不拖垮整个源。
