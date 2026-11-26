"""ひな形カタログ: 型＋オプション＋ゲート部品から hootl.toml を組み立てる／新しいアプリを作る／ゲートを切り替える。

本物の型（npm を入れるもの）は時間とネットワークが要るので、ここではネットワーク不要の小さな型で仕組みを確かめる。
本物の型が最後まで通ることは、作業環境で `hootl new` を実際に流して確かめている。
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hootl import create, stacks
from hootl.config import load

REAL = Path(__file__).resolve().parent.parent / "stacks"
FAKE_AGENT = Path(__file__).resolve().parent / "fixtures" / "fake_agent.py"

TINY = """
title = "小さな型"
tech = "sh"
summary = "テスト用"
notes = "- ポートは {{port}}"

[[scaffold]]
name = "作る"
cmd = "echo scaffold > made.txt"

[[setup]]
name = "prep"
cmd = "true"

[[gates]]
name = "lint"
title = "lint"
cmd = "test -f README.md"

[[gates]]
name = "unit"
title = "単体テスト"
cmd = "test -f made.txt"
required = true

[[gates]]
name = "slow"
title = "遅いゲート"
cmd = "true"
default = false

[[verify]]
name = "起動確認"
cmd = "grep -q '{{name}}' README.md"

[guard]
protected_paths = ["lint.cfg"]

[env]
TINY = "1"

[[options]]
id = "extra"
title = "おまけ"
summary = "ゲートが1つ増える"

[[options.gates]]
name = "extra"
title = "おまけのゲート"
cmd = "test -f extra.txt"
after = "lint"

[[options]]
id = "broken"
title = "壊れたオプション"
summary = "作る途中で失敗する"
conflicts = ["extra"]

