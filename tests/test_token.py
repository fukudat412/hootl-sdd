"""hootl token: ログイン用のトークンを出す。"""

import os

from hootl.cli import main
from hootl.web import job_env


def test_token_show(tmp_path, capsys):
    cfg = tmp_path / "web.toml"
    assert main(["token", "--web-config", str(cfg), "--show"]) == 1          # まだ setup していない
    cfg.write_text('port = 8765\ntoken = "abcdef0123456789abcdef"\n')
    capsys.readouterr()
    assert main(["token", "--web-config", str(cfg), "--show"]) == 0
    assert capsys.readouterr().out.strip() == "abcdef0123456789abcdef"


def test_web_token_is_not_handed_to_agents(tmp_path, monkeypatch, capsys):
    """管理画面のトークンを環境変数で渡していても、仕事・エージェント・ゲートには引き継がない。"""
    monkeypatch.setenv("HOOTL_WEB_TOKEN", "s" * 24)
    assert "HOOTL_WEB_TOKEN" not in job_env("/x") and job_env("/x")["PYTHONPATH"] == "/x"
    cfg = tmp_path / "web.toml"
    cfg.write_text('token = "abcdef0123456789abcdef"\n')
    assert main(["token", "--web-config", str(cfg), "--show"]) == 0          # web 以外のコマンドは、最初に落とす
    assert "HOOTL_WEB_TOKEN" not in os.environ
