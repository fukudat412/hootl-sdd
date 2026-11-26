"""ゲート。すべて「コマンドが終了コード0で終わるか」だけで判定する。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Gate


@dataclass
class GateResult:
    name: str
    ok: bool
    code: int
    output: str


def run_gates(gates: list[Gate], cwd: Path, log_dir: Path, env: dict[str, str],
              prefix: str = "gate") -> list[GateResult]:
    """先頭から順に実行し、落ちた時点で止める（後段は前段が通る前提のため）。
    ログは <prefix>-<name>.log。準備は "setup" で書く（ゲートと見分けるため）。"""
    results: list[GateResult] = []
    for gate in gates:
        try:
            proc = subprocess.run(
                gate.cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                timeout=gate.timeout_sec, env={**os.environ, **env},
            )
            output, code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = f"{exc.stdout or ''}{exc.stderr or ''}\n[hootl] timeout after {gate.timeout_sec}s"
            code = 124
        (log_dir / f"{prefix}-{gate.name}.log").write_text(output, encoding="utf-8")
        result = GateResult(gate.name, code == 0, code, output)
        results.append(result)
        if not result.ok:
            break
    return results
