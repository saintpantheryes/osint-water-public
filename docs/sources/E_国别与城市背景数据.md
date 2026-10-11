# 国别与城市背景数据 — 数据源实测

> 本文是 2026-09-21 对一组 **39 个发展中国家样本**(世行 IDA/IBRD 混合;具体国别清单由使用方在 profile 中配置,本文不列)的数据源可得性实测记录。文中"样本国"均指该集合。已去除任何组织专属内容;实测脚本与样本文件保存在私有证据目录,未随本文发布。


## 结论与建议
## 0. 结论先行
"国别+城市背景层"完全可行,而且**大部分不用新造**:世行 API + IMF DataMapper + UN WUP 2025 三个免费、免 key、39 个样本国 100% 覆盖的源就能撑起 80% 的背景面板;JMP / AQUASTAT / OECD CRC / DSA 清单补水务现状与风险。仓库里已有世行抓取脚本和国家经济面板,背景层的工作量主要是"扩指标 + 补中文名/ISO3 + 加城市表"。

## 1. 样本国 ISO2/ISO3 对照

国别清单与 ISO 对照由 profile 提供;可用世行 `/v2/country/{iso3;…}` 接口回验(本次样本 39/39 命中)。注意几个易错项:刚果(金)=COD、刚果(布)=COG、东帝汶=TLS、波黑=BIH、科特迪瓦=CIV、塞尔维亚=SRB、土耳其=TUR。

世行属性:收入组 LIC 7 / LMC 18 / UMC 11 / HIC 3;贷款类别 IDA 15 / Blend 8 / IBRD 14 / 未分类 2(沙特、阿曼)。

## 2. 世行 API 实测(39 个样本国,mrnev=1,API lastupdated=2026-07-13)
| 指标 | 覆盖 | 最新年分布 | 缺失/陈旧 |
|---|---|---|---|
| GDP、人均GDP、GDP增速、人口、人口增速、城镇化率、城镇人口增速 | 39/39 样本国 | 全部 2025 | 无 |
| GNI 人均 | 39/39 样本国 | 2025×38, 2024×1 | 无 |
| FDI 净流入(额/占GDP) | 39/39 样本国 | 2025×15, 2024×24 | 无 |
| 基本饮水 BASW(含城镇分列) | 39/39 样本国 | 2024×38 | 刚果(布)=2021 |
| 基本卫生 BASS(含城镇分列) | 39/39 样本国 | 2024×36 | 波黑 2018、刚果(布) 2021、塔吉克 2023 |
| 安全管理饮水 SMDW | **24/39** | 2024 | 缺 15 国(西非/中非/太平洋岛国为主) |
| 安全管理卫生 SMSS | **24/39** | 2024 | 缺 15 国(西非/中非/太平洋岛国为主) |
| 水压力 FWST | 38/39 | 全部 2022 | 缺所罗门 |
| 最大城市人口 EN.URB.LCTY | 36/39 | 2025 | 缺东帝汶/所罗门/博茨瓦纳;且口径与 WUP2025 冲突,**弃用** |
| CPI 通胀 | 38/39 | 2025×35 | 缺土库曼;刚果(金)、塔吉克停在 2016 → 用 IMF |
| 官方汇率 | 39/39 样本国 | 2025×33 | 土库曼=2001、几内亚=2020 → 用 FX API |
| 外债/GNI | 35/39 | 2024 | 缺纳米比亚/波兰/沙特/阿曼 |
| 中央政府债务/GDP | 24/39 | 多国 1990s–2010s | **不可用** → 用 IMF GGXWDG_NGDP |
| Doing Business(IC.BUS.*) | — | — | API 已下线;继任 B-READY 仅 13/39 |
| WGI 六维(GOV_WGI_*,source=3) | 39/39 样本国 | 全部 2024 | 旧代码 PV.EST 等已失效 |

核心 17 指标零缺口的有 15 国;缺口最多:所罗门 4、刚果(布) 4、东帝汶 3、几内亚 3、纳米比亚 3、土库曼 3。

