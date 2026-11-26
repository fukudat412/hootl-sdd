"""hootl.toml の読み込み。設定はここで型付きの値に変換し、他のモジュールは辞書を触らない。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentSpec:
    cmd: list[str]
    stdin: bool = True
    timeout_sec: int = 3600


@dataclass
class Gate:
    name: str
    cmd: str
    timeout_sec: int = 600


@dataclass
class Config:
    repo: Path
    main_branch: str
    state_dir: Path
    worktree_dir: Path
    concurrency: int
    max_attempts: int
    max_tasks_per_run: int
    implementer: AgentSpec
    reviewer: AgentSpec | None
    planner: AgentSpec | None
    gates: list[Gate]
    setup: list[Gate]
    protected_paths: list[str]
    test_globs: list[str]
    forbidden_patterns: list[str]
    always_allowed: list[str]
    out_of_scope: str  # "block" | "review"
    prompts_dir: Path
    push_remote: str | None = None
    feedback_tail_chars: int = 4000
    review_tries: int = 3               # レビュー役が判定を返さなかったとき、レビューだけをやり直す回数（最初の1回を含む）
    review_retry_wait_sec: float = 20   # やり直す前に待つ秒数（一時的な不調をやり過ごす）
    diff_max_chars: int = 60000
    discord_webhook: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    # おまかせ: 仕様の凍結とタスク案の取り込みを、人の承認の代わりに機械の確認で進める
    auto_approve: bool = True
    auto_run: bool = True               # 取り込んだら、そのまま回す
    auto_hours: float | None = 7        # おまかせで回す時間の上限（0 か無しで、空になるまで）
    auto_max_tasks: int = 20            # 1つの仕様からこれより多く出たら、人に回す


def _agent(raw: dict | None) -> AgentSpec | None:
    if not raw:
        return None
    return AgentSpec(
        cmd=list(raw["cmd"]),
        stdin=bool(raw.get("stdin", True)),
        timeout_sec=int(raw.get("timeout_sec", 3600)),
    )


def load(path: str | Path = "hootl.toml") -> Config:
    path = Path(path).resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    if "stack" in raw:
        # カタログから作ったアプリ: ゲート・準備・安全柵は stacks/ から組み立て、hootl.toml に書いたものを上に重ねる
        from . import stacks
        raw = stacks.merge_config(stacks.compose(raw["stack"], base), raw)

    def p(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    run = raw.get("run", {})
    guard = raw.get("guard", {})
    auto = raw.get("auto", {})
    agents = raw.get("agents", {})
    implementer = _agent(agents.get("implementer"))
    if implementer is None:
        raise ValueError("hootl.toml: [agents.implementer] is required")

    return Config(
        repo=p(raw.get("repo", ".")),
        main_branch=raw.get("main_branch", "main"),
        state_dir=p(run.get("state_dir", ".hootl")),
        worktree_dir=p(run.get("worktree_dir", ".hootl/worktrees")),
        concurrency=int(run.get("concurrency", 1)),
        max_attempts=int(run.get("max_attempts", 3)),
        max_tasks_per_run=int(run.get("max_tasks_per_run", 1000)),
        implementer=implementer,
        reviewer=_agent(agents.get("reviewer")),
        planner=_agent(agents.get("planner")),
        gates=[Gate(g["name"], g["cmd"], int(g.get("timeout_sec", 600))) for g in raw.get("gates", [])],
        setup=[Gate(g["name"], g["cmd"], int(g.get("timeout_sec", 600))) for g in raw.get("setup", [])],
        protected_paths=list(guard.get("protected_paths", [])),
        test_globs=list(guard.get("test_globs", [])),
        forbidden_patterns=list(guard.get("forbidden_patterns", [])),
        always_allowed=list(guard.get("always_allowed", [])),
        out_of_scope=str(guard.get("out_of_scope", "block")),
        prompts_dir=p(raw.get("prompts_dir", "prompts")),
        push_remote=run.get("push_remote") or None,
        feedback_tail_chars=int(run.get("feedback_tail_chars", 4000)),
        review_tries=max(1, int(run.get("review_tries", 3))),
        review_retry_wait_sec=float(run.get("review_retry_wait_sec", 20)),
        diff_max_chars=int(run.get("diff_max_chars", 60000)),
        discord_webhook=raw.get("notify", {}).get("discord_webhook"),
        extra_env={str(k): str(v) for k, v in raw.get("env", {}).items()},
        auto_approve=bool(auto.get("approve", True)),
        auto_run=bool(auto.get("run", True)),
        auto_hours=float(auto["hours"]) if auto.get("hours") else (None if "hours" in auto else 7),
        auto_max_tasks=int(auto.get("max_tasks", 20)),
    )
