"""安全柵。レビューより前に、機械的に判定できる違反を弾く。

弾くのは「判定を骨抜きにする変更」だけにする（判定する側のファイル・テストの無効化・削除）。
タスクの paths の外を触ったことは、out_of_scope="review" なら弾かずにレビュー役へ判断を回す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch


@dataclass
class GuardResult:
    ok: bool
    violations: list[str]
    out_of_scope: list[str] = field(default_factory=list)  # レビューで妥当性を見てもらう範囲外の変更


def _matches(path: str, globs: list[str]) -> bool:
    return any(fnmatch(path, g) for g in globs)


def check(changes: list[tuple[str, str]], diff: str, allowed: list[str], protected: list[str],
          test_globs: list[str], forbidden_patterns: list[str],
          always_allowed: list[str] | None = None, out_of_scope: str = "block") -> GuardResult:
    """protected = 判定する側（ゲート・規約・CI）。always_allowed = どのタスクでも触ってよいもの（依存・スキーマなど）。"""
    violations: list[str] = []
    outside: list[str] = []
    allowed = allowed + (always_allowed or [])

    if not changes:
        violations.append("変更がありません（何も実装されていない）")

    for status, path in changes:
        if _matches(path, protected):
            violations.append(f"保護ファイルの変更: {path}")
        elif not _matches(path, allowed):
            if out_of_scope == "review":
                outside.append(path)
            else:
                violations.append(f"許可パス外の変更: {path}（許可: {', '.join(allowed)}）")
        if status == "D" and _matches(path, test_globs):
            violations.append(f"テストファイルの削除: {path}")

    added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    for pattern in forbidden_patterns:
        rx = re.compile(pattern)
        hits = [line.strip() for line in added if rx.search(line)]
        for hit in hits[:3]:
            violations.append(f"禁止パターン `{pattern}` を追加: {hit[:120]}")

    return GuardResult(ok=not violations, violations=violations, out_of_scope=outside)
