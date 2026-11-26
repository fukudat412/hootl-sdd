#!/bin/zsh -il
# -i で ~/.zshrc も読む（nvm などで切り替えた Node を、普段のターミナルと同じに使うため）
# hootl の管理画面を起動する（ダブルクリック用）。何度実行しても同じ状態になる。
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 以上が見つかりません。brew install python@3.12 のあと、もう一度ダブルクリックしてください。"
  read -k 1 "?何かキーを押すと閉じます"; exit 1
fi
echo "Python: $PY"
echo "Node: $(command -v node) $(node -v 2>/dev/null)"
export PYTHONPATH="$PWD"
"$PY" -m hootl.cli setup
echo
"$PY" -m hootl.cli doctor
echo
read -k 1 "?何かキーを押すとこの窓を閉じます（管理画面は裏で動き続けます）"
