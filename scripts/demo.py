"""デモ環境を作って管理画面を開く（本物のエージェントは使わない。claude・codex・npm がなくても動く）。

    python3 scripts/demo.py                 # /tmp/hootl-demo に作って http://127.0.0.1:8799 で開く
    python3 scripts/demo.py --no-serve      # 作るだけ
    python3 scripts/demo.py --fresh         # 作り直す

トークンは demo-token-0123456789abcdef。中身:
- quiz-demo: 偽のエージェントで1回回したプロジェクト（完了・要対応・待ちのタスク、レポート、
  仕様の下書きと凍結済みの仕様、タスク案、ネタ帳）。画面から回す・計画する・仕様を作る・おまかせで作るも、
  偽のエージェントで動く（おまかせは、承認待ちのタスク案を取り込むか捨てると始められる）
- kakeibo: カタログ（Webアプリ型）から作った体のプロジェクト。設定タブでゲートを切り替えられる
  （依存は入れていないので、回すとゲートで落ちる）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fixtures" / "fake_agent.py"
TOKEN = "demo-token-0123456789abcdef"

TASKS = [
    ("T-001", "問題データの型を決める", "出題", 10, "ok"),
    ("T-002", "4択の正誤判定", "出題", 11, "idea"),              # 実装とレビューの途中で、案を1つずつ見つける
    ("T-003", "今日の出題キューを作る", "出題", 12, "ok"),
    ("T-004", "間違えた問題を記録する", "復習", 20, "review_fail_once"),
    ("T-005", "外部の過去問サイトから自動で取り込む", "過去問", 30, "never"),
    ("T-006", "論点ごとの正答率を出す", "弱点", 40, "ok"),
    ("T-007", "苦手な論点から優先して出す", "弱点", 41, "ok"),
    ("T-008", "模試モード（時間制限つき）", "模試", 50, "ok"),
]

SPEC = """# 過去問サイト（デモ）

## 目的
資格試験の過去問を、毎日少しずつ解ける自分用のサイト。

## 前提と制約
- 型: Webアプリ。ゲートは check.py（デモ用）

## やること
- 出題・復習・弱点・模試

## やらないこと
- 他人との共有
"""

DRAFT = """# 間違えた問題の復習モード

## 目的
間違えた過去問だけを、もう一度解けるようにする。

## やること
- 間違えた問題だけを出す
- 3回続けて正解したら卒業させる

## やらないこと
- 解説の自動生成

## 前提と制約
- 今の出題キューを使う

## 決めてほしいこと
- 卒業までの正解回数は3回でよいか
"""

FROZEN = """# 論点ごとの弱点

## 目的
論点ごとの正答率を見て、苦手なところから解く。

## やること
- 論点ごとの正答率
- 苦手な論点から優先して出す

## やらないこと
- グラフの書き出し
"""

PROPOSED = [
    {"id": "T-009", "title": "間違えた問題だけを出すキュー", "description": "復習モードの出題キュー",
     "acceptance": ["間違えた問題だけが並ぶ", "卒業した問題は出ない"], "paths": ["src/lib/review/*"],
     "depends_on": ["T-004"], "epic": "復習", "priority": 21},
    {"id": "T-010", "title": "3回続けて正解したら卒業", "description": "卒業の判定",
     "acceptance": ["3回続けて正解で卒業", "途中で間違えたら数え直す"], "paths": ["src/lib/review/*"],
     "depends_on": ["T-009"], "epic": "復習", "priority": 22},
]

# カタログから作ったときの進み具合（新しいアプリの画面の見本。中身は本物の作成と同じ形）
CREATION_LOG = [
    ("check", "名前と道具を確かめる", "Webアプリ・ポート 3101"),
    ("folder", "フォルダを切り、git を始める（kakeibo）", ""),
    ("files", "ひな形を置く", "17 ファイル"),
    ("scaffold-0", "node の版を記録", ""),
    ("scaffold-1", "依存を入れる", ""),
    ("scaffold-2", "開発用の依存を入れる", ""),
    ("setup-node", "準備: node", ""),
    ("setup-install", "準備: install", ""),
    ("gate-lint", "ゲート: lint（ESLint）", "3 秒"),
    ("gate-typecheck", "ゲート: 型チェック", "4 秒"),
    ("gate-unit", "ゲート: 単体テスト（Vitest）", "1 秒"),
    ("gate-build", "ゲート: 本番ビルド", "12 秒"),
    ("verify-0", "起動確認", ""),
    ("commit", "hootl.toml を置いて最初のコミット", "1f14729"),
    ("spec", "作りたいものから仕様の下書きを作る", "20261126-110355"),
]


def sh(*args: str, cwd: Path, env: dict | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, env=env, stdout=subprocess.DEVNULL)


def git_commit(repo: Path, message: str) -> None:
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=hootl-demo", "-c", "user.email=demo@localhost", "commit", "-q", "-m", message, cwd=repo)


def demo_env(base: Path) -> dict:
    return {**os.environ, "PYTHONPATH": str(ROOT), "FAKE_SCENARIO": str(base / "scenario.json"),
            "FAKE_PROMPT_LOG": str(base / "prompts.log"), "FAKE_SLOW": "3"}


def build(base: Path) -> None:
    runs = base / "runs"
    repo = runs / "quiz-demo"
    repo.mkdir(parents=True)
    (base / "scenario.json").write_text(json.dumps({t[0]: t[4] for t in TASKS}), encoding="utf-8")
    agent = json.dumps([sys.executable, str(FAKE)])
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    (repo / "check.py").write_text(
        "import pathlib, sys\n"
        "bad = [p.name for p in pathlib.Path('src').glob('*.txt') if p.read_text() != 'ok']\n"
        "sys.exit(1 if bad else 0)\n", encoding="utf-8")
    (repo / "spec.md").write_text(SPEC, encoding="utf-8")
    (repo / "specs").mkdir()
    (repo / "specs" / "20261120-090000.md").write_text(FROZEN, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / ".keep").write_text("", encoding="utf-8")
    (repo / ".gitignore").write_text(".hootl/\n", encoding="utf-8")
    (repo / "hootl.toml").write_text(f"""# デモ（偽のエージェント）
