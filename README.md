# 全球水务开源情报 · Global Water OSINT

面向全球水务(供水、污水、海水淡化、水利灌排)项目机会的**公开信息追踪系统**。
每天从多边开发银行的采购公告与项目库、各国业主公告栏、新闻等**公开来源**采集,判定相关性、抽取关键字段、挂到在跟的商机上,
再出日报、国别概况、趋势地图和可直接报送的 Word 报告。数据全部在你自己的服务器上。

[English](#english) · [从零部署](docs/部署指南.md) · [安全问题报告](SECURITY.md)

## 它做什么

| 页面 | 内容 |
|---|---|
| 首页 / 助手 | 一句话提问(「越南今年的污水招标」),系统先说明理解为什么,再给数字、清单或图;贴一个项目链接,给出基本研判与相关线索 |
| 市场背景 | 国别概况(宏观、人口、水资源指标,水务机构、国企与法规)、城市概况、趋势机会(水风险 × 经济水平 × 在执行的贷款项目) |
| 项目线索 | 线索总览(筛选、批量处理、转为商机)、线索分析与每日简报、待处理清单 |
| 商机跟进 | 在跟的项目、阶段与跟进记录、时间节点、日历订阅 |
| 报告 | 国别报告、项目分析报告(中文内部版 / 英文公开版)、周报、月报,按 Word 模板生成 |
| 本体 | 系统里有哪些对象、怎么关联、每个选项的定义(`docs/ontology/ontology.yaml` 是唯一来源) |

## 架构

```
浏览器 ──HTTPS──> Caddy ──forward_auth──> Teable(登录、权限、全部数据:线索、商机、国别、报告……)
                    │                        ▲
                    └─ /portal/ 静态页面 ─────┘  页面以登录人的身份直接读写表格
后台任务 worker(core/worker.py)── 机器令牌 ──> Teable
   每天 08:30 采集、判定、出日报 · 每 2 分钟补全新线索 · 按需出 Word 报告 · 每月更新国别宏观数据
```

- **一个库,一份数据**:Teable(开源多维表格)是唯一的台账,没有第二个数据库,也没有自己的帐号体系。
- **两个仓库**:本仓库是程序(公开);使用方自己的配置——profile、页面配置 `portal/config.json`、报告模板、国别台账——
  放在另一个私有配置仓,运行时挂成本仓库的 `private/`(`.gitignore` 已排除)。程序只认 `private/profile.yaml` 这条路径。
  `scripts/privacy_scan.py` 在 CI 里每次检查有没有 IP、邮箱、令牌或使用方的敏感词误入本仓库。
- **本体驱动**:对象、字段、选项写在 `docs/ontology/ontology.yaml`;`python -m core.schema` 据此在 Teable 里建表、补字段(只增不删)。
- **大模型可选**:DeepSeek 或 Claude 做相关性判定、字段抽取(附原文依据)、中文摘要与项目研判;没有密钥时退回规则。
  只把需要判定的那条线索的正文送给模型,从不送整库。

## 目录

```
core/            采集适配器、监测与判定(core/watch/)、后台任务(core/jobs/、worker.py)、建表(schema.py)、报告、助手
core/templates/  缺省的 Word 报告模板(不带任何使用方信息;私有配置仓 templates/ 里有同名文件时优先用那份)
portal/          静态页面(菜单在 portal.js 的 MENU 一处)
docs/ontology/   本体与 Teable 映射
reference/       公开参考数据:国别表、各国水务机构与法规、投标资格规则、多语种水务词表(CC BY 4.0)
global/          全球信源配置(多边开发银行、联合国机构)
profiles/        demo(测试用)与 example(自己部署的样板)
deploy/teable/   安装与运维脚本(Teable + Caddy + worker,一台主机)
tests/           离线测试(不联网、不调模型;私有配置不在时相关测试自动跳过)
```

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q tests                 # 离线测试
.venv/bin/python scripts/privacy_scan.py            # 隐私扫描;有 private/privacy_terms.txt 时一并查敏感词
```

页面测试要 Playwright(`pip install playwright`)。连真实 Teable 跑后台任务的一个周期:
`TEABLE_URL=… TEABLE_TOKEN=… python -m core.worker --profile private/profile.yaml --once`。

## 部署

**让 AI 帮你装**(不懂技术也行):把下面这句话复制给能在你电脑上跑命令的 AI 助手(Claude Code、Cursor、Codex 等),它会一步步带你做。
要你在网页上操作的地方(买服务器、加域名记录、拿模型密钥)它会提示你;密钥由你自己填进本机文件,不用发给 AI。

```text
请帮我安装全球水务开源情报:先读 https://raw.githubusercontent.com/saintpantheryes/osint-water-public/main/docs/AI_INSTALL.md ,然后一步步带我做。
```

**只想先试试**:不用服务器和域名,装好 Docker Desktop 后在本仓库里运行 `bash deploy/local/start.sh`,
浏览器打开 http://localhost:8080/portal/ (单机试用版,只有自己能用;说明见 [AI_INSTALL.md](docs/AI_INSTALL.md#单机试用版))。

**自己装**:准备好云主机、域名和两个仓库后,一条命令装完(可重复运行):
`bash deploy/teable/one_click.sh <密钥.pem> <主机IP> <域名> <你>/osint-water-private`。
每一步分开做见 **[docs/部署指南.md](docs/部署指南.md)**。
运维(备份、恢复、换域名、迁主机、令牌轮换)见 [deploy/teable/README.md](deploy/teable/README.md)。

## 参与

数据有错、信源失效、想加国别信源,欢迎提 issue 或 PR。改代码前先跑 `pytest` 与 `scripts/privacy_scan.py`;
新的对象或字段先改 `docs/ontology/ontology.yaml`。

## 数据来源纪律

- core 只接入**官方开放许可**的来源(世界银行各数据集、IFC 开放数据、IATI、美洲开发银行开放数据、法国开发署开放数据、欧盟 TED、开放采购数据登记册、联合国城市库、国际货币基金组织等)。各源的许可、署名义务、访问依据登记在数据源目录中,报告自动生成"数据来源与许可"附录。
- **不接入**网站内部未公开接口、带非商用条款的数据、付费情报库内容;不绕过验证码、付费墙或反爬措施;不复用页面内嵌的访问密钥。
- 采集用如实的自定义 User-Agent,逐站串行、限速,遇 403/429 即停;采购公告中的自然人联系方式在解析层直接丢弃。
- 数字默认取结构化字段;由文本抽取的数字单列并附原文证据片段;每条记录带来源链接与抓取时间;指标带数据年。
- 台账里只读到检索摘要、未打开原文的来源标为"检索摘要",关键数字须复核后才能改为"读原文"。

## 数据源目录(已实测)

| 文档 | 内容 |
|---|---|
| [多边开发银行项目库与采购公告](docs/sources/D1_多边开发银行项目库与采购公告.md) | 世行项目库(须用 v3)、采购公告、文档 API、IATI 统一层、AIIB/EBRD/EIB/IDB/AfDB/ADB/IsDB 等 27 个源逐一实测:接口、字段、过滤陷阱、许可 |
| [投资情报、竞争格局与国别风险](docs/sources/D3_投资情报-竞争格局-国别风险数据源.md) | 世行 PPI 先例库、IFC 披露、各国 PPP 机构管线、行业媒体 RSS、中资融资数据集、国别风险与可投性维度→数据源 |
| [国别与城市背景数据](docs/sources/E_国别与城市背景数据.md) | 世行指标 API、IMF DataMapper、UN WUP 2025、JMP、AQUASTAT、OECD 国家风险分类、债务可持续性清单、GeoNames、Wikidata 的覆盖实测与最小指标集 |
| [摩洛哥公共采购门户](docs/sources/F_摩洛哥公共采购门户.md) | 摩洛哥水务招标的主渠道:按活动领域取在招公告的办法(表单回发)、字段、数量与规模、金额筛选的坑、接入方式 |
| [全球水务采购信源复核(2026-09-26)](docs/sources/D1_2026-09-26_复核.md) | 世行采购接口、亚行、亚投行、EBRD、IsDB、IDB、AfDB、UNGM/UNDP 8 个全球信源的复测:国别字段、日期写法、坑,以及接入与不接入的理由 |

> 前三份文档是 2026-09-21 对一组 39 个发展中国家样本的实测记录;样本清单由使用方在 profile 中配置,文档不列。

## 已知的实施级陷阱(实测)

- 世行项目库 **v2 已冻结在 2024-12-20,必须用 v3**;v3 对不认识的参数静默忽略,带过滤的请求要断言"过滤后总数 < 不过滤总数"。
- **世行系接口用自有国别码,不是 ISO2**(如刚果(金)=ZR、东帝汶=TP;采购公告与 IFC 数据集里塞尔维亚=YF)。用 ISO2 查会静默丢国别且不报错——profile 的国别表必须带 `wb_country_code` 列,采集后断言每国返回数 >0 或在已知零名单内。
- 储备项目(Pipeline)的部门码不可靠(约半数无码、占比字段普遍为 0),机器过滤精度只有约 37%;量不大(百条量级)时直接人读打标。
- 采购公告的 `submission_date` 是发布日,截止日是 `submission_deadline_date`。
- IMF DataMapper 对浏览器 UA 返回 403、默认 UA 正常;部分世行数据目录接口相反。
- 采购计划文档版本爆炸(单项目可达数十版)、表格被压成逐行文本——先按贷款号切行、丢弃已签/取消、按金额门槛预筛,再决定是否需要 LLM。
- 云端会话的网络策略可能拦截数据源域名(实测 2026-09-24 受限模式下世行、IMF、EBRD、ADB、AIIB、IsDB 均被拦;改为完全访问后均可用),此时 `ingest` 无法运行,台账与报告不受影响。
- 世行指标 API 个别指标偶尔超过 60 秒才响应:适配器超时重试一次,仍超时则跳过该指标并提示,不拖垮整个源。
- 部分机构把文件放在在线翻页阅读器里(如欧佩克基金的采购原则与公告),直接下载返回 403——照记不绕,以借款人网站发布的同一文件或招标文件为准。
- 亚行招标类 RSS 自 2026 年 4 月底停更(源地址 404),官方索引页仍在链接;全站招标列表是前端渲染、要页面令牌,不接入。
- 伊斯兰开发银行列表页的截止日 `time@datetime` 是坏值("00Z"),要读元素文本;列表按截止日升序,最新公告在最后一页。
- 世行采购接口 `rows` 上限 1,000 且静默截断;行业字段与 `qterm` 都是项目级命中,水务项目下的非水务采购要靠逐条判定剔除。
- 亚投行数据文件的键没加引号、国名有双重编码("TÃ¼rkiye");UNGM 的公告列表走带防伪令牌的内部接口,不接入;非洲开发银行采购页有 Cloudflare 验证,不绕。

## 许可

- 代码:GNU Affero General Public License v3.0(见 [LICENSE](LICENSE))。AGPL 要求任何基于本代码对外提供网络服务的一方同样公开其修改。
- 文档(`docs/`)与规则表(`reference/`):CC BY 4.0。
- 各数据源保留其自身许可;使用本工具产出的内容须按各源要求署名。
- 首页与分析页的世界地图 `portal/world.geo.json` 按中国口径生成(`scripts/make_world_geojson.py`):国界取 Natural Earth 10m「中国视角」数据(公有领域),台湾、藏南、阿克赛钦在中国境内,香港、澳门并入中国;南海断续线按自然资源部标准地图画十段(各段位置取自 DataV.GeoAtlas 全国图)。地图只作系统内部的线索分布示意;对外公开发布的地图应使用标准地图服务网站的标准地图或按规定送审。

## 命名说明

产品名 OSINT-WATER 指"基于公开来源信息的水务项目追踪"。在部分组织语境下"情报"一词敏感,对内可称"公开信息跟踪"或"市场信息跟踪"。

## English

**Global Water OSINT** tracks water-sector project opportunities worldwide (water supply, wastewater, desalination, irrigation)
from **public sources only**: procurement notices and project pipelines of multilateral development banks (World Bank, ADB, AIIB,
EBRD, IsDB, IDB, AfDB, UNDP…), national tender portals, utility notice boards and news. Each day it collects, de-duplicates,
classifies (rules plus an optional LLM: DeepSeek or Claude), extracts deadlines, amounts and funders with quoted evidence,
links notices to tracked opportunities, and produces a daily brief, country profiles, a trend map and Word reports.

- **Stack**: [Teable](https://github.com/teableio/teable) (open-source Airtable-like database) is the single source of truth
  and handles login and permissions; Caddy serves static pages under `/portal/` behind Teable's session (forward_auth);
  a Python worker writes to Teable with a machine token. Everything runs on one server you own.
- **Two repositories**: this one holds the code; your own configuration (profile, page config, report templates,
  country ledgers) lives in a separate private repo mounted at `private/`.
- **UI language**: the interface and reports are currently in Chinese; project reports also have an English edition.
- **Deploy**: easiest is to hand an AI coding agent this sentence: "Please install Global Water OSINT for me: read
  https://raw.githubusercontent.com/saintpantheryes/osint-water-public/main/docs/AI_INSTALL.md first, then walk me through it."
  Secrets never go through the chat: you type them into `~/.osint-water/secrets.env` yourself.
  By hand: fork, create an empty private config repo, then run
  `bash deploy/teable/one_click.sh <key>.pem <server-ip> <domain> <you>/osint-water-private` (re-runnable; exit code 10 means
  "do the step printed under 需要你做 and re-run"). Step by step: [docs/部署指南.md](docs/部署指南.md).
- **Try it on your own computer**: with Docker Desktop running, `bash deploy/local/start.sh` in a clone, then open
  http://localhost:8080/portal/ (single-user, no server or domain; admin login in `deploy/local/state/ADMIN.txt`).
- **License**: code AGPL-3.0; `docs/` and `reference/` CC BY 4.0; each data source keeps its own terms.
- **Security**: report vulnerabilities privately, see [SECURITY.md](SECURITY.md).