## 3. 最小可行指标集
**国别(20 项)**
| # | 指标 | 推荐源(代码) | 39 个样本国覆盖 | 刷新 |
|---|---|---|---|---|
| 1 | GDP 现价美元 | WB NY.GDP.MKTP.CD | 39 | 月检 lastupdated |
| 2 | 人均 GDP | WB NY.GDP.PCAP.CD | 39 | 同上 |
| 3 | GDP 实际增速(历史+至 2031 预测) | WB NY.GDP.MKTP.KD.ZG + IMF NGDP_RPCH | 39 | IMF 4/10 月 |
| 4 | 通胀(含预测) | IMF PCPIPCH | 39 | 半年 |
| 5 | 汇率(年均+即期) | WB PA.NUS.FCRF + open.er-api | 39 | 日/年 |
| 6 | FDI 净流入 | WB BX.KLT.DINV.CD.WD | 39 | 年 |
| 7 | 人口(含至 2031/2050 预测) | WB SP.POP.TOTL + IMF LP / WUP2025 | 39 | 年 |
| 8 | 人口增速 | WB SP.POP.GROW | 39 | 年 |
| 9 | 城镇化率(含 2035/2050 预测) | WB SP.URB.TOTL.IN.ZS + WUP2025 国家库 | 39 | 年 / 一次性 |
| 10 | 城镇人口增速 | WB SP.URB.GROW | 39 | 年 |
| 11 | 基本饮水覆盖(总/城镇) | WB SH.H2O.BASW(.UR).ZS | 39 | JMP 两年一更 |
| 12 | 安全管理饮水 | WB SH.H2O.SMDW.ZS | 24(标缺) | 同上 |
| 13 | 基本卫生(总/城镇) | WB SH.STA.BASS(.UR).ZS | 39 | 同上 |
| 14 | 安全管理卫生 | WB SH.STA.SMSS.ZS | 24(标缺) | 同上 |
| 15 | 水压力 + 人均可再生水资源 | WB ER.H2O.FWST.ZS / ER.H2O.INTR.PC | 38 / 39 | 年(数据年 2022) |
| 16 | 市政污水处理率(处理量/产生量)、淡化量 | AQUASTAT 4270/4269/4264 经 Data360 | 23–25 / 15 | 年 |
| 17 | 政府总债务/GDP(含预测) | IMF GGXWDG_NGDP | 39 | 半年 |
| 18 | 经常账户/GDP(可选加 外债/GNI 35/39) | IMF BCA_NGDPD | 39 | 半年 |
| 19 | 国家风险评级 | OECD CRC(37/39)+ LIC-DSA(22/39) | 合并后 39 个样本国至少有其一(波兰除外,用'高收入 OECD'标注) | 季度人工/脚本 |
| 20 | 治理与融资资格 | WGI 政治稳定+政府效能(39)+ WB 收入组/IDA-IBRD(39) | 39 | 年 |
进阶(第二批):JMP 国别 xlsx 的"管道供水比例/入户/污水处理"城乡分列;B-READY 公用事业评分(等覆盖扩大)。

**城市(6 字段)**:①城市名(英/中)+WUP City_Code;②2025 人口;③近 10 年年均增速(由 2015/2025 算 CAGR 或直接取 Pop_rate);④2035 预测人口;⑤占全国人口比重 percPop;⑥首都标记+坐标。全部出自 WUP2025 城市库一个文件(中文名除外)。

## 4. "每国 Top N 城市"最稳妥组合
1. **主源 = UN WUP 2025 城市库**(确认最新版是 2025,不是 2018):39/39 样本国 覆盖、≥5 万人、1975–2050 逐年、自带占比与增速。规则:每国取 2025 人口 Top 10 且 ≥10 万;不足 3 个的国家取全部。
2. **小国补位 = GeoNames cities15000/5000**:所罗门(WUP 仅 Honiara)、东帝汶(仅 Dili)、波黑/博茨瓦纳/刚果(布)/纳米比亚(各 4)用 GeoNames 补 2–5 个次级城镇(人口标"陈旧/参考")。
3. **非洲二线城市发现 = Africapolis**(20 国全覆盖、含 2050 预测),只用于"候选清单",**不用于人口展示**(Kisumu 1756 万这类形态学口径会闹笑话)。
4. **中文名/对照 = Wikidata**(zh 标签 + P1566 GeoNames ID)。WUP 无 GeoNames ID,需按"同国 + 坐标 25km 内 + 名称相似"做一次性匹配,再人工校一张约 300 行的别名表(含 'Kasaï-Oriental'=姆布吉马伊 这类怪名)。
5. citypopulation.de 只做人工核对引用(条款允许署名使用,但 robots 禁 AI 爬虫、无批量);GHSL-UCDB(298MB)与 OECD FUA 现阶段不接。

## 5. 关键坑
- **IMF**:伪装浏览器 UA → Akamai 403;默认 curl UA → 200。路径里的国家过滤无效,总是返回 229 个经济体。
- **世行**:`mrnev=1` 掩盖陈旧(土库曼汇率 2001),入库必须带年份并在前端显示"数据年";WGI 必须用 `GOV_WGI_*`+`source=3`;多指标合并调用要加 `source=2`。
- **口径冲突**:金沙萨 世行 1778 万 vs WUP2025 1094 万 vs Africapolis 1194 万 → 全站城市人口统一 WUP2025,并在脚注写明 DEGURBA 口径。
- **WUP CSV**:国名含逗号必须按带引号解析;人口单位千人;5,512 城可信度=Low。下载链接藏在 SPA 的 JS 里,HTML 无链接。
- **UN Data Portal API** 401 需 token → 走批量 CSV。**OECD** HTML 403(Cloudflare,不绕过),但 content/dam 下 PDF 直链 200;解析时脚注编号 (5)/(6) 别当等级。**DSA PDF** 文本为 latin-1;'Guinea' 会误中 Guinea-Bissau/PNG。
- **JMP** 无 API,39 个样本国 × 6MB;头部指标用世行镜像即可。**AQUASTAT** 原站无公开 API,用 Data360 镜像(数据到 2022)。
- 仓库 `.venv` 无 openpyxl 与 PDF 库;`countries.parquet` 无 iso3、38/39 样本国无中文名。

## 6. 复用与接入顺序
**直接复用**:`scripts/ingest/worldbank_country_stats.py`(扩 INDICATORS 字典即可,现成的分页/重试/聚合区剔除)、`common.py`(write_parquet/IngestReport)、`cities_global.py`+`geonames.py`(GeoNames 下载解析)、`wikidata.py`(run_sparql 缓存重试)、前端 `CountryEconPanel.svelte` 与 `/country/[iso2]` 路由、`plants.parquet`(36/39 个样本国 8,055 座存量厂,作"已有设施"背景)。`city_stats.parquet`(纯中国)与 `utilities.parquet`(826/844 为中国)对海外无用。
**建议新表**:`country_indicators.parquet` 长表(iso3, indicator_key, year, value, source, is_forecast, fetched_at),不要加宽旧 country_stats;`countries` 补 iso3/中文名/使用方自定义区域/收入组/贷款类别;新增 `intl_cities.parquet`(WUP 6 字段)。
**第一批接入顺序**:①世行指标扩到约 20 个代码 + country 元数据 → ②IMF DataMapper 8 个预测指标 → ③WUP2025 城市库+国家库(一次性)→ ④OECD CRC + DSA 两个 PDF(半自动)→ ⑤WGI。第二批:AQUASTAT(Data360)、JMP 明细、FX、Wikidata 中文名、Africapolis。

