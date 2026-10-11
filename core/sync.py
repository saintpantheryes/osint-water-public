"""本机界面与 GitHub 的同步(git)。

数据目录(private/profile.yaml 的 data_dir,即 data/private)是 data 分支的检出:
- 打开界面时拉取(定时任务每天在 GitHub 上写入新线索、提醒页与报告);
- 界面上的操作(标记处理、保存筛选、增删信源、人工提交、生成报告)写在定时任务不碰的文件里,提交后推送;
- 推送前先拉取并变基;万一同一文件两边都改了,以本机为准(-X theirs 在变基中指正在重放的本机提交)。
  只追加的文件(操作记录 watch/events.jsonl、自动写入记录 watch/ledger_auto.jsonl、表格同步状态 watch/teable_state.jsonl)
  例外:两边新加的行都保留(union 合并);合并规则写在 data 分支根目录的 .gitattributes(定时任务提交时确保有,commit_data),
  本机 .git/info/attributes 另有一份。整文件重写的"线索号 → 记录"文件(watch/triage.yaml、watch/ledger_reviewed.yaml)
  按键三方合并(core/yamlmerge.py 作合并驱动,配置写在各检出的 .git/config,不进仓库):两边改了不同线索都保留。
- 定时任务(GitHub Actions)提交 data 分支分两次:监测后一次、自动入账与同步后一次(commit_data),
  后一步失败时监测结果已经推上去了(评审 F26)。

台账(countries/{ISO3}/ledger.yaml 与 sources.yaml)在程序本身的仓库(main 分支)里:界面上"更新台账"保存后
只提交这两个文件并推送,GitHub 上的校验随即运行,定时任务下次运行就用新台账。台账冲突(GitHub 网页上同时改了同一处)
不自动取舍:放弃变基、本机提交保留,提示人工合并。
"""
from __future__ import annotations

