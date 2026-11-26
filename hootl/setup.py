"""hootl setup / doctor: Web 管理画面を使い始めるまでを1コマンドにする。

  hootl setup                       手元で使う（設定ファイル・トークン・自動起動・ブラウザを開く）
  hootl setup --email me@example.com --hostname hootl.example.dev
                                    スマホからも使う（Cloudflare Tunnel。Access を作ったら --access-ready を足して再実行）
  hootl doctor                      いまの状態を一覧する

何度実行してもよい。あるものは残し（トークンは作り直さない）、足りないものだけ作る。
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

WEB_LABEL = "dev.hootl.web"
TUNNEL_LABEL = "dev.hootl.tunnel"
TUNNEL_NAME = "hootl"


# ---- 外の世界とのやりとり（テストで差し替える）--------------------------------
@dataclass
class Env:
    home: Path = field(default_factory=Path.home)
    path: str = field(default_factory=lambda: os.environ.get("PATH", ""))
    is_mac: bool = field(default_factory=lambda: platform.system() == "Darwin")
    dry_run: bool = False
    out: list[str] = field(default_factory=list)   # 画面に出した行（テスト用に控える）

    @property
    def config_dir(self) -> Path:
        return self.home / ".config" / "hootl"

    @property
    def web_toml(self) -> Path:
        return self.config_dir / "web.toml"

    @property
    def launch_agents(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def cloudflared_dir(self) -> Path:
        return self.home / ".cloudflared"

    def say(self, line: str = "") -> None:
        self.out.append(line)
        print(line, flush=True)

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.path)

    def node_major(self) -> int | None:
        try:
            out = subprocess.run(["node", "-p", "process.versions.node.split('.')[0]"], capture_output=True,
                                 text=True, timeout=20, env={**os.environ, "PATH": self.path})
            return int(out.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def run(self, cmd: list[str], interactive: bool = False) -> subprocess.CompletedProcess:
        if self.dry_run:
            self.say(f"  （実行しない）$ {' '.join(cmd)}")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if interactive:
            return subprocess.run(cmd)
        return subprocess.run(cmd, capture_output=True, text=True)

    def write(self, path: Path, text: str, mode: int | None = None) -> None:
        if self.dry_run:
            self.say(f"  （書かない）{path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)


# ---- 1. 道具 ---------------------------------------------------------------
TOOLS = [
    # 名前, 必須か, 無いときの入れ方
    ("git", True, "xcode-select --install"),
    ("claude", True, "Claude Code を入れる（npm i -g @anthropic-ai/claude-code など）"),
    ("codex", True, "brew install codex"),
    ("node", False, "nvm install（プロジェクトの .nvmrc の版）"),
    ("docker", False, "Docker Desktop（DB を使うプロジェクトだけ）"),
]


MIN_NODE = 20   # Next.js 16・Expo SDK 58・wrangler 4 が求める下限


def check_tools(env: Env, need_cloudflared: bool) -> bool:
    env.say("道具")
    ok = sys.version_info >= (3, 11)
    env.say(f"  {'✓' if ok else '✗'} python {platform.python_version()}" + ("" if ok else "（3.11 以上が要る）"))
    tools = TOOLS + ([("cloudflared", True, "brew install cloudflared")] if need_cloudflared else [])
    for name, required, hint in tools:
        where = env.which(name)
        if where and name == "node":
            major = env.node_major()
            good = major is not None and major >= MIN_NODE
            env.say(f"  {'✓' if good else '✗'} node v{major if major is not None else '?'}  {where}"
                    + ("" if good else f"（{MIN_NODE} 以上が要る。nvm などで切り替えたターミナルから実行し直す）"))
            ok = ok and good
        elif where:
            env.say(f"  ✓ {name}  {where}")
        else:
            env.say(f"  {'✗' if required else '・'} {name} が見つかりません → {hint}")
            ok = ok and not required
    return ok


# ---- 2. 設定ファイル ---------------------------------------------------------
def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # TOML の基本文字列と同じ書き方


def ensure_web_config(env: Env, runs_dir: Path, port: int, emails: list[str]) -> tuple[str, int, bool]:
    """web.toml を作る（あれば中身を残して、足りない所だけ足す）。(トークン, port, 新しく作ったか) を返す。"""
    existing: dict = {}
    if env.web_toml.exists():
        existing = tomllib.loads(env.web_toml.read_text(encoding="utf-8"))
    token = existing.get("token") or secrets.token_hex(24)
    port = int(existing.get("port", port))
    allowed = list(dict.fromkeys([*existing.get("allowed_emails", []), *[e.lower() for e in emails]]))
    projects_dir = existing.get("projects_dir") or str(runs_dir)
    lines = [
        "# hootl web の設定（hootl setup が作った。手で直してもよい）",
        f"host = {toml_str(existing.get('host', '127.0.0.1'))}",
        f"port = {port}",
        f"token = {toml_str(token)}",
        f"allowed_emails = [{', '.join(toml_str(e) for e in allowed)}]",
        "# この下の <名前>/hootl.toml を自動で拾う（増やしたら再起動なしで画面に出る）",
        f"projects_dir = {toml_str(projects_dir)}",
    ]
    for p in existing.get("projects", []):  # 手で足したものは残す
        lines += ["", "[[projects]]", f"name = {toml_str(p['name'])}", f"config = {toml_str(p['config'])}"]
        if p.get("spec"):
            lines.append(f"spec = {toml_str(p['spec'])}")
    created = not env.web_toml.exists()
    env.write(env.web_toml, "\n".join(lines) + "\n", mode=0o600)
    done = ("作る" if created else "更新する・トークンはそのまま") if env.dry_run else \
        ("作った" if created else "更新した・トークンはそのまま")
    env.say(f"  ✓ {env.web_toml}（{done}）")
    return token, port, created


def found_projects(runs_dir: Path) -> list[str]:
    return sorted(p.parent.name for p in runs_dir.glob("*/hootl.toml")) if runs_dir.is_dir() else []


# ---- 3. 自動起動（launchd）---------------------------------------------------
def plist_xml(label: str, args: list[str], env_vars: dict[str, str], log: str) -> str:
    arg_xml = "".join(f"\n    <string>{escape(a)}</string>" for a in args)
    env_xml = "".join(f"\n    <key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in env_vars.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>{arg_xml}
  </array>
  <key>EnvironmentVariables</key>
  <dict>{env_xml}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{escape(log)}</string>
  <key>StandardErrorPath</key><string>{escape(log)}</string>
</dict>
</plist>
"""


