"""仕様の流れ（自分で読む道）: 作りたいもの → 下書き → 直す → 凍結（main にコミット）→ タスク案（番号は続きから）。

承認なしで進む道（おまかせ）は tests/test_auto.py。
"""

import json
import subprocess
import time

import pytest

from hootl import specs
from hootl.config import load
from hootl.queue import Queue
from hootl.web import Api, Jobs, Project, WebConfig
from tests.test_harness import project, write_tasks  # noqa: F401


@pytest.fixture
def api(project):  # noqa: F811
    repo, scenario, prompt_log = project
    toml = repo / "hootl.toml"
    fake = toml.read_text().split("[agents.implementer]\ncmd = ")[1].split("\n")[0]
    toml.write_text(toml.read_text() + f"\n[agents.planner]\ncmd = {fake}\n")
    wc = WebConfig(token="t" * 24, projects={"demo": Project("demo", toml)})
    return Api(wc, Jobs()), repo, scenario, prompt_log


def wait(api, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = api.jobs.get("demo")
        if job and not job.alive():
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def git_log(repo):
    return subprocess.run(["git", "log", "--format=%s", "-3"], cwd=repo, capture_output=True, text=True).stdout


def test_idea_to_tasks(api):
    api, repo, scenario, prompt_log = api
    # すでに T-001, T-002 がある
    tasks = write_tasks(repo, scenario, [{"id": "T-001"}, {"id": "T-002"}])
    api.project("demo").queue().import_jsonl(tasks)

    # 1. 作りたいもの → 下書き
    with pytest.raises(ValueError):
        api.spec_draft("demo", "短い")
    r = api.spec_draft("demo", "間違えた過去問だけを、もう一度解ける復習モードがほしい", auto=False)
    slug = r["slug"]
    assert wait(api).proc.returncode == 0
    listed = api.specs("demo")["specs"]
    assert listed["drafts"][0]["slug"] == slug and listed["drafts"][0]["title"] == "過去問の復習モード"
    assert "間違えた過去問だけを" in prompt_log.read_text()          # ことばがエージェントに渡っている

    # 2. 直して保存
    text = api.spec(name="demo", ref=f"draft:{slug}")["text"]
    api.spec_save("demo", slug, text.replace("- 間違えた問題だけ出す", "- 間違えた問題だけ出す\n- 正解したら外す"))
    assert "正解したら外す" in api.spec("demo", f"draft:{slug}")["text"]

    # 3. 凍結 → main にコミット → そのままタスク案
    r = api.spec_freeze("demo", slug, auto=False)
    assert r["frozen"] == f"specs/{slug}.md" and r["job"]["kind"] == "plan"
    assert (repo / "specs" / f"{slug}.md").is_file()
    assert git_log(repo).splitlines()[0] == "spec: 過去問の復習モード"
    assert wait(api).proc.returncode == 0
    prop = api.proposed("demo")
    assert [t["id"] for t in prop["tasks"]] == ["T-003", "T-004"]   # 続きから
    assert prop["error"] is None
    # 計画には、すでにあるタスクの一覧が渡っている
    assert "T-002" in prompt_log.read_text().split("=== planner")[-1]
    # 下書きは控えに回り、一覧からは消える
    assert api.specs("demo")["specs"]["drafts"] == []
    assert api.specs("demo")["specs"]["frozen"][0]["path"] == f"specs/{slug}.md"


def test_freeze_guards(api):
    api, repo, scenario, _ = api
    cfg = load(repo / "hootl.toml")
    slug = specs.new_slug()
    specs.save_draft(cfg, slug, "# 見出しだけ\n\n## 目的\nx\n")
    with pytest.raises(ValueError, match="やること"):
        api.spec_freeze("demo", slug)
    specs.save_draft(cfg, slug, "# 仕様\n\n## やること\n- x\n")
    (repo / "README.md").write_text("changed\n")                 # 未コミットの変更がある
    with pytest.raises(ValueError, match="未コミット"):
        api.spec_freeze("demo", slug)
    subprocess.run(["git", "checkout", "README.md"], cwd=repo, check=True)
    # 実行中は凍結しない
    tasks = write_tasks(repo, scenario, [{"id": "T-001"}])
    q = Queue(cfg.state_dir / "state.db")
    q.import_jsonl(tasks)
    q.claim_next()
    with pytest.raises(ValueError, match="実行中"):
        api.spec_freeze("demo", slug)


def test_resolve_only_known_files(api):
    api, repo, *_ = api
    cfg = load(repo / "hootl.toml")
    for bad in ["../hootl.toml", "specs/../hootl.toml", "draft:../../x", "draft:abc", "/etc/passwd", "hootl.toml"]:
        with pytest.raises(LookupError):
            specs.resolve(cfg, bad)


def test_next_task_id(api):
    api, repo, scenario, _ = api
    q = api.project("demo").queue()
    assert specs.next_task_id(q) == "T-001"
    q.import_jsonl(write_tasks(repo, scenario, [{"id": "T-009"}, {"id": "T-010"}, {"id": "X-1"}]))
    assert specs.next_task_id(q) == "T-011"


def test_slugs_do_not_collide_within_a_second(api):
    api, repo, *_ = api
    cfg = load(repo / "hootl.toml")
    first = specs.new_slug(cfg)
    specs.save_draft(cfg, first, "# 仕様\n\n## やること\n- x\n")
    second = specs.new_slug(cfg)                       # 同じ秒でも、別の名前になる
    assert second != first and specs.SLUG.match(second)
    specs.freeze(cfg, first)                            # 凍結済みの名前とも重ならない
    (specs.drafts_dir(cfg) / f"{first}.frozen").unlink()
    assert specs.new_slug(cfg) != first


def test_strip_fences_and_title():
    assert specs.strip_fences("```markdown\n# A\nb\n```") == "# A\nb\n"
    assert specs.title_of("x\n# 名前\n") == "名前"


def test_cli_notice_is_not_part_of_the_spec():
    raw = "# 名前\n\n## やること\n- a\n\nWarning: this sign-in ends between 2026-12-02 and 2026-12-05. Run claude auth login before then.\n"
    assert specs.strip_fences(raw) == "# 名前\n\n## やること\n- a\n"
    assert specs.strip_fences("# 名前\n\n## やること\n- Warning: と書いた行は残す\n").endswith("残す\n")