import functools
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BRANCH = "data"
# 界面做的提交与变基用这个身份:本机 git 没配置用户名和邮箱也能提交(变基会重写本机提交,同样要身份)
APP_ID = ("-c", "user.name=osint-water-app", "-c", "user.email=app@osint-water.local")
# 定时任务(GitHub Actions)自动写台账时用的身份
BOT_ID = ("-c", "user.name=osint-water-bot", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com")
# 只追加的文件:合并时两边新加的行都保留
UNION_FILES = ("watch/events.jsonl", "watch/ledger_auto.jsonl", "watch/teable_state.jsonl")
# 整文件重写的"线索号 → 记录"文件:按键三方合并(core/yamlmerge.py)
KEYED_FILES = ("watch/triage.yaml", "watch/ledger_reviewed.yaml")
YAML_MERGE = Path(__file__).resolve().parent / "yamlmerge.py"


@dataclass
class Result:
    ok: bool
    output: str


# 同一进程里的 git 操作排队做:服务器上一个界面进程同时服务多人(每人一个会话线程),提交、拉取、推送这几步不能交错。
# 可重入:一步里又调另一步(提交后推送、拉取台账时顺带推送)不会自己卡住。本机与定时任务只有一个线程,不受影响。
LOCK = threading.RLock()


def _serial(fn):
    @functools.wraps(fn)
    def run(*args, **kw):
        with LOCK:
            return fn(*args, **kw)
    return run


def _git(cwd: Path, *args: str, timeout: int = 120, raw: bool = False) -> Result:
    try:
        # 不让 git 在终端里等人输入账号密码(界面会卡住):没有保存的凭据时直接报错,界面提示怎么配置
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return Result(False, "没有找到 git:Mac 上请先安装 Xcode 命令行工具(终端运行 xcode-select --install)")
    except subprocess.TimeoutExpired:
        return Result(False, f"git {' '.join(args)} 超时")
    out = (p.stdout + p.stderr).strip()
    if not raw:  # 输出会显示在界面上:网址里若带凭据,遮掉
        out = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", out)
    if p.returncode != 0 and ("terminal prompts disabled" in out or "Authentication failed" in out or "could not read Username" in out):
        out += "\n(本机 git 还没有保存 GitHub 凭据:按 README「快速开始」在终端里克隆一次并输入令牌)"
    return Result(p.returncode == 0, out)


def is_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def origin_url(repo_root: Path) -> str | None:
    r = _git(repo_root, "remote", "get-url", "origin", raw=True)  # 克隆 data 分支要用原样的地址
    return r.output.splitlines()[0] if r.ok and r.output else None


@_serial
def init_data(data_dir: Path, repo_root: Path) -> Result:
    """第一次在本机使用:把 data 分支克隆到数据目录(目录已有内容时先挪到 .bak)。"""
    url = origin_url(repo_root)
    if not url:
        return Result(False, "当前程序目录不是从 GitHub 克隆的,找不到仓库地址")
    data_dir = Path(data_dir)
    if data_dir.exists() and any(data_dir.iterdir()):
        data_dir.rename(data_dir.with_name(f"{data_dir.name}.bak-{datetime.now():%Y%m%d%H%M%S}"))
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    return _git(data_dir.parent, "clone", "-b", BRANCH, "--single-branch", url, data_dir.name, timeout=600)


def _union_merge(data_dir: Path) -> None:
    """数据目录本机的合并规则(.git/info/attributes 与 .git/config,不进仓库):只追加的文件 union 合并,
    "线索号 → 记录"的 YAML 按键合并(驱动命令用当前的 Python 跑 core/yamlmerge.py)。"""
    r = _git(data_dir, "rev-parse", "--git-path", "info/attributes", raw=True)
    if not r.ok or not r.output:
        return
    fp = Path(data_dir) / r.output.splitlines()[0]
    have = fp.read_text(encoding="utf-8").splitlines() if fp.exists() else []
    want = [f"{f} merge=union" for f in UNION_FILES] + [f"{f} merge=yamlmap" for f in KEYED_FILES]
    add = [x for x in want if x not in have]
    if add:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("\n".join(have + add) + "\n", encoding="utf-8")
    driver = f"{shlex.quote(sys.executable)} {shlex.quote(str(YAML_MERGE))} %O %A %B"
    if _git(data_dir, "config", "--get", "merge.yamlmap.driver", raw=True).output != driver:
        _git(data_dir, "config", "merge.yamlmap.name", "按键合并线索号记录(core/yamlmerge.py)")
        _git(data_dir, "config", "merge.yamlmap.driver", driver)


def ensure_attributes(data_dir: Path) -> bool:
    """data 分支根目录的 .gitattributes 给只追加的文件配 union 合并(随提交进分支,定时任务与各台电脑都生效)→ 是否改了文件。"""
    fp = Path(data_dir) / ".gitattributes"
    have = fp.read_text(encoding="utf-8").splitlines() if fp.exists() else []
    add = [f"{f} merge=union" for f in UNION_FILES if f"{f} merge=union" not in have]
    if add:
        fp.write_text("\n".join(have + add) + "\n", encoding="utf-8")
    return bool(add)


@_serial
def commit_data(data_dir: Path, message: str, identity: tuple[str, ...] = BOT_ID, remote: str = "origin", tries: int = 3) -> Result:
    """定时任务:数据目录的改动提交并推送到 data 分支(监测后、自动入账与同步后各一次)。先确保 .gitattributes;
    推送被拒(这期间本机界面也推了)就变基再推:只追加的文件两边的行都保留,同一处两边都改了以本次运行为准
    (-X theirs;处理状态等人管字段在表格与本机都改时本来就以表格为准,见 core/teable.py)。没有改动时 ok、output 为"无变化"。"""
    d = Path(data_dir)
    ensure_attributes(d)
    _union_merge(d)  # 本次变基就要用上(远端还没有 .gitattributes 的第一次也一样)
    _git(d, "add", "-A")
    if _git(d, "diff", "--cached", "--quiet").ok:
        if not _unpushed(d, remote):  # 上一次提交没推上去的(第一次提交失败),这次再推
            return Result(True, "无变化")
    else:
        c = _git(d, *identity, "commit", "-q", "-m", message)
        if not c.ok:
            return c
    out = Result(False, "没有推送")
    for _ in range(tries):
        out = _git(d, "push", "-q", remote, f"HEAD:{BRANCH}", timeout=300)
        if out.ok:
            return Result(True, f"已提交并推送:{message}")
        p = _git(d, *identity, "pull", "-q", "--rebase", "-X", "theirs", remote, BRANCH, timeout=300)
        if not p.ok:
            _git(d, "rebase", "--abort")
            return Result(False, "变基失败:" + p.output[:300])
    return out


def _unpushed(data_dir: Path, remote: str) -> bool:
    """本地 data 分支有没有还没推到远端的提交(远端还没有 data 分支时,本地有提交就算)。"""
    f = _git(data_dir, "fetch", "-q", remote, BRANCH, timeout=300)
    r = _git(data_dir, "rev-list", "--count", "FETCH_HEAD..HEAD" if f.ok else "HEAD")
    return r.ok and r.output.strip().isdigit() and int(r.output) > 0


@_serial
def pull(data_dir: Path) -> Result:
    """取 data 分支的新提交(变基,本机没提交的改动先收起、之后放回)。变基停在半路(-X theirs 也解不开的冲突,
    如一边改了、一边删了同一个文件)时退回拉取以前,本机提交与改动都在,下次再拉——不留半截的变基卡住以后的提交与推送。"""
    _union_merge(data_dir)
    if rebasing(data_dir):  # 上次变基做到一半进程被停掉(git 操作在本进程里排队,这里剩下的只能是没做完的):先退回
        _git(data_dir, "rebase", "--abort")
    r = _git(data_dir, *APP_ID, "pull", "--rebase", "--autostash", "-X", "theirs", "origin", BRANCH, timeout=300)
    if not r.ok and rebasing(data_dir):
        _git(data_dir, "rebase", "--abort")
    return r


def rebasing(repo: Path) -> bool:
    """仓库里有没有停在半路的变基(变基目录还在:冲突停下的,或者变基中途进程被停掉的)。"""
    for name in ("rebase-merge", "rebase-apply"):
        r = _git(repo, "rev-parse", "--git-path", name, raw=True)
        if r.ok and r.output and (Path(repo) / r.output.splitlines()[0]).exists():
            return True
    return False


def head(repo: Path) -> str:
    """当前提交的编号;不是 git 检出时空。"""
    r = _git(repo, "rev-parse", "HEAD")
    return r.output.strip() if r.ok else ""


def status(data_dir: Path) -> Result:
    return _git(data_dir, "status", "--short", "--branch")


def dirty(repo: Path) -> bool:
    """有没有要提交或推送的:工作区有没提交的改动,或本地有还没推上去的提交(上次推送失败的)。"""
    r = _git(repo, "status", "--porcelain", "--branch")
    lines = r.output.splitlines() if r.ok else []
    return any(not x.startswith("##") for x in lines if x.strip()) or any(x.startswith("##") and "[ahead" in x for x in lines)


@_serial
def commit_push(data_dir: Path, message: str) -> Result:
    """提交本机改动并推送;没有改动时只拉取。推送失败(离线、没权限)时本机提交保留,下次再推。"""
    _git(data_dir, "add", "-A")
    if not _git(data_dir, "diff", "--cached", "--quiet").ok:
        c = _git(data_dir, *APP_ID, "commit", "-q", "-m", message)
        if not c.ok:
            return c
    p = pull(data_dir)
    if not p.ok:
        return Result(False, "拉取失败:" + p.output)
    return _git(data_dir, "push", "-q", "origin", f"HEAD:{BRANCH}", timeout=300)


# ---------- 程序仓库(台账) ----------


def toplevel(path: Path) -> Path | None:
    """文件所在 git 仓库的根目录;不在仓库里(或没有 git)时 None。"""
    d = Path(path).resolve().parent
    if not d.exists():
        return None
    r = _git(d, "rev-parse", "--show-toplevel")
    return Path(r.output.splitlines()[0]) if r.ok and r.output else None


def branch(repo_root: Path) -> str:
    r = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    name = r.output.strip() if r.ok else ""
    return name if name and name != "HEAD" else "main"


def ahead(repo_root: Path) -> int:
    """本机还没推送的提交数(没有上游分支时 0)。"""
    r = _git(repo_root, "rev-list", "--count", "@{u}..HEAD")
    return int(r.output) if r.ok and r.output.strip().isdigit() else 0


@_serial
def push_program(repo_root: Path, identity: tuple[str, ...] = APP_ID, tries: int = 3) -> Result:
    """把本机的程序仓库提交(台账改动)变基到最新后推送;推送被拒(这期间定时任务或别的电脑也推了)、拉取出错(网络)
    就稍等再变基再推;变基冲突时放弃变基,本机提交保留。"""
    br = branch(repo_root)
    out = Result(False, "没有推送")
    for i in range(tries):
        if i:
            time.sleep(2 * i)
        p = _git(repo_root, *identity, "pull", "--rebase", "--autostash", "origin", br, timeout=300)
        if not p.ok:
            _git(repo_root, "rebase", "--abort")
            conflict = "CONFLICT" in p.output or "conflict" in p.output
            if conflict or i == tries - 1:
                hint = ("GitHub 上的台账也改过同一处,需要人工合并:在终端进入程序目录运行 git pull,按提示解决冲突后 git push"
                        if conflict else "联网后在\"设置与同步\"里点\"检查并更新程序\"会再推送")
                return Result(False, f"台账已保存并在本机提交,推送没有完成({hint}):{p.output[:300]}")
            continue
        out = _git(repo_root, "push", "-q", "origin", f"HEAD:{br}", timeout=300)
        if out.ok:
            return out
    return out


@_serial
def drop_commit(repo_root: Path, message: str, files: list[Path]) -> None:
    """定时任务推送失败时退回刚才那次提交(只退回说明相符的最新一次;没提交成的把文件改回去),
    免得本次运行后面的步骤用上没推上去的台账、或别的步骤把它顺带推上去而记录对不上。"""
    root = Path(repo_root).resolve()
    head = _git(root, "log", "-1", "--format=%s", raw=True)
    if head.ok and head.output == message:
        _git(root, "reset", "-q", "--keep", "HEAD~1")
    else:
        _git(root, "checkout", "HEAD", "--", *(_rel(root, f) for f in files))


@_serial
def commit_program(repo_root: Path, files: list[Path], message: str, identity: tuple[str, ...] = APP_ID) -> Result:
    """台账(或内部叠加)的改动提交到程序仓库并推送。只提交给定的文件(程序目录里别的改动不动)。
    identity:本机界面用 APP_ID,定时任务用 BOT_ID。"""
    root = Path(repo_root).resolve()
    rel = [str(Path(f).resolve().relative_to(root)) for f in files]
    a = _git(root, "add", "--", *rel)
    if not a.ok:
        return a
    if not _git(root, "diff", "--cached", "--quiet", "--", *rel).ok:
        c = _git(root, *identity, "commit", "-q", "-m", message, "--", *rel)
        if not c.ok:
            return c
    return push_program(root, identity)


@_serial
def commit_files(repo_root: Path, files: list[Path], message: str, identity: tuple[str, ...] = APP_ID) -> Result:
    """只提交给定的文件,不推送。没有改动时 ok=False、output 为空。"""
    root = Path(repo_root).resolve()
    rel = [str(Path(f).resolve().relative_to(root)) for f in files]
    a = _git(root, "add", "--", *rel)
    if not a.ok:
        return a
    if _git(root, "diff", "--cached", "--quiet", "--", *rel).ok:
        return Result(False, "")
    return _git(root, *identity, "commit", "-q", "-m", message, "--", *rel)


@_serial
def push_retry(repo_root: Path, identity: tuple[str, ...] = APP_ID, tries: int = 3) -> Result:
    """变基到最新后推送;推送被拒(这期间别处也推了)就再拉再推。变基冲突时放弃变基,本机提交保留。"""
    br = branch(repo_root)
    out = Result(False, "没有推送")
    for _ in range(tries):
        p = _git(repo_root, *identity, "pull", "--rebase", "--autostash", "origin", br, timeout=300)
        if not p.ok:
            _git(repo_root, "rebase", "--abort")
            return Result(False, "变基失败(台账同一处别处也改过):" + p.output[:300])
        out = _git(repo_root, "push", "-q", "origin", f"HEAD:{br}", timeout=300)
        if out.ok:
            return out
    return out


def recent_commits(repo_root: Path, n: int) -> list[tuple[str, str]]:
    """最近 n 次提交 → [(完整编号, 提交说明首行)],新的在前。"""
    r = _git(repo_root, "log", f"-{n}", "--format=%H%x09%s")
    return [tuple(ln.split("\t", 1)) for ln in r.output.splitlines() if "\t" in ln] if r.ok else []


def _rel(repo_root: Path, file: Path) -> str:
    return Path(file).resolve().relative_to(Path(repo_root).resolve()).as_posix()


def file_log(repo_root: Path, files: list[Path]) -> list[tuple[str, str, str]]:
    """改过这些文件的提交 → [(完整编号, 提交时间 ISO, 提交说明首行)],新的在前。浅克隆只列得出克隆下来的那段历史。"""
    r = _git(repo_root, "log", "--format=%H%x09%cI%x09%s", "--", *(_rel(repo_root, f) for f in files), raw=True)
    return [tuple(ln.split("\t", 2)) for ln in r.output.splitlines() if ln.count("\t") >= 2] if r.ok else []


def show(repo_root: Path, rev: str, file: Path) -> str | None:
    """某次提交里这个文件的内容;没有(文件那时还不存在、浅克隆缺这次提交)时 None。"""
    r = _git(repo_root, "show", f"{rev}:{_rel(repo_root, file)}", raw=True)
    return r.output if r.ok else None


@_serial
def pull_ledger_updates(repo_root: Path, prefix: str | tuple[str, ...] = ("countries/", "private/overlay/", "private/team.yaml")) -> Result:
    """程序仓库:远端只改了台账(countries/ 下,如定时任务自动写入)、内部叠加(private/overlay/ 下,如表格里改的项目优先级)
    与代号表(private/team.yaml)时拉取,界面立即用新的;远端也改了程序代码时不拉(运行中换代码会出错),
    提示到"设置与同步"更新程序(服务器上每天 09:30 自动更新)。"""
    br = branch(repo_root)
    f = _git(repo_root, "fetch", "-q", "origin", br, timeout=120)
    if not f.ok:
        return f
    d = _git(repo_root, "diff", "--name-only", f"HEAD...origin/{br}")
    names = [x for x in d.output.splitlines() if x.strip()] if d.ok else []
    if not names:
        return Result(True, "")
    if any(not x.startswith(prefix) for x in names):
        return Result(False, "程序有更新(不只是台账),到\"管理 → 设置与同步\"点\"检查并更新程序\"后重启界面")
    r = update_program(repo_root) if ahead(repo_root) else _git(repo_root, "merge", "-q", "--ff-only", f"origin/{br}")
    if not r.ok:
        return Result(False, "拉取台账更新失败:" + r.output[:300])
    return Result(True, f"已拉取台账更新({len(names)} 个文件)")


def github_url(repo_root: Path, file: Path, mode: str = "edit") -> str | None:
    """GitHub 网页上查看(blob)或编辑(edit)这个文件的地址;仓库不在 GitHub 上时 None。地址里不带凭据。"""
    url = origin_url(repo_root) or ""
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    try:
        rel = Path(file).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return None
    return f"https://github.com/{m[1]}/{m[2]}/{mode}/{branch(repo_root)}/{rel}"


@_serial
def update_program(repo_root: Path) -> Result:
    """更新程序本身(main 分支)。本机有还没推送的台账提交时,变基到最新后推送。"""
    r = _git(repo_root, "pull", "--ff-only", timeout=300)
    if not ahead(repo_root):
        return r
    p = push_program(repo_root)
    return Result(p.ok, "\n".join(x for x in (r.output if r.ok else "", p.output or "本机的台账改动已推送") if x))
