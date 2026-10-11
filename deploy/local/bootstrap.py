"""单机版的初始化(在后台任务的镜像里跑,由 deploy/local/start.sh 调用;可重复运行):
等表格起来 → 注册管理员、关掉自助注册 → 建空间与接口令牌 → 写 state/ADMIN.txt 与 state/worker.env → 按本体建库建表(只增不删)。
管理员密码与令牌只写进 state/ 下只有本人能读的文件,不打印。"""
from __future__ import annotations

import os
import secrets
import sys
import time
from datetime import date
from pathlib import Path

import httpx

TEABLE = os.environ.get("TEABLE_URL", "http://teable:3000").rstrip("/")
STATE = Path(os.environ.get("OW_STATE", "/state"))
EMAIL = os.environ.get("OW_ADMIN_EMAIL", "admin@example.com")
SITE = os.environ.get("OW_SITE", "http://localhost:8080")
SCOPES = ["space|read", "base|create", "base|read", "base|update", "table|create", "table|read", "table|update",
          "view|create", "view|read", "view|update", "field|create", "field|read", "field|update",
          "record|create", "record|read", "record|update", "record|delete"]


def wait_for_teable(c: httpx.Client) -> None:
    for _ in range(90):
        try:
            if c.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    sys.exit("表格 3 分钟内没起来:docker compose -p osint-water-local logs --tail 50 teable")


def secret_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def admin_token(c: httpx.Client) -> str:
    """已有 ADMIN.txt 就用里面的令牌;没有就注册管理员、建空间和令牌。"""
    admin = STATE / "ADMIN.txt"
    if admin.exists():
        for line in admin.read_text(encoding="utf-8").splitlines():
            if line.startswith("接口令牌:"):
                return line.split(":", 1)[1].strip()
        sys.exit(f"{admin} 里没有接口令牌")
    pw_file = STATE / ".admin_pw"  # 先存下:万一半途失败,重跑还能登录
    if not pw_file.exists():
        secret_file(pw_file, secrets.token_urlsafe(12))
    pw = pw_file.read_text(encoding="utf-8").strip()
    r = c.post("/api/auth/signup", json={"email": EMAIL, "password": pw, "defaultSpaceName": "水务项目机会"})
    if r.status_code not in (200, 201):
        r = c.post("/api/auth/signin", json={"email": EMAIL, "password": pw})
        if r.status_code not in (200, 201):
            sys.exit(f"注册和登录管理员都没成功(HTTP {r.status_code})。表格里没有数据的话,"
                     "可以 docker compose -p osint-water-local down -v 清空后重跑")
    spaces = c.get("/api/space").raise_for_status().json()
    space_id = spaces[0]["id"] if spaces else c.post("/api/space", json={"name": "水务项目机会"}).raise_for_status().json()["id"]
    exp = date.today().replace(year=date.today().year + 10).isoformat()
    tok = c.post("/api/access-token", json={"name": "osint-water local", "description": "单机版后台任务与建表",
                                            "scopes": SCOPES, "spaceIds": [space_id], "expiredTime": exp})
    tok.raise_for_status()
    token = tok.json()["token"]
    secret_file(admin, f"网址:            {SITE}/portal/\n管理员邮箱:      {EMAIL}\n管理员密码:      {pw}\n"
                       f"接口令牌:        {token}\n空间 ID:         {space_id}\n")
    pw_file.unlink(missing_ok=True)
    s = c.patch("/api/admin/setting", json={"disallowSignUp": True})  # 单机也关掉自助注册
    print("  管理员已建好" + ("" if s.status_code in (200, 201, 204) else f"(没能关掉自助注册:HTTP {s.status_code})"))
    return token


def main() -> int:
    with httpx.Client(base_url=TEABLE, timeout=60) as c:
        wait_for_teable(c)
        token = admin_token(c)
    wenv = STATE / "worker.env"
    keep = [ln for ln in (wenv.read_text(encoding="utf-8").splitlines() if wenv.exists() else [])
            if not ln.startswith(("TEABLE_URL=", "TEABLE_TOKEN="))]
    secret_file(wenv, "\n".join([f"TEABLE_URL={TEABLE}", f"TEABLE_TOKEN={token}", *keep]) + "\n")
    os.environ.update(TEABLE_URL=TEABLE, TEABLE_ADMIN_TOKEN=token)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 程序根目录(脚本在 deploy/local/ 下)
    from core import schema
    return schema.main(["--profile", "private/profile.yaml", "--config", "private/portal/config.json"])


if __name__ == "__main__":
    sys.exit(main())
