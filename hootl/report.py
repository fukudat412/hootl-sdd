"""朝のレポート。人が見るのは blocked だけで済むように並べる。"""

from __future__ import annotations

import json
import urllib.request
from collections import Counter

from .queue import Queue


SOURCE_NAME = {"human": "自分で", "spec-writer": "仕様づくり中", "planner": "計画中",
               "implementer": "実装中", "reviewer": "レビュー中"}


def build(queue: Queue, since_iso: str, new_ideas: list | None = None) -> str:
    tasks = queue.all()
    by_status = Counter(t.status for t in tasks)
    attempts = queue.attempts_since(since_iso)
    done_ids = {t.id for t in tasks if t.status == "done"}
    blocked = [t for t in tasks if t.status == "blocked"]
    blocked_ids = {t.id for t in blocked}
    waiting = [t for t in tasks if t.status == "todo" and any(d in blocked_ids for d in t.depends_on)]
    stage_fail = Counter(a["stage"] for a in attempts if not a["ok"])
    cost = sum(a["cost_usd"] or 0 for a in attempts)
    turns = sum(a["turns"] or 0 for a in attempts)

    lines = [
        f"# hootl レポート（{since_iso} 以降）",
        "",
        f"- タスク: done {by_status.get('done', 0)} / blocked {by_status.get('blocked', 0)} / "
        f"todo {by_status.get('todo', 0)} / running {by_status.get('running', 0)}",
        f"- 試行: {len(attempts)} 回（成功 {sum(1 for a in attempts if a['ok'])}）",
        f"- 差し戻しの内訳: " + (", ".join(f"{k} {v}" for k, v in stage_fail.most_common()) or "なし"),
        f"- 消費: 約 ${cost:.2f} 相当 / {turns} ターン（取得できた分のみ）",
        "",
        "## 要対応（blocked）",
        "",
    ]
    if blocked:
        lines += ["| ID | タイトル | 試行 | 理由 |", "| --- | --- | --- | --- |"]
        for t in blocked:
            reason = (t.last_error or "").replace("|", "\\|").replace("\n", " ")[:160]
            lines.append(f"| {t.id} | {t.title} | {t.attempts} | {reason} |")
        lines += ["", "直したら `hootl reset <ID>` で todo に戻す。ログは `.hootl/runs/<ID>/` にある。"]
    else:
        lines.append("なし")
    push_failed = [a for a in attempts if a["ok"] and "push失敗" in (a["detail"] or "")]
    if push_failed:
        lines += ["", "## push に失敗したもの（main にはマージ済み。手で `git push` する）", ""]
        lines += [f"- {a['task_id']}: {a['detail']}" for a in push_failed]
    if waiting:
        lines += ["", "## blocked の巻き込みで止まっているもの", ""]
        lines += [f"- {t.id} {t.title}（待ち: {', '.join(d for d in t.depends_on if d in blocked_ids)}）"
                  for t in waiting]
    finished = [a for a in attempts if a["ok"] and a["task_id"] in done_ids]
    if finished:
        lines += ["", "## 完了", ""] + [f"- {a['task_id']}（{a['attempt']} 回目）" for a in finished]
    if new_ideas:
        lines += ["", "## ネタ帳に増えた案（作っていない。選んだものだけが次の仕様になる）", ""]
        for i in reversed(new_ideas):
            where = SOURCE_NAME.get(i.source, i.source) + (f"・{i.task_id}" if i.task_id else "")
            lines.append(f"- {i.title}（{where}）" + (f": {i.body}" if i.body else ""))
    return "\n".join(lines) + "\n"


def notify_discord(webhook: str, text: str) -> None:
    body = json.dumps({"content": text[:1900]}).encode()
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "hootl"})
    urllib.request.urlopen(req, timeout=10).read()
