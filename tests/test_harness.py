"""偽エージェントでハーネスの全経路を通す。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hootl import agents, gitops, guard
from hootl.cli import main
from hootl.config import load
from hootl.queue import Queue

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fixtures" / "fake_agent.py"

CHECK = """
import pathlib, sys
bad = [p.name for p in pathlib.Path('src').glob('*.txt') if p.read_text() != 'ok']
print('bad:', bad)
sys.exit(1 if bad else 0)
"""


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    (repo / "check.py").write_text(CHECK)
    (repo / ".gitignore").write_text(".hootl/\n.setup_done\n")
    (repo / "README.md").write_text("target\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    scenario = tmp_path / "scenario.json"
    prompt_log = tmp_path / "prompts.log"
    monkeypatch.setenv("FAKE_SCENARIO", str(scenario))
    monkeypatch.setenv("FAKE_PROMPT_LOG", str(prompt_log))
    (repo / "hootl.toml").write_text(f"""
repo = "."
prompts_dir = "{ROOT / 'prompts'}"
[run]
concurrency = 2
max_attempts = 3
review_retry_wait_sec = 0
[agents.implementer]
cmd = ["{sys.executable}", "{FAKE}"]
[agents.reviewer]
cmd = ["{sys.executable}", "{FAKE}"]
[[setup]]
name = "prepare"
cmd = "{sys.executable} -c \\"open('.setup_done', 'w').write('1')\\""
[[gates]]
name = "check"
cmd = "{sys.executable} check.py"
[guard]
protected_paths = ["hootl.toml", "check.py"]
test_globs = ["*.test.ts"]
forbidden_patterns = ['\\b(it|test|describe)\\.(skip|only|todo)\\(']
""")
    return repo, scenario, prompt_log


def write_tasks(repo, scenario_path, tasks):
    lines = []
    scenario = {}
    for t in tasks:
        scenario[t["id"]] = t.pop("scenario", "ok")
        t.setdefault("title", t["id"])
        t.setdefault("description", "test task")
        t.setdefault("acceptance", ["src/<id>.txt が ok"])
        t.setdefault("paths", ["src/*"])
        lines.append(json.dumps(t))
    scenario_path.write_text(json.dumps(scenario))
    path = repo / "tasks.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def run_cli(repo, *args):
    return main(["-c", str(repo / "hootl.toml"), *args])


def statuses(repo):
    q = Queue(load(repo / "hootl.toml").state_dir / "state.db")
    return {t.id: (t.status, t.attempts) for t in q.all()}


def test_end_to_end_paths(project):
    repo, scenario, prompt_log = project
    tasks = write_tasks(repo, scenario, [
        {"id": "T-001"},                                         # 一発で通る
        {"id": "T-002", "scenario": "fail_once"},                # ゲートで1回落ちて直る
        {"id": "T-003", "scenario": "review_fail_once"},         # レビューで1回落ちて直る
        {"id": "T-004", "scenario": "never"},                    # 3回落ちて blocked
        {"id": "T-005", "scenario": "outside"},                  # 許可パス外 → blocked
        {"id": "T-006", "scenario": "skip"},                     # テスト無効化 → blocked
        {"id": "T-007", "scenario": "crash"},                    # エージェント異常終了 → blocked
        {"id": "T-008", "scenario": "review_garbage"},           # verdict なし → 無言合格させない
        {"id": "T-011", "scenario": "review_flaky"},             # レビュー役が1回だけ無言で落ちる → レビューだけやり直して通る
        {"id": "T-009", "depends_on": ["T-001"]},                # 依存先 done → 走る
        {"id": "T-010", "depends_on": ["T-004"]},                # 依存先 blocked → 走らない
    ])
    assert run_cli(repo, "import", str(tasks)) == 0
    assert run_cli(repo, "run") == 0

    s = statuses(repo)
    assert s["T-001"] == ("done", 1)
    assert s["T-002"] == ("done", 2)
    assert s["T-003"] == ("done", 2)
    assert s["T-004"] == ("blocked", 3)
    assert s["T-005"][0] == "blocked"
    assert s["T-006"][0] == "blocked"
    assert s["T-007"][0] == "blocked"
    assert s["T-008"] == ("blocked", 1)          # 判定が返らないのは実装のせいではないので、実装をやり直さずに人へ回す
    assert s["T-009"] == ("done", 1)
    assert s["T-010"] == ("todo", 0)
    assert s["T-011"] == ("done", 1)             # レビューだけをやり直して通る（試行は増えない）
    q = Queue(load(repo / "hootl.toml").state_dir / "state.db")
    assert "レビュー役が判定を返しませんでした" in q.get("T-008").last_error
    assert (repo / ".hootl" / "runs" / "T-011" / "1" / "review-noverdict-1.log").is_file()
    assert "終了コード" in (repo / ".hootl" / "runs" / "T-011" / "1" / "review.log").read_text()

    # main には通ったものだけが入り、ゲートが通る状態
    files = sorted(p.name for p in (repo / "src").glob("*.txt"))
    assert files == ["T-001.txt", "T-002.txt", "T-003.txt", "T-009.txt", "T-011.txt"]
    assert subprocess.run([sys.executable, "check.py"], cwd=repo).returncode == 0
    assert not (repo / "other").exists()

    # 差し戻し内容が次の試行のプロンプトに入っている
    log = prompt_log.read_text()
    assert "ゲート `check` で落ちました" in log
    assert "AC1 のテストがない" in log

    # worktree とタスクブランチが残っていない
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "task/" not in branches

    # レポートに blocked と巻き込みが出る
    report = sorted((repo / ".hootl").glob("report-*.md"))[-1].read_text()
    assert "T-004" in report and "T-010" in report
    assert "$" in report


def test_import_does_not_overwrite_done(project):
    repo, scenario, _ = project
    tasks = write_tasks(repo, scenario, [{"id": "T-001"}])
    run_cli(repo, "import", str(tasks))
    run_cli(repo, "run")
    tasks = write_tasks(repo, scenario, [{"id": "T-001", "title": "changed"}])
    run_cli(repo, "import", str(tasks))
    q = Queue(repo / ".hootl" / "state.db")
    assert q.all()[0].title == "T-001"


def test_reset_and_recover(project):
    repo, scenario, _ = project
    tasks = write_tasks(repo, scenario, [{"id": "T-001", "scenario": "crash"}])
    run_cli(repo, "import", str(tasks))
    run_cli(repo, "run")
    assert statuses(repo)["T-001"][0] == "blocked"
    scenario.write_text(json.dumps({"T-001": "ok"}))
    run_cli(repo, "reset", "T-001")
    q = Queue(repo / ".hootl" / "state.db")
    q.claim_next()                       # running のまま放置された状態を作る
    assert q.recover_running() == 1
    run_cli(repo, "run")
    assert statuses(repo)["T-001"] == ("done", 1)


def test_push_to_remote(project, tmp_path):
    repo, scenario, _ = project
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    sh(repo, "git", "remote", "add", "origin", str(remote))
    sh(repo, "git", "push", "-q", "origin", "main")
    # 人が別の場所から push した変更（ラン開始時に取り込まれるべきもの）
    other = tmp_path / "other"
    sh(tmp_path, "git", "clone", "-q", str(remote), str(other))
    (other / "HUMAN.md").write_text("human\n")
    sh(other, "git", "add", "-A")
    sh(other, "git", "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "human")
    sh(other, "git", "push", "-q", "origin", "main")

    toml = repo / "hootl.toml"
    toml.write_text(toml.read_text().replace("[run]\n", '[run]\npush_remote = "origin"\n'))
    tasks = write_tasks(repo, scenario, [{"id": "T-001"}, {"id": "T-002"}])
    run_cli(repo, "import", str(tasks))
    assert run_cli(repo, "run") == 0

    log = subprocess.run(["git", "log", "--oneline", "main"], cwd=remote, capture_output=True, text=True).stdout
    assert "merge T-001" in log and "merge T-002" in log and "human" in log


def test_run_refuses_when_main_diverged(project, tmp_path):
    repo, scenario, _ = project
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    sh(repo, "git", "remote", "add", "origin", str(remote))
    sh(repo, "git", "push", "-q", "origin", "main")
    other = tmp_path / "other"
    sh(tmp_path, "git", "clone", "-q", str(remote), str(other))
    (other / "A.md").write_text("remote\n")
    sh(other, "git", "add", "-A")
    sh(other, "git", "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "remote side")
    sh(other, "git", "push", "-q", "origin", "main")
    (repo / "B.md").write_text("local\n")
    sh(repo, "git", "add", "B.md")
    sh(repo, "git", "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "local side")

    toml = repo / "hootl.toml"
    toml.write_text(toml.read_text().replace("[run]\n", '[run]\npush_remote = "origin"\n'))
    tasks = write_tasks(repo, scenario, [{"id": "T-001"}])
    run_cli(repo, "import", str(tasks))
    assert run_cli(repo, "run") == 2
    assert statuses(repo)["T-001"] == ("todo", 0)


def test_merge_conflict_is_reported(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    wt, _ = gitops.create_worktree(repo, tmp_path / "wt", "T-1", "main")
    (wt / "a.txt").write_text("branch\n")
    gitops.commit_all(wt, "branch")
    (repo / "a.txt").write_text("main\n")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "main")
    ok, info = gitops.merge_to_main(repo, "task/T-1", "main", "m")
    assert not ok and "CONFLICT" in info
    assert (repo / "a.txt").read_text() == "main\n"      # 中途半端なマージが残らない


def test_guard_rules():
    r = guard.check([("M", "src/a.ts")], "+ok", ["src/*"], [], [], [])
    assert r.ok
    r = guard.check([], "", ["src/*"], [], [], [])
    assert not r.ok
    r = guard.check([("D", "src/a.test.ts")], "", ["src/*"], [], ["*.test.ts"], [])
    assert any("削除" in v for v in r.violations)
    r = guard.check([("M", "hootl.toml")], "", ["*"], ["hootl.toml"], [], [])
    assert any("保護" in v for v in r.violations)
    # どのタスクでも触ってよいもの（依存・スキーマ）は paths 外でも通す
    r = guard.check([("M", "src/a.ts"), ("M", "package.json")], "+ok", ["src/*"], [], [], [],
                    ["package.json"])
    assert r.ok
    # ただし判定する側（protected）は always_allowed より優先して弾く
    r = guard.check([("M", "vitest.config.ts")], "", ["src/*"], ["vitest.config.*"], [], [],
                    ["vitest.config.*"])
    assert not r.ok
    # out_of_scope="review" なら範囲外は弾かずにレビューへ回す（保護ファイルは引き続き弾く）
    r = guard.check([("M", "src/a.ts"), ("M", "lib/b.ts")], "+ok", ["src/*"], [], [], [],
                    out_of_scope="review")
    assert r.ok and r.out_of_scope == ["lib/b.ts"]
    r = guard.check([("M", "hootl.toml")], "", ["src/*"], ["hootl.toml"], [], [], out_of_scope="review")
    assert not r.ok


def test_out_of_scope_goes_to_review(project):
    repo, scenario, prompt_log = project
    toml = repo / "hootl.toml"
    toml.write_text(toml.read_text().replace("[guard]\n", '[guard]\nout_of_scope = "review"\n'))
    tasks = write_tasks(repo, scenario, [{"id": "T-001", "scenario": "outside"}])
    run_cli(repo, "import", str(tasks))
    run_cli(repo, "run")
    assert statuses(repo)["T-001"] == ("done", 1)          # 偽レビューは pass を返す
    assert "other/T-001.txt" in prompt_log.read_text()     # 範囲外の変更がレビューに渡っている


def test_verdict_parsing():
    assert agents.parse_verdict('text\n{"verdict": "pass", "findings": []}').passed
    v = agents.parse_verdict('{"verdict": "pass"}\n…\n{"verdict": "fail", "findings": ["x"]}')
    assert not v.passed and v.findings == ["x"]
    assert not agents.parse_verdict("looks good").passed
