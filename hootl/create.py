"""hootl new: カタログの型から新しいアプリを作る。

フォルダを切る → git → ひな形を置く → 依存を入れる → ループと同じ準備・ゲートを全部通す
→ 起動確認 → hootl.toml を置いてコミット → （作りたいものがあれば）仕様の下書き。
下書きから先（凍結 → 計画 → 実装）は、管理画面がおまかせで続ける（web.py の Api._continue_creation）。

進み具合は1行ずつ標準出力に出す（管理画面がこれを読んでチェックリストにする）:
    ::plan [{"id": ..., "label": ...}, ...]
    ::step <id> start|ok|fail|skip [補足]
    ::done <名前>     または   ::failed <名前> <退避先>

hootl.toml は最後に置く。管理画面は <runs_dir>/*/hootl.toml を見てプロジェクトを出すので、
出来上がるまでは一覧に出ない。途中で失敗したら <runs_dir>/.failed/ に退避する（消さない）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import stacks

DOTFILES = {"gitignore", "env", "npmrc"}   # ひな形では先頭の . を外して置く（隠しファイルにしない）
RESERVED_PORTS = {8765, 8799}


@dataclass
class Request:
    runs_dir: Path
    name: str
    stack: str
    title: str = ""
    options: list[str] = field(default_factory=list)
    gates: list[str] | None = None          # None ならカタログの既定
    idea: str = ""
    python: str = sys.executable
    draft_spec: bool = True                 # 作りたいものがあれば仕様の下書きまで作る


class Failed(Exception):
    pass


def say(line: str) -> None:
    print(line, flush=True)


def step(sid: str, state: str, note: str = "") -> None:
    say(f"::step {sid} {state}" + (f" {note}" if note else ""))


# ---- 下ごしらえ --------------------------------------------------------------
def validate(req: Request) -> stacks.Stack:
    if not stacks.NAME_RE.match(req.name):
        raise Failed("名前は英小文字・数字・ハイフンで3〜40文字（先頭は英字）にしてください。例: kakeibo, quiz-app")
    if (req.runs_dir / req.name).exists():
        raise Failed(f"{req.runs_dir / req.name} はもうあります。別の名前にしてください")
    stack = stacks.load_stack(req.stack)
    stacks.check_selection(stack, req.options, req.gates)
    return stack


def missing_tools(stack: stacks.Stack, options: list[str]) -> list[str]:
    need = list(stack.requires)
    for o in options:
        need += stack.options[o].requires
    out = []
    for tool in dict.fromkeys(need):
        if not shutil.which(tool):
            out.append(f"{tool} が見つかりません")
        elif tool == "node" and (stack.raw.get("min_node")):
            major = _node_major()
            if major is None or major < int(stack.raw["min_node"]):
                out.append(f"Node {stack.raw['min_node']} 以上が要ります（いまの node は v{major}）。"
                           "管理画面を、nvm などで新しい Node に切り替えたターミナルから起動し直してください"
                           "（hootl-start.command をダブルクリックし直す）")
        elif tool == "docker":
            ok = subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
            if not ok:
                out.append("Docker が起動していません（Docker Desktop を起動してください）")
    return out


def _node_major() -> int | None:
    try:
        out = subprocess.run(["node", "-p", "process.versions.node.split('.')[0]"], capture_output=True,
                             text=True, timeout=20)
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def used_vars(runs_dir: Path) -> dict[str, set[int]]:
    used: dict[str, set[int]] = {"port": set(RESERVED_PORTS), "e2e_port": set(), "db_port": set()}
    for cfg in runs_dir.glob("*/hootl.toml"):
        try:
            v = tomllib.loads(cfg.read_text(encoding="utf-8")).get("stack", {}).get("vars", {})
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for k in used:
            if isinstance(v.get(k), int):
                used[k].add(v[k])
    return used


def allocate(runs_dir: Path) -> dict[str, int]:
    """アプリごとに重ならないポートを決める（開発サーバー・E2E・DB）。"""
    used = used_vars(runs_dir)
    port = next(p for p in range(3101, 3999) if p not in used["port"] and p + 1000 not in used["e2e_port"])
    db = next(p for p in range(54401, 54999) if p not in used["db_port"])
    return {"port": port, "e2e_port": port + 1000, "db_port": db}


def template_vars(req: Request, stack: stacks.Stack, ports: dict[str, int], gate_names: list[str]) -> dict[str, str]:
    now = datetime.now()
    title = req.title.strip() or req.name
    idea_lines = [ln.strip() for ln in req.idea.strip().splitlines() if ln.strip()]
    all_gates = {g.name: g for g in stack.gates(req.options)}
    base = {
        "name": req.name, "pkg": req.name.replace("-", "_"), "title": title,
        "hootl": str(stacks.ROOT), "python": req.python, "repo": str(req.runs_dir / req.name),
        "today": now.strftime("%Y-%m-%d"), "created": now.strftime("%Y-%m-%d %H:%M"),
        **{k: str(v) for k, v in ports.items()},
        "stack": stack.name, "stack_title": stack.title, "stack_tech": stack.tech,
        "summary_line": idea_lines[0] if idea_lines else f"{title}（{stack.title}）",
        "idea_or_placeholder": req.idea.strip() or "（管理画面の「仕様」タブで、作りたいものを書くと下書きができる）",
        "gate_titles": "、".join(all_gates[n].title for n in gate_names),
        "option_titles": "、".join(stack.options[o].title for o in req.options) or "なし",
        "option_lines": "".join(f"- {stack.options[o].title}: {stack.options[o].summary}\n" for o in req.options),
    }
    base["stack_notes"] = stacks.substitute(stack.notes.strip(), base)
    return base


# ---- ファイル -----------------------------------------------------------------
def layer_dirs(stack: stacks.Stack, options: list[str]) -> list[Path]:
    root = stacks.default_dir()
    dirs = [root / stacks.COMMON / "files", root / stack.name / "files"]
    dirs += [root / stack.name / "options" / o / "files" for o in options]
    return [d for d in dirs if d.is_dir()]


def place_files(dest: Path, stack: stacks.Stack, options: list[str], variables: dict[str, str]) -> list[str]:
    written = []
    for layer in layer_dirs(stack, options):
        for src in sorted(p for p in layer.rglob("*") if p.is_file()):
            rel = Path(stacks.substitute(str(src.relative_to(layer)), variables))   # フォルダ名の {{pkg}} なども埋める
            is_tmpl = rel.name.endswith(".tmpl")
            name = rel.name[:-5] if is_tmpl else rel.name
            if rel.parent == Path(".") and name in DOTFILES:
                name = "." + name
            out = dest / rel.parent / name
            out.parent.mkdir(parents=True, exist_ok=True)
            if is_tmpl:
                out.write_text(stacks.substitute(src.read_text(encoding="utf-8"), variables), encoding="utf-8")
            else:
                shutil.copyfile(src, out)
            written.append(str(out.relative_to(dest)))
    return written


def hootl_toml(req: Request, stack: stacks.Stack, ports: dict[str, int], gate_names: list[str],
               variables: dict[str, str]) -> str:
    def arr(xs: list[str]) -> str:
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in xs) + "]"

    used = {k: v for k, v in ports.items() if k != "db_port" or "postgres" in req.options}
    return "\n".join([
        f"# {variables['title']} — hootl のカタログ「{stack.title}」から作成（{variables['created']}）",
        f"# ゲート・準備・安全柵の中身は {stacks.ROOT}/stacks/{stack.name}/stack.toml にあり、同じ型のアプリで共通。",
        "# 使うゲートは管理画面の「設定」で切り替えられる（下の gates の行が書き換わる）。",
        "# このアプリだけの設定は下に書き足す（[[gates]] を書くと、カタログのゲートの後に走る）。",
        'spec = "spec.md"',
        "",
        "[stack]",
        f"name = {json.dumps(stack.name)}",
        f"app = {json.dumps(req.name)}",
        f"title = {json.dumps(variables['title'], ensure_ascii=False)}",
        f"options = {arr(req.options)}",
        stacks.render_gates_line(gate_names),
        "",
        "[stack.vars]",
        *[f"{k} = {v}" for k, v in used.items()],
        f"python = {json.dumps(req.python)}",
        "",
    ])


# ---- 実行 -----------------------------------------------------------------------
def sh(cmd: str, cwd: Path, env: dict[str, str], timeout: int) -> None:
    """1行ずつ流しながら実行する（管理画面に途中経過が出るように）。"""
    say(f"$ {cmd}")
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env={**os.environ, **env}, text=True, errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + timeout
    assert proc.stdout is not None
    for line in proc.stdout:
        say(line.rstrip("\n"))
        if time.time() > deadline:
            break
    try:
        proc.wait(timeout=max(1, deadline - time.time()))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 15)
        proc.wait()
        raise Failed(f"{timeout} 秒で終わりませんでした: {cmd}")
    if time.time() > deadline and proc.returncode is None:
        raise Failed(f"{timeout} 秒で終わりませんでした: {cmd}")
    if proc.returncode != 0:
        raise Failed(f"失敗しました（コード {proc.returncode}）: {cmd}")


def git(dest: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=dest, check=True, capture_output=True, text=True).stdout.strip()


def commit(dest: Path, message: str) -> str:
    git(dest, "add", "-A")
    ident = []
    for key, fallback in (("user.name", "hootl"), ("user.email", "hootl@localhost")):
        if subprocess.run(["git", "config", key], cwd=dest, capture_output=True).returncode != 0:
            ident += ["-c", f"{key}={fallback}"]
    subprocess.run(["git", *ident, "commit", "-q", "-m", message], cwd=dest, check=True)
    return git(dest, "rev-parse", "--short", "HEAD")


def plan_steps(stack: stacks.Stack, req: Request, setup: list[dict], gate_names: list[str]) -> list[dict]:
    steps = [{"id": "check", "label": "名前と道具を確かめる"},
             {"id": "folder", "label": f"フォルダを切り、git を始める（{req.name}）"},
             {"id": "files", "label": "ひな形を置く"}]
    scaffold = list(stack.raw.get("scaffold", []))
    for o in req.options:
        scaffold += stack.options[o].raw.get("scaffold", [])
    steps += [{"id": f"scaffold-{i}", "label": s["name"]} for i, s in enumerate(scaffold)]
    steps += [{"id": f"setup-{s['name']}", "label": f"準備: {s['name']}"} for s in setup]
    gates = {g.name: g for g in stack.gates(req.options)}
    steps += [{"id": f"gate-{n}", "label": f"ゲート: {gates[n].title}"} for n in gate_names]
    steps += [{"id": f"verify-{i}", "label": v["name"]} for i, v in enumerate(stack.raw.get("verify", []))]
    steps += [{"id": "commit", "label": "hootl.toml を置いて最初のコミット"}]
    if req.idea.strip() and req.draft_spec:
        steps += [{"id": "spec", "label": "作りたいものから仕様の下書きを作る"}]
    return steps


def create(req: Request) -> int:
    req.runs_dir = req.runs_dir.expanduser().resolve()
    dest = req.runs_dir / req.name
    started = False
    current = "check"
    try:
        stack = validate(req)
        gate_names = stacks.check_selection(stack, req.options, req.gates)
        ports = allocate(req.runs_dir)
        variables = template_vars(req, stack, ports, gate_names)
        section = {"name": stack.name, "app": req.name, "title": variables["title"], "options": req.options,
                   "gates": gate_names, "vars": {**ports, "python": req.python}}
        composed = stacks.compose(section, dest)
        env = {str(k): str(v) for k, v in composed.get("env", {}).items()}
        steps = plan_steps(stack, req, composed["setup"], gate_names)
        say("::plan " + json.dumps(steps, ensure_ascii=False))

        step("check", "start")
        problems = missing_tools(stack, req.options)
        if not shutil.which("git"):
            problems.append("git が見つかりません")
        if problems:
            raise Failed("、".join(problems))
        step("check", "ok", f"{stack.title}・ポート {ports['port']}")

        current = "folder"
        step(current, "start")
        req.runs_dir.mkdir(parents=True, exist_ok=True)
        dest.mkdir()
        started = True
        git(dest, "init", "-q", "-b", "main")
        step(current, "ok", str(dest))

        current = "files"
        step(current, "start")
        written = place_files(dest, stack, req.options, variables)
        step(current, "ok", f"{len(written)} ファイル")

        scaffold = list(stack.raw.get("scaffold", []))
        for o in req.options:
            scaffold += stack.options[o].raw.get("scaffold", [])
        for i, s in enumerate(scaffold):
            current = f"scaffold-{i}"
            step(current, "start")
            sh(stacks.substitute(s["cmd"], variables), dest, env, int(s.get("timeout_sec", 900)))
            step(current, "ok")

        # ここからはループと同じ条件（毎回の準備 → ゲート）で通ることを確かめる
        for s in composed["setup"]:
            current = f"setup-{s['name']}"
            step(current, "start")
            sh(s["cmd"], dest, env, int(s.get("timeout_sec", 600)))
            step(current, "ok")
        for g in composed["gates"]:
            current = f"gate-{g['name']}"
            step(current, "start")
            t0 = time.time()
            sh(g["cmd"], dest, env, int(g.get("timeout_sec", 600)))
            step(current, "ok", f"{time.time() - t0:.0f} 秒")
        for i, v in enumerate(stack.raw.get("verify", [])):
            current = f"verify-{i}"
            step(current, "start")
            sh(stacks.substitute(v["cmd"], variables), dest, env, int(v.get("timeout_sec", 300)))
            step(current, "ok")

        current = "commit"
        step(current, "start")
        (dest / "hootl.toml").write_text(hootl_toml(req, stack, ports, gate_names, variables), encoding="utf-8")
        from .config import load   # 置いた hootl.toml が読めることを確かめてからコミット
        load(dest / "hootl.toml")
        opts = "・".join(stack.options[o].title for o in req.options)
        sha = commit(dest, f"chore: ひな形から作成（{stack.title}{'・' + opts if opts else ''}）\n\nhootl new {req.name} --stack {stack.name}")
        step(current, "ok", sha)
    except Failed as exc:
        step(current, "fail", str(exc))
        return _give_up(req, dest, started)
    except Exception as exc:  # 想定外でも退避まではする
        step(current, "fail", f"{type(exc).__name__}: {exc}")
        return _give_up(req, dest, started)

    if req.idea.strip() and req.draft_spec:
        step("spec", "start")
        try:
            slug = draft_spec(dest, req.idea)
            step("spec", "ok", slug)
        except Exception as exc:   # 下書きはアプリ作成の成否に含めない（仕様タブからやり直せる）
            step("spec", "fail", f"{exc}（仕様タブからやり直せます）")
    say(f"::done {req.name}")
    return 0


def _give_up(req: Request, dest: Path, started: bool) -> int:
    if not started or not dest.exists():
        say(f"::failed {req.name} -")
        return 1
    failed_dir = req.runs_dir / ".failed"
    failed_dir.mkdir(exist_ok=True)
    moved = failed_dir / f"{req.name}-{datetime.now():%Y%m%d-%H%M%S}"
    dest.rename(moved)
    say(f"::failed {req.name} {moved}")
    return 1


def draft_spec(repo: Path, idea: str) -> str:
    from . import auto, specs
    from .config import load
    cfg = load(repo / "hootl.toml")
    slug = specs.new_slug(cfg)
    ddir = specs.drafts_dir(cfg)
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / f"{slug}.idea.txt").write_text(idea, encoding="utf-8")
    try:
        # おまかせで先へ進む設定なら、決めきれない点も書き手に決めさせる（読む人がいないので、質問を残さない）
        auto.draft(cfg, idea, ddir / f"{slug}.md", decide=cfg.auto_approve)
    except auto.Stopped as exc:
        raise Failed(str(exc)) from exc
    return slug