## 7. 覆盖缺口(尤其小岛国/冲突国)
- **所罗门群岛**:无水压力、无安全管理饮水/卫生、无 OECD 评级(未审议)、WUP 仅 1 城、AQUASTAT 污水/取水全缺、仓库 0 座厂 → 背景面板会很空,只能靠基本饮水 71.6%、DSA=Low、WGI 与 IMF 预测撑。
- **东帝汶**:无安全管理两项、WUP 仅 Dili、AQUASTAT 污水缺;用美元无汇率问题;DSA=Moderate、OECD 6 级。
- **土库曼斯坦**:世行无 CPI、汇率停在 2001(官方固定汇率 3.5,存在平行汇率);IMF 有全套但属估计值;OECD 7 级;不在 DSA 清单。基本饮水 99.9% 这类官方数需谨慎引用。
- **乍得**:世行核心指标齐全,但 AQUASTAT 污水/淡化缺;DSA=High、OECD 7 级、政治稳定 -1.78;B-READY 反而有数据。
- **刚果(布)**:JMP 停在 2021、已处于债务困境(In debt distress)。**刚果(金)/塔吉克**:世行 CPI 停在 2016(用 IMF)。**几内亚**:汇率 2020。
- **安全管理饮水/卫生**在约 15 个样本国无估计(西非、中部非洲、太平洋岛国为主)→ 统一"标缺 + 用 basic/piped 替代"。
- **水司/水价/漏损(城市级供排水运营现状)是最大空白**:IBNET 被 Cloudflare 挡未能验证,Wikidata 39 个样本国仅 6 条水务机构 → 这一层短期只能从世行/亚行项目文件(PAD)里人工或 LLM 抽取,应与"项目信息"管线合并考虑。
- 波兰不在 OECD CRC 与 DSA(高收入国),沙特/阿曼无世行外债数据与贷款类别——这三国的"风险"维度需用主权信用评级(无免费 API,未验证)。

## 未验证事项
- IBNET(newibnet.org)被 Cloudflare 质询挡住(403),旧域名连接失败;水司级水价/NRW/覆盖率数据是否可导出、是否需注册——未验证,需使用者本人用浏览器确认
- 世行 EN.URB.LCTY 的底层口径(疑仍为 WUP2018 外推)未证实,只实测到与 WUP2025 城市库差异很大(金沙萨 1778 万 vs 1094 万)
- Africapolis 的数据许可未核实;所用 JSON 为前端内部静态资源(非文档化 API),URL 稳定性未知
- GHSL GHS-UCDB R2024A(298MB)未下载,字段与 39 个样本国覆盖仅凭文档推断
- OECD-EC FUA 未实测(oecd.org HTML 403),仅凭常识判断其对 39 个样本国覆盖差
- UN WPP 2024 CSV 只做了 HEAD(200/16.6MB),未解析逐国覆盖;下一版(2026?)发布时间未验证;UN Data Portal API token 申请流程未验证
- FAO AQUASTAT 原站是否有未公开的批量/API 端点未找到;FAO 数据许可(CC BY-NC-SA?)未逐字核对;Data360 镜像滞后程度只知当前最新为 2022
- open.er-api.com 免费层的限频与署名条款未逐字核对;土库曼、尼日利亚等国官方汇率与平行市场汇率的差异未量化
- 主权信用评级(标普/穆迪/惠誉)无免费结构化源,未测试;中国信保国家风险评级、商务部国别指南新地址未找到(旧地址 404)
- UNCTAD 未测试(任务允许略过)
- IMF 对土库曼、乍得等国的 WEO 数值为 IMF 估计,数据质量未评估;LIC-DSA 清单为何不含尼日利亚(PRGT 资格)未核实
- B-READY 后续年份覆盖国扩充计划(约 180 国)未验证
- 世行 API 未发现限流,但未做高并发压测;WDI 每年具体更新月份为经验值,仅实测到 lastupdated=2026-07-13

