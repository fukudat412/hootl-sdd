"""起動確認: アプリを空いているポートで起動し、URL が 2xx を返すまで待って止める。

ゲートとして使う（どの型でも同じ部品）。並列で回してもポートがぶつからないよう、毎回空きを探す。

    python3 boot.py --cmd "npx next start -p {port}" --url "http://127.0.0.1:{port}/api/health"

hootl パッケージには依存しない（ゲートの環境に PYTHONPATH が無くても動くように）。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="起動して応答を確かめる")
    ap.add_argument("--cmd", help="{port} を空きポートに置き換えて実行する")
    ap.add_argument("--static", help="このフォルダを静的に配信して確かめる（書き出した Web 版など）")
    ap.add_argument("--url", required=True, help="{port} を置き換えて GET する")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--expect", default="", help="本文に含まれているべき文字列")
    args = ap.parse_args(argv)

    if not args.cmd and not args.static:
        ap.error("--cmd か --static のどちらかが要ります")
    port = free_port()
    if args.static:
        args.cmd = f'"{sys.executable}" -m http.server {{port}} -b 127.0.0.1 -d "{args.static}"'
    cmd, url = args.cmd.replace("{port}", str(port)), args.url.replace("{port}", str(port))
    print(f"起動: {cmd}", flush=True)
    proc = subprocess.Popen(cmd, shell=True, start_new_session=True,
                            env={**os.environ, "PORT": str(port)})
    deadline = time.time() + args.timeout
    last = ""
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"起動したプロセスが終了しました（コード {proc.returncode}）", file=sys.stderr)
                return 1
            try:
                with urllib.request.urlopen(url, timeout=5) as res:
                    body = res.read().decode("utf-8", "replace")
                    if 200 <= res.status < 300 and args.expect in body:
                        print(f"応答あり: {url} → {res.status}", flush=True)
                        return 0
                    last = f"{res.status} {body[:200]}"
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                last = str(exc)
            time.sleep(1)
        print(f"{args.timeout:.0f} 秒待っても応答がありません: {url}（最後: {last}）", file=sys.stderr)
        return 1
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    sys.exit(main())