repo = "."
prompts_dir = "{ROOT / 'prompts'}"

[run]
max_attempts = 2
max_tasks_per_run = 7

[agents.planner]
cmd = {agent}
[agents.implementer]
cmd = {agent}
[agents.reviewer]
cmd = {agent}

[[setup]]
name = "prepare"
cmd = "python3 -c \\"open('.setup_done','w').write('1')\\""

[[gates]]
name = "check"
cmd = "python3 check.py"

[guard]
protected_paths = ["hootl.toml", "check.py"]
""", encoding="utf-8")
    git_commit(repo, "chore: デモの土台")

    tasks = base / "tasks.jsonl"
    tasks.write_text("".join(json.dumps({
        "id": tid, "title": title, "description": f"{title}。受け入れ条件をすべてテストで確かめる。",
        "acceptance": ["テストで確かめられる条件 1", "テストで確かめられる条件 2"], "paths": ["src/*"],
        "depends_on": [], "epic": epic, "priority": prio}, ensure_ascii=False) + "\n"
        for tid, title, epic, prio, _ in TASKS), encoding="utf-8")
    env = demo_env(base)
    cli = [sys.executable, "-m", "hootl.cli", "-c", str(repo / "hootl.toml")]
    sh(*cli, "import", str(tasks), cwd=repo, env=env)
    print("偽のエージェントで1回回しています（数十秒）…", flush=True)
    sh(*cli, "run", cwd=repo, env={**env, "FAKE_SLOW": "0"})

    state = repo / ".hootl"
    sys.path.insert(0, str(ROOT))
    from hootl.ideas import Notebook
    Notebook(state / "state.db").add("模試の結果を1枚にまとめる", "終わったあとに、科目ごとの点と弱い論点を1画面で見たい。", "human")
    (state / "spec-drafts").mkdir(parents=True, exist_ok=True)
    (state / "spec-drafts" / "20261126-110355.md").write_text(DRAFT, encoding="utf-8")
    (state / "proposed-tasks.jsonl").write_text(
        "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in PROPOSED), encoding="utf-8")

    # カタログから作った体のプロジェクト（設定タブの見本。依存は入れていない）
    app = runs / "kakeibo"
    app.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=app)
    (app / "spec.md").write_text("# 家計簿\n\n## 目的\nレシートから月ごとの費目別の合計を出す。\n", encoding="utf-8")
    (app / ".gitignore").write_text(".hootl/\n", encoding="utf-8")
    (app / "hootl.toml").write_text(f"""# 家計簿 — hootl のカタログ「Webアプリ」から作成（デモ）
spec = "spec.md"

[stack]
name = "web-next"
app = "kakeibo"
title = "家計簿"
options = ["e2e"]
gates = ["lint", "typecheck", "unit", "build", "e2e"]

[stack.vars]
port = 3101
e2e_port = 4101
python = {json.dumps(sys.executable)}
""", encoding="utf-8")
    git_commit(app, "chore: ひな形から作成（Webアプリ・E2E テスト（Playwright））")

    new_dir = runs / ".hootl-new"
    new_dir.mkdir()
    plan = [{"id": sid, "label": label} for sid, label, _ in CREATION_LOG]
    lines = ["::plan " + json.dumps(plan, ensure_ascii=False)]
    for sid, _, note in CREATION_LOG:
        lines += [f"::step {sid} start", f"::step {sid} ok {note}".rstrip()]
    lines.append("::done kakeibo")
    (new_dir / "kakeibo.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (base / "web.toml").write_text(f'port = 8799\ntoken = "{TOKEN}"\nprojects_dir = "{runs}"\n', encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default="/tmp/hootl-demo")
    ap.add_argument("--fresh", action="store_true", help="あれば消して作り直す")
    ap.add_argument("--no-serve", action="store_true", help="作るだけで開かない")
    args = ap.parse_args()
    base = Path(args.dir).expanduser().resolve()
    if base.exists() and args.fresh:
        shutil.rmtree(base)
    if not base.exists():
        build(base)
    if args.no_serve:
        print(f"できました: {base}")
        return 0
    print(f"http://127.0.0.1:8799 を開いて、トークン {TOKEN} で入る（止めるのは Ctrl+C）", flush=True)
    env = demo_env(base)
    os.execvpe(sys.executable, [sys.executable, "-m", "hootl.cli", "web", "--web-config", str(base / "web.toml")], env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
