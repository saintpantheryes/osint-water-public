"""portal.js 的 P.records:第一页满了以后,后面的页同时取(城市表三四千行,一页等一页要多走好几轮跨洋来回),结果与顺序不变。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "portal" / "portal.js"

RUN = r"""
const fs = require("fs");
global.window = {}; global.document = { documentElement: { classList: { add() {} } } };
global.location = { pathname: "/", search: "" };
new Function(fs.readFileSync(process.argv[1], "utf8"))();
const P = window.P;
P.cfg = { tables: { city: { id: "tblC", fields: { name: "fldN" } } } };
const N = Number(process.argv[2]), calls = []; let live = 0, peak = 0;
P.api = async (path, q) => {
  calls.push(q.skip); live++; peak = Math.max(peak, live);
  await new Promise((r) => setTimeout(r, 5)); live--;
  const recs = []; for (let i = q.skip; i < Math.min(N, q.skip + q.take); i++) recs.push({ id: "r" + i });
  return { records: recs };
};
(async () => {
  const all = await P.records("city", {});
  const capped = await P.records("city", { take: 2500 });
  console.log(JSON.stringify({ n: all.length, ordered: all.every((r, i) => r.id === "r" + i), capped: capped.length, peak, calls: calls.length }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
@pytest.mark.parametrize("rows,peak", [(3823, 4), (999, 1), (1000, 4), (0, 1)])
def test_records_fetches_later_pages_together(rows, peak):
    out = subprocess.run(["node", "-e", RUN, str(JS), str(rows)], capture_output=True, text=True, check=True).stdout
    r = json.loads(out)
    assert r["n"] == rows and r["ordered"]
    assert r["capped"] == min(rows, 2500)
    assert r["peak"] == peak  # 一页不满就一次;满了后面四页一起发
