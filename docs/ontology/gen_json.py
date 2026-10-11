"""把 ontology.yaml 整个转成页面能读的 portal/ontology.json(页面与 Caddy 只认 portal/ 目录;docs/ 不对外)。
页面(portal.js 的 P.onto)在运行时读它:阶段顺序、「跟踪中」口径、资金来源归组、区域的「未划区」、分类体系的定义与说明都从这里来,
不再在脚本里抄一份。改本体后重跑:python docs/ontology/gen_json.py;--check 只检查是否已同步(tests/test_ontology.py 用)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "ontology.yaml"
JSON_PATH = HERE.parent.parent / "portal" / "ontology.json"


def _plain(x):
    """YAML 里没加引号的日期会被读成 date;页面只要文字。"""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain(v) for v in x]
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x


def render(onto: dict) -> str:
    return json.dumps(_plain(onto), ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str]) -> int:
    onto = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    new = render(onto)
    if "--check" in argv:
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != new:
            print("portal/ontology.json 与 ontology.yaml 不同步:运行 python docs/ontology/gen_json.py")
            return 1
        return 0
    JSON_PATH.write_text(new, encoding="utf-8")
    print("已重生成", JSON_PATH.relative_to(HERE.parent.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
