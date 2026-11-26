"""hootl token: ログイン用のトークンを出す。"""

from hootl.cli import main


def test_token_show(tmp_path, capsys):
    cfg = tmp_path / "web.toml"
    assert main(["token", "--web-config", str(cfg), "--show"]) == 1          # まだ setup していない
    cfg.write_text('port = 8765\ntoken = "abcdef0123456789abcdef"\n')
    capsys.readouterr()
    assert main(["token", "--web-config", str(cfg), "--show"]) == 0
    assert capsys.readouterr().out.strip() == "abcdef0123456789abcdef"
