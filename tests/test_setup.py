"""hootl setup / doctor: 何度実行しても壊れない・外への公開は Access の確認後だけ。"""

import json
import plistlib
import subprocess
import tomllib
from pathlib import Path

from hootl.setup import Env, doctor, ensure_web_config, plist_xml, setup
from hootl.web import load_web_config


class FakeEnv(Env):
    """コマンドは実行せず記録だけ。which は与えた道具だけ見つかる。"""

    def __init__(self, home: Path, tools=("git", "claude", "codex", "node"), is_mac=True, tunnels=None):
        super().__init__(home=home, path="/opt/bin:/usr/bin", is_mac=is_mac)
        self.node = 26
        self.tools = set(tools)
        self.cmds: list[list[str]] = []
        self.tunnels = tunnels if tunnels is not None else []

    def node_major(self):
        return self.node

    def which(self, name):
        return f"/opt/bin/{name}" if name in self.tools else None

    def run(self, cmd, interactive=False):
        self.cmds.append(cmd)
        out = ""
        if cmd[:3] == ["cloudflared", "tunnel", "list"]:
            out = json.dumps(self.tunnels)
        if cmd[:3] == ["cloudflared", "tunnel", "create"]:
            self.tunnels.append({"name": cmd[3], "id": "abc-123"})
        return subprocess.CompletedProcess(cmd, 0, out, "")


def make_project(runs: Path, name: str, spec: str | None = None):
    d = runs / name
    d.mkdir(parents=True)
    (d / "hootl.toml").write_text((f'spec = "{spec}"\n' if spec else "") +
                                  '[agents.implementer]\ncmd = ["true"]\n')


def test_setup_local_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("hootl.setup.wait_web", lambda *a, **k: True)
    runs = tmp_path / "runs"
    make_project(runs, "quiz")
    env = FakeEnv(tmp_path / "home")
    assert setup(env, runs, open_browser=False) == 0

    raw = tomllib.loads(env.web_toml.read_text())
    assert len(raw["token"]) == 48 and raw["projects_dir"] == str(runs)
    assert oct(env.web_toml.stat().st_mode & 0o777) == "0o600"
    plist = env.launch_agents / "dev.hootl.web.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["EnvironmentVariables"]["PATH"] == "/opt/bin:/usr/bin"       # launchd にいまの PATH を渡す
    assert data["ProgramArguments"][1:5] == ["-m", "hootl.cli", "web", "--web-config"]
    assert ["launchctl", "bootstrap"] == env.cmds[1][:2]

    # 2回目: トークンは作り直さない・メールは足される
    token = raw["token"]
    env2 = FakeEnv(tmp_path / "home")
    assert setup(env2, runs, emails=["Me@Example.com"], open_browser=False) == 0
    raw2 = tomllib.loads(env2.web_toml.read_text())
    assert raw2["token"] == token and raw2["allowed_emails"] == ["me@example.com"]


def test_web_discovers_projects_without_restart(tmp_path):
    runs = tmp_path / "runs"
    make_project(runs, "quiz")
    env = FakeEnv(tmp_path / "home")
    ensure_web_config(env, runs, 8765, [])
    wc = load_web_config(env.web_toml)
    assert list(wc.all_projects()) == ["quiz"]
    make_project(runs, "mono", spec="mono/spec.md")   # あとから増やす
    found = wc.all_projects()
    assert list(found) == ["mono", "quiz"] and found["mono"].spec == "mono/spec.md"


def test_manual_projects_are_kept(tmp_path):
    env = FakeEnv(tmp_path / "home")
    env.config_dir.mkdir(parents=True)
    env.web_toml.write_text('token = "x' + "y" * 30 + '"\n[[projects]]\nname = "other"\nconfig = "/x/hootl.toml"\n')
    ensure_web_config(env, tmp_path / "runs", 8765, [])
    raw = tomllib.loads(env.web_toml.read_text())
    assert raw["projects"] == [{"name": "other", "config": "/x/hootl.toml"}]


def test_missing_tools_stop_before_writing(tmp_path):
    env = FakeEnv(tmp_path / "home", tools=("git",))
    assert setup(env, tmp_path / "runs", open_browser=False) == 1
    assert not env.web_toml.exists()
    assert any("claude が見つかりません" in line for line in env.out)


def test_tunnel_waits_for_access(tmp_path, monkeypatch):
    monkeypatch.setattr("hootl.setup.wait_web", lambda *a, **k: True)
    tools = ("git", "claude", "codex", "cloudflared")
    env = FakeEnv(tmp_path / "home", tools=tools)
    # Access を作る前は、DNS もトンネルも触らない
    code = setup(env, tmp_path / "runs", emails=["me@example.com"], hostname="hootl.example.dev", open_browser=False)
    assert code == 2
    assert not any(c[:2] == ["cloudflared", "tunnel"] for c in env.cmds)
    assert any("Zero Trust" in line for line in env.out)

    # Access を作った後
    env = FakeEnv(tmp_path / "home", tools=tools)
    (env.cloudflared_dir).mkdir(parents=True, exist_ok=True)
    (env.cloudflared_dir / "cert.pem").write_text("x")
    code = setup(env, tmp_path / "runs", emails=["me@example.com"], hostname="hootl.example.dev",
                 access_ready=True, open_browser=False)
    assert code == 0
    assert ["cloudflared", "tunnel", "create", "hootl"] in env.cmds
    assert ["cloudflared", "tunnel", "route", "dns", "hootl", "hootl.example.dev"] in env.cmds
    cfg = (env.cloudflared_dir / "hootl.yml").read_text()
    assert "abc-123.json" in cfg and "http://127.0.0.1:8765" in cfg
    assert (env.launch_agents / "dev.hootl.tunnel.plist").exists()


def test_dry_run_changes_nothing(tmp_path):
    env = FakeEnv(tmp_path / "home")
    env.dry_run = True
    env.run = Env.run.__get__(env)        # 実際の run と write の dry_run 分岐を使う
    env.write = Env.write.__get__(env)
    assert setup(env, tmp_path / "runs", open_browser=False) == 0
    assert not env.web_toml.exists() and not env.launch_agents.exists()


def test_doctor(tmp_path, monkeypatch):
    monkeypatch.setattr("hootl.setup.wait_web", lambda *a, **k: True)
    env = FakeEnv(tmp_path / "home")
    assert doctor(env, tmp_path / "runs") == 1                       # 設定が無い
    ensure_web_config(env, tmp_path / "runs", 8765, [])
    assert doctor(env, tmp_path / "runs") == 0


def test_plist_escapes():
    xml = plist_xml("x", ["/a&b"], {"PATH": "<p>"}, "/log")
    data = plistlib.loads(xml.encode())
    assert data["ProgramArguments"] == ["/a&b"] and data["EnvironmentVariables"]["PATH"] == "<p>"


def test_old_node_is_reported(tmp_path):
    env = FakeEnv(tmp_path)
    env.node = 16
    from hootl.setup import check_tools
    assert check_tools(env, need_cloudflared=False) is False
    assert any("node v16" in line and "20 以上" in line for line in env.out)
