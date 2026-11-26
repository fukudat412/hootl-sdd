"""テスト用の偽エージェント。シナリオに応じてファイルを書く・落ちる・レビュー判定を返す。"""

import json
import os
import pathlib
import sys

role = os.environ["HOOTL_ROLE"]
task = os.environ["HOOTL_TASK_ID"]
attempt = int(os.environ["HOOTL_ATTEMPT"])
scenario = json.loads(pathlib.Path(os.environ["FAKE_SCENARIO"]).read_text()).get(task, "ok")
prompt = sys.stdin.read()
pathlib.Path(os.environ["FAKE_PROMPT_LOG"]).open("a").write(f"=== {role} {task} {attempt}\n{prompt}\n")

if role == "spec-writer":
    if scenario == "spec_empty":
        print("すみません、書けませんでした")
        sys.exit(0)
    last = "## 自分で決めたこと\n- 卒業は3回正解（小さく始める）" if "自分で決めたこと" in prompt else "## 決めてほしいこと\nなし"
    print("# 過去問の復習モード\n\n## 目的\n間違えた過去問を復習する。\n\n## やること\n- 間違えた問題だけ出す\n\n"
          "## やらないこと\n- なし\n\n## 前提と制約\n- 今の構成\n\n" + last)
    # 仕様を書いている途中で気づいた案（本文には残さず、ネタ帳へ）
    print('::idea {"title": "復習の時間を通知する", "body": "復習する問題がたまったら知らせると、続けやすい。"}')
    sys.exit(0)
if role == "planner":
    import re
    m = re.search(r"id は (T-(\d+)) から", prompt)
    n = int(m.group(2)) if m else 1
    for i in range(2):
        deps = ["T-999"] if scenario == "plan_bad_dep" and i == 1 else []
        print(json.dumps({"id": f"T-{n + i:03d}", "title": f"計画 {i}", "description": "d",
                          "acceptance": ["a"], "paths": ["src/*"], "depends_on": deps, "epic": "e", "priority": 10}))
    sys.exit(0)
if role == "reviewer":
    if scenario == "review_flaky":   # 1回目だけ、何も返さずに落ちる（レビュー役の一時的な不調）
        seen = pathlib.Path(os.environ["FAKE_PROMPT_LOG"]).read_text().count(f"=== reviewer {task} {attempt}\n")
        if seen == 1:
            sys.exit(1)
    if scenario == "idea":
        print('::idea {"title": "正答率をグラフで見る", "body": "差分を読んでいて気づいた。推移が見えると弱点が分かる。"}')
    if scenario == "review_fail_once" and attempt == 1:
        print('考察…\n{"verdict": "fail", "findings": ["AC1 のテストがない"]}')
    elif scenario == "review_garbage":
        print("LGTM!")
    else:
        print('{"verdict": "pass", "findings": []}')
    sys.exit(0)

if not pathlib.Path(".setup_done").exists():  # 準備工程がエージェントより先に走っていること
    sys.exit(3)
src = pathlib.Path("src")
src.mkdir(exist_ok=True)
if scenario == "slow":
    import time
    time.sleep(float(os.environ.get("FAKE_SLOW", "2")))
if scenario == "crash":
    sys.exit(1)
if scenario == "noop":
    sys.exit(0)
if scenario == "outside":
    pathlib.Path("other").mkdir(exist_ok=True)
    (pathlib.Path("other") / f"{task}.txt").write_text("ok")
elif scenario == "skip":
    (src / f"{task}.txt").write_text("ok")
    (src / f"{task}.test.ts").write_text("it.skip('x', () => {})\n")
elif scenario == "never" or (scenario == "fail_once" and attempt == 1):
    (src / f"{task}.txt").write_text("bad")
else:
    (src / f"{task}.txt").write_text("ok")
result = "実装しました。"
if scenario == "idea":   # 本文は JSON の result の中に入る（claude -p --output-format json と同じ形）
    result += '\n::idea {"title": "間違えた理由をメモできる", "body": "実装中に気づいた。あとで見返せると復習が速い。"}'
    result += '\n::idea {"title": "間違えた理由をメモできる", "body": "同じ案は1つだけ入る"}'
print(json.dumps({"type": "result", "result": result, "total_cost_usd": 0.5, "num_turns": 7}, ensure_ascii=False))