[[options.scaffold]]
name = "失敗する"
cmd = "exit 3"
"""


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    shutil.copytree(REAL / "_common", root / "_common")
    base = root / "_common" / "base.toml"
    agent = f'cmd = ["{sys.executable}", "{FAKE_AGENT}"]'
    # base.toml の cmd はエージェントの3役だけ。全部を偽のエージェントに差し替える
    text = re.sub(r"^cmd = \[.*?\]$", agent, base.read_text(), flags=re.S | re.M)
    assert text.count(agent) == 3
    base.write_text(text)
    tiny = root / "tiny"
    (tiny / "files").mkdir(parents=True)
    (tiny / "stack.toml").write_text(TINY)
    (tiny / "files" / "README.md.tmpl").write_text("# {{title}}\n{{name}} {{port}}\n")
    (tiny / "files" / "gitignore.tmpl").write_text(".hootl/\n")
    (tiny / "options" / "extra" / "files").mkdir(parents=True)
    (tiny / "options" / "extra" / "files" / "extra.txt").write_text("x")
    monkeypatch.setattr(stacks, "STACKS_DIR", root)
    monkeypatch.setenv("HOOTL_STACKS_DIR", str(root))   # 管理画面から起動する別プロセスにも効かせる
    runs = tmp_path / "runs"
    runs.mkdir()
    return runs


def make(runs, name="memo", **kw):
    req = create.Request(runs, name, "tiny", kw.pop("title", "メモ"), **kw)
    return create.create(req)


def test_catalog_lists_real_stacks():
    names = [s.name for s in stacks.catalog(REAL)]
    assert names == ["web-next", "mobile-expo", "batch-python", "worker-cf"]
    for s in stacks.catalog(REAL):
        gates = s.gates(list(s.options))
        assert any(g.required for g in gates), f"{s.name} に必須ゲートがない"
        assert s.raw.get("verify"), f"{s.name} に起動確認がない"


def test_create_runs_everything_and_commits(catalog, capsys):
    assert make(catalog, options=["extra"]) == 0
    out = capsys.readouterr().out
    repo = catalog / "memo"
    plan = json.loads(next(ln for ln in out.splitlines() if ln.startswith("::plan "))[7:])
    assert [s["id"] for s in plan][:3] == ["check", "folder", "files"]
    assert "::step gate-extra ok" in out and "::step verify-0 ok" in out and "::done memo" in out
    # 置いたファイル・置き換え・ドットファイル
    assert (repo / ".gitignore").exists() and not (repo / "gitignore").exists()
    assert "# メモ" in (repo / "README.md").read_text()
    assert "TINY" not in (repo / "CLAUDE.md").read_text() and "ポートは 31" in (repo / "CLAUDE.md").read_text()
    # コミットは1つ、作業ツリーはきれい
    log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
    assert log.strip().splitlines() == ["chore: ひな形から作成（小さな型・おまけ）"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""
    # hootl.toml は [stack] だけ。ゲートはカタログから組み立てられる（オプションのゲートは after の位置）
    cfg = load(repo / "hootl.toml")
    assert [g.name for g in cfg.gates] == ["lint", "extra", "unit"]
    assert [s.name for s in cfg.setup] == ["prep"]
    assert "lint.cfg" in cfg.protected_paths and "hootl.toml" in cfg.protected_paths
    assert cfg.extra_env["TINY"] == "1" and cfg.extra_env["CI"] == "1"
    assert str(FAKE_AGENT) in cfg.implementer.cmd


def test_required_gates_cannot_be_dropped_and_unknown_are_refused(catalog):
    assert make(catalog, gates=["lint"]) == 0
    raw = tomllib.loads((catalog / "memo" / "hootl.toml").read_text())
    assert raw["stack"]["gates"] == ["lint", "unit"]
    with pytest.raises(ValueError, match="使えないゲート"):
        stacks.check_selection(stacks.load_stack("tiny"), [], ["lint", "nope"])
    with pytest.raises(ValueError, match="一緒に使えません"):
        stacks.check_selection(stacks.load_stack("tiny"), ["extra", "broken"], None)


def test_failure_is_moved_aside_not_deleted(catalog, capsys):
    assert make(catalog, options=["broken"]) == 1
    out = capsys.readouterr().out
    assert "::step scaffold-1 fail" in out
    assert not (catalog / "memo").exists()
    moved = list((catalog / ".failed").iterdir())
    assert len(moved) == 1 and (moved[0] / "README.md").exists()
    assert f"::failed memo {moved[0]}" in out
    # 失敗したものは一覧に出ない（hootl.toml がない）、同じ名前でもう一度作れる
    assert not (moved[0] / "hootl.toml").exists()
    assert make(catalog) == 0


def test_names_ports_and_duplicates(catalog, capsys):
    assert make(catalog, name="Bad_Name") == 1
    assert "英小文字" in capsys.readouterr().out
    assert make(catalog, name="one") == 0 and make(catalog, name="two") == 0
    ports = [tomllib.loads((catalog / n / "hootl.toml").read_text())["stack"]["vars"]["port"] for n in ("one", "two")]
    assert ports[0] != ports[1]
    assert make(catalog, name="one") == 1
    assert "もうあります" in capsys.readouterr().out


def test_switch_gates_and_custom_gates(catalog):
    assert make(catalog) == 0
    path = catalog / "memo" / "hootl.toml"
    assert stacks.set_gates(path, ["slow"]) == ["unit", "slow"]
    assert [g.name for g in load(path).gates] == ["unit", "slow"]
    status = stacks.gate_status(tomllib.loads(path.read_text()))
    assert [(g["name"], g["on"]) for g in status["gates"]] == [("lint", False), ("unit", True), ("slow", True)]
    # このアプリだけのゲートは [[gates]] で後ろに足せる
    path.write_text(path.read_text() + '\n[[gates]]\nname = "mine"\ncmd = "true"\n')
    assert [g.name for g in load(path).gates] == ["unit", "slow", "mine"]
    assert stacks.gate_status(tomllib.loads(path.read_text()))["custom"] == [{"name": "mine", "cmd": "true"}]


def test_idea_becomes_spec_draft(catalog, tmp_path, monkeypatch, capsys):
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    monkeypatch.setenv("FAKE_SCENARIO", str(scenario))
    monkeypatch.setenv("FAKE_PROMPT_LOG", str(tmp_path / "prompts.log"))
    assert make(catalog, idea="間違えた問題だけ解き直したい\n3回正解で卒業") == 0
    out = capsys.readouterr().out
    assert "::step spec ok" in out
    repo = catalog / "memo"
    from hootl import specs
    drafts = specs.list_specs(load(repo / "hootl.toml"))["drafts"]
    assert len(drafts) == 1 and drafts[0]["title"]
    assert "間違えた問題だけ解き直したい" in (repo / "spec.md").read_text()
    assert "間違えた問題だけ解き直したい" in (tmp_path / "prompts.log").read_text()


# ---- 管理画面の API ----------------------------------------------------------
def test_web_new_app_progress_settings_and_gates(catalog):
    import time as _time
    from hootl.web import Api, Jobs, WebConfig

    wc = WebConfig(token="t" * 24, projects_dir=catalog)
    api = Api(wc, Jobs())
    names = [s["name"] for s in api.stacks()["stacks"]]
    assert names == ["tiny"]
    with pytest.raises(ValueError, match="英小文字"):
        api.new_app({"name": "Bad", "stack": "tiny"})
    with pytest.raises(ValueError, match="一緒に使えません"):
        api.new_app({"name": "ok-name", "stack": "tiny", "options": ["extra", "broken"]})

    api.new_app({"name": "web-made", "stack": "tiny", "title": "画面から", "options": ["extra"], "gates": ["lint"]})
    end = _time.time() + 30
    while _time.time() < end and api.creations()["creations"][0]["alive"]:
        _time.sleep(0.2)
    c = api.creations()["creations"][0]
    assert c["result"] == "done" and all(s["state"] == "ok" for s in c["steps"])
    assert "web-made" in [p["name"] for p in api.projects()["projects"]]
    with pytest.raises(ValueError, match="もうあります"):
        api.new_app({"name": "web-made", "stack": "tiny"})

    s = api.settings("web-made")
    assert s["catalog"]["stack"]["name"] == "tiny"
    assert [(g["name"], g["on"]) for g in s["catalog"]["gates"]] == [
        ("lint", True), ("extra", False), ("unit", True), ("slow", False)]
    s = api.set_gates("web-made", ["extra", "slow"])
    assert [g["name"] for g in s["gates"]] == ["extra", "unit", "slow"]
    log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=catalog / "web-made", capture_output=True, text=True)
    assert log.stdout.strip() == "chore: ゲートを extra, unit, slow にする"

    # 再起動しても（Jobs が空でも）、最近作ったものはログから出る
    again = Api(wc, Jobs()).creations()["creations"]
    assert again[0]["name"] == "web-made" and again[0]["result"] == "done"


def test_new_app_with_an_idea_goes_on_to_planning_by_itself(catalog, tmp_path, monkeypatch):
    """作りたいものを書いて作ると、下書きで止まらずに、凍結 → 計画 → 取り込み → 実行まで続く。"""
    import time as _time
    from hootl.web import Api, Jobs, WebConfig

    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    monkeypatch.setenv("FAKE_SCENARIO", str(scenario))
    monkeypatch.setenv("FAKE_PROMPT_LOG", str(tmp_path / "prompts.log"))
    api = Api(WebConfig(token="t" * 24, projects_dir=catalog), Jobs())
    api.new_app({"name": "goes-on", "stack": "tiny", "idea": "間違えた問題だけ解き直したい"})
    end = _time.time() + 60
    while _time.time() < end and not ((job := api.jobs.get("goes-on")) and not job.alive()):
        _time.sleep(0.2)
    job = api.jobs.get("goes-on")
    assert job and job.kind == "auto" and job.info()["phase"] == "run"
    c = api.creations()["creations"][0]
    assert c["result"] == "done" and "おまかせで続き" in c["note"]
    repo = catalog / "goes-on"
    log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
    assert "spec: 過去問の復習モード" in log
    from hootl.queue import Queue
    tasks = Queue(load(repo / "hootl.toml").state_dir / "state.db").all()
    assert [t.id for t in tasks] == ["T-001", "T-002"]          # 承認なしで取り込まれている
    assert all(t.status != "todo" for t in tasks)               # 回すところまで進んだ（この小さな型では実装は通らない）

    # 「おまかせ」を外して作ると、下書きで止まる
    api.new_app({"name": "stops", "stack": "tiny", "idea": "間違えた問題だけ解き直したい", "auto": False})
    while _time.time() < end + 30 and api.creations()["creations"][0]["alive"]:
        _time.sleep(0.2)
    _time.sleep(0.5)
    assert api.jobs.get("stops") is None and api.creations()["creations"][0]["note"] is None
    from hootl import specs
    assert len(specs.list_specs(load(catalog / "stops" / "hootl.toml"))["drafts"]) == 1


def test_agent_permissions_always_protect_the_harness_and_the_settings(tmp_path, monkeypatch):
    """エージェントに渡す権限設定には、元がどれでも、ハーネス自身と設定フォルダへの拒否が入る。"""
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("HOOTL_CONFIG_DIR", str(cfg_dir))
    need = stacks.required_denies()
    assert any(r.startswith("Edit(") and "hootl-sdd" in r or str(stacks.ROOT.name) in r for r in need)
    assert any(r.startswith("Read(") and "cfg" in r for r in need) and any(r.startswith("Edit(") and "cfg" in r for r in need)

    # 同梱の設定が元: 自分の環境の話は入っていないが、必須の拒否は足されている
    out = stacks.claude_settings_path()
    assert out == cfg_dir / "claude-settings.rendered.json" and oct(out.stat().st_mode)[-3:] == "600"
    deny = json.loads(out.read_text())["permissions"]["deny"]
    assert set(need) <= set(deny) and "Bash(git push:*)" in deny

    # 手元の版が元: 足した拒否は残り、必須の拒否は、書いていなくても（消されていても）足される
    (cfg_dir / "claude-settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Read"], "deny": ["Read(~/secret/**)"]}}))
    deny = json.loads(stacks.claude_settings_path().read_text())["permissions"]["deny"]
    assert "Read(~/secret/**)" in deny and set(need) <= set(deny)

    # カタログから組み立てた実装役は、この組み立てた設定を使う
    cmd = stacks.compose({"name": "web-next", "app": "x", "vars": {"port": 3101, "e2e_port": 4101}},
                         tmp_path, REAL)["agents"]["implementer"]["cmd"]
    assert cmd[cmd.index("--settings") + 1] == str(out)

    # 読めない設定のまま、守りなしでは回さない
    (cfg_dir / "claude-settings.json").write_text("{こわれた")
    with pytest.raises(ValueError, match="権限設定を読めません"):
        stacks.claude_settings_path()


def test_conflicts_are_two_way():
    web = stacks.load_stack("web-next", REAL)
    assert "postgres" in web.options["cloudflare"].conflicts
    assert "cloudflare" in web.options["postgres"].conflicts


def test_failed_creation_can_be_retried_and_dismissed(catalog):
    import time as _time
    from hootl.web import Api, Jobs, WebConfig

    api = Api(WebConfig(token="t" * 24, projects_dir=catalog), Jobs())
    api.new_app({"name": "will-fail", "stack": "tiny", "title": "失敗する", "options": ["broken"], "idea": "作りたいもの"})
    end = _time.time() + 30
    while _time.time() < end and api.creations()["creations"][0]["alive"]:
        _time.sleep(0.2)
    c = api.creations()["creations"][0]
    assert c["result"] == "failed"
    # 作り直すときの中身が残っている（画面がフォームに戻す）
    assert c["request"] == {"name": "will-fail", "title": "失敗する", "stack": "tiny", "options": ["broken"],
                            "gates": None, "idea": "作りたいもの", "auto": True}
    # 同じ名前でもう一度作れる（失敗したものは .failed に退避済み）
    api.new_app({**c["request"], "options": []})
    while _time.time() < end + 30 and api.creations()["creations"][0]["alive"]:
        _time.sleep(0.2)
    assert api.creations()["creations"][0]["result"] == "done"
    # 一覧から消せる（ログは脇にどけるだけ）。再起動後の一覧にも出ない
    api.dismiss_creation("will-fail")
    assert api.creations()["creations"] == []
    assert Api(api.wc, Jobs()).creations()["creations"] == []
    assert list((catalog / ".hootl-new").glob("will-fail.*.log.dismissed"))
