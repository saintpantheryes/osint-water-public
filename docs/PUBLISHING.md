# 公开核心仓与私有配置仓

2026-10-06 起,系统拆成两个仓库(此前的「择机公开」检查清单已落实为长期结构):

| 仓库 | 内容 | 可见性 |
|---|---|---|
| **公开核心仓**(本仓库) | `core/`(含缺省报告模板 `core/templates/`)、`portal/`(不含 `config.json`)、`reference/`、`global/`、`profiles/demo/` 与 `profiles/example/`、`docs/`、`tests/`、`deploy/`、`scripts/` | 现为私有;内容按公开标准维护,随时可改为公开 |
| **私有配置仓** | 真实 `profile.yaml`、`countries/`(国别台账与国别信源,2026-10-11 从核心仓移来)、`markets.yaml`(市场分布表)、`team.yaml`(岗位代号表)、`overlay/`(内部叠加)、`templates/`(Word 版式)、`portal/config.json`(库、表、字段编号)、`materials/`、`reports/`、`migration/`、`privacy_terms.txt`(敏感词清单);`data` 分支(旧监测状态) | 永久私有 |

运行时两者合在一起:私有配置仓检出到核心仓的 `private/` 目录(本机用软链接;主机上 compose 把它挂到容器里的 `/opt/osint-water/private`,把 `portal/config.json` 挂到页面目录)。程序只认 `private/profile.yaml` 这条路径。

## 核心仓里的三道闸

1. `scripts/privacy_scan.py`:公网 IP、邮箱、私钥、令牌、密钥文件名;有私有检出时再查 `private/privacy_terms.txt` 里的敏感词(使用方名称、域名、人名)。CI 每次提交都跑,`tests/test_privacy.py` 本地也跑。
2. `.gitignore` 排除 `private/`、`portal/config.json`、`*.pem`、`.env`、`data/`。
3. 文档与注释一律写「使用方」「经营区域」「<主机IP>」「<站点域名>」,不写具体名称。

## 如果要把核心仓改为公开

2026-10-11 核查:核心仓的历史里没有主机地址、域名、帐号邮箱、令牌与表格编号,但早期提交里有使用方的工作成果
(国别台账与竞争分析、内部规划文档、带岗位代号的测试数据)。这些已在 2026-10-11 移到私有配置仓,**但仍在历史里**,
所以不能直接把本仓库改为公开。做法二选一(由使用方决定):

1. **新建公开仓库(推荐)**:用当前 `main` 的工作区建一个只有一次提交的新仓库并公开;主机改从新仓库更新
   (`worker_from_mac.sh` 取本检出的 origin,在新仓库的检出里重跑一次即可),本仓库保留为私有存档。
2. **改写本仓库的历史**:`git filter-repo` 或压成一次提交后强推 `main`,再改为公开;所有检出(主机、本机)要重新克隆。

无论哪种,公开前:本地有私有检出时跑一次 `python scripts/privacy_scan.py`(带敏感词),再全文检索一遍人名与部门名。

## 旧仓库的历史

旧仓库(现为私有配置仓)的 git 历史里仍有程序代码与私有内容并存的提交。它永久私有,不需要改写;若将来要缩小体积,再用 `git filter-repo` 只保留私有配置的路径。

## 许可

代码 AGPL-3.0(见 `LICENSE`);`docs/` 与 `reference/` CC BY 4.0;各数据源保留其自身许可,报告按各源要求署名。
