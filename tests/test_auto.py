"""おまかせ（承認なしで 仕様 → 凍結 → 計画 → 取り込み → 実行）と、ネタ帳。"""

import json
import subprocess
import time

import pytest

from hootl import ideas, report, specs
from hootl.config import load
from hootl.queue import Queue
from hootl.web import Api, Jobs, Project, WebConfig
from tests.test_harness import project, run_cli, write_tasks  # noqa: F401

IDEA = "間違えた過去問だけを、もう一度解ける復習モードがほしい"


@pytest.fixture
def api(project):  # noqa: F811
    repo, scenario, prompt_log = project
    toml = repo / "hootl.toml"
    fake = toml.read_text().split("[agents.implementer]\ncmd = ")[1].split("\n")[0]
    toml.write_text(toml.read_text() + f"\n[agents.planner]\ncmd = {fake}\n")
    scenario.write_text("{}")
    wc = WebConfig(token="t" * 24, projects={"demo": Project("demo", toml)})
    return Api(wc, Jobs()), repo, scenario, prompt_log


def wait(api, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = api.jobs.get("demo")
        if job and not job.alive():
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def git_subjects(repo):
    return subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout.splitlines()


# ---- おまかせ ---------------------------------------------------------------------
def test_idea_goes_all_the_way_without_approval(api):
    api, repo, scenario, prompt_log = api
    r = api.spec_draft("demo", IDEA)                       # 既定はおまかせ
    assert r["auto"] is True and r["job"]["kind"] == "auto"
    job = wait(api)
    assert job.proc.returncode == 0, job.info()["tail"]
    assert job.info()["phase"] == "run"                    # 最後の段まで進んだ
    slug = r["slug"]

    # 仕様: 人が凍結を押していないのに、main に入っている。下書きは残っていない
    spec = (repo / "specs" / f"{slug}.md").read_text()
    assert "spec: 過去問の復習モード" in git_subjects(repo)
    assert api.specs("demo")["specs"]["drafts"] == []
    # 質問は残さず、書き手が決めて理由を残している。ネタ帳の行は本文に混ざらない
    assert "自分で決めたこと" in spec and "決めてほしいこと" not in spec and "::idea" not in spec
    assert "質問は残さない" in prompt_log.read_text().split("=== spec-writer")[1]

    # 計画: 人が承認を押していないのに、取り込まれて、実装まで終わっている
    st = {t.id: t.status for t in api.project("demo").queue().all()}
    assert st == {"T-001": "done", "T-002": "done"}
    assert api.proposed("demo")["text"] == ""              # 承認待ちは残らない
    assert list((repo / ".hootl").glob("imported-*.jsonl"))  # 控えは残る
    assert (repo / "src" / "T-001.txt").read_text() == "ok"

    # 仕様づくりの途中で見つけた案は、作らずにネタ帳へ
    book = api.ideas("demo")["ideas"]
    assert [(i["title"], i["source"], i["status"]) for i in book] == [("復習の時間を通知する", "spec-writer", "new")]
    assert api.projects()["projects"][0]["ideas_new"] == 1


def test_bad_plan_is_not_imported_and_waits_for_a_human(api):
    api, repo, scenario, _ = api
    scenario.write_text(json.dumps({"-": "plan_bad_dep"}))   # ないタスク（T-999）に依存する案
    r = api.spec_draft("demo", IDEA)
    job = wait(api)
    assert job.proc.returncode == 3 and "T-999" in job.info()["tail"]
    assert job.info()["phase"] == "import"
    assert api.project("demo").queue().all() == []          # 取り込んでいない
    prop = api.proposed("demo")
    assert len(prop["tasks"]) == 2                           # 案は残っていて、人が直せる
    assert (repo / "specs" / f"{r['slug']}.md").is_file()
    # 承認待ちが残っている間は、次のおまかせを始めない（上書きして失わせない）
    with pytest.raises(ValueError, match="承認待ち"):
        api.spec_draft("demo", IDEA)
    with pytest.raises(ValueError, match="承認待ち"):
        api.idea_build("demo", api.ideas("demo")["ideas"][0]["id"])
    assert api.ideas("demo")["ideas"][0]["status"] == "new"  # 始められなかった案は、新しいまま
    # 捨てれば（消さずに控えに回る）、また始められる
    api.discard_proposed("demo")
    assert list((repo / ".hootl").glob("discarded-*.jsonl"))
    scenario.write_text("{}")
    api.spec_draft("demo", IDEA)
    assert wait(api).proc.returncode == 0


def test_spec_that_is_not_a_spec_stops_before_freezing(api):
    api, repo, scenario, _ = api
    scenario.write_text(json.dumps({"-": "spec_empty"}))
    before = git_subjects(repo)
    api.spec_draft("demo", IDEA)
    job = wait(api)
    assert job.proc.returncode == 3 and job.info()["phase"] == "draft"
    assert git_subjects(repo) == before and not (repo / "specs").exists()


def test_manual_path_still_works_and_can_hand_over(api):
    api, repo, scenario, _ = api
    r = api.spec_draft("demo", IDEA, auto=False)             # 下書きだけ（自分で読む）
    assert r["job"]["kind"] == "spec"
    assert wait(api).proc.returncode == 0
    slug = r["slug"]
    assert "決めてほしいこと" in api.spec("demo", f"draft:{slug}")["text"]
    r = api.spec_freeze("demo", slug)                        # 読んだあとは、おまかせ
    assert r["auto"] is True and r["job"]["kind"] == "auto"
    assert wait(api).proc.returncode == 0
    assert {t.status for t in api.project("demo").queue().all()} == {"done"}


def test_auto_can_be_turned_off_per_project(api):
    api, repo, scenario, _ = api
    toml = repo / "hootl.toml"
    toml.write_text(toml.read_text() + "\n[auto]\napprove = false\n")
    assert load(toml).auto_approve is False
    api = Api(api.wc, Jobs())
    assert api.projects()["projects"][0]["auto"]["approve"] is False
    r = api.spec_draft("demo", IDEA)
    assert r["auto"] is False and r["job"]["kind"] == "spec"
    wait(api)
    r = api.spec_freeze("demo", r["slug"])
    assert r["job"]["kind"] == "plan"
    wait(api)
    assert len(api.proposed("demo")["tasks"]) == 2 and api.project("demo").queue().all() == []


def test_cli_plan_approve_and_no_run(api):
    _, repo, scenario, _ = api
    (repo / "spec.md").write_text("# 全体\n\n## やること\n- x\n")
    assert run_cli(repo, "plan", str(repo / "spec.md"), "--approve") == 0
    q = Queue(load(repo / "hootl.toml").state_dir / "state.db")
    assert {t.id: t.status for t in q.all()} == {"T-001": "todo", "T-002": "todo"}
    idea = repo / ".hootl" / "idea.txt"
    idea.write_text(IDEA)
    assert run_cli(repo, "auto", "--idea-file", str(idea), "--no-run") == 0
    assert [t.id for t in q.all()] == ["T-001", "T-002", "T-003", "T-004"]     # 番号は続きから
    assert {t.status for t in q.all()} == {"todo"}                              # 回してはいない


def test_check_plan():
    class Q:
        def all(self):
            return [type("T", (), {"id": "T-001"})()]

    def t(tid, deps=(), acc=("a",)):
        return {"id": tid, "title": "t", "description": "d", "acceptance": list(acc), "paths": ["x"],
                "depends_on": list(deps)}
    assert specs.check_plan([t("T-002", ["T-001"]), t("T-003", ["T-002"])], Q(), 20) == []
    assert "重なって" in specs.check_plan([t("T-001")], Q(), 20)[0]
    assert "ないタスク" in specs.check_plan([t("T-002", ["T-404"])], Q(), 20)[0]
    assert "輪" in " ".join(specs.check_plan([t("T-002", ["T-003"]), t("T-003", ["T-002"])], Q(), 20))
    assert "輪" in " ".join(specs.check_plan([t("T-002", ["T-002"])], Q(), 20))
    assert "件まで" in specs.check_plan([t(f"T-{i:03d}") for i in range(2, 6)], Q(), 3)[0]
    assert "空" in specs.check_plan([t("T-002", acc=("",))], Q(), 20)[0]


# ---- ネタ帳 -------------------------------------------------------------------------
def test_extract_and_strip():
    out = "\n".join([
        "考えたこと",
        '::idea {"title": "通知", "body": "知らせる"}',
        '- ::idea {"title": "通 知"}',                                   # 同じ案（空白ちがい）は1つ
        '`::idea {"title": "グラフ", "body": "見える"}`',
        ideas.EXAMPLE,                                                   # プロンプトの見本が写っても拾わない
        '::idea {こわれた}',
        '::idea ["配列"]',
        json.dumps({"type": "result", "result": '終わり\n::idea {"title": "結果の中", "body": "b"}'}),
        '::idea {"title": "4つ目"}',
    ])
    assert [i["title"] for i in ideas.extract(out)] == ["通知", "グラフ", "4つ目"]      # 1回の出力から3件まで
    assert [i["title"] for i in ideas.extract(out.replace('::idea {"title": "4つ目"}', ""))] == ["通知", "グラフ", "結果の中"]
    assert ideas.extract("ふつうの出力") == []
    assert ideas.strip('# 仕様\n::idea {"title": "x"}\n本文\n') == "# 仕様\n本文\n"
    assert specs.spec_text_of('```markdown\n# 仕様\n\n## やること\n- a\n```\n::idea {"title": "x"}\n') == "# 仕様\n\n## やること\n- a\n"


def test_notebook(tmp_path):
    book = ideas.Notebook(tmp_path / "state.db")
    a = book.add("復習の通知", "知らせる", "implementer", "T-003")
    assert a and book.add("復習 の 通知！", "ちがう中身", "reviewer") is None      # 同じ名前は入れない
    b = book.add("グラフ")
    assert [i.title for i in book.fresh()] == ["グラフ", "復習の通知"] and book.count_new() == 2
    assert book.mark(b, "dropped") and [i.id for i in book.fresh()] == [a]
    assert book.add("グラフ") is None                                           # 捨てた案は、また拾わない
    assert book.mark(a, "adopted", "20261126-000000") and book.get(a).spec == "20261126-000000"
    assert book.count_new() == 0 and len(book.all()) == 2
    assert not book.mark(999, "dropped")
    assert book.get(a).as_request() == "復習の通知\n\n知らせる"
    with pytest.raises(ValueError):
        book.add("  ")
    # キュー（同じ state.db）と同居できる
    Queue(tmp_path / "state.db").counts()
    assert len(ideas.Notebook(tmp_path / "state.db").all()) == 2


def test_run_collects_ideas_but_does_not_build_them(project):  # noqa: F811
    repo, scenario, prompt_log = project
    tasks = write_tasks(repo, scenario, [{"id": "T-001", "scenario": "idea"}, {"id": "T-002"}])
    assert run_cli(repo, "import", str(tasks)) == 0
    assert run_cli(repo, "run", "--concurrency", "1") == 0
    cfg = load(repo / "hootl.toml")
    q = Queue(cfg.state_dir / "state.db")
    assert {t.id: t.status for t in q.all()} == {"T-001": "done", "T-002": "done"}   # 案が出てもタスクは増えない
    book = ideas.notebook(cfg)
    got = {(i.title, i.source, i.task_id) for i in book.all()}
    assert got == {("間違えた理由をメモできる", "implementer", "T-001"), ("正答率をグラフで見る", "reviewer", "T-001")}
    prompts = prompt_log.read_text()
    assert "::idea" in prompts.split("=== implementer T-001")[1] and "::idea" in prompts.split("=== reviewer T-001")[1]
    # 2件目のタスクには、すでにある案の名前が渡っている（同じ案を書かせない）
    assert "「間違えた理由をメモできる」" in prompts.split("=== implementer T-002")[1]
    # 朝のレポートに出る
    text = sorted(cfg.state_dir.glob("report-*.md"))[-1].read_text()
    assert "ネタ帳に増えた案" in text and "正答率をグラフで見る（レビュー中・T-001）" in text
    assert "ネタ帳" not in report.build(q, "2000-01-01T00:00:00+00:00")


def test_ideas_api(api):
    api, repo, scenario, _ = api
    with pytest.raises(ValueError, match="名前"):
        api.idea_add("demo", " ", "")
    first = api.idea_add("demo", "模試の結果を共有", "URL で見せる")["id"]
    with pytest.raises(ValueError, match="もうあります"):
        api.idea_add("demo", "模試の結果を共有", "")
    second = api.idea_add("demo", "暗い画面", "")["id"]
    api.idea_mark("demo", second, "dropped")
    assert [i["status"] for i in api.ideas("demo")["ideas"]] == ["dropped", "new"]
    api.idea_mark("demo", second, "new")
    with pytest.raises(LookupError):
        api.idea_mark("demo", 999, "dropped")
    with pytest.raises(LookupError):
        api.idea_mark("demo", second, "adopted")            # 画面から「作った」ことにはできない
    # 選んだ案だけが、作りたいものとして流れる（短い名前でもよい）
    r = api.idea_build("demo", second)
    assert r["job"]["kind"] == "auto"
    assert wait(api).proc.returncode == 0
    by = {i["id"]: i for i in api.ideas("demo")["ideas"]}
    assert by[second]["status"] == "adopted" and by[second]["spec"] == r["slug"] and by[first]["status"] == "new"
    with pytest.raises(LookupError):
        api.idea_build("demo", second)                      # 同じ案を二度は作らない
    assert (repo / ".hootl" / "spec-drafts" / f"{r['slug']}.idea.txt").read_text().startswith("暗い画面")
    assert {t.status for t in api.project("demo").queue().all()} == {"done"}
