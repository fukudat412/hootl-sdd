"""hootl の Web 管理画面（標準ライブラリだけ）。

外から触れるのは「決まった操作」だけ:
  見る       状況・タスク・試行の記録とログ・差分・レポート・タスク案・ネタ帳
  おまかせ   作りたいものを書くと、仕様 → 凍結 → 計画 → 取り込み → 実行まで承認なしで進む（hootl auto）
  ネタ帳     作業中に見つかった案を読む・足す・捨てる・選んで作る
  計画/承認  自分で読みたいとき、または機械の確認で止まったときに、タスク案を直して取り込む
  回す/止める hootl run の開始と、今のタスクが終わったら止める停止
  やり直す   blocked のタスクを todo に戻す
  使い方     /help（ログイン前でも読める。トークンの出し方・画面の使い方）
任意のコマンドやファイルの読み書きはできない。

認証は2段:
  1. トークン（HOOTL_WEB_TOKEN）。ログイン画面で入れると HttpOnly の cookie になる
  2. allowed_emails を設定したら、Cloudflare Access が付けるメールのヘッダも照合する
サーバーは 127.0.0.1 にだけ待ち受ける。外からは Cloudflare Tunnel 経由で入る前提。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import auto as auto_mod
from . import ideas as ideas_mod
from . import specs
from . import stacks as stacks_mod
from .config import Config, load
from .queue import Queue
from .specs import TASK_ID, validate_jsonl

STATIC = Path(__file__).parent / "web_static"
# 使い方のページ。トークンの出し方が載っているので、ログイン前でも読める（秘密は書いていない）
HELP_PAGES = {"/help": STATIC / "help.html",
              "/help/how-it-works": Path(__file__).resolve().parent.parent / "docs" / "how-it-works.html"}
LOG_NAME = re.compile(r"^(implement\.log|review\.log|review-noverdict-\d+\.log|review\.json|diff\.patch|outcome\.txt|"
                      r"(?:gate|setup)-[\w.-]+\.log|"
                      r"implement\.prompt\.md|review\.prompt\.md)$")
MAX_LOG = 400_000  # 1ファイルの表示上限（末尾を見せる）


# ---- state.db への接続 -----------------------------------------------------
# 接続は、このプロセスの中で db ごとに1本だけ持って使い回す。要求のたびに開くと、閉じられる前にたまって
# （接続は循環参照の回収を待つので、すぐには閉じない）、開けるファイルの上限を超えて画面が落ちる。
_STORES: dict[tuple[type, str], tuple[tuple[int, int], object]] = {}
_STORES_LOCK = threading.Lock()


def _ident(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def store(kind: type, db_path: Path):
    """kind（Queue か Notebook）の、db_path につながった使い回しの1本を返す。

    同じ名前でフォルダごと作り直されたとき（別のファイルになる）は、古い接続を閉じて開き直す。
    """
    key = (kind, str(db_path))
    with _STORES_LOCK:
        cur = _STORES.get(key)
        if cur and cur[0] == _ident(db_path):
            return cur[1]
        if cur:
            try:
                cur[1].close()
            except Exception:  # 閉じ損ねても、新しい接続は開く
                pass
        obj = kind(db_path)
        _STORES[key] = (_ident(db_path) or (0, 0), obj)
        return obj


# ---- 設定 ---------------------------------------------------------------
@dataclass
class Project:
    name: str
    config_path: Path
    spec: str = "spec.md"            # repo からの相対パス
    _cfg: Config | None = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = load(self.config_path)
        return self._cfg

    def queue(self) -> Queue:
        return store(Queue, self.cfg.state_dir / "state.db")

    def notebook(self) -> ideas_mod.Notebook:
        return store(ideas_mod.Notebook, self.cfg.state_dir / "state.db")


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""
    allowed_emails: list[str] = field(default_factory=list)
    projects: dict[str, Project] = field(default_factory=dict)   # 手で並べたもの
    projects_dir: Path | None = None                              # この下の <名前>/hootl.toml を自動で拾う

    def all_projects(self) -> dict[str, Project]:
        """手で並べたもの＋projects_dir の下で見つかったもの。毎回探すので、増えたら再起動なしで出る。"""
        found = dict(self.projects)
        if self.projects_dir and self.projects_dir.is_dir():
            for cfg in sorted(self.projects_dir.glob("*/hootl.toml")):
                name = cfg.parent.name
                if name in found or not TASK_ID.match(name):
                    continue
                found[name] = Project(name, cfg, spec_of(cfg))
        return found


def spec_of(hootl_toml: Path) -> str:
    """hootl.toml の一番上に spec = "..." があればそれ、なければ spec.md。"""
    try:
        return str(tomllib.loads(hootl_toml.read_text(encoding="utf-8")).get("spec", "spec.md"))
    except (OSError, tomllib.TOMLDecodeError):
        return "spec.md"


def load_web_config(path: str | Path) -> WebConfig:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"{path} がありません。先に `hootl setup` を実行してください")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    token = os.environ.get("HOOTL_WEB_TOKEN") or raw.get("token", "")
    if len(token) < 16:
        raise SystemExit("トークンが短すぎます。`hootl setup` で作り直すか、16文字以上を入れてください")
    projects = {}
    for p in raw.get("projects", []):
        name = p["name"]
        if not TASK_ID.match(name):
            raise SystemExit(f"プロジェクト名に使えない文字があります: {name}")
        cfg_path = Path(p["config"]).expanduser()
        if not cfg_path.is_absolute():
            cfg_path = (path.parent / cfg_path).resolve()
        projects[name] = Project(name, cfg_path, p.get("spec", spec_of(cfg_path)))
    pdir = raw.get("projects_dir")
    wc = WebConfig(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8765)),
        token=token,
        allowed_emails=[e.lower() for e in raw.get("allowed_emails", [])],
        projects=projects,
        projects_dir=Path(pdir).expanduser() if pdir else None,
    )
    if not wc.all_projects() and not wc.projects_dir:
        raise SystemExit("projects_dir も [[projects]] もありません")
    return wc


# ---- バックグラウンドの仕事（run / plan / spec / auto / new）-------------------
def job_env(pythonpath: str) -> dict[str, str]:
    """仕事（とその先のエージェント・ゲート）に渡す環境変数。管理画面のトークンは渡さない。"""
    env = {k: v for k, v in os.environ.items() if k != "HOOTL_WEB_TOKEN"}
    return {**env, "PYTHONUNBUFFERED": "1", "PYTHONPATH": pythonpath}


JOB_RECORD = "web-job.json"   # 動かした仕事の控え（管理画面を再起動しても、動いている仕事を見失わないため）
# 引き取ってよい仕事の種類と、その hootl のサブコマンド。控えは .hootl の中にあり、エージェントが書き換えられる
# 場所なので、控えから信じるのは「種類・pid・始めた時刻」だけにする（ログの場所は控えから取らない）
JOB_COMMANDS = {"run": "run", "plan": "plan", "spec": "spec-draft", "auto": "auto"}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


class Adopted:
    """管理画面を起動し直す前から動いている仕事。自分の子プロセスではないので、pid で様子を見る。

    仕事は別セッションで動かしているので、管理画面が落ちても再起動しても止まらない。終了コードは分からない。
    """
    ENDED = -1

    def __init__(self, pid: int):
        self.pid = pid
        self._gone = False

    def poll(self) -> int | None:
        if not self._gone and not _pid_alive(self.pid):
            self._gone = True
        return self.ENDED if self._gone else None

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)


@dataclass
class Job:
    kind: str                 # run | plan | spec | auto | new
    proc: subprocess.Popen | Adopted
    log_path: Path
    started: float
    stop_requested: int = 0

    def alive(self) -> bool:
        return self.proc.poll() is None

    def info(self) -> dict:
        tail = ""
        try:
            data = self.log_path.read_bytes()[-6000:]
            tail = data.decode("utf-8", "replace")
        except OSError:
            pass
        adopted = isinstance(self.proc, Adopted)
        return {"kind": self.kind, "alive": self.alive(), "code": None if adopted else self.proc.poll(),
                "started": self.started, "stop_requested": self.stop_requested, "tail": tail,
                "phase": self.phase(), "adopted": adopted}

    def phase(self) -> str | None:
        """おまかせが、いまどの段にいるか（hootl auto が出す ::phase の最後の行）。"""
        if self.kind != "auto":
            return None
        try:
            text = self.log_path.read_bytes()[-200_000:].decode("utf-8", "replace")
        except OSError:
            return None
        found = [ln[8:].strip() for ln in text.splitlines() if ln.startswith("::phase ")]
        return found[-1] if found else None


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, project: str) -> Job | None:
        with self._lock:
            return self._jobs.get(project)

    def adopt(self, project: Project) -> None:
        """この管理画面が知らない仕事が、前の起動から動き続けていれば引き取る（見る・止めるを続けられる）。"""
        with self._lock:
            if project.name in self._jobs:
                return
        try:
            state = project.cfg.state_dir
            path = state / JOB_RECORD
            rec = json.loads(path.read_text(encoding="utf-8"))
            pid, kind, started = int(rec["pid"]), str(rec["kind"]), float(rec["started"])
        except Exception:  # 控えがない・読めない・設定が壊れている → 引き取るものはない
            return
        # 控えは書き換えられうるので、そのまま信じない:
        #   種類は決まったものだけ。ログの場所は控えから取らず、start() と同じ決まった場所にする（好きなファイルを読ませない）
        #   pid は「この管理画面が起動する形の、このプロジェクトの、その種類の hootl」であることを確かめる（止める合図の宛先になる）
        expected = f"-m hootl.cli -c {project.config_path} {JOB_COMMANDS.get(kind, '')}"
        cmd = _cmdline(pid) if kind in JOB_COMMANDS and pid > 1 and _pid_alive(pid) else ""
        if not (cmd.rstrip().endswith(expected) or f"{expected} " in cmd):
            path.unlink(missing_ok=True)
            return
        with self._lock:
            self._jobs.setdefault(project.name, Job(kind, Adopted(pid), state / f"web-{kind}.log", started))

    def start(self, project: Project, kind: str, args: list[str]) -> Job:
        state = project.cfg.state_dir
        state.mkdir(parents=True, exist_ok=True)
        return self.start_cmd(project.name, kind, ["-c", str(project.config_path), *args],
                              project.cfg.repo, state / f"web-{kind}.log")

    def start_cmd(self, key: str, kind: str, args: list[str], cwd: Path, log_path: Path) -> Job:
        with self._lock:
            cur = self._jobs.get(key)
            if cur and cur.alive():
                raise ValueError(f"いま {cur.kind} が動いています。終わってから実行してください")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(log_path, "wb")
            cmd = [sys.executable, "-m", "hootl.cli", *args]
            # pip で入れていない checkout からでも同じ hootl を使えるように、このパッケージの場所を通す
            pkg_root = str(Path(__file__).resolve().parent.parent)
            pythonpath = os.pathsep.join(p for p in (pkg_root, os.environ.get("PYTHONPATH", "")) if p)
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True,
                                        env=job_env(pythonpath))
            finally:
                log.close()               # 子プロセスが自分の分を持っている。こちらで開けたままにしない
            job = Job(kind, proc, log_path, time.time())
            self._jobs[key] = job
            if not key.startswith("new:"):
                try:
                    (log_path.parent / JOB_RECORD).write_text(json.dumps(
                        {"pid": proc.pid, "kind": kind, "started": job.started}), encoding="utf-8")
                except OSError:
                    pass                  # 控えが書けなくても、仕事は動かす
            return job

    def forget(self, key: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job and not job.alive():
                del self._jobs[key]

    def creations(self) -> dict[str, Job]:
        with self._lock:
            return {k[4:]: j for k, j in self._jobs.items() if k.startswith("new:")}

    def stop(self, project: str) -> Job | None:
        """1回目: SIGINT（今のタスクが終わったら止まる）。2回目以降: SIGTERM をプロセスグループへ。"""
        with self._lock:
            job = self._jobs.get(project)
            if not job or not job.alive():
                return job
            job.stop_requested += 1
            if job.stop_requested == 1:
                job.proc.send_signal(signal.SIGINT)
            else:
                os.killpg(job.proc.pid, signal.SIGTERM)
            return job


# ---- API の中身 -----------------------------------------------------------
def stage_of(run_dir: Path) -> str:
    """実行中の試行が、いまどの工程にいるかをログの有無から推す（ハーネスは工程ごとにファイルを書く）。"""
    if not run_dir.is_dir():
        return "setup"
    names = {f.name for f in run_dir.iterdir()}
    if "review.json" in names or "outcome.txt" in names:
        return "merge"
    if "review.prompt.md" in names or "review.log" in names:
        return "review"
    if any(n.startswith("gate-") for n in names):
        return "gate"
    if "diff.patch" in names:
        return "guard"
    if "implement.prompt.md" in names:
        return "implement"
    return "setup"


def current_stage(cfg: Config, t) -> str | None:
    if t.status != "running" or t.attempts < 1:
        return None
    return stage_of(cfg.state_dir / "runs" / t.id / str(t.attempts))


def task_dict(t, updated: dict[str, str] | None = None) -> dict:
    return {"id": t.id, "title": t.title, "description": t.description, "acceptance": t.acceptance,
            "paths": t.paths, "depends_on": t.depends_on, "epic": t.epic, "priority": t.priority,
            "status": t.status, "attempts": t.attempts, "last_error": t.last_error,
            "updated_at": (updated or {}).get(t.id)}


def project_summary(p: Project, jobs: Jobs) -> dict:
    auto, ideas_new = None, 0
    try:
        q = p.queue()
        counts = q.counts()
        cfg = p.cfg
        auto = {"approve": cfg.auto_approve, "run": cfg.auto_run, "hours": cfg.auto_hours,
                "max_tasks": cfg.auto_max_tasks}
        ideas_new = p.notebook().count_new()
        error = None
    except Exception as exc:  # 設定やDBの不備を画面に出す
        counts, error = {}, str(exc)
    job = jobs.get(p.name)
    return {"name": p.name, "counts": counts, "error": error,
            "job": job.info() if job else None, "auto": auto, "ideas_new": ideas_new,
            "has_proposed": (p.cfg.state_dir / "proposed-tasks.jsonl").exists() if not error else False}


def parse_progress(text: str) -> dict:
    """hootl new の出力から、工程のチェックリストを作る（::plan / ::step / ::done / ::failed）。"""
    plan: list[dict] = []
    state: dict[str, dict] = {}
    result = None
    moved = None
    for line in text.splitlines():
        if line.startswith("::plan "):
            try:
                plan = json.loads(line[7:])
            except json.JSONDecodeError:
                pass
        elif line.startswith("::step "):
            bits = line.split(" ", 3)
            if len(bits) >= 3:
                state[bits[1]] = {"state": bits[2], "note": bits[3] if len(bits) > 3 else ""}
        elif line.startswith("::done "):
            result = "done"
        elif line.startswith("::failed "):
            result = "failed"
            bits = line.split(" ", 2)
            moved = bits[2] if len(bits) > 2 and bits[2] != "-" else None
    steps = [{**s, **state.get(s["id"], {"state": "todo", "note": ""})} for s in plan]
    return {"steps": steps, "result": result, "moved_to": moved}


def read_tail(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_LOG:
        return "…(前略)…\n" + data[-MAX_LOG:].decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


class Api:
    def __init__(self, wc: WebConfig, jobs: Jobs | None = None):
        self.wc = wc
        self.jobs = jobs or Jobs()

    def project(self, name: str) -> Project:
        p = self.wc.all_projects().get(name)
        if not p:
            raise LookupError("プロジェクトがありません")
        self.jobs.adopt(p)
        return p

    # GET
    def projects(self) -> dict:
        projects = list(self.wc.all_projects().values())
        for p in projects:
            self.jobs.adopt(p)
        return {"projects": [project_summary(p, self.jobs) for p in projects]}

    def tasks(self, name: str) -> dict:
        p = self.project(name)
        q = p.queue()
        upd = q.updated_at()
        tasks = []
        for t in q.all():
            d = task_dict(t, upd)
            d["stage"] = current_stage(p.cfg, t)
            tasks.append(d)
        return {"summary": project_summary(p, self.jobs), "tasks": tasks}

    def task(self, name: str, task_id: str) -> dict:
        p = self.project(name)
        q = p.queue()
        t = q.get(task_id)
        if not t:
            raise LookupError("タスクがありません")
        attempts = []
        for a in q.attempts_for(task_id):
            run_dir = p.cfg.state_dir / "runs" / task_id / str(a["attempt"])
            files = sorted(f.name for f in run_dir.iterdir() if LOG_NAME.match(f.name)) if run_dir.is_dir() else []
            attempts.append({**{k: a[k] for k in a.keys()}, "files": files})
        # 実行中の試行（まだ attempts に記録されていない）もログを見られるように
        running = []
        base = p.cfg.state_dir / "runs" / task_id
        if base.is_dir():
            recorded = {a["attempt"] for a in attempts}
            for d in sorted(base.iterdir(), key=lambda d: int(d.name) if d.name.isdigit() else 0):
                if d.name.isdigit() and int(d.name) not in recorded:
                    running.append({"attempt": int(d.name),
                                    "files": sorted(f.name for f in d.iterdir() if LOG_NAME.match(f.name))})
        return {"task": task_dict(t), "attempts": attempts, "in_progress": running}

    def log(self, name: str, task_id: str, attempt: str, fname: str) -> str:
        p = self.project(name)
        if not TASK_ID.match(task_id) or not attempt.isdigit() or not LOG_NAME.match(fname):
            raise LookupError("見られないファイルです")
        path = p.cfg.state_dir / "runs" / task_id / attempt / fname
        if not path.is_file():
            raise LookupError("ファイルがありません")
        return read_tail(path)

    def reports(self, name: str) -> dict:
        p = self.project(name)
        files = sorted(p.cfg.state_dir.glob("report-*.md"), reverse=True)
        return {"reports": [{"name": f.name, "text": read_tail(f)} for f in files[:20]]}

    def proposed(self, name: str) -> dict:
        p = self.project(name)
        path = p.cfg.state_dir / "proposed-tasks.jsonl"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        tasks, error = [], None
        if text.strip():
            try:
                tasks = validate_jsonl(text)
            except ValueError as exc:
                error = str(exc)
        existing = {t.id: t.status for t in p.queue().all()}
        for t in tasks:
            t["_existing"] = existing.get(t["id"])
        return {"text": text, "tasks": tasks, "error": error,
                "spec": p.spec, "spec_exists": (p.cfg.repo / p.spec).is_file()}

    # POST
    def save_proposed(self, name: str, text: str) -> dict:
        p = self.project(name)
        validate_jsonl(text)
        path = p.cfg.state_dir / "proposed-tasks.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n", encoding="utf-8")
        return self.proposed(name)

    def import_proposed(self, name: str) -> dict:
        p = self.project(name)
        return {"imported": specs.import_proposed(p.cfg, p.queue())}   # 取り込んだ案は控えとして残る

    def discard_proposed(self, name: str) -> dict:
        """タスク案を取り込まずに下げる（消さずに discarded-<日時>.jsonl に回す）。"""
        p = self.project(name)
        path = specs.proposed_path(p.cfg)
        if not path.exists():
            raise ValueError("タスク案がありません")
        path.rename(path.with_name(f"discarded-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
        return {"ok": True}

    def plan(self, name: str, ref: str | None = None) -> dict:
        p = self.project(name)
        spec = specs.resolve(p.cfg, ref) if ref else (p.cfg.repo / p.spec)
        if not spec.is_file():
            raise ValueError(f"{ref or p.spec} がありません")
        if ref and ref.startswith("draft:"):
            raise ValueError("下書きからは計画しません。先に凍結してください")
        return {"job": self.jobs.start(p, "plan", ["plan", str(spec)]).info()}

    # ---- 仕様 ----
    def _idle(self, p: Project) -> None:
        job = self.jobs.get(p.name)
        if job and job.alive():
            raise ValueError(f"いま {job.kind} が動いています。終わってから実行してください")
        if p.queue().counts().get("running", 0):
            raise ValueError("実行中のタスクがあります")

    def specs(self, name: str) -> dict:
        p = self.project(name)
        return {"specs": specs.list_specs(p.cfg), "summary": project_summary(p, self.jobs)}

    def spec(self, name: str, ref: str) -> dict:
        p = self.project(name)
        path = specs.resolve(p.cfg, ref)
        return {"ref": ref, "text": path.read_text(encoding="utf-8")}

    def _ready(self, p: Project, drafting: bool) -> None:
        """おまかせを始められるか。始めてから分かる行き止まりを、押した時点で返す。"""
        self._idle(p)
        try:
            auto_mod.ready(p.cfg, drafting)
        except auto_mod.Stopped as exc:
            raise ValueError(str(exc)) from exc

    def spec_draft(self, name: str, idea: str, auto: bool | None = None) -> dict:
        """auto（省略時は hootl.toml の [auto] approve）なら、下書きで止まらずに実装まで進む。"""
        p = self.project(name)
        idea = idea.strip()
        if len(idea) < 10:
            raise ValueError("作りたいものを、もう少し書いてください（10文字以上）")
        if len(idea) > 20000:
            raise ValueError("長すぎます（2万字まで）")
        return self._start_draft(p, idea, p.cfg.auto_approve if auto is None else auto)

    def _start_draft(self, p: Project, idea: str, auto: bool) -> dict:
        if auto:
            self._ready(p, drafting=True)
        slug = specs.new_slug(p.cfg)
        d = specs.drafts_dir(p.cfg)
        d.mkdir(parents=True, exist_ok=True)
        idea_file = d / f"{slug}.idea.txt"
        idea_file.write_text(idea + "\n", encoding="utf-8")
        if auto:
            job = self.jobs.start(p, "auto", ["auto", "--idea-file", str(idea_file), "--slug", slug])
        else:
            job = self.jobs.start(p, "spec", ["spec-draft", "--idea-file", str(idea_file),
                                              "--out", str(d / f"{slug}.md")])
        return {"slug": slug, "auto": auto, "job": job.info()}

    def spec_save(self, name: str, slug: str, text: str) -> dict:
        p = self.project(name)
        specs.save_draft(p.cfg, slug, text)
        return self.spec(name, f"draft:{slug}")

    def spec_discard(self, name: str, slug: str) -> dict:
        p = self.project(name)
        path = specs.resolve(p.cfg, f"draft:{slug}")
        path.rename(path.with_suffix(".discarded"))
        return {"ok": True}

    def spec_freeze(self, name: str, slug: str, auto: bool | None = None) -> dict:
        """凍結して計画へ。auto なら、タスク案の承認で止まらずに取り込み・実行まで進む。"""
        p = self.project(name)
        auto = p.cfg.auto_approve if auto is None else auto
        if auto:
            self._ready(p, drafting=False)
        else:
            self._idle(p)
        target = specs.freeze(p.cfg, slug)
        ref = f"{specs.SPECS_DIR}/{target.name}"
        if auto:
            job = self.jobs.start(p, "auto", ["auto", "--spec", str(target)])
        else:
            job = self.jobs.start(p, "plan", ["plan", str(target)])
        return {"frozen": ref, "auto": auto, "job": job.info()}

    # ---- ネタ帳 ----
    def ideas(self, name: str) -> dict:
        p = self.project(name)
        return {"ideas": [i.to_dict() for i in p.notebook().all()],
                "summary": project_summary(p, self.jobs)}

    def idea_add(self, name: str, title: str, body: str) -> dict:
        p = self.project(name)
        if not title.strip():
            raise ValueError("案の名前を書いてください")
        new_id = p.notebook().add(title, body, "human")
        if new_id is None:
            raise ValueError("同じ名前の案がもうあります")
        return {"id": new_id}

    def idea_mark(self, name: str, idea_id: int, status: str) -> dict:
        p = self.project(name)
        if status not in ("new", "dropped") or not p.notebook().mark(idea_id, status):
            raise LookupError("その案はありません")
        return {"ok": True}

    def idea_build(self, name: str, idea_id: int) -> dict:
        """ネタ帳の案を「作りたいもの」として流す（何を作るかを選ぶのは人）。"""
        p = self.project(name)
        book = p.notebook()
        idea = book.get(idea_id)
        if not idea or idea.status != "new":
            raise LookupError("その案はありません")
        r = self._start_draft(p, idea.as_request(), p.cfg.auto_approve)
        book.mark(idea_id, "adopted", r["slug"])
        return r

    def run(self, name: str, hours: float | None, concurrency: int | None) -> dict:
        p = self.project(name)
        if p.queue().counts().get("running", 0):
            raise ValueError("実行中のタスクがあります（ターミナルから回していませんか）")
        args = ["run"]
        if hours:
            if not 0 < hours <= 24:
                raise ValueError("時間は 0〜24 の範囲で")
            args += ["--hours", str(hours)]
        if concurrency:
            if not 1 <= concurrency <= 8:
                raise ValueError("並列は 1〜8 の範囲で")
            args += ["--concurrency", str(concurrency)]
        return {"job": self.jobs.start(p, "run", args).info()}

    def stop(self, name: str) -> dict:
        self.project(name)
        job = self.jobs.stop(name)
        return {"job": job.info() if job else None}

    # ---- 新しいアプリ（カタログから）----
    def stacks(self) -> dict:
        return {"stacks": [s.to_public() for s in stacks_mod.catalog()],
                "runs_dir": str(self.wc.projects_dir) if self.wc.projects_dir else None}

    def creations(self) -> dict:
        out = []
        live = self.jobs.creations()
        for name, job in live.items():
            try:
                text = job.log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            info = job.info()
            out.append({"name": name, "alive": info["alive"], "code": info["code"], "started": info["started"],
                        "tail": info["tail"], "request": self._request_of(name), "note": self._note_of(name),
                        **parse_progress(text)})
        # 管理画面を再起動したあとも、最近作ったものはログから出す
        work = self.wc.projects_dir / ".hootl-new" if self.wc.projects_dir else None
        if work and work.is_dir():
            logs = sorted(work.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:8]
            for log in logs:
                if log.stem in live:
                    continue
                text = log.read_text(encoding="utf-8", errors="replace")
                prog = parse_progress(text)
                out.append({"name": log.stem, "alive": False, "code": None, "started": log.stat().st_mtime,
                            "tail": text[-6000:], "request": self._request_of(log.stem),
                            "note": self._note_of(log.stem), **prog, "result": prog["result"] or "failed"})
        out.sort(key=lambda c: -c["started"])
        return {"creations": out}

    def new_app(self, body: dict) -> dict:
        if not self.wc.projects_dir:
            raise ValueError("web.toml に projects_dir がありません（hootl setup を実行すると入ります）")
        name = str(body.get("name", "")).strip()
        if not stacks_mod.NAME_RE.match(name):
            raise ValueError("名前は英小文字・数字・ハイフンで3〜40文字（先頭は英字）にしてください。例: kakeibo, quiz-app")
        if name in self.wc.all_projects() or (self.wc.projects_dir / name).exists():
            raise ValueError(f"{name} はもうあります。別の名前にしてください")
        stack = stacks_mod.load_stack(str(body.get("stack", "")))
        options = [str(o) for o in body.get("options", [])]
        gates = [str(g) for g in body["gates"]] if isinstance(body.get("gates"), list) else None
        stacks_mod.check_selection(stack, options, gates)       # 組み合わせの誤りはここで返す
        title = str(body.get("title", "")).strip()[:80]
        idea = str(body.get("idea", "")).strip()
        if len(idea) > 20000:
            raise ValueError("作りたいものが長すぎます（2万字まで）")
        work = self.wc.projects_dir / ".hootl-new"
        work.mkdir(parents=True, exist_ok=True)
        args = ["new", name, "--stack", stack.name, "--runs-dir", str(self.wc.projects_dir)]
        if title:
            args += ["--title", title]
        for o in options:
            args += ["--option", o]
        if gates is not None:
            args += ["--gates", ",".join(gates)]
        if idea:
            (work / f"{name}.idea.txt").write_text(idea + "\n", encoding="utf-8")
            args += ["--idea-file", str(work / f"{name}.idea.txt")]
        go_on = bool(body.get("auto", True)) and bool(idea)     # できたら、そのまま実装まで進める
        (work / f"{name}.request.json").write_text(json.dumps(
            {"name": name, "title": title, "stack": stack.name, "options": options, "gates": gates, "idea": idea,
             "auto": go_on}, ensure_ascii=False), encoding="utf-8")
        (work / f"{name}.note").unlink(missing_ok=True)          # 前に同じ名前で作ったときの続きの記録
        job = self.jobs.start_cmd(f"new:{name}", "new", args, self.wc.projects_dir, work / f"{name}.log")
        if go_on:
            threading.Thread(target=self._continue_creation, args=(name, job), daemon=True).start()
        return {"name": name, "job": job.info()}

    def _continue_creation(self, name: str, job: Job) -> None:
        """アプリができたら、仕様の下書きから先（凍結 → 計画 → 実装）をおまかせで続ける。

        続けられなくても、下書きは「仕様」タブに残っているので、そこから人が続けられる。
        """
        job.proc.wait()
        if job.proc.returncode != 0:
            return
        try:
            p = self.project(name)
            if not p.cfg.auto_approve:
                return
            drafts = specs.list_specs(p.cfg)["drafts"]
            if not drafts:
                note = "仕様の下書きができなかったので、続きは始めていません（「仕様」タブからやり直せます）"
            else:
                self.spec_freeze(name, drafts[0]["slug"], auto=True)
                note = "おまかせで続きを始めました（凍結 → 計画 → 実装）。開くと進み具合が見られます"
        except Exception as exc:  # 続きが始まらなくても、作ったアプリは残す
            note = f"おまかせの続きを始められませんでした: {exc}（「仕様」タブから続けられます）"
        try:
            job.log_path.with_suffix(".note").write_text(note + "\n", encoding="utf-8")
        except OSError:
            pass

    def _note_of(self, name: str) -> str | None:
        """アプリができたあと、おまかせの続きがどうなったか（_continue_creation が書く）。"""
        try:
            return (self.wc.projects_dir / ".hootl-new" / f"{name}.note").read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _request_of(self, name: str) -> dict:
        """作り直すときの中身（画面で作ったものは依頼を丸ごと、古いものは名前と作りたいものだけ）。"""
        work = self.wc.projects_dir / ".hootl-new"
        try:
            return json.loads((work / f"{name}.request.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            idea = work / f"{name}.idea.txt"
            return {"name": name, "idea": idea.read_text(encoding="utf-8").strip() if idea.exists() else ""}

    def dismiss_creation(self, name: str) -> dict:
        """一覧から消す（ログを脇にどけるだけ。退避したフォルダには触らない）。"""
        if not self.wc.projects_dir or not stacks_mod.NAME_RE.match(name):
            raise LookupError("ありません")
        job = self.jobs.creations().get(name)
        if job and job.alive():
            raise ValueError("作っている途中です")
        log = self.wc.projects_dir / ".hootl-new" / f"{name}.log"
        if log.exists():
            log.rename(log.with_name(f"{name}.{time.strftime('%Y%m%d-%H%M%S')}.log.dismissed"))
        self.jobs.forget(f"new:{name}")
        return {"ok": True}

    def settings(self, name: str) -> dict:
        p = self.project(name)
        raw = tomllib.loads(p.config_path.read_text(encoding="utf-8"))
        status = stacks_mod.gate_status(raw)
        cfg = p.cfg
        return {"name": name, "repo": str(cfg.repo), "config": str(p.config_path), "catalog": status,
                "gates": [{"name": g.name, "cmd": g.cmd} for g in cfg.gates],
                "setup": [{"name": g.name, "cmd": g.cmd} for g in cfg.setup],
                "reviewer": " ".join(cfg.reviewer.cmd) if cfg.reviewer else None,
                "auto": {"approve": cfg.auto_approve, "run": cfg.auto_run, "hours": cfg.auto_hours,
                         "max_tasks": cfg.auto_max_tasks}}

    def set_gates(self, name: str, gates: list[str]) -> dict:
        p = self.project(name)
        self._idle(p)
        specs.ensure_main_clean(p.cfg, allow=str(p.config_path.resolve().relative_to(p.cfg.repo.resolve()))
                                if p.config_path.resolve().is_relative_to(p.cfg.repo.resolve()) else None)
        before = p.config_path.read_text(encoding="utf-8")
        chosen = stacks_mod.set_gates(p.config_path, gates)
        if p.config_path.read_text(encoding="utf-8") != before:
            try:
                specs.commit_file(p.cfg, p.config_path, f"chore: ゲートを {', '.join(chosen)} にする")
            except Exception:
                p.config_path.write_text(before, encoding="utf-8")   # コミットできなければ元に戻す
                raise
        p._cfg = None
        return self.settings(name)

    def reset(self, name: str, task_id: str) -> dict:
        p = self.project(name)
        if not p.queue().reset(task_id, only_blocked=True):
            raise ValueError("やり直せるのは blocked のタスクだけです")
        return {"ok": True}


# ---- HTTP -----------------------------------------------------------------
LOGIN_PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>hootl ログイン</title>
<style>body{font-family:system-ui,sans-serif;background:#f6f5f2;color:#222;display:grid;place-items:center;min-height:100vh;margin:0}
@media (prefers-color-scheme:dark){body{background:#17171a;color:#eee}input{background:#222;color:#eee}}
form{display:grid;gap:12px;width:min(360px,90vw)}input,button{font:inherit;padding:12px;border-radius:10px;border:1px solid #8885}
button{background:#2f6f4f;color:#fff;border:0}p{margin:0;color:#b33}
small{color:#777;line-height:1.7}code{font:13px ui-monospace,Menlo,monospace;border:1px solid #8885;border-radius:6px;padding:1px 6px}
a{color:inherit}</style></head>
<body><form method="post" action="/login"><h1 style="margin:0;font-size:20px">hootl</h1>
<input type="password" name="token" placeholder="トークン" autocomplete="current-password" autofocus>
<button>入る</button>{msg}
<small>トークンは、ターミナルで <code>hootl token</code> を実行するとクリップボードに入ります。<a href="/help">使い方</a></small>
</form></body></html>"""


