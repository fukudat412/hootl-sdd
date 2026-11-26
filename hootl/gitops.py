"""git 操作。worktree の作成・自動コミット・差分取得・マージ。"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

_merge_lock = threading.Lock()


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def head(cwd: Path) -> str:
    return git(cwd, "rev-parse", "HEAD").strip()


def create_worktree(repo: Path, worktree_root: Path, task_id: str, main_branch: str) -> tuple[Path, str]:
    """main の最新から task/<ID> を作り直す。前回の試行の残骸は捨てる。"""
    wt = worktree_root / task_id
    branch = f"task/{task_id}"
    remove_worktree(repo, wt, branch)
    worktree_root.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(wt), main_branch)
    return wt, head(wt)


def remove_worktree(repo: Path, wt: Path, branch: str) -> None:
    if wt.exists():
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    git(repo, "branch", "-D", branch, check=False)


def commit_all(wt: Path, message: str) -> bool:
    """エージェントが残した変更を全部コミットする。変更がなければ False。"""
    git(wt, "add", "-A")
    if not git(wt, "status", "--porcelain").strip():
        return False
    git(wt, "-c", "user.name=hootl", "-c", "user.email=hootl@localhost", "commit", "-q", "-m", message)
    return True


def changes(wt: Path, base: str) -> list[tuple[str, str]]:
    """base からの変更を (状態, パス) で返す。状態は A/M/D/R など。"""
    out = git(wt, "diff", "--name-status", "--no-renames", f"{base}..HEAD")
    result = []
    for line in out.splitlines():
        if line.strip():
            status, path = line.split("\t", 1)
            result.append((status[0], path))
    return result


def diff_text(wt: Path, base: str) -> str:
    return git(wt, "diff", f"{base}..HEAD")


def sync_main(repo: Path, remote: str, main_branch: str) -> tuple[bool, str]:
    """ラン開始時に remote の main を取り込む（fast-forward のみ。人が push した分を拾う）。"""
    with _merge_lock:
        git(repo, "checkout", "-q", main_branch)
        fetched = subprocess.run(["git", "fetch", "-q", remote, main_branch], cwd=repo,
                                 capture_output=True, text=True)
        if fetched.returncode != 0:
            return False, fetched.stderr.strip()
        ff = subprocess.run(["git", "merge", "--ff-only", "-q", f"{remote}/{main_branch}"], cwd=repo,
                            capture_output=True, text=True)
        if ff.returncode != 0:
            return False, "ローカルの main と remote の main が分岐しています: " + ff.stderr.strip()
        return True, head(repo)


def push_main(repo: Path, remote: str, main_branch: str) -> tuple[bool, str]:
    with _merge_lock:
        proc = subprocess.run(["git", "push", "-q", remote, f"{main_branch}:{main_branch}"], cwd=repo,
                              capture_output=True, text=True)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def merge_to_main(repo: Path, branch: str, main_branch: str, message: str) -> tuple[bool, str]:
    """main にマージする。並列ワーカー間で直列化し、衝突したら元に戻す。"""
    with _merge_lock:
        current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != main_branch:
            git(repo, "checkout", "-q", main_branch)
        proc = subprocess.run(
            ["git", "-c", "user.name=hootl", "-c", "user.email=hootl@localhost",
             "merge", "--no-ff", "-q", "-m", message, branch],
            cwd=repo, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            git(repo, "merge", "--abort", check=False)
            return False, (proc.stdout + proc.stderr).strip()
        return True, head(repo)
