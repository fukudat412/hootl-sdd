"""ネタ帳: 作業の途中で見つけた「今回は作らないが、よさそうな案」をためておく所。

エージェント（仕様・計画・実装・レビュー）は、出力に次の1行を書くだけでよい（ファイルは触らない。
読み取り専用のレビュー役でも書ける）:

    ::idea {"title": "短い名前", "body": "何が良くなるか"}

ハーネスがその行を拾って state.db の ideas に入れる。ためるだけで、勝手には作らない。
持ち主が選んだものだけが「作りたいもの」として仕様づくりに入る（無人で案を足し続けて発散させないための、人の持ち場）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .queue import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,   -- 名前から空白と記号を除いたもの（同じ案を二度入れない）
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,          -- human | spec-writer | planner | implementer | reviewer
    task_id     TEXT,                   -- どのタスクの途中で見つけたか
    status      TEXT NOT NULL DEFAULT 'new',   -- new | adopted（作ることにした） | dropped（捨てた）
    spec        TEXT,                   -- adopted のとき、仕様の下書きの名前
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

EXAMPLE_TITLE = "20字くらいの名前"      # プロンプトに見本として書く名前。出力に写っても拾わない
EXAMPLE = '::idea {"title": "' + EXAMPLE_TITLE + '", "body": "何が良くなるか・どこで気づいたかを1〜3文で"}'
MAX_PER_OUTPUT = 3                      # 1回の出力から拾う上限（案を量産させない）
MAX_TITLE, MAX_BODY = 80, 1000
LINE = re.compile(r"^[\s>*`-]*::idea\s+(\{.*\})[\s`]*$")
SOURCES = ("human", "spec-writer", "planner", "implementer", "reviewer")


@dataclass
class Idea:
    id: int
    title: str
    body: str
    source: str
    task_id: str | None
    status: str
    spec: str | None
    created_at: str

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "body": self.body, "source": self.source,
                "task_id": self.task_id, "status": self.status, "spec": self.spec, "created_at": self.created_at}

    def as_request(self) -> str:
        """仕様づくりに渡す「作りたいもの」のことば。"""
        return self.title + ("\n\n" + self.body if self.body else "")


def key_of(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title).lower()


# ---- 出力から拾う ---------------------------------------------------------------
def _texts(output: str) -> list[str]:
    """出力そのものと、claude -p --output-format json の result（本文が JSON の中に入っている）。"""
    texts = [output]
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            texts.append(data["result"])
    return texts


def extract(output: str) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for text in _texts(output):
        for line in text.splitlines():
            m = LINE.match(line)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            title = " ".join(str(data.get("title", "")).split())[:MAX_TITLE]
            key = key_of(title)
            if not key or title == EXAMPLE_TITLE or key in seen:
                continue
            seen.add(key)
            found.append({"title": title, "body": str(data.get("body", "")).strip()[:MAX_BODY]})
            if len(found) >= MAX_PER_OUTPUT:
                return found
    return found


def strip(text: str) -> str:
    """成果物（仕様の本文など）から ::idea の行を取り除く。"""
    return "\n".join(ln for ln in text.splitlines() if not LINE.match(ln)) + ("\n" if text.endswith("\n") else "")


# ---- ためる -----------------------------------------------------------------------
class Notebook:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, title: str, body: str = "", source: str = "human", task_id: str | None = None) -> int | None:
        """入れたら id、同じ名前の案がもうあれば None（捨てた案・作った案も、もう一度は入れない）。"""
        title = " ".join(title.split())[:MAX_TITLE]
        key = key_of(title)
        if not key:
            raise ValueError("案の名前が空です")
        if source not in SOURCES:
            raise ValueError(f"source が不正です: {source}")
        with self._lock:
            cur = self._db.execute(
                """INSERT OR IGNORE INTO ideas (key, title, body, source, task_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (key, title, body.strip()[:MAX_BODY], source, task_id, now(), now()))
            return cur.lastrowid if cur.rowcount == 1 else None

    def collect(self, output: str, source: str, task_id: str | None = None) -> list[str]:
        """エージェントの出力から拾って入れる。新しく入った案の名前を返す。"""
        return [i["title"] for i in extract(output) if self.add(i["title"], i["body"], source, task_id)]

    def _rows(self, where: str = "", args: tuple = ()) -> list[Idea]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, body, source, task_id, status, spec, created_at FROM ideas "
                + where + " ORDER BY id DESC", args).fetchall()
        return [Idea(**dict(r)) for r in rows]

    def all(self) -> list[Idea]:
        return self._rows()

    def fresh(self) -> list[Idea]:
        return self._rows("WHERE status='new'")

    def since(self, since_iso: str) -> list[Idea]:
        return self._rows("WHERE created_at >= ?", (since_iso,))

    def get(self, idea_id: int) -> Idea | None:
        rows = self._rows("WHERE id=?", (idea_id,))
        return rows[0] if rows else None

    def mark(self, idea_id: int, status: str, spec: str | None = None) -> bool:
        if status not in ("new", "adopted", "dropped"):
            raise ValueError(f"status が不正です: {status}")
        with self._lock:
            cur = self._db.execute("UPDATE ideas SET status=?, spec=?, updated_at=? WHERE id=?",
                                   (status, spec, now(), idea_id))
            return cur.rowcount == 1

    def count_new(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM ideas WHERE status='new'").fetchone()[0]


def notebook(cfg) -> Notebook:
    return Notebook(cfg.state_dir / "state.db")


# ---- プロンプトに足す説明 -----------------------------------------------------------
def fragment(cfg, book: Notebook | None = None, limit: int = 30) -> str:
    """各役のプロンプトの {ideas} に入る説明。prompts_dir に ideas.md が無ければ何も足さない。"""
    path = cfg.prompts_dir / "ideas.md"
    if not path.is_file():
        return ""
    titles = [i.title for i in (book or notebook(cfg)).all()[:limit]]
    existing = "、".join(f"「{t}」" for t in titles) or "（まだない）"
    return path.read_text(encoding="utf-8").replace("{example}", EXAMPLE).replace("{existing}", existing).strip()