def auto_of(body: dict) -> bool | None:
    """要求に auto があればそれ、なければ None（プロジェクトの設定に従う）。"""
    return bool(body["auto"]) if isinstance(body.get("auto"), bool) else None


def make_handler(api: Api):
    wc = api.wc

    class Handler(BaseHTTPRequestHandler):
        server_version = "hootl-web"

        def log_message(self, fmt, *args):  # 標準エラーへの1行ログ（トークンは出さない）
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

        # -- 認証 --
        def _email_ok(self) -> bool:
            if not wc.allowed_emails:
                return True
            email = (self.headers.get("Cf-Access-Authenticated-User-Email") or "").lower()
            return email in wc.allowed_emails

        def _authed(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"(?:^|;\s*)hootl_token=([^;]+)", cookie)
            return bool(m) and hmac.compare_digest(m.group(1), wc.token) and self._email_ok()

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _error(self, exc: Exception) -> None:
            code = 404 if isinstance(exc, LookupError) else 400 if isinstance(exc, ValueError) else 500
            self._json(code, {"error": str(exc)})

        # -- ルーティング --
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/login":
                return self._send(200, LOGIN_PAGE.replace("{msg}", "").encode(), "text/html; charset=utf-8")
            if not self._email_ok():
                return self._send(403, "Cloudflare Access のメールが許可されていません".encode(), "text/plain; charset=utf-8")
            if url.path in HELP_PAGES:
                page = HELP_PAGES[url.path]
                if not page.is_file():
                    return self._send(404, "このページはありません".encode(), "text/plain; charset=utf-8")
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if not self._authed():
                if url.path.startswith("/api/"):
                    return self._json(401, {"error": "ログインしてください"})
                return self._send(303, b"", "text/plain", {"Location": "/login"})
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path in ("/app.js", "/app.css"):
                ctype = "text/javascript; charset=utf-8" if url.path.endswith(".js") else "text/css; charset=utf-8"
                return self._send(200, (STATIC / url.path[1:]).read_bytes(), ctype)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = url.path.strip("/").split("/")
            try:
                if parts == ["api", "projects"]:
                    return self._json(200, api.projects())
                if parts == ["api", "stacks"]:
                    return self._json(200, api.stacks())
                if parts == ["api", "new"]:
                    return self._json(200, api.creations())
                if len(parts) >= 4 and parts[:2] == ["api", "p"]:
                    name, what = parts[2], parts[3]
                    if what == "tasks":
                        return self._json(200, api.tasks(name))
                    if what == "task" and len(parts) == 5:
                        return self._json(200, api.task(name, parts[4]))
                    if what == "log":
                        text = api.log(name, qs.get("task", ""), qs.get("attempt", ""), qs.get("file", ""))
                        return self._send(200, text.encode(), "text/plain; charset=utf-8")
                    if what == "reports":
                        return self._json(200, api.reports(name))
                    if what == "proposed":
                        return self._json(200, api.proposed(name))
                    if what == "specs":
                        return self._json(200, api.specs(name))
                    if what == "ideas":
                        return self._json(200, api.ideas(name))
                    if what == "spec":
                        return self._json(200, api.spec(name, qs.get("ref", "")))
                    if what == "settings":
                        return self._json(200, api.settings(name))
                return self._json(404, {"error": "ありません"})
            except Exception as exc:
                return self._error(exc)

        def do_POST(self):
            url = urlparse(self.path)
            length = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
            raw = self.rfile.read(length) if length else b""
            if url.path == "/login":
                token = parse_qs(raw.decode("utf-8", "replace")).get("token", [""])[0]
                if self._email_ok() and hmac.compare_digest(token, wc.token):
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" or \
                        self.headers.get("Cf-Visitor", "").find("https") >= 0 else ""
                    return self._send(303, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"hootl_token={wc.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000{secure}"})
                time.sleep(1)  # 総当たりを遅くする
                page = LOGIN_PAGE.replace("{msg}", "<p>トークンが違います</p>")
                return self._send(401, page.encode(), "text/html; charset=utf-8")
            if not self._authed():
                return self._json(401, {"error": "ログインしてください"})
            # 別サイトからのフォーム送信を弾く（独自ヘッダは CORS の事前確認なしに付けられない）
            if self.headers.get("X-Hootl") != "1":
                return self._json(403, {"error": "不正な要求です"})
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "JSON が読めません"})
            parts = url.path.strip("/").split("/")
            try:
                if parts == ["api", "new"]:
                    return self._json(200, api.new_app(body))
                if parts == ["api", "new", "dismiss"]:
                    return self._json(200, api.dismiss_creation(str(body.get("name", ""))))
                if len(parts) == 4 and parts[:2] == ["api", "p"]:
                    name, what = parts[2], parts[3]
                    if what == "proposed":
                        return self._json(200, api.save_proposed(name, str(body.get("text", ""))))
                    if what == "import":
                        return self._json(200, api.import_proposed(name))
                    if what == "proposed-discard":
                        return self._json(200, api.discard_proposed(name))
                    if what == "idea-add":
                        return self._json(200, api.idea_add(name, str(body.get("title", "")), str(body.get("body", ""))))
                    if what in ("idea-drop", "idea-restore"):
                        return self._json(200, api.idea_mark(name, int(body.get("id") or 0),
                                                             "dropped" if what == "idea-drop" else "new"))
                    if what == "idea-build":
                        return self._json(200, api.idea_build(name, int(body.get("id") or 0)))
                    if what == "plan":
                        return self._json(200, api.plan(name, body.get("ref") or None))
                    if what == "spec-draft":
                        return self._json(200, api.spec_draft(name, str(body.get("idea", "")), auto_of(body)))
                    if what == "spec-save":
                        return self._json(200, api.spec_save(name, str(body.get("slug", "")), str(body.get("text", ""))))
                    if what == "spec-discard":
                        return self._json(200, api.spec_discard(name, str(body.get("slug", ""))))
                    if what == "spec-freeze":
                        return self._json(200, api.spec_freeze(name, str(body.get("slug", "")), auto_of(body)))
                    if what == "run":
                        hours = float(body["hours"]) if body.get("hours") else None
                        conc = int(body["concurrency"]) if body.get("concurrency") else None
                        return self._json(200, api.run(name, hours, conc))
                    if what == "stop":
                        return self._json(200, api.stop(name))
                    if what == "reset":
                        return self._json(200, api.reset(name, str(body.get("id", ""))))
                    if what == "gates":
                        return self._json(200, api.set_gates(name, [str(g) for g in body.get("gates", [])]))
                return self._json(404, {"error": "ありません"})
            except Exception as exc:
                return self._error(exc)

    return Handler


def raise_file_limit(want: int = 4096) -> None:
    """開けるファイルの上限を上げる（launchd から起動すると 256 しかない）。上げられなくても動く。"""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError):
        pass


def serve(config_path: str) -> None:
    wc = load_web_config(config_path)
    raise_file_limit()
    if wc.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"注意: {wc.host} で待ち受けます。外に出すなら Cloudflare Tunnel と Access を通してください", file=sys.stderr)
    httpd = ThreadingHTTPServer((wc.host, wc.port), make_handler(Api(wc)))
    names = ', '.join(wc.all_projects()) or f'まだなし（{wc.projects_dir} の下に作ると出てくる）'
    print(f"hootl web: http://{wc.host}:{wc.port}  （プロジェクト: {names}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
