"""iron ui: the local dashboard's JSON API and its defences."""

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import commit, git, iron, loc, make_repo, write
from iron import ui


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


@pytest.fixture
def dash(env):
    repo = make_repo(env / "project", {"README.md": "hello\n", "src/app.py": "print('v1')\n"})
    d = ui.Dashboard(repo)
    server = ui.make_server(d)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    d.port, d.base, d.project = server.server_port, "http://127.0.0.1:%d" % server.server_port, repo
    yield d
    server.shutdown()
    server.server_close()


def call(d, path, body=None, token=True, headers=None):
    """(status, decoded JSON or text, response headers)."""
    h = {}
    if token:
        h["X-Iron-Token"] = d.token if token is True else token
    if body is not None:
        h.update({"Content-Type": "application/json", "Origin": d.base})
    h.update(headers or {})
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(d.base + path, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        resp = OPENER.open(req, timeout=60)
        code, raw, hdrs = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        code, raw, hdrs = e.code, e.read(), e.headers
    text = raw.decode("utf-8")
    try:
        return code, json.loads(text), hdrs
    except ValueError:
        return code, text, hdrs


def ok(d, path, body=None):
    code, data, _ = call(d, path, body)
    assert code == 200, data
    return data


def wait(d, job):
    deadline = time.time() + 120
    while job["state"] == "running":
        assert time.time() < deadline, "job never finished"
        time.sleep(0.1)
        job = ok(d, "/api/jobs/" + job["id"])
    return job


def test_status_history_changes_and_commit(dash):
    status = ok(dash, "/api/status")
    assert status["name"] == "project" and status["branch"] == "main" and status["changes"] == 0
    assert status["remotes"] == [] and status["latest"]["subject"] == "add src/app.py"

    write(dash.project / "src" / "app.py", "print('v2')\n")
    write(dash.project / "notes" / "new.txt", "brand new\n")
    changes = ok(dash, "/api/changes")["changes"]
    assert {(c["path"], c["kind"]) for c in changes} == {("src/app.py", "changed"), ("notes/new.txt", "new")}
    diff = ok(dash, "/api/diff?path=src/app.py")["diff"]
    assert "-print('v1')" in diff and "+print('v2')" in diff and "new.txt" not in diff
    assert "+brand new" in ok(dash, "/api/diff")["diff"]

    code, data, _ = call(dash, "/api/commit", {"message": "  "})
    assert code == 400 and "describe" in data["error"]
    sha = ok(dash, "/api/commit", {"message": "Second version"})["sha"]
    assert git(dash.project, "rev-parse", "HEAD").stdout.strip() == sha
    assert ok(dash, "/api/changes")["changes"] == []
    code, data, _ = call(dash, "/api/commit", {"message": "again"})
    assert code == 400 and "nothing to save" in data["error"]
    history = ok(dash, "/api/history?n=2")["history"]
    assert [h["subject"] for h in history] == ["Second version", "add src/app.py"]


def test_checkpoint_and_restore_report_the_safety_checkpoint(dash):
    first = ok(dash, "/api/history")["history"][-1]["sha"]
    cp = ok(dash, "/api/checkpoint", {"message": "before the agent"})["checkpoint"]
    assert ok(dash, "/api/checkpoints")["checkpoints"][0]["id"] == cp["id"]

    write(dash.project / "src" / "app.py", "broken\n")
    write(dash.project / "junk.txt", "junk\n")
    result = ok(dash, "/api/restore", {"checkpoint": cp["id"]})
    assert (dash.project / "src" / "app.py").read_text() == "print('v1')\n"
    assert not (dash.project / "junk.txt").exists()
    safety = result["safety_checkpoint"]
    assert safety in [c["id"] for c in ok(dash, "/api/checkpoints")["checkpoints"]]

    # restoring an old version from History; the branch does not move
    head = git(dash.project, "rev-parse", "HEAD").stdout.strip()
    result = ok(dash, "/api/restore", {"version": first})
    assert not (dash.project / "src" / "app.py").exists() and result["safety_checkpoint"]
    assert git(dash.project, "rev-parse", "HEAD").stdout.strip() == head
    # and undo with the safety checkpoint
    ok(dash, "/api/restore", {"checkpoint": result["safety_checkpoint"]})
    assert (dash.project / "src" / "app.py").read_text() == "print('v1')\n"

    for body in ({"version": "--output=x"}, {"checkpoint": "../../etc"}, {"version": "abcdef1234"}, {}):
        code, data, _ = call(dash, "/api/restore", body)
        assert code == 400 and "error" in data and "Traceback" not in data["error"]


def test_sync_and_verify_run_in_the_background(dash, env):
    iron(dash.project, "remote", "add", "origin", loc(env / "backup.iron"))
    status = ok(dash, "/api/status")
    assert [(r["remote"], r["state"]) for r in status["remotes"]] == [("origin", "unpublished")]
    storage = ok(dash, "/api/storage")["storage"]
    assert storage[0]["healthy"] and storage[0]["encrypted"] is False

    job = ok(dash, "/api/sync", {})
    assert job["state"] in ("running", "done")
    job = wait(dash, job)
    assert job["state"] == "done", job
    assert ok(dash, "/api/status")["remotes"][0]["state"] == "synced"

    commit(dash.project, "more.txt", "more\n", "Third version")
    assert ok(dash, "/api/status")["remotes"][0]["ahead"] == 1
    assert wait(dash, ok(dash, "/api/sync", {}))["state"] == "done"

    report = wait(dash, ok(dash, "/api/verify", {"remote": "origin"}))
    assert report["state"] == "done" and report["result"]["ok"] and report["result"]["commits"] is None
    report = wait(dash, ok(dash, "/api/verify", {"remote": "origin", "deep": True}))
    assert report["result"]["ok"] and report["result"]["commits"] == 3

    failed = wait(dash, ok(dash, "/api/verify", {"remote": "nowhere-at-all"}))
    assert failed["state"] == "failed" and "Traceback" not in failed["error"]


def test_agent_sessions_come_from_trailers(dash, env):
    iron(dash.project, "checkpoint", "-m", "before task", "--agent", "Claude", "--session", "t-42")
    write(dash.project / "src" / "app.py", "print('agent')\n")
    git(dash.project, "commit", "-qam", "Agent work\n\nIron-Agent: Claude\nIron-Session: t-42")
    sessions = ok(dash, "/api/agents")["sessions"]
    assert len(sessions) == 1
    s = sessions[0]
    assert (s["agent"], s["session"], s["versions"], s["checkpoints"], s["latest"]) == ("Claude", "t-42", 1, 1, "Agent work")
    assert ok(dash, "/api/history?n=1")["history"][0]["agent"] == "Claude"


def test_page_needs_the_token_and_trades_it_for_a_strict_cookie(dash):
    code, text, _ = call(dash, "/", token=False)
    assert code == 403 and "iron ui" in text
    code, _, headers = call(dash, "/?token=wrong", token=False)
    assert code == 403
    code, _, headers = call(dash, "/?token=" + dash.token, token=False)
    assert code == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and dash.token in cookie
    code, page, headers = call(dash, "/", token=False, headers={"Cookie": cookie.split(";")[0]})
    assert code == 200 and "Sync now" in page and "Save version" in page
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "http" not in csp
    assert "<script src" not in page and "<link rel=\"stylesheet\"" not in page  # nothing from a CDN
    # the cookie alone opens the read-only API too
    code, data, _ = call(dash, "/api/status", token=False, headers={"Cookie": cookie.split(";")[0]})
    assert code == 200 and data["name"] == "project"


def test_requests_without_token_or_from_elsewhere_are_refused(dash):
    assert call(dash, "/api/status", token=False)[0] == 403
    assert call(dash, "/api/history", token="not-the-token")[0] == 403
    # DNS rebinding: a foreign Host header is refused even with the right token
    assert call(dash, "/api/status", headers={"Host": "evil.example:%d" % dash.port})[0] == 403
    assert call(dash, "/?token=" + dash.token, token=False, headers={"Host": "evil.example"})[0] == 403
    assert call(dash, "/api/status", headers={"Host": "localhost:%d" % dash.port})[0] == 200

    head = git(dash.project, "rev-parse", "HEAD").stdout.strip()
    write(dash.project / "x.txt", "x\n")
    # cross-site POSTs, a POST without the header token, and one without an Origin
    assert call(dash, "/api/commit", {"message": "evil"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert call(dash, "/api/commit", {"message": "evil"}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    cookie = "%s=%s" % ("iron_ui_%d" % dash.port, dash.token)
    assert call(dash, "/api/commit", {"message": "evil"}, token=False, headers={"Cookie": cookie})[0] == 403
    req = urllib.request.Request(dash.base + "/api/commit", data=b'{"message": "evil"}', method="POST",
                                 headers={"X-Iron-Token": dash.token, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        OPENER.open(req, timeout=30)
    assert e.value.code == 403
    # a form post (the classic CSRF shape) is refused too
    assert call(dash, "/api/commit", {"message": "evil"}, headers={"Content-Type": "text/plain"})[0] == 415
    assert git(dash.project, "rev-parse", "HEAD").stdout.strip() == head


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_is_valid_javascript(tmp_path):
    script = ui.PAGE.split('<script nonce="__NONCE__">')[1].split("</script>")[0]
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(tmp_path / "page.js")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_cli_registers_ui():
    from iron.cli import USAGE, build_parsers
    a = build_parsers()["ui"].parse_args(["--port", "0", "--no-browser"])
    assert a.port == 0 and a.no_browser and "iron ui" in USAGE