def install_agent(env: Env, label: str, args: list[str], env_vars: dict[str, str]) -> Path:
    plist = env.launch_agents / f"{label}.plist"
    log = str(env.home / "Library" / "Logs" / f"{label}.log")
    env.write(plist, plist_xml(label, args, env_vars, log))
    uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
    env.run(["launchctl", "bootout", f"gui/{uid}", str(plist)])          # 前のを止める（無ければ何もしない）
    r = env.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
    if r.returncode != 0:
        env.say(f"  ✗ launchctl bootstrap に失敗: {(r.stderr or '').strip()}")
    else:
        env.say(f"  ✓ 自動で起動する: {label}（ログ: {log}）")
    return plist


def web_agent_args(env: Env) -> tuple[list[str], dict[str, str]]:
    pkg_root = str(Path(__file__).resolve().parent.parent)
    args = [sys.executable, "-m", "hootl.cli", "web", "--web-config", str(env.web_toml)]
    # launchd はターミナルの PATH を持たない。hootl run の中で claude・codex・node を呼ぶので、いまの PATH を渡す
    return args, {"PATH": env.path, "PYTHONPATH": pkg_root, "PYTHONUNBUFFERED": "1"}


def wait_web(port: int, seconds: float = 15) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2) as res:
                if res.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


# ---- 4. Cloudflare Tunnel ----------------------------------------------------
ACCESS_STEPS = """\
  Cloudflare の門番（Access）を先に作ってください。作るまで、外への公開（DNS とトンネル）は始めません。
    1. Cloudflare のダッシュボード → Zero Trust → Access → Applications → Add an application → Self-hosted
    2. ドメイン: {host}
    3. ポリシー: Allow ／ Include: Emails = {emails}
    4. ログイン方法: One-time PIN
  作ったら、同じコマンドに --access-ready を足して、もう一度実行してください。"""


def tunnel_id(env: Env) -> str | None:
    r = env.run(["cloudflared", "tunnel", "list", "--output", "json"])
    try:
        for t in json.loads(r.stdout or "[]"):
            if t.get("name") == TUNNEL_NAME:
                return t.get("id")
    except json.JSONDecodeError:
        pass
    return None


def setup_tunnel(env: Env, host: str, port: int, emails: list[str], access_ready: bool) -> bool:
    env.say("")
    env.say(f"スマホから（{host}）")
    if not emails:
        env.say("  ✗ --email が要ります（Access と画面の両方で、このメールだけを通す）")
        return False
    if not access_ready:
        env.say(ACCESS_STEPS.format(host=host, emails=", ".join(emails)))
        return False
    if not (env.cloudflared_dir / "cert.pem").exists():
        env.say("  ・Cloudflare にログインします（ブラウザが開くので、ドメインを選んでください）")
        env.run(["cloudflared", "tunnel", "login"], interactive=True)
    tid = tunnel_id(env)
    if not tid:
        env.run(["cloudflared", "tunnel", "create", TUNNEL_NAME])
        tid = tunnel_id(env) or ("<UUID>" if env.dry_run else None)
    if not tid:
        env.say("  ✗ トンネルを作れませんでした。`cloudflared tunnel list` で確かめてください")
        return False
    env.say(f"  ✓ トンネル {TUNNEL_NAME}（{tid}）")
    r = env.run(["cloudflared", "tunnel", "route", "dns", TUNNEL_NAME, host])
    if r.returncode != 0 and "already exists" not in (r.stderr or "") + (r.stdout or ""):
        env.say(f"  ✗ DNS の設定に失敗: {(r.stderr or r.stdout or '').strip()[:200]}")
        return False
    env.say(f"  ✓ DNS: {host} → トンネル")
    cfg = env.cloudflared_dir / "hootl.yml"
    env.write(cfg, "\n".join([
        f"tunnel: {TUNNEL_NAME}",
        f"credentials-file: {env.cloudflared_dir / (tid + '.json')}",
        "ingress:",
        f"  - hostname: {host}",
        f"    service: http://127.0.0.1:{port}",
        "  - service: http_status:404",
    ]) + "\n")
    env.say(f"  ✓ {cfg}")
    cloudflared = env.which("cloudflared") or "cloudflared"
    if env.is_mac:
        install_agent(env, TUNNEL_LABEL, [cloudflared, "tunnel", "--config", str(cfg), "run", TUNNEL_NAME], {"PATH": env.path})
    else:
        env.say(f"  ・Mac ではないので自動起動は登録しません。起動: {cloudflared} tunnel --config {cfg} run {TUNNEL_NAME}")
    return True


