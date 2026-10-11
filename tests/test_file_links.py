"""附件链接:表格存的签名地址带着生成时的网址,换域名后旧网址打不开。页面和后台都只取路径,在本站/表格本身打开。"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.jobs import reports

PORTAL = Path(__file__).resolve().parents[1] / "portal"
OLD = "https://water.old.example/api/attachments/read/private/table/Iwy?token=abc&response-content-disposition=x"

RUN = r"""
const fs = require("fs");
global.window = {}; global.document = { documentElement: { classList: { add() {} } } };
global.location = { pathname: "/", search: "", origin: "https://osint.new.example" };
new Function(fs.readFileSync(process.argv[1], "utf8"))();
const P = window.P;
console.log(JSON.stringify(JSON.parse(process.argv[2]).map((a) => P.fileUrl(a))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_file_links_open_on_this_site_whatever_host_was_stored():
    atts = [{"presignedUrl": OLD}, {"presignedUrl": "/api/attachments/read/private/table/B?token=t"},
            {"url": "https://example.org/notice/1"}, {}, None]
    out = subprocess.run(["node", "-e", RUN, str(PORTAL / "portal.js"), json.dumps(atts)], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["/api/attachments/read/private/table/Iwy?token=abc&response-content-disposition=x",
                               "/api/attachments/read/private/table/B?token=t", "https://example.org/notice/1", "#", "#"]


def test_pages_link_files_only_through_the_helper():
    for f in PORTAL.glob("*.html"):
        assert not re.search(r"presignedUrl", f.read_text(encoding="utf-8")), f.name


def test_worker_downloads_attachments_from_the_table_not_the_stored_host():
    got = []

    class Http:
        def get(self, url, follow_redirects=True):
            got.append(url)
            return SimpleNamespace(raise_for_status=lambda: None, content=b"PK")

    ctx = SimpleNamespace(api=SimpleNamespace(url="http://teable:3000", http=Http()))
    assert reports._download(ctx, {"presignedUrl": OLD}) == b"PK"
    reports._download(ctx, {"presignedUrl": "/api/attachments/read/private/table/B?token=t"})
    assert got == ["http://teable:3000/api/attachments/read/private/table/Iwy?token=abc&response-content-disposition=x",
                   "http://teable:3000/api/attachments/read/private/table/B?token=t"]
