"""ループ本体。外側はキュー駆動、内側は 実装→安全柵→ゲート→レビュー→マージ。"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import agents, gates, gitops, guard, ideas
from .config import Config
from .queue import Queue, Task, now

log = logging.getLogger("hootl")


@dataclass
class Outcome:
    stage: str           # 落ちた（または完了した）工程
    ok: bool
    detail: str
    feedback: str = ""   # 次の試行に渡す差し戻し内容
    retryable: bool = True
    cost_usd: float | None = None
    turns: int | None = None


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…(前略)…\n" + text[-limit:]


def _bullets(items: list[str]) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


class Runner:
    def __init__(self, cfg: Config, queue: Queue):
        self.cfg = cfg
        self.queue = queue
        self.book = ideas.notebook(cfg)      # ネタ帳（作業中に見つけた案をためる）
        self._claimed = 0
        self._claim_lock = threading.Lock()
        self._stop = threading.Event()

    # ---- 外側のループ -----------------------------------------------------
    def run(self, deadline: float | None = None) -> None:
        recovered = self.queue.recover_running()
        if recovered:
            log.info("running のまま残っていた %d 件を todo に戻しました", recovered)
        if self.cfg.push_remote:
            ok, info = gitops.sync_main(self.cfg.repo, self.cfg.push_remote, self.cfg.main_branch)
            if not ok:
                # 分岐したまま回すと push できない成果が積み上がるので、始めない
                raise RuntimeError(f"main を remote と同期できません: {info}")
            log.info("main を %s/%s に同期しました（%s）", self.cfg.push_remote, self.cfg.main_branch, info[:12])
        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            for i in range(self.cfg.concurrency):
                pool.submit(self._worker, i, deadline)

    def stop(self) -> None:
        self._stop.set()

    def _worker(self, index: int, deadline: float | None) -> None:
        idle_rounds = 0
        while not self._stop.is_set():
            if deadline and time.time() > deadline:
                return
            with self._claim_lock:
                if self._claimed >= self.cfg.max_tasks_per_run:
                    return
                task = self.queue.claim_next()
                if task:
                    self._claimed += 1
            if task is None:
                # 他のワーカーが依存先を処理中なら少し待つ。誰も動いていなければ終了。
                if self.queue.counts().get("running", 0) == 0:
                    return
                idle_rounds += 1
                time.sleep(min(5, 0.2 * idle_rounds))
                continue
            idle_rounds = 0
            try:
                self.process(task)
            except Exception as exc:  # ハーネス自体の不具合でも夜を止めない
                log.exception("worker %d: %s で想定外のエラー", index, task.id)
                self.queue.mark(task.id, "blocked", f"harness error: {exc}")

    # ---- 1タスク1試行 -----------------------------------------------------
    def process(self, task: Task) -> None:
        attempt = self.queue.begin_attempt(task.id)
        started = now()
        run_dir = self.cfg.state_dir / "runs" / task.id / str(attempt)
        run_dir.mkdir(parents=True, exist_ok=True)
        log.info("▶ %s attempt %d: %s", task.id, attempt, task.title)

        outcome = self._attempt(task, attempt, run_dir)
        self.queue.record_attempt(task.id, attempt, outcome.stage, outcome.ok, outcome.detail,
                                  started, outcome.cost_usd, outcome.turns)
        (run_dir / "outcome.txt").write_text(f"{outcome.stage} ok={outcome.ok}\n{outcome.detail}\n\n"
                                             f"--- feedback ---\n{outcome.feedback}", encoding="utf-8")

        if outcome.ok:
            self.queue.mark(task.id, "done")
            log.info("✔ %s done", task.id)
        elif not outcome.retryable or attempt >= self.cfg.max_attempts:
            self.queue.mark(task.id, "blocked", f"[{outcome.stage}] {outcome.detail}")
            log.warning("✖ %s blocked at %s: %s", task.id, outcome.stage, outcome.detail[:200])
        else:
            self.queue.requeue(task.id, f"[{outcome.stage}] {outcome.detail}", outcome.feedback)
            log.info("↺ %s 差し戻し（%s）", task.id, outcome.stage)

    def _attempt(self, task: Task, attempt: int, run_dir: Path) -> Outcome:
        cfg = self.cfg
        env = {**cfg.extra_env, "HOOTL_TASK_ID": task.id, "HOOTL_ATTEMPT": str(attempt)}
        branch = f"task/{task.id}"
        try:
            wt, base = gitops.create_worktree(cfg.repo, cfg.worktree_dir, task.id, cfg.main_branch)
        except gitops.GitError as exc:
            return Outcome("setup", False, str(exc), retryable=False)

        try:
            # 0. 準備（依存のインストールなど。エージェントがテストを回せる状態にする）
            for r in gates.run_gates(cfg.setup, wt, run_dir, env, prefix="setup"):
                if not r.ok:
                    return Outcome("setup", False, f"準備 {r.name} が失敗（code {r.code}）: "
                                   f"{_tail(r.output, 300)}", retryable=False)
            if cfg.setup:
                gitops.commit_all(wt, f"hootl({task.id}): setup")  # 準備で生じた生成物は差分に含めない
                base = gitops.head(wt)

            # 1. 実装
            result = agents.run_agent(cfg.implementer, self._implement_prompt(task), wt,
                                      run_dir / "implement.log", {**env, "HOOTL_ROLE": "implementer"})
            self._note(result.stdout, "implementer", task)
            if not result.ok:
                return Outcome("agent", False, f"実装エージェントが異常終了（code {result.code}）",
                               feedback="前回は実装エージェントが途中で異常終了しました。より小さな手順で進めてください。",
                               cost_usd=result.cost_usd, turns=result.turns)
            gitops.commit_all(wt, f"hootl({task.id}): attempt {attempt}")
            cost, turns = result.cost_usd, result.turns

            # 2. 安全柵
            changes = gitops.changes(wt, base)
            diff = gitops.diff_text(wt, base)
            (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
            g = guard.check(changes, diff, task.paths, cfg.protected_paths, cfg.test_globs,
                            cfg.forbidden_patterns, cfg.always_allowed, cfg.out_of_scope)
            if not g.ok:
                return Outcome("guard", False, "; ".join(g.violations),
                               feedback="前回の試行は安全柵で差し戻されました:\n" + _bullets(g.violations),
                               cost_usd=cost, turns=turns)

            # 3. ゲート
            for r in gates.run_gates(cfg.gates, wt, run_dir, env):
                if not r.ok:
                    return Outcome("gate", False, f"ゲート {r.name} が失敗（code {r.code}）",
                                   feedback=f"前回の試行はゲート `{r.name}` で落ちました。出力の末尾:\n```\n"
                                            f"{_tail(r.output, cfg.feedback_tail_chars)}\n```",
                                   cost_usd=cost, turns=turns)

            # 4. 独立レビュー
            if cfg.reviewer:
                prompt = self._review_prompt(task, diff, g.out_of_scope)
                for round_ in range(1, cfg.review_tries + 1):
                    review = agents.run_agent(cfg.reviewer, prompt, wt, run_dir / "review.log",
                                              {**env, "HOOTL_ROLE": "reviewer"})
                    self._note(review.stdout, "reviewer", task)
                    verdict = agents.parse_verdict(review.output)
                    if verdict.raw_ok or round_ == cfg.review_tries:
                        break
                    # 判定が返らないのはレビュー役の不調で、実装への指摘ではない。実装はやり直さず、レビューだけやり直す
                    log.warning("… %s レビュー役が判定を返しませんでした（code %s・%d/%d 回目）。レビューだけやり直します",
                                task.id, review.code, round_, cfg.review_tries)
                    (run_dir / "review.log").replace(run_dir / f"review-noverdict-{round_}.log")
                    if self._stop.wait(cfg.review_retry_wait_sec):
                        break
                if not verdict.raw_ok:
                    # 実装の良し悪しは分かっていないので、同じ実装のやり直しに回さず、人に回す（合格にもしない）
                    return Outcome("review", False,
                                   f"レビュー役が判定を返しませんでした（終了コード {review.code}・{round_} 回試した）。"
                                   "実装への指摘ではありません。レビュー役（hootl.toml の [agents.reviewer]）が動くか"
                                   "確かめてから、やり直してください",
                                   retryable=False, cost_usd=cost, turns=turns)
                (run_dir / "review.json").write_text(
                    json.dumps({"passed": verdict.passed, "findings": verdict.findings}, ensure_ascii=False),
                    encoding="utf-8")
                if not verdict.passed:
                    return Outcome("review", False, "; ".join(verdict.findings)[:500],
                                   feedback="前回の試行は独立レビューで差し戻されました。指摘:\n"
                                            + _bullets(verdict.findings),
                                   cost_usd=cost, turns=turns)

            # 5. マージ
            merged, info = gitops.merge_to_main(cfg.repo, branch, cfg.main_branch,
                                                f"merge {task.id}: {task.title}")
            if not merged:
                return Outcome("merge", False, f"main へのマージで衝突: {info[:300]}",
                               feedback="前回の実装は main と衝突しました。最新の main から作り直しています。",
                               cost_usd=cost, turns=turns)
            detail = f"merged at {info[:12]}"
            if cfg.push_remote:
                pushed, push_info = gitops.push_main(cfg.repo, cfg.push_remote, cfg.main_branch)
                if pushed:
                    detail += f", pushed to {cfg.push_remote}"
                else:
                    # マージ済みなのでタスクは done。push は次のタスクの push でまとめて送られる
                    log.warning("push に失敗: %s", push_info[:200])
                    detail += f", push失敗: {push_info[:200]}"
            return Outcome("done", True, detail, cost_usd=cost, turns=turns)
        finally:
            gitops.remove_worktree(cfg.repo, wt, branch)

    def _note(self, output: str, source: str, task: Task) -> None:
        """出力の ::idea 行をネタ帳に移す。ためるだけで、このランでは作らない。"""
        try:
            found = self.book.collect(output, source, task.id)
        except Exception:  # ネタ帳の不具合でタスクを落とさない
            log.exception("ネタ帳に書けませんでした（%s）", task.id)
            return
        if found:
            log.info("✎ %s ネタ帳に %d 件: %s", task.id, len(found), "、".join(found))

    # ---- プロンプト -------------------------------------------------------
    def _template(self, name: str) -> str:
        return (self.cfg.prompts_dir / name).read_text(encoding="utf-8")

    def _implement_prompt(self, task: Task) -> str:
        feedback = f"## 前回の試行からの差し戻し（最優先で対応すること）\n\n{task.feedback}" if task.feedback else ""
        return self._template("implement.md").format(
            id=task.id, title=task.title, description=task.description,
            acceptance=_bullets(task.acceptance),
            paths="\n".join(f"- `{p}`" for p in task.paths),
            gates="\n".join(f"  - `{g.cmd}`" for g in self.cfg.gates) or "  - （ゲートなし）",
            feedback=feedback, ideas=ideas.fragment(self.cfg, self.book),
        )

    def _review_prompt(self, task: Task, diff: str, out_of_scope: list[str] | None = None) -> str:
        if len(diff) > self.cfg.diff_max_chars:
            diff = diff[: self.cfg.diff_max_chars] + "\n…(差分が長いため以降省略。必要なら git diff で確認)…"
        scope_note = ""
        if out_of_scope:
            scope_note = (
                "## タスクの想定範囲外で変更されたファイル\n\n"
                + "\n".join(f"- `{p}`" for p in out_of_scope)
                + "\n\nタスクを満たすために必要な変更で、理由が `docs/decisions/` に残っていれば問題にしない。"
                  "タスクと無関係な変更や、理由のない変更なら fail にする。\n"
            )
        return self._template("review.md").format(
            id=task.id, title=task.title, description=task.description,
            acceptance=_bullets(task.acceptance), diff=diff, scope_note=scope_note,
            ideas=ideas.fragment(self.cfg, self.book),
        )