## 数据源明细(22)
### World Bank Indicators API (WDI, v2) — https://api.worldbank.org/v2/country/{iso3;分号拼接}/indicator/{code}?format=json&per_page=500&mrnev=1
- **category**: 国别指标
- **access_method**: API(JSON),免 key
- **verified**: True
- **probe_evidence**: 脚本 scratchpad/wb_probe.py:curl -s -m 25 逐指标请求 39 个样本国 mrnev=1,统计最新年份分布与缺失国;结果存 scratchpad/wb_out.json。多指标合并调用需加 source=2(实测 TCD;SLB × GDP;POP 返回 4 行)。IC.BUS.EASE.XQ / IC.BUS.DFRN.XQ 返回 'indicator was not found… deleted or archived'(Doing Business 已下线)。
- **key_fields**: countryiso3code, country.id(=ISO2), indicator.id, date, value; meta.lastupdated
- **water_filter**: 水务相关代码:SH.H2O.BASW(.UR).ZS / SH.H2O.SMDW(.UR).ZS / SH.STA.BASS(.UR).ZS / SH.STA.SMSS(.UR).ZS / ER.H2O.FWST.ZS / ER.H2O.INTR.PC / ER.H2O.FWDM.ZS(生活取水占比,37/39) / SH.STA.WASH.P5
- **tos_notes**: CC BY 4.0,须署名;无 key 无明显限流(本次 25+ 次连续请求无 429)
- **update_frequency**: 实测 lastupdated=2026-07-13;WDI 每年约 4 次大更新(4/7/9/12 月),建议月度 cron 比对 lastupdated 再全量重拉
- **value_for_investment**: high — 39 个样本国宏观/人口/城镇化/WASH 头部指标一站式,39 个样本国一次请求拿完,是背景层主干
- **notes**: 坑:mrnev=1 会掩盖陈旧值(土库曼汇率=2001),入库必须带 year 并在前端标'数据年';EN.URB.LCTY(金沙萨 1778 万)与 WUP2025 城市库(1094 万)口径冲突,城市人口不要用它。
### World Bank Country metadata(收入组/贷款类别/首都) — https://api.worldbank.org/v2/country/{iso3;…}?format=json&per_page=100
- **category**: 国别指标
- **access_method**: API(JSON)
- **verified**: True
- **probe_evidence**: curl 一次返回 39 行,字段 ['id','iso2Code','name','region','adminregion','incomeLevel','lendingType','capitalCity','longitude','latitude']
- **key_fields**: id(ISO3), iso2Code, region, incomeLevel, lendingType, capitalCity, longitude, latitude
- **tos_notes**: CC BY 4.0
- **update_frequency**: 每年 7 月 1 日收入分组更新
- **value_for_investment**: high — IDA/IBRD/Blend 资格直接决定该国世行项目的融资性质(赠款/优惠贷/硬贷),是机会筛选的一级标签
- **notes**: 首都≠水务主战场:世行给科特迪瓦=Yamoussoukro、坦桑=Dodoma,而项目集中在阿比让/达累斯萨拉姆,城市层须按人口取 Top N 而非按首都。可直接当 ISO2↔ISO3 对照的权威来源(现仓库 countries.parquet 无 iso3 列)。
### World Bank WGI 全球治理指标(经 Indicators API source=3) — https://api.worldbank.org/v2/country/{iso3;…}/indicator/GOV_WGI_PV.EST?source=3&format=json&mrnev=1
- **category**: 风险评级
- **access_method**: API(JSON)
- **verified**: True
- **probe_evidence**: 旧代码 PV.EST/GE.EST 等返回 'indicator was not found';/v2/sources/3/indicators 列出 36 个新代码 GOV_WGI_*;用新代码+source=3 实测 6 指标均 n=39、year=2024
- **key_fields**: GOV_WGI_{CC,GE,PV,RL,RQ,VA}.{EST(-2.5~2.5),SC(0-100),SE,SR}
- **tos_notes**: CC BY 4.0
- **update_frequency**: 年更(source lastupdated=2026-03-18)
- **value_for_investment**: high — 政治稳定/政府效能/监管质量/法治/腐败控制,39 个样本国齐全,适合做国别风险雷达
- **notes**: 坑:网上教程几乎全是旧代码(PV.EST),必须用 GOV_WGI_ 前缀且显式 source=3。
### IMF DataMapper API(WEO 2026 年 4 月版,含预测至 2031) — https://www.imf.org/external/datamapper/api/v1/{indicator}
- **category**: 国别指标
- **access_method**: API(JSON),免 key
- **verified**: True
- **probe_evidence**: 带 'Mozilla/5.0…' UA → 403 Access Denied;默认 curl UA 或自定义简单 UA → 200(121,723 bytes)。URL 路径里的国家过滤被忽略,始终返回全部 229 个经济体,需客户端过滤
- **key_fields**: values.{indicator}.{ISO3}.{year}=value;/indicators 返回 132 个指标及 source 标签(实测标注 'World Economic Outlook (April 2026)')
- **tos_notes**: IMF 数据可免费使用须署名;Akamai 反爬:伪装浏览器 UA 直接 403
- **update_frequency**: 每年 4 月、10 月两次(1/7 月小更新只含主要经济体)
- **value_for_investment**: high — 唯一免费、39 个样本国齐全的前瞻预测源(GDP 增速/通胀/政府债务/财政余额/经常账户/人口),且补上世行 CPI、政府债务的缺口
- **notes**: 坑 1:不要伪造浏览器 UA(仓库 common.py 的 USER_AGENT 若含 Mozilla 字样需为此源单独设置)。坑 2:土库曼等国官方数据可信度存疑,报告中应脚注'IMF 估计'。需给每个值打 is_forecast 标记(年份>当年)。
### UN World Urbanization Prospects 2025 — 城市库(DEGURBA Cities DB) — https://population.un.org/wup/assets/Download/Cities/WUP2025-DB-DEGURBA-Cities-Population-Surface-Data.csv.gz
- **category**: 城市数据
- **access_method**: 批量下载(CSV.gz 58.7MB;另有 F21 等 xlsx)
- **verified**: True
- **probe_evidence**: curl HEAD 200 content-length 58,726,569;下载后 duckdb read_csv(quote='"', types Notes=VARCHAR) 统计(脚本 scratchpad/wup_an.py)。下载地址从站点 JS bundle main-YFF5NQE7.js 中 grep 'assets/Download/' 得到(页面为 Angular SPA,HTML 里无链接)
- **key_fields**: ISO3_Code, ISO2_Code, City_Code, City_Name, Capital(0/1), Year, PWCent_Latitude/Longitude, Pop_plausibility(Low/Moderate/High), Pop(千人), AREA_km2, Pop_density, BU_km2, percPop(占全国人口%), Pop_rate(年增速%)
- **tos_notes**: UN 数据,署名即可(UN Terms of Use;页面称 Data Portal 有 open API,但 WPP 同门户 API 实测 401 需 token)
- **update_frequency**: 不定期大修(2018→2025),视为一次性导入;文件 last-modified 2025-12-01
- **value_for_investment**: high — 城市层主数据:39 个样本国全覆盖、≥5 万人城市、1975–2050 逐年人口+增速+占全国比重+面积密度,一份文件解决'Top N 城市'
- **notes**: 坑:①国名含逗号('China, Hong Kong SAR')必须按带引号 CSV 解析,duckdb auto-detect 会失败;②Pop 单位=千人;③采用'城市化程度 DEGURBA'统一口径(基于 GHSL 栅格的连片城区),与各国行政口径差异大:金沙萨 1094 万 vs 世行 EN.URB.LCTY 1778 万;④个别命名怪异(刚果(金)第二大为 'Kasaï-Oriental'=姆布吉马伊),Top N 需一张人工别名表;⑤5,512 城 Pop_plausibility=Low,要在前端标注。
### UN WUP 2025 — 国家级城乡人口库(National Definitions + DEGURBA Level1/2) — https://population.un.org/wup/assets/Download/Countries%20and%20Aggregates/WUP2025-DB-National-Definitions-Population-Data.csv.gz
- **category**: 国别指标
- **access_method**: 批量下载(CSV.gz 3.4MB)
- **verified**: True
- **probe_evidence**: 下载后 duckdb 统计:count(distinct ISO3)=39, Year 1950–2050;与世行 SP.URB.TOTL.IN.ZS 2025 值逐国比对,39/39 样本国 差值<0.2pt → 世行城镇化率已切到 WUP2025 国家口径
- **key_fields**: ISO3_Code, Category(Urban/Rural/Total), Year, Pop(千人), percPop, Pop_rate, percPop_rate
- **tos_notes**: UN 署名
- **update_frequency**: 同 WUP 大修周期
- **value_for_investment**: high — 城镇化率 1950–2050 预测,'未来 25 年城镇化增量'是水务需求最直观的领导汇报指标
- **notes**: 历史值直接用世行 API 即可,本文件只为拿 2030/2035/2050 预测。
### UN World Population Prospects 2024(人口预测) — https://population.un.org/wpp/assets/Excel%20Files/1_Indicator%20(Standard)/CSV_FILES/WPP2024_Demographic_Indicators_Medium.csv.gz
- **category**: 国别指标
- **access_method**: 批量下载(CSV.gz 16.6MB);Data Portal API 需 token
- **verified**: True
- **probe_evidence**: curl -I 批量 CSV → 200;curl https://population.un.org/dataportalapi/api/v1/data/indicators/49/locations/148/… → 401(需向 UN 申请 Bearer token)
- **key_fields**: ISO3_code, Time, TPopulation1July, PopGrowthRate, PopDensity, MedianAgePop …
- **tos_notes**: CC BY 3.0 IGO
- **update_frequency**: 两年一版(2022、2024,下一版预计 2026 年内——未验证)
- **value_for_investment**: medium — 长期人口预测(至 2100);短中期已被 IMF LP(至 2031)和 WUP2025(至 2050,与 WPP2024 一致)覆盖,MVP 可不单独接
- **notes**: 不要走 Data Portal API,直接用 CSV。
### WHO/UNICEF JMP(washdata.org)国别文件 — https://washdata.org/data/country/{ISO3}/household/download
- **category**: 国别指标
- **access_method**: 批量下载(每国 1 个 xlsx ≈6MB,无 API)
- **verified**: True
- **probe_evidence**: curl -L …/country/SLB/household/download → 200, 5,845,847 bytes xlsx;openpyxl(系统 python3)读出 16 个 sheet 名与 Estimates 表头;下载索引页 grep 出 238 个 household 链接并与 39 个样本国 ISO3 比对无缺
- **key_fields**: Sheet 'Estimates':Country, Year(2000–2024), Setting(Total/Urban/Rural), Population, Improved, At least basic, Limited, Unimproved, Surface water, Piped, Non-piped, Accessible on premises, Available when needed, Free from contamination;另有 Sanitation/Wastewater Data/Population 等 16 个 sheet
- **water_filter**: 全部即水务指标;取 Setting=Urban 行作为城市供排水缺口代理
- **tos_notes**: WHO/UNICEF 公开数据,署名使用;无公开 API,按国循环下载 39 次即可,注意限速
- **update_frequency**: 两年一次 household 更新(当前为 2025 update,数据到 2024;下次约 2027)
- **value_for_investment**: high — 供排水现状的权威源;比世行镜像多出'管道供水比例/入户/按需可得/污水处理'以及城乡分列,是判断管网与水厂缺口的关键
- **notes**: 安全管理(SMDW/SMSS)在约 15 个样本国无估计值(含所罗门/东帝汶/PNG/多数西非中非国),前端必须'标缺'而非填 0;此时用 basic + piped 两项替代。仓库 .venv 未装 openpyxl(需加依赖)。
### FAO AQUASTAT(经 World Bank Data360 API 镜像 FAO_AS) — https://data360api.worldbank.org/data360/data?DATABASE_ID=FAO_AS&INDICATOR=FAO_AS_4270&REF_AREA={ISO3,逗号}&timePeriodFrom=2000
- **category**: 国别指标
- **access_method**: API(JSON);AQUASTAT 原站为 SPA,仅网页内 Bulk download,未找到公开 API
- **verified**: True
- **probe_evidence**: GET /data360/indicators?datasetId=FAO_AS → 112 个代码;POST /data360/searchv2 取指标名;逐指标拉 39 个样本国统计最新年与缺失国(沙特 2022:污水处理 1.66、淡化 2.137 ×10^9 m3)。AQUASTAT 原站 data.apps.fao.org/aquastat 返回 4.7KB SPA 壳,主 JS 仅 25KB 无端点
- **key_fields**: REF_AREA(ISO3), TIME_PERIOD, OBS_VALUE, UNIT_MEASURE(如 M3_Y=10^9 m3/年);分页 skip,每页 1000
- **water_filter**: 4269 产生的市政污水 / 4270 处理的市政污水 / 4491 未处理 / 4264 淡化水 / 4253 总取水 / 4551 用水效率
- **tos_notes**: FAO 数据 CC BY-NC-SA 3.0 IGO(注意 NC:内部研究报告可用,对外商用产品需评估)——许可条款未逐字核对,标记未验证
- **update_frequency**: AQUASTAT 年更,Data360 镜像滞后(当前最新 2022)
- **value_for_investment**: medium-high — 市政污水产生量/处理量、海水淡化量、总取水量:可算'污水处理率''淡化规模',直接对应污水厂/淡化厂机会;但覆盖不齐
- **notes**: 4251 市政取水量不在 Data360 子集里,用世行 ER.H2O.FWDM.ZS(生活取水占比,37/39)×总取水量代替。
### OECD 国家风险分类(Country Risk Classification) — https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/country-risk-classification/cre-crc-current-english.pdf
- **category**: 风险评级
- **access_method**: PDF 下载(6 页,pdftotext 可解析);HTML 页面被 Cloudflare 挡
- **verified**: True
- **probe_evidence**: curl PDF → 200 application/pdf 766,147 bytes;pdftotext -layout 后正则按 ISO3 匹配 39/39 样本国 行;人工核对 POL/SLB 两行为 '-' + 脚注
- **key_fields**: ISO Alpha-3, Country Name, Previous, Current Prevailing, Notes
- **tos_notes**: OECD 公开文件,可引用;oecd.org HTML 页 curl 与 WebFetch 均 403('Just a moment…' Cloudflare 质询)——不要尝试绕过,直接取 content/dam 下 PDF(实测 200, 766KB)
- **update_frequency**: 一年数次(历史版 PDF:cre-crc-historical-internet-english.pdf)
- **value_for_investment**: high — 0–7 级官方出口信贷国别风险,国内银行/信保定价的通用参照,领导层熟悉
- **notes**: 解析坑:未评级行的脚注编号 '(5)/(6)' 会被误当成等级,解析器要区分。PDF 版式若变需人工兜底(一年几次,手工维护成本也很低)。
### IMF/WB 低收入国家债务可持续性清单(LIC DSA List) — https://www.imf.org/external/pubs/ft/dsa/dsalist.pdf
- **category**: 风险评级
- **access_method**: PDF 下载(65KB,pdftotext 可解析)
- **verified**: True
- **probe_evidence**: curl → 200 application/pdf 64,885 bytes;pdftotext -layout(文本为 latin-1 编码,utf-8 解码会报错)后按国名匹配出 22 行
- **key_fields**: Country, Latest publication date, Risk of external debt distress, Debt sustainability assessment, Joint with WB
- **tos_notes**: IMF 公开文件;默认 curl UA 可取
- **update_frequency**: 约每月/每季更新一次
- **value_for_investment**: high — '外债困境风险'直接决定该国能否再举主权债、项目是否只能走赠款/优贷,影响投建营回款判断
- **notes**: 国名匹配要防 'Guinea' 误中 'Guinea-Bissau'/'Papua New Guinea';建议维护一张英文名→ISO3 映射。
### GeoNames cities15000 / cities5000 / cities1000 — https://download.geonames.org/export/dump/cities15000.zip
- **category**: 城市数据
- **access_method**: 批量下载(zip:15000=3.36MB,5000=5.70MB,1000=10.96MB;alternateNamesV2=204.7MB)
- **verified**: True
- **probe_evidence**: curl -I 取各 zip 的 Content-Length;下载 15000/5000 两个 zip 后 python 按 country_code 统计 39 个样本国城数、≥10 万城数及小国 Top4
- **key_fields**: geonameid, name, asciiname, alternatenames, lat, lon, feature_code(PPLC/PPLA…), country_code(ISO2), admin1, population, modification_date
- **tos_notes**: CC BY 4.0,须署名
- **update_frequency**: 每日 dump(实测 Last-Modified=2026-09-21)
- **value_for_investment**: medium — 作为城市'主键+坐标+别名'骨架和小国次级城镇补充很稳;但人口字段陈旧,不能当人口源
- **notes**: 仓库已有 scripts/ingest/cities_global.py(cities1000→cities.parquet,只收行政中心 PPLC/PPLA/PPLA2/PPLA3)与 geonames.py 可直接复用下载/解析逻辑。中文名可从 alternateNamesV2(204MB,isolanguage=zh)或 Wikidata 取。
### Africapolis(OECD/SWAC 非洲城市集聚区) — https://africapolis.org/data/statistics/json/africapolis_agglomeration.json
- **category**: 城市数据
- **access_method**: 站点静态 JSON(33.6MB,gzip 传输 5.2MB;非官方文档化接口),官网另有按国 CSV 下载按钮
- **verified**: True
- **probe_evidence**: 从 /assets/index-C9RF2-oY.js 中正则找到 data/statistics/json/*.json;curl --compressed → 200,列表 12,904 条;python 统计 20 国数量与 Top3
- **key_fields**: Agglomeration_ID, Agglomeration_Name, ISO3, Longitude, Latitude, Population_1950…2020(实测), Population_2025…2050(预测), Built up_2015…2050, Metropole, Dist to Metro, 绿地/PM2.5/路网等
- **tos_notes**: 许可未核实(未验证);该 JSON 是前端内部资源,URL 可能随发版变化,生产接入应改用官网下载的 CSV 并本地存档
- **update_frequency**: 约 5 年一轮(2015、2020 基准;2025 年新增 2050 预测;文件 Last-Modified 2026-04-22)
- **value_for_investment**: medium — 非洲 20 个样本国的次级城市(1 万人以上)+2025–2050 人口/建成区预测,用来发现'二线城市供水'机会;但口径特殊不宜直接上领导报告
- **notes**: 大坑:'形态学集聚区'把连片高密度农村算成一个城——Kisumu 1756 万(>内罗毕 1596 万)、Onitsha 1035 万、Mbale 415 万、Gisenyi 147 万,与常识严重不符;只适合做次级城镇清单,人口展示仍以 WUP2025 为准。
### GHSL Urban Centre Database GHS-UCDB R2024A(JRC) — https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_UCDB_GLOBE_R2024A/GHS_UCDB_GLOBE_R2024A/V1-0/GHS_UCDB_GLOBE_R2024A_V1_0.zip
- **category**: 城市数据
- **access_method**: 批量下载(zip 298MB,GeoPackage;另有 MTUC 多时相包 35MB)
- **verified**: False
- **probe_evidence**: 仅实测目录索引(200):V1-0 下 GHS_UCDB_GLOBE_R2024A_V1_0.zip 298M(2024-11-25)、MTUC 35M;读取 copyright.txt;因体积大且与 WUP2025 重叠,未下载
- **key_fields**: (据文档,未验证)ID_UC_G0, 名称, 国别, 多期人口/建成区, 主题属性(气候、暴露、绿地、GDP 等)
- **tos_notes**: 实测 copyright.txt:CC BY 4.0,署名 European Union
- **update_frequency**: R2019A→R2024A,约 5 年
- **value_for_investment**: low(现阶段)— WUP2025 城市库本身就是基于 GHSL/DEGURBA 的,且已带人口序列;UCDB 的增量价值在城区边界多边形与气候/暴露主题属性,等做 GIS 层再接
- **notes**: 使用方已明确'先不管 GIS',建议 P2 再评估。
### Wikidata SPARQL — https://query.wikidata.org/sparql
- **category**: 城市数据
- **access_method**: API(SPARQL/JSON)
- **verified**: True
- **probe_evidence**: curl -G --data-urlencode query@wd.rq → 200, 29.9KB, 3.96s,40 行;第二条按国计数查询 1.8s 返回 6 国
- **key_fields**: ?city, rdfs:label@zh, P1082 人口 + P585 时点, P1566 GeoNames ID, P6 市长, P625 坐标
- **tos_notes**: CC0;需自定义 User-Agent;单查询 60s 超时,须分页/按国 VALUES
- **update_frequency**: 实时
- **value_for_investment**: medium — 价值在'中文名 + GeoNames ID 对照 + QID',不是人口;水司实体几乎为空
- **notes**: P31/P279* 路径会产生重复行,需 DISTINCT/按 QID 去重。仓库已有 scripts/ingest/wikidata.py(run_sparql 带缓存重试)可复用。
### citypopulation.de — https://www.citypopulation.de/
- **category**: 城市数据
- **access_method**: HTML(人工查阅);无 API/批量
- **verified**: True
- **probe_evidence**: curl 首页 200;读取 termsofuse 页面正文与 robots.txt
- **key_fields**: 城市/行政区名、历次普查人口、来源脚注
- **tos_notes**: 实测条款页 /en/help/termsofuse/:人口数据可商用与改编,须按 'Thomas Brinkhoff: City Population' 署名(等同 CC BY 3.0),并遵守原始统计机构版权;地图/坐标数据不提供。robots.txt 明确 Disallow ClaudeBot/anthropic-ai/GPTBot 等 AI 爬虫 → 不要做自动抓取
- **update_frequency**: 随各国普查
- **value_for_investment**: low-medium — 各国最新普查的城市/行政区人口,适合人工核对小国(所罗门、东帝汶、PNG)二线城镇,不适合自动化
- **notes**: 仅作人工引用源,写入报告脚注即可。
### World Bank B-READY(Doing Business 的继任,经 WDI 代码 IC.BRE.*) — https://api.worldbank.org/v2/country/{iso3;…}/indicator/IC.BRE.US.OS?format=json&mrnev=1
- **category**: 国别指标
- **access_method**: API(JSON)
- **verified**: True
- **probe_evidence**: Data360 searchv2 搜到 WB_WDI_IC_BRE_US_* ;WB API 实测 IC.BRE.US.OS n=13、year=2024
- **key_fields**: IC.BRE.US.OS(公用事业总分)/ IC.BRE.US.P1-P3(监管质量/治理透明/运营效率)
- **tos_notes**: CC BY 4.0
- **update_frequency**: 年更,覆盖国逐年扩充(计划 2026 年达 ~180 国——未验证)
- **value_for_investment**: low(现阶段)— 含'公用事业服务(水/电/网)'专题评分,概念很贴,但目前只覆盖 13/39
- **notes**: 先不进最小指标集,留代码位,等覆盖扩大后自动补齐。
### open.er-api.com(即期汇率) — https://open.er-api.com/v6/latest/USD
- **category**: 国别指标
- **access_method**: API(JSON),免 key
- **verified**: True
- **probe_evidence**: curl → result=success, 2026-09-21 更新, n=166;集合差为空
- **key_fields**: rates.{CCY}, time_last_update_utc
- **tos_notes**: 免费层需署名、限频(具体条款未逐字核对——未验证);官方管制汇率(TMT 3.53)≠市场汇率
- **update_frequency**: 每日
- **value_for_investment**: medium — 补世行年均汇率的陈旧缺口(土库曼 2001、几内亚 2020),项目金额本币→美元/人民币换算必需
- **notes**: 土库曼、尼日利亚等存在官方/平行汇率差,报告里应注明。
### IBNET(世行水务公用事业对标网络:水价、漏损、覆盖率,按水司/城市) — https://newibnet.org/
- **category**: 城市数据
- **access_method**: 需浏览器/可能需登录(Cloudflare 质询)
- **verified**: False
- **probe_evidence**: curl -L 四个候选 URL:403/000/403/000
- **tos_notes**: newibnet.org 对 curl 返回 403 'Just a moment…'(Cloudflare bot 质询),按纪律不绕过;旧域名 www.ib-net.org / tariffs.ib-net.org 连接失败(000)
- **update_frequency**: 未验证
- **value_for_investment**: high(若能拿到)— 唯一成体系的水司级运营数据(NRW、水价、回收率),正是投建营尽调要的;但可得性未证实
- **notes**: 建议使用者本人用浏览器登录确认是否有导出功能,再决定是否纳入;在此之前'水司/水价'层只能靠项目文件(世行 PAD)与人工整理。
### OECD-EC Functional Urban Areas(FUA) — https://www.oecd.org/en/data/datasets/oecd-definition-of-cities-and-functional-urban-areas.html
- **category**: 城市数据
- **access_method**: 未实测(oecd.org 有 Cloudflare)
- **verified**: False
- **probe_evidence**: 未实测:oecd.org HTML 页面统一 403,且覆盖面与本项目 39 个样本国错位,优先级低
- **value_for_investment**: low — 官方 FUA 只覆盖 OECD 成员及少数伙伴国(39 个样本国里大致只有波兰、土耳其、哥伦比亚、秘鲁/巴西部分),对非洲/中亚/太平洋岛国无用
- **notes**: 不建议接入。
### 现有仓库资产 water-tracker(可直接复用) — static/data/ 与 scripts/ingest/
- **category**: 国别指标
- **access_method**: 本地 parquet + Python 入库脚本
- **verified**: True
- **probe_evidence**: scripts/.venv/bin/python + duckdb 1.5.3 describe/统计各 parquet(脚本 scratchpad/repo_an.py、repo_an2.py);sed 阅读 worldbank_country_stats.py 头 80 行、cities_global.py 头 60 行、wikidata.py 的 UTILITIES_QUERY
- **key_fields**: scripts/ingest/worldbank_country_stats.py:INDICATORS 字典(现仅 NY.GDP.MKTP.CD、SP.POP.TOTL)、DATE_RANGE='2013:2025'、fetch_json 重试、用 /v2/country/all 的 region.id=='NA' 剔除聚合区;scripts/ingest/common.py:DATA_DIR/USER_AGENT/IngestReport/write_parquet;前端 src/lib/widgets/CountryEconPanel.svelte(+.ts)与 src/routes/country/[iso2]/+page.svelte 已消费 country_stats
- **tos_notes**: —
- **update_frequency**: 手动跑脚本
- **value_for_investment**: high — 世行抓取脚本、GeoNames/Wikidata 管线、国家页经济面板都已存在,背景层是'扩指标'而不是'从零造'
- **notes**: 建议:不要把 country_stats 加宽成 20 列,新建长表 country_indicators.parquet(iso3, indicator_key, year, value, source, is_forecast, fetched_at),旧表保留给现有面板;countries.parquet 补 iso3 + 39 个样本国中文名 + 区域(使用方自定义区域)+ 世行收入组/贷款类别;.venv 缺 openpyxl/pdf 解析库(JMP xlsx、OECD/DSA PDF 需要)。
### 商务部《对外投资合作国别(地区)指南》/ 中国信保国家风险评级 — http://fec.mofcom.gov.cn/article/gbdqzn/
- **category**: 中文渠道
- **access_method**: HTML/PDF(人工);信保评级为付费/内部
- **verified**: False
- **probe_evidence**: curl -L 旧地址 → 404(跳到 '海综服务平台-在线办事' 页),新地址未找到;信保评级无公开接口,未测
- **value_for_investment**: medium — 中文定性背景(法律、税务、劳工、中资存量),适合报告引用;非结构化
- **notes**: 中国信保国别风险评级不公开;使用方若有订阅可作为 profile 私有输入,公开数据层只留'外部链接/附件'位。