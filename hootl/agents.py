"""エージェント呼び出し。ベンダー差は hootl.toml のコマンド配列に閉じ込める。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AgentSpec


@dataclass
class AgentResult:
    ok: bool
    code: int
    output: str
    cost_usd: float | None = None
    turns: int | None = None
    stdout: str = ""     # エージェント自身の出力だけ（標準エラーには、渡したプロンプトの写しが混ざることがある）


def run_agent(spec: AgentSpec, prompt: str, cwd: Path, log_path: Path, env: dict[str, str]) -> AgentResult:
    """プロンプトは標準入力で渡す（引数長の上限を避ける）。{prompt_file} があればファイルで渡す。"""
    prompt_file = log_path.with_suffix(".prompt.md")
    prompt_file.write_text(prompt, encoding="utf-8")
    cmd = [arg.replace("{prompt_file}", str(prompt_file)).replace("{cwd}", str(cwd)) for arg in spec.cmd]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, input=prompt if spec.stdin else None, capture_output=True, text=True,
            timeout=spec.timeout_sec, env={**os.environ, **env},
        )
        output, code = proc.stdout, proc.returncode
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        output, code, stderr = str(exc.stdout or ""), 124, f"[hootl] timeout after {spec.timeout_sec}s"
    except FileNotFoundError as exc:
        output, code, stderr = "", 127, f"[hootl] command not found: {exc}"
    log_path.write_text(f"$ {' '.join(cmd)}\n\n--- stdout ---\n{output}\n--- stderr ---\n{stderr}\n--- 終了コード ---\n{code}\n",
                        encoding="utf-8")

    cost, turns = _usage(output)
    return AgentResult(ok=code == 0, code=code, output=output + ("\n" + stderr if stderr else ""),
                       cost_usd=cost, turns=turns, stdout=output)


def _usage(output: str) -> tuple[float | None, int | None]:
    """claude -p --output-format json の結果から消費量を拾う。拾えなければ None。"""
    try:
        data = json.loads(output.strip().splitlines()[-1]) if output.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    cost = data.get("total_cost_usd")
    turns = data.get("num_turns")
    return (float(cost) if cost is not None else None, int(turns) if turns is not None else None)


@dataclass
class Verdict:
    passed: bool
    findings: list[str]
    raw_ok: bool  # verdict JSON を読めたか


_VERDICT_RX = re.compile(r"\{[^{}]*\"verdict\"\s*:\s*\"(pass|fail)\"[^{}]*(?:\[[^\]]*\][^{}]*)?\}", re.S)


def parse_verdict(output: str) -> Verdict:
    """出力中の最後の verdict JSON を読む。読めなければ fail 扱い（無言の合格を作らない）。"""
    matches = list(_VERDICT_RX.finditer(output))
    for match in reversed(matches):
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        findings = [str(f) for f in data.get("findings", [])]
        return Verdict(passed=data.get("verdict") == "pass", findings=findings, raw_ok=True)
    return Verdict(passed=False, findings=["レビュー結果の verdict JSON が読めませんでした"], raw_ok=False)
