# profiles/

一个 profile = 一个使用方的全部个性化配置。core 只读它,不知道它属于谁。

- `demo/`:按公开准则定义(东非 IDA 合格国家)的演示配置,供开发与测试。
- `example/`:自己部署用的样板,复制到私有配置仓根目录改名 `profile.yaml`(见 docs/部署指南.md)。
- 使用方的真实 profile:在**私有配置仓**里(另一个仓库),检出到本仓库的 `private/` 目录(`.gitignore` 已排除),命令用 `--profile private/profile.yaml` 指向。本仓库不含它。
- **不要把真实 profile 复制进本目录**——`.gitignore` 只放行 `demo/` 与 `example/`。

profile 里可以有、而 core 永远不碰的东西:国别清单与分档、行业口径、金额门槛、报告模板与固定用语、投标人国籍、内部流程周期、内部信息叠加(在地资源、已掌握情况、责任单位)、线索台账。

台账相关字段(可选):

```yaml
countries:
  - { iso2: AZ, iso3: AZE, ..., ledger: countries/AZE/ledger.yaml, overlay: overlay/AZE.yaml }  # 路径相对 profile 所在目录(台账放私有配置仓)
bidder:
  nationality: XXX          # 投标资格按规则表的成员国规则判断
  local_entity: {}          # 已在当地注册实体的国别,如 {AZE: branch}
lead_times:                 # 内部流程周期(天),最晚启动日期 = 下一节点 − 周期
  pq_deadline: {准备: 28, 审批: 14}
  local_registration: 150   # 仅对"投标前须当地注册"的采购制度计入
procurement_rules: ...      # 可选,默认 reference/procurement_rules.yaml
data_dir: ../data/private     # 可选,该 profile 的快照目录;真实 profile 应与 demo 分开,避免互相覆盖
teable:                     # 可选,部门共用的多维表格(deploy/teable/README.md);不写时取缺省
  base: 水务项目机会         # 库名,按名找
  pull: false               # 是否把同事在表里改的人管字段拉回系统
  internal_reports: false   # 表格「报告」里是否也传含内部信息的报告(月报内部版、国别动态、线索简报);缺省只传周报与月报公开版
  notify_owner: false       # 同事被设为负责人时 Teable 站内通知本人(推送时打开两张表「负责人」字段的通知);缺省不动
```
