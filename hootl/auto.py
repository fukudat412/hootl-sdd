"""おまかせ: 作りたいもの → 仕様 → 凍結 → 計画 → 取り込み → 実行 を、人の承認なしで1本につなぐ。

人の承認の代わりに置くもの:
  仕様   「## やること」があること（specs.freeze）。決めきれない点は、書き手が自分で決めて「自分で決めたこと」に残す
  計画   specs.check_plan（id の重なり・行き先のない依存・依存の輪・件数の上限）
  実装   これまでどおり 安全柵 → ゲート → 独立レビュー

確かめで通らなければ、そこで止めて成果物（下書き・タスク案）を残す。人が「仕様」「計画と承認」から続きをやれる。
何を作るかを決めるのは人のまま: ここに入るのは、人が書いたことばか、人がネタ帳から選んだ案だけ。

進み具合は `::phase <名前>` を1行ずつ出す（管理画面が、いまの段を読む）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import agents, gitops, ideas, specs
from .config import Config
from .queue import Queue

# 計画・仕様づくりはタスクの外で動くので、タスク用の環境変数は空で渡す
ROLE_ENV = {"HOOTL_TASK_ID": "-", "HOOTL_ATTEMPT": "0"}
PHASES = ("draft", "freeze", "plan", "import", "run")


class Stopped(Exception):
    """作れなかった・確かめで止めた。メッセージはそのまま人に見せる。"""


def say(line: str) -> None:
    print(line, flush=True)


def phase(name: str) -> None:
    say(f"::phase {name}")


def _noted(cfg: Config, output: str, source: str) -> None:
    found = ideas.notebook(cfg).collect(output, source)
    if found:
        say(f"ネタ帳に {len(found)} 件足しました: " + "、".join(found))


def draft(cfg: Config, idea: str, out: Path, decide: bool = False) -> str:
    """作りたいものから仕様の下書きを書く。decide なら、決めきれない点も自分で決めさせる。"""
    if not cfg.planner:
        raise Stopped("hootl.toml に [agents.planner] がありません")
    out.parent.mkdir(parents=True, exist_ok=True)
    log = out.with_suffix(".log")
    result = agents.run_agent(cfg.planner, specs.spec_prompt(cfg, idea, decide), cfg.repo, log,
                              {**cfg.extra_env, **ROLE_ENV, "HOOTL_ROLE": "spec-writer"})
    _noted(cfg, result.stdout, "spec-writer")
    text = specs.spec_text_of(result.stdout if result.ok else "")
    if not result.ok or not text.strip().startswith("#"):
        raise Stopped(f"仕様の下書きを作れませんでした（code {result.code}）。{log} を見てください")
    out.write_text(text, encoding="utf-8")
    return text


def plan(cfg: Config, queue: Queue, spec_path: Path) -> int:
    """仕様からタスク案を作って proposed-tasks.jsonl に書く。件数を返す。"""
    if not cfg.planner:
        raise Stopped("hootl.toml に [agents.planner] がありません")
    # 既存のタスクを見せて、番号を続きから振らせる（T-001 からやり直すと完了済みとぶつかって消える）
    prompt = specs.plan_prompt(cfg, spec_path.resolve(), queue)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    result = agents.run_agent(cfg.planner, prompt, cfg.repo, cfg.state_dir / "plan.log",
                              {**cfg.extra_env, **ROLE_ENV, "HOOTL_ROLE": "planner"})
    _noted(cfg, result.stdout, "planner")
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip().startswith('{"id"')]
    existing = {t.id for t in queue.all()}
    clashes = []
    for ln in lines:
        try:
            tid = json.loads(ln).get("id")
        except json.JSONDecodeError:
            continue                    # 読めない行は、取り込む前の確かめ（validate_jsonl）が止める
        if tid in existing:
            clashes.append(str(tid))
    if clashes:
        raise Stopped(f"すでにある id と重なりました: {', '.join(clashes)}。タスク案は書きません")
    if not lines:
        raise Stopped(f"タスク案が1件もできませんでした（code {result.code}）。{cfg.state_dir / 'plan.log'} を見てください")
    specs.proposed_path(cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def approve(cfg: Config, queue: Queue) -> int:
    """人の承認の代わり。確かめを通ったタスク案だけを取り込む。通らなければ、案は残して止まる。"""
    path = specs.proposed_path(cfg)
    if not path.exists():
        raise Stopped("タスク案がありません")
    try:
        tasks = specs.validate_jsonl(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Stopped(f"タスク案を取り込めません: {exc}。管理画面の「計画と承認」で直してください") from exc
    problems = specs.check_plan(tasks, queue, cfg.auto_max_tasks)
    if problems:
        raise Stopped("タスク案を取り込めません: " + " / ".join(problems) + "。管理画面の「計画と承認」で直してください")
    return specs.import_proposed(cfg, queue)


def ready(cfg: Config, drafting: bool) -> None:
    """始める前に分かる行き止まりは、時間をかける前に言う。"""
    if specs.proposed_path(cfg).exists():
        raise Stopped("承認待ちのタスク案が残っています。「計画と承認」で取り込むか捨ててから、もう一度")
    if drafting:
        try:
            specs.ensure_main_clean(cfg)
        except ValueError as exc:
            raise Stopped(str(exc)) from exc


def pipeline(cfg: Config, queue: Queue, run: Callable[[], int] | None, idea: str | None = None,
             slug: str | None = None, spec: Path | None = None) -> int:
    """idea から（下書き → 凍結 →）、または凍結済みの spec から、計画 → 取り込み → 実行。"""
    ready(cfg, drafting=idea is not None)
    if idea is not None:
        slug = slug or specs.new_slug(cfg)
        ddir = specs.drafts_dir(cfg)
        ddir.mkdir(parents=True, exist_ok=True)
        idea_file = ddir / f"{slug}.idea.txt"
        if not idea_file.exists():
            idea_file.write_text(idea.strip() + "\n", encoding="utf-8")
        phase("draft")
        text = draft(cfg, idea, ddir / f"{slug}.md", decide=True)
        say(f"仕様を書きました: {specs.title_of(text)}")
        phase("freeze")
        try:
            spec = specs.freeze(cfg, slug)
        except (ValueError, LookupError, gitops.GitError) as exc:
            raise Stopped(f"仕様を凍結できません: {exc}。下書きは「仕様」タブに残してあります") from exc
        say(f"凍結しました: {specs.SPECS_DIR}/{spec.name}")
    if spec is None or not spec.is_file():
        raise Stopped("仕様がありません")
    phase("plan")
    n = plan(cfg, queue, spec)
    say(f"{n} 件のタスク案ができました")
    phase("import")
    imported = approve(cfg, queue)
    say(f"{imported} 件を「待ち」に入れました（機械の確認を通過）")
    if run is None:
        return 0
    phase("run")
    return run()
