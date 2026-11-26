"""hootl コマンド。

  hootl auto --idea-file F      おまかせ: 仕様 → 凍結 → 計画 → 取り込み → 実行 を、承認なしで1本につなぐ
  hootl auto --spec specs/x.md  凍結済みの仕様から、計画 → 取り込み → 実行
  hootl ideas [add 名前 | drop 番号 | build 番号]   ネタ帳（作業中に見つけた案）を見る・足す・捨てる・作る
  hootl spec-draft --idea-file F --out O   作りたいものから仕様の下書きを作る（自分で読んで直すとき）
  hootl plan spec.md [--approve]  仕様からタスク案を作る（--approve で、機械の確認だけで取り込む）
  hootl import tasks.jsonl      タスクをキューに入れる（自分で読んで承認するとき）
  hootl run [--hours 8]         キューが空になるか時間切れまで回す
  hootl status                  件数と blocked の一覧
  hootl report [--since ISO]    レポートを出す（通知設定があれば送る）
  hootl reset T-001             blocked を todo に戻す
  hootl web                     Web 管理画面（~/.config/hootl/web.toml）
  hootl setup [--email --hostname]  Web 管理画面を使い始めるまでを1回で
  hootl doctor                  Web 管理画面まわりの状態を一覧する
  hootl token [--show]          ログイン用のトークンをクリップボードに入れる（--show で画面に出す）
  hootl stacks                  ひな形カタログ（型・オプション・ゲート）を一覧する
  hootl new NAME --stack web-next [--option e2e] [--gates lint,unit] [--idea-file F]
                                カタログの型から新しいアプリを作る（git・依存・ゲート・起動確認まで）
  hootl gates [--set lint,unit] このアプリのゲートを表示・切り替える（hootl.toml の [stack]）
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import auto, ideas, report
from . import specs
from .config import Config, load
from .queue import Queue
from .runner import Runner


def default_runs_dir() -> str:
    """専用 clone を置く所の既定: $HOOTL_RUNS_DIR → いまの web.toml の projects_dir → ~/hootl-runs。"""
    import os
    import tomllib
    if os.environ.get("HOOTL_RUNS_DIR"):
        return os.environ["HOOTL_RUNS_DIR"]
    try:
        raw = tomllib.loads(Path("~/.config/hootl/web.toml").expanduser().read_text(encoding="utf-8"))
        if raw.get("projects_dir"):
            return str(raw["projects_dir"])
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return "~/hootl-runs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hootl")
    parser.add_argument("-c", "--config", default="hootl.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("spec")
    p_plan.add_argument("--approve", action="store_true", help="機械の確認を通ったら、そのまま取り込む")
    p_draft = sub.add_parser("spec-draft", help="作りたいものから仕様の下書きを作る")
    p_draft.add_argument("--idea-file", required=True)
    p_draft.add_argument("--out", required=True)
    p_draft.add_argument("--decide", action="store_true", help="決めきれない点も、質問にせず自分で決めさせる")
    p_auto = sub.add_parser("auto", help="おまかせ: 仕様 → 凍結 → 計画 → 取り込み → 実行")
    src = p_auto.add_mutually_exclusive_group(required=True)
    src.add_argument("--idea-file", help="作りたいものを書いたファイル")
    src.add_argument("--spec", help="凍結済みの仕様（計画から始める）")
    p_auto.add_argument("--slug", help="下書きの名前（管理画面が決めて渡す）")
    p_auto.add_argument("--no-run", action="store_true", help="取り込んだところで止める")
    p_auto.add_argument("--hours", type=float, help="回す時間の上限（既定は hootl.toml の [auto] hours）")
    p_ideas = sub.add_parser("ideas", help="ネタ帳を見る・足す・捨てる・作る")
    p_ideas.add_argument("action", nargs="?", choices=["add", "drop", "build"])
    p_ideas.add_argument("value", nargs="?", help="add なら名前、drop / build なら番号")
    p_ideas.add_argument("--body", default="", help="add のとき、中身")
    p_ideas.add_argument("--all", action="store_true", help="作ったもの・捨てたものも出す")
    p_import = sub.add_parser("import")
    p_import.add_argument("jsonl")
    p_run = sub.add_parser("run")
    p_run.add_argument("--hours", type=float)
    p_run.add_argument("--concurrency", type=int)
    sub.add_parser("status")
    p_report = sub.add_parser("report")
    p_report.add_argument("--since", help="ISO8601。既定は24時間前")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("task_id")
    p_web = sub.add_parser("web", help="Web 管理画面を起動する")
    p_web.add_argument("--web-config", default="~/.config/hootl/web.toml")
    p_setup = sub.add_parser("setup", help="Web 管理画面を使い始めるまでを1回で（何度実行してもよい）")
    p_setup.add_argument("--runs-dir", default=default_runs_dir(),
                         help="専用 clone を置く所（既定: いまの設定の projects_dir、なければ ~/hootl-runs）")
    p_setup.add_argument("--port", type=int, default=8765)
    p_setup.add_argument("--email", action="append", default=[], help="通してよいメール（スマホから使うとき）")
    p_setup.add_argument("--hostname", help="スマホから開くドメイン（例: hootl.example.com）")
    p_setup.add_argument("--access-ready", action="store_true", help="Cloudflare Access を作り終えた")
    p_setup.add_argument("--no-launchd", action="store_true", help="自動起動を登録しない")
    p_setup.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    p_setup.add_argument("--dry-run", action="store_true", help="何をするかだけ表示する")
    p_doctor = sub.add_parser("doctor", help="Web 管理画面まわりの状態を一覧する")
    p_doctor.add_argument("--runs-dir", default=default_runs_dir())
    p_token = sub.add_parser("token", help="ログイン用のトークンをクリップボードに入れる")
    p_token.add_argument("--web-config", default="~/.config/hootl/web.toml")
    p_token.add_argument("--show", action="store_true", help="クリップボードに入れずに、画面に出す")
    sub.add_parser("stacks", help="ひな形カタログを一覧する")
    p_new = sub.add_parser("new", help="カタログの型から新しいアプリを作る")
    p_new.add_argument("name")
    p_new.add_argument("--stack", required=True)
    p_new.add_argument("--title", default="")
    p_new.add_argument("--option", action="append", default=[])
    p_new.add_argument("--gates", help="使うゲートをカンマ区切りで（省略するとカタログの既定）")
    p_new.add_argument("--idea-file")
    p_new.add_argument("--no-spec", action="store_true", help="仕様の下書きは作らない")
    p_new.add_argument("--runs-dir", default=default_runs_dir())
    p_gates = sub.add_parser("gates", help="ゲートを表示・切り替える")
    p_gates.add_argument("--set", help="使うゲートをカンマ区切りで")
    p_gates.add_argument("--commit", action="store_true", help="hootl.toml の変更を main にコミットする")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.cmd != "web":
        # 管理画面のトークンは、管理画面だけが使う。ここから起動するエージェントやゲートには渡さない
        import os
        os.environ.pop("HOOTL_WEB_TOKEN", None)
    if args.cmd == "web":
        from .web import serve
        serve(args.web_config)
        return 0
    if args.cmd == "setup":
        from .setup import Env, setup
        return setup(Env(dry_run=args.dry_run), Path(args.runs_dir).expanduser(), args.port, args.email,
                     args.hostname, args.access_ready, not args.no_launchd, not args.no_open)
    if args.cmd == "doctor":
        from .setup import Env, doctor
        return doctor(Env(), Path(args.runs_dir).expanduser())
    if args.cmd == "token":
        return cmd_token(Path(args.web_config).expanduser(), args.show)
    if args.cmd == "stacks":
        from . import stacks
        print(json.dumps([s.to_public() for s in stacks.catalog()], ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "new":
        from .create import Request, create
        idea = Path(args.idea_file).read_text(encoding="utf-8") if args.idea_file else ""
        gates = [g.strip() for g in args.gates.split(",") if g.strip()] if args.gates is not None else None
        return create(Request(Path(args.runs_dir), args.name, args.stack, args.title, args.option, gates,
                              idea, draft_spec=not args.no_spec))
    if args.cmd == "gates":
        return cmd_gates(Path(args.config), args.set, args.commit)
    cfg = load(args.config)
    queue = Queue(cfg.state_dir / "state.db")

    if args.cmd == "plan":
        try:
            n = auto.plan(cfg, queue, Path(args.spec))
            out = specs.proposed_path(cfg)
            if not args.approve:
                print(f"{n} 件のタスク案を {out} に書きました。読んで直してから `hootl import` してください。")
                return 0
            print(f"{auto.approve(cfg, queue)} 件を取り込みました（機械の確認を通過）")
            return 0
        except auto.Stopped as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 1

    if args.cmd == "spec-draft":
        idea = Path(args.idea_file).read_text(encoding="utf-8")
        out = Path(args.out)
        try:
            text = auto.draft(cfg, idea, out, decide=args.decide)
        except auto.Stopped as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 1
        print(f"仕様の下書きを {out} に書きました: {specs.title_of(text)}")
        return 0

    if args.cmd == "auto":
        hours = args.hours if args.hours is not None else cfg.auto_hours
        run = None if (args.no_run or not cfg.auto_run) else (lambda: run_queue(cfg, queue, hours or None, None))
        try:
            if args.idea_file:
                return auto.pipeline(cfg, queue, run, idea=Path(args.idea_file).read_text(encoding="utf-8"),
                                     slug=args.slug)
            return auto.pipeline(cfg, queue, run, spec=Path(args.spec).resolve())
        except auto.Stopped as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 3
        except KeyboardInterrupt:
            print("✖ 止めました（途中までの成果物は残してあります）", file=sys.stderr)
            return 130

    if args.cmd == "ideas":
        return cmd_ideas(cfg, queue, args.action, args.value, args.body, args.all)

    if args.cmd == "import":
        n = queue.import_jsonl(Path(args.jsonl))
        print(f"{n} 件を取り込みました（done のものは上書きしません）")
        return 0

    if args.cmd == "run":
        return run_queue(cfg, queue, args.hours, args.concurrency)

    if args.cmd == "status":
        counts = queue.counts()
        print(" / ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "キューは空です")
        for t in queue.all():
            if t.status == "blocked":
                print(f"  blocked {t.id} {t.title}: {(t.last_error or '')[:120]}")
        return 0

    if args.cmd == "report":
        since = args.since or (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        print(report.build(queue, since, ideas.notebook(cfg).since(since)))
        return 0

    if args.cmd == "reset":
        queue.reset(args.task_id)
        print(f"{args.task_id} を todo に戻しました")
        return 0
    return 1


def run_queue(cfg: Config, queue: Queue, hours: float | None, concurrency: int | None) -> int:
    """キューが空になるか時間切れまで回し、レポートを書く。"""
    if concurrency:
        cfg.concurrency = concurrency
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    deadline = time.time() + hours * 3600 if hours else None
    runner = Runner(cfg, queue)
    # Ctrl+C や Web の「停止」（SIGINT/SIGTERM）では、新しいタスクを取らずに、今のタスクが終わったら止まる
    def _graceful(signum, _frame):
        logging.getLogger("hootl").info("停止の合図を受けました。今のタスクが終わったら止まります")
        runner.stop()
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)
    try:
        runner.run(deadline)
    except RuntimeError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 2
    text = report.build(queue, started, ideas.notebook(cfg).since(started))
    path = cfg.state_dir / f"report-{started[:19].replace(':', '')}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    if cfg.discord_webhook:
        try:
            report.notify_discord(cfg.discord_webhook, text)
        except Exception as exc:  # 通知の失敗でラン結果を失わない
            print(f"通知に失敗: {exc}", file=sys.stderr)
    return 0


def cmd_ideas(cfg: Config, queue: Queue, action: str | None, value: str | None, body: str, show_all: bool) -> int:
    book = ideas.notebook(cfg)
    if action == "add":
        if not value:
            print("✖ 名前を書いてください: hootl ideas add \"名前\" --body \"中身\"", file=sys.stderr)
            return 2
        new_id = book.add(value, body, "human")
        print(f"#{new_id} を足しました" if new_id else "同じ名前の案がもうあります")
        return 0
    if action in ("drop", "build"):
        idea = book.get(int(value)) if value and value.isdigit() else None
        if not idea:
            print("✖ その番号の案はありません", file=sys.stderr)
            return 2
        if action == "drop":
            book.mark(idea.id, "dropped")
            print(f"#{idea.id} を捨てました")
            return 0
        slug = specs.new_slug(cfg)
        run = (lambda: run_queue(cfg, queue, cfg.auto_hours or None, None)) if cfg.auto_run else None
        try:
            auto.ready(cfg, drafting=True)          # 始められないなら、案は「新しい」のまま残す
            book.mark(idea.id, "adopted", slug)
            return auto.pipeline(cfg, queue, run, idea=idea.as_request(), slug=slug)
        except auto.Stopped as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 3
    items = book.all() if show_all else book.fresh()
    if not items:
        print("ネタ帳は空です（作業の途中でよさそうな案が見つかると、ここにたまります）")
    for i in items:
        where = report.SOURCE_NAME.get(i.source, i.source) + (f"・{i.task_id}" if i.task_id else "")
        mark = {"adopted": "［作る］", "dropped": "［捨てた］"}.get(i.status, "")
        print(f"#{i.id} {mark}{i.title}（{where}）" + (f"\n     {i.body}" if i.body else ""))
    return 0


def cmd_token(path: Path, show: bool) -> int:
    """ログイン画面に貼るトークン（~/.config/hootl/web.toml の token）。既定では画面に出さず、クリップボードに入れる。"""
    import shutil
    import subprocess
    import tomllib
    if not path.exists():
        print(f"✖ {path} がありません。先に `hootl setup` を実行してください", file=sys.stderr)
        return 1
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    token = raw.get("token", "")
    if not token:
        print(f"✖ {path} に token がありません。`hootl setup` を実行すると作られます", file=sys.stderr)
        return 1
    url = f"http://{raw.get('host', '127.0.0.1')}:{raw.get('port', 8765)}"
    if not show and shutil.which("pbcopy") and subprocess.run(["pbcopy"], input=token, text=True).returncode == 0:
        print(f"ログイン用のトークンをクリップボードに入れました。{url} を開いて、貼り付けて入ってください")
        return 0
    print(token)
    return 0


def cmd_gates(path: Path, names: str | None, do_commit: bool) -> int:
    import tomllib
    from . import stacks
    path = path.resolve()
    if names is not None:
        chosen = stacks.set_gates(path, [n.strip() for n in names.split(",") if n.strip()])
        if do_commit:
            from .specs import commit_file
            commit_file(load(path), path, f"chore: ゲートを {', '.join(chosen)} にする")
    status = stacks.gate_status(tomllib.loads(path.read_text(encoding="utf-8")))
    if not status:
        print("このプロジェクトはカタログから作られていません（ゲートは hootl.toml の [[gates]]）")
        return 0
    print(f"{status['stack']['title']}（{status['stack']['tech']}）")
    for g in status["gates"]:
        mark = "■" if g["on"] else "□"
        print(f"  {mark} {g['name']:<10} {g['title']}{'（必須）' if g['required'] else ''}")
    for g in status["custom"]:
        print(f"  ■ {g['name']:<10} {g['cmd']}（このアプリだけ）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
