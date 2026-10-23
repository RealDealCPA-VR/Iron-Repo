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
from iron.errors import IronError


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
    """(status, decoded JSON or text, response headers). No reply ever sets a cookie."""
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
    assert hdrs.get_all("Set-Cookie") is None, "the dashboard must never set a cookie"
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

    # only this repository's own remotes, by name: never a path the page made up
    for remote in ("nowhere-at-all", loc(env / "backup.iron"), str(env / "backup.iron"),
                   "\\\\attacker.example\\share\\x.iron", "//attacker.example/share/x.iron", ["origin"]):
        code, data, _ = call(dash, "/api/verify", {"remote": remote})
        assert code == 400 and "Traceback" not in data["error"], (remote, data)
    assert "not one of this repository's backups" in call(dash, "/api/verify", {"remote": "nowhere-at-all"})[1]["error"]
    assert wait(dash, ok(dash, "/api/verify", {}))["state"] == "done"      # no name: the default remote


def test_agent_sessions_come_from_trailers(dash, env):
    iron(dash.project, "checkpoint", "-m", "before task", "--agent", "Claude", "--session", "t-42")
    write(dash.project / "src" / "app.py", "print('agent')\n")
    git(dash.project, "commit", "-qam", "Agent work\n\nIron-Agent: Claude\nIron-Session: t-42")
    sessions = ok(dash, "/api/agents")["sessions"]
    assert len(sessions) == 1
    s = sessions[0]
    assert (s["agent"], s["session"], s["versions"], s["checkpoints"], s["latest"]) == ("Claude", "t-42", 1, 1, "Agent work")
    assert ok(dash, "/api/history?n=1")["history"][0]["agent"] == "Claude"


def test_page_keeps_the_token_in_memory_never_in_a_cookie(dash):
    code, page, headers = call(dash, "/?token=" + dash.token, token=False)
    assert code == 200 and "Sync now" in page and "Save version" in page
    assert dash.token not in page                          # the page itself carries no secret
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "http" not in csp
    assert headers["X-Frame-Options"] == "DENY"
    assert "<script src" not in page and "<link rel=\"stylesheet\"" not in page  # nothing from a CDN
    # the script reads ?token=, drops it from the address bar, and sends it as a header
    assert "history.replaceState" in page and "sessionStorage" in page and '"X-Iron-Token": TOKEN' in page
    assert "document.cookie" not in page
    # a reload (no token in the URL) gets the same empty shell; the script finds the token in sessionStorage
    code, shell, _ = call(dash, "/", token=False)
    assert code == 200 and dash.token not in shell and "open the link" in shell.lower()
    # an old or wrong link says so
    code, text, _ = call(dash, "/?token=wrong", token=False)
    assert code == 403 and "iron ui" in text


def test_every_other_path_needs_the_header(dash):
    """Same host and port, no X-Iron-Token: refused, whatever the path and whatever cookie comes along."""
    cookie = {"Cookie": "iron_ui_%d=%s; token=%s" % (dash.port, dash.token, dash.token)}
    for path in ("/api/status", "/api/changes", "/api/history", "/api/diff", "/api/storage", "/api/agents",
                 "/api/checkpoints", "/api/jobs/abc", "/api/nope", "/favicon.ico", "/index.html",
                 "/api/status?token=" + dash.token):
        assert call(dash, path, token=False)[0] == 403, path
        assert call(dash, path, token=False, headers=cookie)[0] == 403, path
    assert call(dash, "/api/status")[0] == 200


def test_conflicts_kept_out_and_refusals_reach_the_page(dash, monkeypatch):
    status = ok(dash, "/api/status")
    assert status["conflicts"] == [] and status["kept_out"] == []
    # a real merge conflict shows up even before Repository.status() reports "conflicts"
    git(dash.project, "switch", "-qc", "other")
    commit(dash.project, "README.md", "theirs\n", "their edit")
    git(dash.project, "switch", "-q", "main")
    commit(dash.project, "README.md", "ours\n", "our edit")
    git(dash.project, "merge", "-q", "other", check=False)
    assert ok(dash, "/api/status")["conflicts"] == ["README.md"]
    git(dash.project, "merge", "--abort")

    real = dash.repo.status
    monkeypatch.setattr(dash.repo, "status", lambda: dict(real(), conflicts=["src/app.py", {"path": "b.txt"}],
                                                          kept_out=[".env"]))
    status = ok(dash, "/api/status")
    assert status["conflicts"] == ["src/app.py", "b.txt"] and status["kept_out"] == [".env"]
    page = call(dash, "/", token=False)[1]
    assert "These files conflict between your computers" in page and "look like secrets" in page

    def refuse(message):
        raise IronError("these files conflict between your computers: src/app.py")
    monkeypatch.setattr(dash.repo, "commit", refuse)
    write(dash.project / "src" / "app.py", "print('v2')\n")
    code, data, _ = call(dash, "/api/commit", {"message": "try"})
    assert code == 400 and data["error"] == "these files conflict between your computers: src/app.py"
    assert "The version was not saved: " in page                       # shown as it came from the API


def test_history_marks_autosaved_versions(dash):
    write(dash.project / "src" / "app.py", "print('auto')\n")
    git(dash.project, "commit", "-qam", "Autosave 12:00\n\nIron-Autosave: true")
    history = ok(dash, "/api/history")["history"]
    assert [(h["subject"], h["autosave"]) for h in history] == [
        ("Autosave 12:00", True), ("add src/app.py", False), ("add README.md", False)]
    assert "autosaved" in call(dash, "/", token=False)[1]


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
    assert call(dash, "/api/commit", {"message": "evil"}, token=False)[0] == 403
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
