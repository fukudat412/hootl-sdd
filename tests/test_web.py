"""Web 管理画面: 認証・決まった操作だけ・計画→承認→回す→止める→やり直す の通し。"""

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from hootl.web import Api, Jobs, Project, WebConfig, make_handler, stage_of
from tests.test_harness import project, write_tasks  # noqa: F401  (fixture を使う)

TOKEN = "t" * 24


@pytest.fixture
def server(project):  # noqa: F811
    repo, scenario, _ = project
    (repo / "spec.md").write_text("# spec\n")
    wc = WebConfig(token=TOKEN, projects={"demo": Project("demo", repo / "hootl.toml")})
    api = Api(wc, Jobs())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield httpd.server_address[1], repo, scenario, api, wc
    httpd.shutdown()


def call(port, method, path, body=None, cookie=True, headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json", "X-Hootl": "1"}
    if cookie:
        hdrs["Cookie"] = f"hootl_token={TOKEN}"
    hdrs.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=data, headers=hdrs)
    res = conn.getresponse()
    payload = res.read()
    ctype = res.getheader("Content-Type") or ""
    return res.status, (json.loads(payload) if "json" in ctype else payload.decode()), res


def wait_job(api, name="demo", timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = api.jobs.get(name)
        if job and not job.alive():
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_auth(server):
    port, *_ , wc = server
    assert call(port, "GET", "/api/projects", cookie=False)[0] == 401
    code, _, res = call(port, "GET", "/", cookie=False)
    assert code == 303 and res.getheader("Location") == "/login"
    code, _, _ = call(port, "POST", "/login", cookie=False, raw=b"token=wrong",
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert code == 401
    code, _, res = call(port, "POST", "/login", cookie=False, raw=f"token={TOKEN}".encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert code == 303 and "HttpOnly" in res.getheader("Set-Cookie")
    assert call(port, "GET", "/api/projects")[0] == 200
    # 別サイトからの送信（独自ヘッダなし）は弾く
    assert call(port, "POST", "/api/p/demo/plan", {}, headers={"X-Hootl": ""})[0] == 403
    # Cloudflare Access のメールを照合する設定
    wc.allowed_emails = ["me@example.com"]
    assert call(port, "GET", "/api/projects")[0] == 403
    assert call(port, "GET", "/api/projects", headers={"Cf-Access-Authenticated-User-Email": "ME@example.com"})[0] == 200
    wc.allowed_emails = []


def test_help_is_reachable_before_login(server):
    port, *_, wc = server
    # 入り方が分からない人が読むページなので、トークンなしで読める。ログイン画面と管理画面の両方から辿れる
    code, body, res = call(port, "GET", "/help", cookie=False)
    assert code == 200 and "hootl token" in body and "hootl-start.command" in body
    assert "frame-ancestors 'none'" in res.getheader("Content-Security-Policy")
    code, body, _ = call(port, "GET", "/help/how-it-works", cookie=False)
    assert code == 200 and "ネタ帳" in body
    code, body, _ = call(port, "GET", "/login", cookie=False)
    assert 'href="/help"' in body and "hootl token" in body
    assert 'href="/help"' in call(port, "GET", "/")[1]
    assert wc.token not in call(port, "GET", "/help", cookie=False)[1]
    # 門番（Cloudflare Access のメール）を置いているときは、その内側でだけ読める
    wc.allowed_emails = ["me@example.com"]
    assert call(port, "GET", "/help", cookie=False)[0] == 403
    wc.allowed_emails = []
    assert call(port, "GET", "/help/nope", cookie=False)[0] == 303          # ほかのページは、これまでどおりログインへ


def test_static_and_headers(server):
    port, *_ = server
    code, body, res = call(port, "GET", "/")
    assert code == 200 and "hootl" in body
    assert "frame-ancestors 'none'" in res.getheader("Content-Security-Policy")
    assert call(port, "GET", "/app.js")[0] == 200
    assert call(port, "GET", "/api/p/nope/tasks")[0] == 404


def test_plan_approve_run_reset(server):
    port, repo, scenario, api, _ = server
    # 1. タスク案を保存（不正な案は弾く）
    code, body, _ = call(port, "POST", "/api/p/demo/proposed", {"text": '{"id": "T-001"}'})
    assert code == 400 and "title" in body["error"]
    lines = [json.dumps({"id": i, "title": i, "description": "d", "acceptance": ["a"], "paths": ["src/*"]})
             for i in ("T-001", "T-002")]
    scenario.write_text(json.dumps({"T-001": "ok", "T-002": "never"}))
    code, body, _ = call(port, "POST", "/api/p/demo/proposed", {"text": "\n".join(lines)})
    assert code == 200 and len(body["tasks"]) == 2 and body["error"] is None
    # 2. 承認して取り込む → 案は控えに移る
    code, body, _ = call(port, "POST", "/api/p/demo/import", {})
    assert code == 200 and body["imported"] == 2
    assert not (repo / ".hootl" / "proposed-tasks.jsonl").exists()
    assert list((repo / ".hootl").glob("imported-*.jsonl"))
    # 3. 回す（別プロセス）→ 終わるまで待つ
    code, body, _ = call(port, "POST", "/api/p/demo/run", {"hours": 1, "concurrency": 1})
    assert code == 200 and body["job"]["kind"] == "run"
    assert call(port, "POST", "/api/p/demo/run", {})[0] == 400          # 二重起動はできない
    job = wait_job(api)
    assert job.proc.returncode == 0
    code, body, _ = call(port, "GET", "/api/p/demo/tasks")
    st = {t["id"]: t["status"] for t in body["tasks"]}
    assert st == {"T-001": "done", "T-002": "blocked"}
    # 4. 詳細とログ（決まったファイル名だけ・パスの細工は通さない）
    code, body, _ = call(port, "GET", "/api/p/demo/task/T-002")
    assert code == 200 and len(body["attempts"]) == 3 and "implement.log" in body["attempts"][0]["files"]
    code, text, _ = call(port, "GET", "/api/p/demo/log?task=T-002&attempt=1&file=gate-check.log")
    assert code == 200 and "bad" in text
    assert call(port, "GET", "/api/p/demo/log?task=T-002&attempt=1&file=../../state.db")[0] == 404
    assert call(port, "GET", "/api/p/demo/log?task=..&attempt=1&file=implement.log")[0] == 404
    # 5. レポート
    code, body, _ = call(port, "GET", "/api/p/demo/reports")
    assert code == 200 and "T-002" in body["reports"][0]["text"]
    # 6. やり直せるのは blocked だけ
    assert call(port, "POST", "/api/p/demo/reset", {"id": "T-001"})[0] == 400
    assert call(port, "POST", "/api/p/demo/reset", {"id": "T-002"})[0] == 200
    code, body, _ = call(port, "GET", "/api/p/demo/tasks")
    assert {t["id"]: t["status"] for t in body["tasks"]}["T-002"] == "todo"


def test_stop_finishes_current_task_only(server):
    port, repo, scenario, api, _ = server
    tasks = write_tasks(repo, scenario, [{"id": f"T-00{i}", "scenario": "slow"} for i in range(1, 5)])
    api.project("demo").queue().import_jsonl(tasks)
    call(port, "POST", "/api/p/demo/run", {"concurrency": 1})
    time.sleep(1.0)                                          # 1件目の途中
    code, body, _ = call(port, "POST", "/api/p/demo/stop", {})
    assert code == 200 and body["job"]["stop_requested"] == 1
    wait_job(api)
    st = [t.status for t in api.project("demo").queue().all()]
    assert st.count("done") == 1 and st.count("todo") == 3  # 今のタスクは終わらせ、次は取らない


def test_polling_does_not_pile_up_open_files(server):
    """画面は数秒おきに状態を取りに来る。そのたびに state.db を開きっぱなしにすると、上限（launchd では 256）で落ちる。"""
    import os
    port, repo, scenario, api, _ = server
    api.project("demo").queue().import_jsonl(write_tasks(repo, scenario, [{"id": "T-001"}]))
    api.idea_add("demo", "案", "")
    for path in ("/api/p/demo/tasks", "/api/projects"):
        assert call(port, "GET", path)[0] == 200
    before = len(os.listdir("/dev/fd"))
    for _ in range(40):
        for path in ("/api/p/demo/tasks", "/api/p/demo/task/T-001", "/api/p/demo/ideas", "/api/p/demo/proposed",
                     "/api/p/demo/specs", "/api/projects"):
            assert call(port, "GET", path)[0] == 200
    assert len(os.listdir("/dev/fd")) - before <= 6


def test_restart_does_not_lose_a_running_job(server):
    """管理画面を起動し直しても、動いている仕事は止まらず、新しい管理画面から見える・止められる。"""
    port, repo, scenario, api, wc = server
    tasks = write_tasks(repo, scenario, [{"id": f"T-00{i}", "scenario": "slow"} for i in range(1, 4)])
    api.project("demo").queue().import_jsonl(tasks)
    api.run("demo", None, 1)
    time.sleep(1.0)
    again = Api(wc, Jobs())                                   # 起動し直した管理画面（前の仕事を知らない）
    job = again.tasks("demo")["summary"]["job"]
    assert job["alive"] and job["kind"] == "run" and job["adopted"]
    with pytest.raises(ValueError, match="動いています|実行中"):  # 二重には回さない
        again.run("demo", None, 1)
    assert again.stop("demo")["job"]["stop_requested"] == 1    # 止められる（今のタスクは終わらせる）
    end = time.time() + 60
    while time.time() < end and (api.jobs.get("demo").alive() or again.jobs.get("demo").alive()):
        time.sleep(0.2)
    info = again.tasks("demo")["summary"]["job"]
    assert not info["alive"] and info["code"] is None
    st = [t.status for t in again.project("demo").queue().all()]
    assert st.count("done") == 1 and st.count("todo") == 2
    # 終わった仕事の控えは、次の起動では引き取らない
    assert Api(wc, Jobs()).tasks("demo")["summary"]["job"] is None


def test_job_record_is_not_trusted(server, tmp_path):
    """仕事の控えは .hootl の中にあり、エージェントが書き換えられる。書かれた場所のファイルを読んだり、よその pid を引き取ったりしない。"""
    import os
    port, repo, scenario, api, wc = server
    tasks = write_tasks(repo, scenario, [{"id": "T-001", "scenario": "slow"}])
    api.project("demo").queue().import_jsonl(tasks)
    api.run("demo", None, 1)
    record = repo / ".hootl" / "web-job.json"
    rec = json.loads(record.read_text())
    assert "log" not in rec
    secret = tmp_path / "secret.txt"
    secret.write_text("ひみつの中身")
    # ログの場所を書き足しても、読むのは決まった場所（.hootl/web-run.log）だけ
    record.write_text(json.dumps({**rec, "log": str(secret)}))
    job = Api(wc, Jobs()).tasks("demo")["summary"]["job"]
    assert job["alive"] and job["adopted"] and "ひみつ" not in job["tail"]
    # 知らない種類・よその pid（このテスト自身）・種類の食い違いは引き取らず、控えを消す
    for bad in ({**rec, "kind": "../../etc"}, {**rec, "pid": os.getpid()}, {**rec, "kind": "auto"}, {**rec, "pid": 1}):
        record.write_text(json.dumps(bad))
        assert Api(wc, Jobs()).tasks("demo")["summary"]["job"] is None and not record.exists()
    wait_job(api)


def test_stage_of(tmp_path):
    d = tmp_path / "1"
    assert stage_of(d) == "setup"
    d.mkdir()
    (d / "setup-install.log").write_text("")     # 準備のログはゲートと数えない
    assert stage_of(d) == "setup"
    (d / "implement.prompt.md").write_text("")
    assert stage_of(d) == "implement"
    (d / "diff.patch").write_text("")
    assert stage_of(d) == "guard"
    (d / "gate-lint.log").write_text("")
    assert stage_of(d) == "gate"
    (d / "review.prompt.md").write_text("")
    assert stage_of(d) == "review"
    (d / "review.json").write_text("{}")
    assert stage_of(d) == "merge"
