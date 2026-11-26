"""処理の本体（ひな形の見本。最初の機能を作ったら置き換えてよい）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Result:
    ok: bool
    summary: str


def run(now: datetime, dry_run: bool = False) -> Result:
    mode = "（dry-run）" if dry_run else ""
    return Result(ok=True, summary=f"{now:%Y-%m-%d %H:%M} に実行しました{mode}")
