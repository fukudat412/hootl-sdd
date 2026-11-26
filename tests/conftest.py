"""テストでは、手元の設定フォルダ（~/.config/hootl）を読み書きしない。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOOTL_CONFIG_DIR", str(tmp_path_factory.mktemp("hootl-config")))
    monkeypatch.delenv("HOOTL_CLAUDE_SETTINGS", raising=False)