# ---- まとめ ------------------------------------------------------------------
def setup(env: Env, runs_dir: Path, port: int = 8765, emails: list[str] | None = None,
          hostname: str | None = None, access_ready: bool = False,
          launchd: bool = True, open_browser: bool = True) -> int:
    emails = emails or []
    if not check_tools(env, need_cloudflared=bool(hostname)):
        env.say("")
        env.say("✗ 足りない道具があります。入れてから、もう一度 hootl setup を実行してください")
        return 1

    env.say("")
    env.say("設定")
    token, port, _ = ensure_web_config(env, runs_dir, port, emails)
    projects = found_projects(runs_dir)
    env.say(f"  {'✓' if projects else '・'} プロジェクト: {', '.join(projects) or f'まだなし（{runs_dir} の下に専用 clone を作ると出てくる）'}")

    env.say("")
    env.say("起動")
    if launchd and env.is_mac:
        args, env_vars = web_agent_args(env)
        install_agent(env, WEB_LABEL, args, env_vars)
        if not env.dry_run:
            env.say(f"  {'✓ 応答あり' if wait_web(port) else '✗ 応答がありません（ログを見てください）'}: http://127.0.0.1:{port}")
    else:
        env.say(f"  ・自動起動は登録しません。起動: hootl web")

    tunnel_ok = True
    if hostname:
        tunnel_ok = setup_tunnel(env, hostname, port, emails, access_ready)

    env.say("")
    copied = False
    if env.is_mac and env.which("pbcopy") and not env.dry_run:
        copied = subprocess.run(["pbcopy"], input=token, text=True).returncode == 0
    env.say("ログイン用のトークン: " + ("クリップボードに入れました（貼り付けて入る）" if copied else token))
    url = f"https://{hostname}" if hostname and tunnel_ok and access_ready else f"http://127.0.0.1:{port}"
    env.say(f"開く: {url}")
    if open_browser and env.is_mac and not env.dry_run:
        env.run(["open", f"http://127.0.0.1:{port}"])
    return 0 if tunnel_ok else 2


def doctor(env: Env, runs_dir: Path) -> int:
    ok = check_tools(env, need_cloudflared=(env.cloudflared_dir / "hootl.yml").exists())
    env.say("")
    env.say("設定")
    if not env.web_toml.exists():
        env.say(f"  ✗ {env.web_toml} がありません → hootl setup")
        return 1
    raw = tomllib.loads(env.web_toml.read_text(encoding="utf-8"))
    port = int(raw.get("port", 8765))
    mode = oct(env.web_toml.stat().st_mode & 0o777)
    env.say(f"  {'✓' if mode == '0o600' else '・'} {env.web_toml}（権限 {mode}{'' if mode == '0o600' else '・chmod 600 にする'}）")
    env.say(f"  ・メールの照合: {', '.join(raw.get('allowed_emails', [])) or 'しない（Mac の中だけで使う設定）'}")
    pdir = Path(raw.get("projects_dir", runs_dir)).expanduser()
    projects = found_projects(pdir) + [p["name"] for p in raw.get("projects", [])]
    env.say(f"  {'✓' if projects else '・'} プロジェクト: {', '.join(projects) or 'まだなし'}")
    env.say("")
    env.say("起動")
    up = wait_web(port, seconds=2)
    env.say(f"  {'✓' if up else '✗'} hootl web（http://127.0.0.1:{port}）{'' if up else ' が応答しません → hootl setup で自動起動を登録'}")
    tunnel_cfg = env.cloudflared_dir / "hootl.yml"
    if tunnel_cfg.exists():
        host = next((ln.split(":", 1)[1].strip() for ln in tunnel_cfg.read_text().splitlines()
                     if ln.strip().startswith("- hostname:")), "?")
        env.say(f"  ✓ トンネルの設定: {host}（{tunnel_cfg}）")
    else:
        env.say("  ・トンネルなし（スマホから使うなら hootl setup --email ... --hostname ...）")
    return 0 if ok and up else 1
