"""``iron ui``: a local dashboard in the browser (PRD section 17, Phase 8).

Standard library only: an http.server bound to 127.0.0.1 serves one
self-contained page (no CDN, works offline) plus a small JSON API on top of
iron.api.Repository, iron.checkpoint and iron.maintenance.

Security, because any web page the user visits can try to talk to localhost:

* a random per-launch token arrives in the URL the terminal prints. The page
  script keeps it in memory (and in sessionStorage, which is per origin, so per
  port, and per tab) and removes it from the address bar. There is no cookie:
  cookies ignore the port, so a cookie would also be sent to every other
  server on 127.0.0.1;
* every API request (GET and POST) must carry the token in the X-Iron-Token
  header, which a page from another origin cannot add without a CORS
  preflight that this server never answers;
* the page itself holds no secret and no data, so it is served without the
  token (a reload needs that); everything it shows comes from the API;
* the Host header must be 127.0.0.1 or localhost on our port, which defeats
  DNS rebinding;
* every change (POST) also needs a same-origin Origin (CSRF);
* a strict Content-Security-Policy with a per-response nonce, no framing.
"""

import hmac
import json
import re
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import gitutil
from .api import Repository
from .cli import iron_remotes
from .errors import IronError

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_BODY = 1 << 20
MAX_DIFF = 400000  # characters shown in the diff viewer
JOB_KEEP = 50
VERSION_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
CHECKPOINT_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]+$")


def friendly(exc):
    """Text for the page: IronError messages as they are, anything else without a traceback."""
    if isinstance(exc, IronError):
        return str(exc)
    return "Something unexpected went wrong (%s: %s)" % (type(exc).__name__, exc)


def paths_of(items):
    """File paths from a list of paths or of {"path": ...} dicts, without repeats."""
    out = []
    for item in items or []:
        path = item.get("path") if isinstance(item, dict) else item
        if path and str(path) not in out:
            out.append(str(path))
    return out


def clean_output(text):
    return "\n".join(l[len("iron: "):] if l.startswith("iron: ") else l for l in text.splitlines()).strip()


class Dashboard:
    """What the page can see and do, as plain dicts. One change runs at a time."""

    def __init__(self, path=".", token=None):
        self.repo = Repository(path)
        self.root = self.repo.path
        self.token = token or secrets.token_urlsafe(32)
        self.jobs = {}
        self._jobs_lock = threading.Lock()
        self._busy = threading.Lock()
        self.busy_with = None

    # -- reading ------------------------------------------------------------

    def status(self):
        latest = self.repo.history(1)
        changes = self.repo.changes()
        st = self.repo.status()
        conflicts = paths_of(st.get("conflicts", []))
        conflicts += [c["path"] for c in changes if c["kind"] == "conflict" and c["path"] not in conflicts]
        return {"name": self.root.name, "path": str(self.root), "branch": gitutil.current_branch(self.root),
                "remotes": self.repo.sync_state(), "changes": len(changes),
                "checkpoints": len(self.repo.checkpoints()), "latest": latest[0] if latest else None,
                "conflicts": conflicts, "kept_out": paths_of(st.get("kept_out", [])), "busy": self.busy_with}

    def changes(self):
        return {"changes": self.repo.changes(), "has_versions": bool(gitutil.head_sha(self.root))}

    def diff(self, path=None):
        text = self.repo.diff(paths=[path] if path else [])
        truncated = len(text) > MAX_DIFF
        return {"path": path, "diff": text[:MAX_DIFF], "truncated": truncated}

    def history(self, n=50):
        try:
            n = max(1, min(int(n), 1000))
        except (TypeError, ValueError):
            raise IronError("n must be a number") from None
        items = self.repo.history(n)
        autosaved = self._autosaved(n) if items else set()
        for item in items:
            item["autosave"] = item["sha"] in autosaved
        return {"history": items}

    def _autosaved(self, n):
        """Short ids of the last ``n`` versions `iron autosave` made (trailer ``Iron-Autosave: true``)."""
        out = gitutil.git("log", "-n", str(n),
                          "--format=%h%x00%(trailers:key=Iron-Autosave,valueonly,separator=%x2C)%x1e", cwd=self.root)
        found = set()
        for chunk in out.split("\x1e"):
            sha, _, value = chunk.strip("\n").partition("\x00")
            if sha and "true" in [v.strip().lower() for v in value.split(",")]:
                found.add(sha)
        return found

    def checkpoints(self):
        return {"checkpoints": self.repo.checkpoints()}

    def storage(self):
        return {"storage": self.repo.storage()}

    def agents(self):
        return {"sessions": self.repo.agent_sessions()}

    def job(self, job_id):
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise IronError("that task is unknown (the dashboard may have been restarted)")
            return dict(job)

    # -- changing -----------------------------------------------------------

    def _claim(self, what):
        if not self._busy.acquire(blocking=False):
            raise IronError("please wait: %s is still running" % (self.busy_with or "another action"))
        self.busy_with = what

    def _release(self):
        self.busy_with = None
        self._busy.release()

    def _exclusive(self, what, func):
        self._claim(what)
        try:
            return func()
        finally:
            self._release()

    def commit(self, message):
        message = (message or "").strip()
        if not message:
            raise IronError("describe what changed before saving a version")

        def work():
            result = self.repo.commit(message)
            extra = {}
            if isinstance(result, dict):  # tolerate a richer result from Repository.commit
                if result.get("error"):
                    raise IronError(str(result["error"]))
                extra = {"kept_out": paths_of(result.get("kept_out", []))}
                result = result.get("sha")
            if not result:
                raise IronError("there is nothing to save: no changes since the last version")
            return dict(extra, sha=result, short=result[:10])
        return self._exclusive("saving a version", work)

    def checkpoint(self, message=""):
        return self._exclusive("a checkpoint", lambda: {"checkpoint": self.repo.checkpoint((message or "").strip())})

    def restore(self, version=None, checkpoint_id=None):
        if bool(version) == bool(checkpoint_id):
            raise IronError("say which version or checkpoint to restore")
        if version and not VERSION_RE.match(str(version)):
            raise IronError("there is no version %r" % version)
        if checkpoint_id and not CHECKPOINT_RE.match(str(checkpoint_id)):
            raise IronError("there is no checkpoint %r" % checkpoint_id)
        if version:
            proc = gitutil.run_git(["rev-parse", "--verify", "--quiet", "%s^{commit}" % version],
                                   cwd=self.root, check=False)
            target = proc.stdout.decode().strip()
            if proc.returncode or not target:
                raise IronError("there is no version %r" % version)
        else:
            target = checkpoint_id
        return self._exclusive("a restore", lambda: self.repo.restore(target))

    def _start(self, kind, label, func, exclusive):
        with self._jobs_lock:
            for job in self.jobs.values():
                if job["kind"] == kind and job["state"] == "running":
                    return dict(job)
        if exclusive:
            self._claim(label)
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "label": label, "state": "running",
               "started": time.time(), "result": None, "error": None}

        def work():
            try:
                result = func()
                with self._jobs_lock:
                    job.update(state="done", result=result)
            except Exception as e:  # noqa: BLE001 - shown in the page, never as a traceback
                with self._jobs_lock:
                    job.update(state="failed", error=friendly(e))
            finally:
                if exclusive:
                    self._release()
        with self._jobs_lock:
            self.jobs[job["id"]] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["started"])[:-JOB_KEEP]:
                if old["state"] != "running":
                    del self.jobs[old["id"]]
        threading.Thread(target=work, name="iron-ui-" + kind, daemon=True).start()
        return dict(job)

    def start_sync(self, remote=None):
        def work():
            result = self.repo.sync(remote or None)
            output = clean_output(result["output"])
            if not result["ok"]:
                raise IronError(output or "sync failed")
            return {"output": output}
        return self._start("sync", "a sync", work, exclusive=True)

    def start_verify(self, remote=None, deep=False):
        # Only this repository's own Iron remotes: a free-form location would make the
        # server open any path it is given (on Windows a UNC path starts an SMB connection).
        if remote is not None and not isinstance(remote, str):
            raise IronError("say which backup to check by its name")
        remote = (remote or "").strip() or None
        if remote is not None and remote not in iron_remotes(self.root):
            raise IronError("%r is not one of this repository's backups; choose one listed under Storage" % remote)
        kind = "verify-deep" if deep else "verify"
        return self._start("%s:%s" % (kind, remote or ""), "a check", lambda: dict(
            self.repo.verify(remote or None, deep=bool(deep)), remote=remote, deep=bool(deep)), exclusive=False)


# -- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "IronUI"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- checks -------------------------------------------------------------

    @property
    def dash(self):
        return self.server.dash

    def _origins(self):
        port = self.server.server_port
        return {"http://%s:%d" % (h, port) for h in ALLOWED_HOSTS}

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        name, _, port = host.rpartition(":")
        return name in ALLOWED_HOSTS and port == str(self.server.server_port)

    def _origin_ok(self, required):
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return False
        if origin is None:
            return not required
        return origin in self._origins()

    def _match(self, value):
        return bool(value) and hmac.compare_digest(value.encode("utf-8", "replace"), self.dash.token.encode())

    # -- replies ------------------------------------------------------------

    def _send(self, code, body, ctype, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _refuse(self, why):
        self._drain()
        self._send(403, ("Forbidden: %s\n" % why).encode(), "text/plain; charset=utf-8")

    def _drain(self):
        """Read and drop a refused request's body, so closing does not reset the connection."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)
        self.close_connection = True

    def _page(self):
        nonce = secrets.token_urlsafe(16)
        csp = ("default-src 'none'; script-src 'nonce-{0}'; style-src 'nonce-{0}'; connect-src 'self'; "
               "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'").format(nonce)
        body = PAGE.replace("__NONCE__", nonce)
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8", [("Content-Security-Policy", csp)])

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._refuse("unknown host")
        if not self._origin_ok(required=False):
            return self._refuse("cross-site request")
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            # The page is a static shell with no data and no secret: the script takes the
            # token from ?token=, or from sessionStorage after a reload. A wrong token is
            # refused here, so an old link says so at once.
            if "token" in query and not self._match(query["token"]):
                return self._refuse("this link is out of date; open the link that `iron ui` printed in the terminal")
            return self._page()
        if not self._match(self.headers.get("X-Iron-Token")):
            return self._refuse("missing or wrong token")
        d = self.dash
        routes = {"/api/status": d.status, "/api/changes": d.changes, "/api/checkpoints": d.checkpoints,
                  "/api/storage": d.storage, "/api/agents": d.agents,
                  "/api/history": lambda: d.history(query.get("n", 50)),
                  "/api/diff": lambda: d.diff(query.get("path") or None)}
        if url.path.startswith("/api/jobs/"):
            return self._call(lambda: d.job(url.path[len("/api/jobs/"):]))
        if url.path not in routes:
            return self._json(404, {"error": "not found"})
        return self._call(routes[url.path])

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._refuse("unknown host")
        if not self._origin_ok(required=True):
            return self._refuse("cross-site request")
        if not self._match(self.headers.get("X-Iron-Token")):
            return self._refuse("missing or wrong token")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._json(413, {"error": "request too large"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if length and ctype != "application/json":
            return self._json(415, {"error": "send JSON"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "that request is not valid JSON"})
        d = self.dash
        routes = {"/api/commit": lambda: d.commit(body.get("message")),
                  "/api/checkpoint": lambda: d.checkpoint(body.get("message")),
                  "/api/restore": lambda: d.restore(body.get("version"), body.get("checkpoint")),
                  "/api/sync": lambda: d.start_sync(body.get("remote")),
                  "/api/verify": lambda: d.start_verify(body.get("remote"), body.get("deep"))}
        path = urlsplit(self.path).path
        if path not in routes:
            return self._json(404, {"error": "not found"})
        return self._call(routes[path])

    def _call(self, func):
        try:
            return self._json(200, func())
        except IronError as e:
            return self._json(400, {"error": friendly(e)})
        except Exception as e:  # noqa: BLE001 - the page gets a sentence, never a traceback
            print("iron ui: %s" % friendly(e), file=sys.stderr, flush=True)
            return self._json(500, {"error": friendly(e)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(dash, port=0):
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as e:
        raise IronError("cannot listen on 127.0.0.1:%d (%s); try another --port, or leave it out" % (port, e)) from None
    server.dash = dash
    return server


def serve(path=".", port=0, open_browser=True, out=None):
    out = out or (lambda msg: print(msg, flush=True))
    dash = Dashboard(path)
    server = make_server(dash, port)
    url = "http://127.0.0.1:%d/?token=%s" % (server.server_port, dash.token)
    out("Iron dashboard for %s" % dash.root.name)
    out("  %s" % url)
    out("  Only this computer can open it, and only with that link, until you stop it. Press Ctrl+C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - no browser available is fine, the link is printed
            pass
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        out("Stopped.")
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Iron Repository</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%23333'/%3E%3Cpath d='M4 4h8v2H9v6H7V6H4z' fill='%23fff'/%3E%3C/svg%3E">
<style nonce="__NONCE__">
:root{--bg:#f6f7f9;--panel:#fff;--text:#1b1f24;--muted:#5f6b7a;--line:#dde1e6;--accent:#1f6feb;--accent-text:#fff;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--add-bg:#e6ffec;--add:#116329;--del-bg:#ffebe9;--del:#82071e;--hunk:#eef3fb;
--chip:#eef1f4;--warn-bg:#fff8c5;--shadow:0 1px 2px rgba(0,0,0,.06),0 2px 8px rgba(0,0,0,.04)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#171b21;--text:#e6e9ed;--muted:#97a2b0;--line:#2a3039;
--accent:#4c8dff;--accent-text:#fff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--add-bg:#12261a;--add:#7ee2a0;
--del-bg:#2d1416;--del:#ffa198;--hunk:#18233a;--chip:#222831;--warn-bg:#2b2111;--shadow:none}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,textarea{font:inherit;color:inherit}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header.top{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow);
display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.brand{font-size:11px;letter-spacing:.14em;color:var(--muted);font-weight:600}
h1{font-size:24px;margin:2px 0 4px;word-break:break-word}
.sub{color:var(--muted);font-size:13px}
.states{display:flex;flex-direction:column;gap:4px;margin-top:8px}
.state{font-size:14px}.state .where{color:var(--muted);font-size:12px;word-break:break-all}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.btn{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:7px 14px;cursor:pointer}
.btn:hover{border-color:var(--accent)}.btn:disabled{opacity:.5;cursor:default}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-text)}
.btn.danger{background:var(--bad);border-color:var(--bad);color:#fff}
.btn.big{font-size:17px;padding:12px 26px;border-radius:10px;font-weight:600}
nav.tabs{display:flex;gap:4px;margin:16px 0 12px;overflow-x:auto;-webkit-overflow-scrolling:touch}
nav.tabs button{border:0;background:none;padding:9px 14px;border-radius:8px;cursor:pointer;color:var(--muted);white-space:nowrap;font-weight:500}
nav.tabs button[aria-selected=true]{background:var(--panel);color:var(--text);box-shadow:var(--shadow);border:1px solid var(--line)}
.count{display:inline-block;min-width:20px;padding:0 6px;margin-left:6px;border-radius:10px;background:var(--chip);font-size:12px;text-align:center}
section.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow)}
section.panel[hidden]{display:none}
h2{font-size:17px;margin:0 0 4px}.hint{color:var(--muted);font-size:13px;margin:0 0 14px}
.notice{border-radius:10px;padding:10px 14px;margin:12px 0 0;border:1px solid var(--line);background:var(--panel);display:flex;gap:10px;align-items:flex-start;justify-content:space-between}
.notice.ok{border-color:var(--ok)}.notice.bad{border-color:var(--bad)}.notice .msg{color:var(--text);white-space:pre-wrap;word-break:break-word}
.notice .x{border:0;background:none;cursor:pointer;color:var(--muted);font-size:18px;line-height:1}
.grid{display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:14px}
.files{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;align-self:start}
.files li button{display:flex;gap:8px;width:100%;text-align:left;border:0;border-bottom:1px solid var(--line);background:none;padding:8px 10px;cursor:pointer;align-items:baseline}
.files li:last-child button{border-bottom:0}
.files li button[aria-current=true]{background:var(--hunk)}
.files .p{word-break:break-all;font-size:13px}
.kind{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--chip);white-space:nowrap}
.kind.new{color:var(--ok)}.kind.deleted{color:var(--bad)}.kind.conflict{color:var(--bad);font-weight:700}.kind.changed,.kind.renamed{color:var(--warn)}
.diff{border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:65vh;font:12.5px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;min-width:0}
.diff .l{white-space:pre;padding:0 10px;min-height:1.5em}
.diff .a{background:var(--add-bg);color:var(--add)}.diff .d{background:var(--del-bg);color:var(--del)}
.diff .h{background:var(--hunk);color:var(--muted);padding:3px 10px;font-family:system-ui,sans-serif;font-size:12px}
.diff .f{background:var(--chip);font-weight:600;padding:6px 10px;font-family:system-ui,sans-serif;position:sticky;top:0}
.diff .empty{padding:14px;color:var(--muted);font-family:system-ui,sans-serif}
.save{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.save input{flex:1 1 260px;border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:9px 12px;min-width:0}
.list{list-style:none;margin:0;padding:0}
.list>li{display:flex;gap:12px;justify-content:space-between;align-items:center;padding:11px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.list>li:last-child{border-bottom:0}
.list .main{min-width:0;flex:1 1 300px}.list .title{font-weight:500;word-break:break-word}
.meta{color:var(--muted);font-size:12.5px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:2px}
.who{font-size:11.5px;padding:1px 8px;border-radius:9px;background:var(--chip)}.who.agent{background:var(--hunk);color:var(--accent);font-weight:600}
code,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.card{border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.card h3{margin:0 0 4px;font-size:15px}.card .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.report{margin:10px 0 0;padding:0;list-style:none;font-size:13.5px}.report li{padding:2px 0;word-break:break-word}
.key{margin-top:10px;padding:10px 12px;border-radius:8px;background:var(--hunk);font-size:13.5px}
.empty-state{color:var(--muted);padding:18px 0;text-align:center}
.spin{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:s .8s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes s{to{transform:rotate(360deg)}}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:16px;z-index:10}
.modal[hidden]{display:none}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;max-width:460px;width:100%;padding:18px}
.modal .box h3{margin:0 0 8px}.modal .box p{margin:0 0 16px;color:var(--muted);white-space:pre-wrap}
.modal .row{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
pre.out{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:12px;color:var(--muted)}
.more{margin-top:10px}
.banner{border-radius:12px;padding:14px 18px;margin:0 0 12px;border:2px solid var(--line);word-break:break-word}
.banner[hidden]{display:none}
.banner.bad{border-color:var(--bad);background:var(--del-bg)}.banner.warn{border-color:var(--warn);background:var(--warn-bg)}
.banner h2{margin:0 0 6px}.banner.bad h2{color:var(--bad)}.banner.warn h2{color:var(--warn)}
.banner ul{margin:6px 0 8px;padding-left:20px}.banner p{margin:6px 0 0}
.badge{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--chip);color:var(--muted)}
@media (max-width:720px){.grid{grid-template-columns:1fr}.wrap{padding:10px}header.top{padding:14px}.btn.big{width:100%}
.diff{max-height:none}h1{font-size:21px}.list>li .btn{width:100%}}
</style>
</head>
<body>
<div class="wrap">
  <div class="banner bad" id="gate" role="alert" hidden><h2>Open the dashboard link again</h2>
    <p>This page needs the link that <code>iron ui</code> printed in the terminal. Copy it from there, or start the dashboard again with <code>iron ui</code>.</p></div>
  <div class="banner bad" id="conflicts" role="alert" hidden></div>
  <div class="banner warn" id="kept-out" role="status" hidden></div>
  <header class="top">
    <div>
      <div class="brand">IRON REPOSITORY</div>
      <h1 id="name">Loading...</h1>
      <div class="sub" id="branch"></div>
      <div class="states" id="states"></div>
    </div>
    <button class="btn primary big" id="sync">Sync now</button>
  </header>
  <div id="notices"></div>
  <nav class="tabs" role="tablist">
    <button role="tab" data-tab="changes" aria-selected="true">Changes<span class="count" id="n-changes">0</span></button>
    <button role="tab" data-tab="history" aria-selected="false">History</button>
    <button role="tab" data-tab="checkpoints" aria-selected="false">Checkpoints<span class="count" id="n-checkpoints">0</span></button>
    <button role="tab" data-tab="storage" aria-selected="false">Storage</button>
    <button role="tab" data-tab="agents" aria-selected="false">Agents</button>
  </nav>
  <section class="panel" id="tab-changes">
    <h2>Changes</h2>
    <p class="hint">What is different from the last saved version. Pick a file to see what changed in it.</p>
    <div id="changes-body"></div>
  </section>
  <section class="panel" id="tab-history" hidden>
    <h2>History</h2>
    <p class="hint">Every saved version, newest first. Restoring puts your files back exactly as they were in that version; what you have now is kept as a checkpoint first, so nothing is lost.</p>
    <ul class="list" id="history-list"></ul>
    <button class="btn more" id="history-more" hidden>Show older versions</button>
  </section>
  <section class="panel" id="tab-checkpoints" hidden>
    <h2>Checkpoints</h2>
    <p class="hint">A checkpoint is a snapshot of every file right now, including ones not saved in a version yet. Make one before a risky change; restore it if things go wrong.</p>
    <div class="save"><input id="cp-msg" maxlength="200" placeholder="What is this checkpoint for? (optional)"><button class="btn primary" id="cp-make">Create checkpoint</button></div>
    <ul class="list" id="cp-list"></ul>
  </section>
  <section class="panel" id="tab-storage" hidden>
    <h2>Storage</h2>
    <p class="hint">Where your backups live. "Check backup" confirms the storage holds a complete history; "Full restore test" rebuilds everything from the storage alone, which proves you could recover on a new computer.</p>
    <div id="storage-body"></div>
  </section>
  <section class="panel" id="tab-agents" hidden>
    <h2>Agents</h2>
    <p class="hint">Work done by AI agents, grouped by session, from the attribution recorded in each version and checkpoint.</p>
    <ul class="list" id="agents-list"></ul>
  </section>
</div>
<div class="modal" id="modal" hidden role="dialog" aria-modal="true" aria-labelledby="modal-title">
  <div class="box"><h3 id="modal-title"></h3><p id="modal-text"></p>
  <div class="row"><button class="btn" id="modal-no">Cancel</button><button class="btn primary" id="modal-yes">OK</button></div></div>
</div>
<script nonce="__NONCE__">
"use strict";
const $ = id => document.getElementById(id);
const state = {tab: "changes", historyN: 50, selected: null, status: null, draft: ""};
// The token lives in this page only: from ?token= in the link, or from sessionStorage
// (per port and per tab) after a reload. It never goes into a cookie, and it is removed
// from the address bar at once.
const TOKEN = (() => {
  let t = null;
  try { t = new URLSearchParams(location.search).get("token"); } catch (e) {}
  if (t) { try { sessionStorage.setItem("iron-token", t); } catch (e) {} }
  else { try { t = sessionStorage.getItem("iron-token"); } catch (e) {} }
  if (location.search) { try { history.replaceState(null, "", location.pathname + location.hash); } catch (e) {} }
  return t || "";
})();
const EXPIRED = "This dashboard link is no longer valid (the dashboard was restarted, or the link is old). Open the link that `iron ui` printed in the terminal again.";
function lockOut() {
  try { sessionStorage.removeItem("iron-token"); } catch (e) {}
  $("gate").hidden = false;
  document.querySelectorAll("button").forEach(b => { b.disabled = true; });
}

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path, body) {
  const opt = body === undefined ? {headers: {"X-Iron-Token": TOKEN}}
    : {method: "POST", headers: {"X-Iron-Token": TOKEN, "Content-Type": "application/json"}, body: JSON.stringify(body)};
  if (!TOKEN) { lockOut(); throw new Error(EXPIRED); }
  let r;
  try { r = await fetch(path, opt); }
  catch (e) { throw new Error("The dashboard is not running any more. Start it again with: iron ui"); }
  if (r.status === 403) { lockOut(); throw new Error(EXPIRED); }
  let data;
  try { data = await r.json(); } catch (e) { data = {error: "Unexpected reply from the dashboard (" + r.status + ")"}; }
  if (!r.ok) throw new Error(data.error || ("Error " + r.status));
  return data;
}
async function job(path, body) {
  let j = await api(path, body);
  while (j.state === "running") { await sleep(700); j = await api("/api/jobs/" + j.id); }
  if (j.state === "failed") throw new Error(j.error);
  return j.result;
}

function notice(kind, text, extra) {
  const n = h("div", {class: "notice " + kind, role: kind === "bad" ? "alert" : "status"},
    h("div", {class: "msg"}, text, extra || null),
    h("button", {class: "x", "aria-label": "Dismiss", onclick: () => n.remove()}, "×"));
  $("notices").prepend(n);
  while ($("notices").children.length > 4) $("notices").lastChild.remove();
  if (kind === "ok" && !extra) setTimeout(() => n.remove(), 8000);
  return n;
}
const fail = e => (e && e.message === EXPIRED) ? null : notice("bad", e.message || String(e));  // the banner says it

function ask(title, text, yes, danger) {
  return new Promise(resolve => {
    $("modal-title").textContent = title; $("modal-text").textContent = text;
    const y = $("modal-yes"), n = $("modal-no");
    y.textContent = yes || "OK"; y.className = "btn " + (danger ? "danger" : "primary");
    $("modal").hidden = false; n.focus();
    const done = v => { $("modal").hidden = true; y.onclick = n.onclick = document.onkeydown = null; resolve(v); };
    y.onclick = () => done(true); n.onclick = () => done(false);
    document.onkeydown = ev => { if (ev.key === "Escape") done(false); };
  });
}

async function busy(button, label, fn) {
  const old = button.textContent; button.disabled = true;
  button.replaceChildren(h("span", {class: "spin"}), label);
  try { return await fn(); } finally { button.disabled = false; button.textContent = old; }
}

// -- header -----------------------------------------------------------------
function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
function syncLine(r) {
  const where = h("div", {class: "where"}, r.location);
  const name = r.remote;
  if (r.state === "synced") return h("div", {class: "state"}, h("span", {class: "ok"}, "✓ Synced with " + name), where);
  if (r.state === "unpublished") return h("div", {class: "state"}, h("span", {class: "warn"}, "! Not published to " + name + " yet"), where);
  const parts = [];
  if (r.ahead) parts.push(plural(r.ahead, "version", "versions") + " to publish");
  if (r.behind) parts.push(plural(r.behind, "new version", "new versions") + " to download");
  return h("div", {class: "state"}, h("span", {class: "warn"}, "! " + name + ": " + parts.join(", ")), where);
}
async function loadStatus() {
  const s = await api("/api/status");
  state.status = s;
  document.title = s.name + " - Iron Repository";
  $("name").textContent = s.name;
  $("branch").textContent = s.branch ? "Working on: " + s.branch + (s.latest ? "  ·  current version: " + s.latest.subject : "")
    : "Not on a line of work (detached); switch to one in a terminal";
  $("states").replaceChildren(...(s.remotes.length ? s.remotes.map(syncLine)
    : [h("div", {class: "state warn"}, "! No backup storage yet. Run `iron setup` in a terminal to add one.")]));
  $("sync").disabled = !s.remotes.length || !!s.busy;
  $("n-changes").textContent = s.changes; $("n-checkpoints").textContent = s.checkpoints;
  showAlerts(s.conflicts || [], s.kept_out || []);
}
function showAlerts(conflicts, keptOut) {
  const c = $("conflicts"), k = $("kept-out");
  c.hidden = !conflicts.length;
  if (conflicts.length) c.replaceChildren(
    h("h2", {}, "These files conflict between your computers:"),
    h("ul", {}, conflicts.map(p => h("li", {}, h("code", {}, p)))),
    h("p", {}, "The same part of these files was changed on two computers, and Iron cannot tell which one to keep. "
      + "Open each file, find the blocks between the lines starting with <<<<<<< and >>>>>>>, keep the text you want, "
      + "and delete those marker lines (and the ======= line). Then save a version on the Changes tab and press Sync now."));
  k.hidden = !keptOut.length;
  if (keptOut.length) k.replaceChildren(
    h("h2", {}, "Kept out because they look like secrets:"),
    h("ul", {}, keptOut.map(p => h("li", {}, h("code", {}, p)))),
    h("p", {}, "These files stay on this computer only: they are not saved in versions or backups, "
      + "so they are not copied to your other computers either."));
}
$("sync").onclick = () => busy($("sync"), "Syncing...", async () => {
  try {
    const r = await job("/api/sync", {});
    notice("ok", "Sync finished.", r.output ? h("pre", {class: "out"}, r.output) : null);
  } catch (e) { fail(e); }
  await refresh();
});

// -- changes ----------------------------------------------------------------
function renderDiff(text, truncated) {
  const box = h("div", {class: "diff"});
  if (!text.trim()) { box.append(h("div", {class: "empty"}, "No visible difference in the content.")); return box; }
  let header = false, from = "";
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) { header = true; from = ""; continue; }
    if (header) {  // file header: up to the first @@, never content
      if (line.startsWith("--- ")) from = line.slice(4).replace(/^a\//, "");
      else if (line.startsWith("+++ ")) {
        const to = line.slice(4).replace(/^b\//, "");
        box.append(h("div", {class: "f"}, to === "/dev/null" ? from + "  (deleted)" : to));
      } else if (line.startsWith("Binary files")) {
        box.append(h("div", {class: "f"}, line.replace(/^Binary files (a\/)?/, "").replace(/ and .*$/, "")),
          h("div", {class: "empty"}, "This file is not text, so there is nothing to show line by line."));
      }
      if (!line.startsWith("@@")) continue;
      header = false;
    }
    if (line.startsWith("@@")) { const m = /\+(\d+)/.exec(line); box.append(h("div", {class: "h"}, m ? "around line " + m[1] : "...")); continue; }
    if (line.startsWith("\\")) continue;  // "\ No newline at end of file"
    box.append(h("div", {class: "l" + (line[0] === "+" ? " a" : line[0] === "-" ? " d" : "")}, line || " "));
  }
  if (truncated) box.append(h("div", {class: "empty"}, "The rest is too long to show here."));
  return box;
}
async function showDiff(path) {
  state.selected = path;
  document.querySelectorAll(".files li button").forEach(b => b.setAttribute("aria-current", String(b.dataset.path === (path || ""))));
  const pane = $("diff-pane");
  pane.replaceChildren(h("div", {class: "empty"}, h("span", {class: "spin"}), "Loading..."));
  try { const d = await api("/api/diff" + (path ? "?path=" + encodeURIComponent(path) : "")); pane.replaceChildren(renderDiff(d.diff, d.truncated)); }
  catch (e) { pane.replaceChildren(h("div", {class: "empty bad"}, e.message)); }
}
const KIND = {new: "new", changed: "changed", deleted: "deleted", renamed: "renamed", conflict: "both sides changed"};
async function loadChanges() {
  const body = $("changes-body");
  const d = await api("/api/changes");
  if (!d.changes.length) {
    body.replaceChildren(h("div", {class: "empty-state"}, "✓ Everything is saved. No changes since the last version."));
    return;
  }
  const files = h("ul", {class: "files"},
    h("li", {}, h("button", {"data-path": "", onclick: () => showDiff(null)}, h("span", {class: "p"}, h("b", {}, "All changes")))),
    d.changes.map(c => h("li", {}, h("button", {"data-path": c.path, onclick: () => showDiff(c.path), title: c.from ? "was " + c.from : null},
      h("span", {class: "kind " + c.kind}, KIND[c.kind] || c.kind), h("span", {class: "p"}, c.path)))));
  const msg = h("input", {id: "commit-msg", maxlength: "500", placeholder: "Describe what changed, e.g. “Fix the login page”", "aria-label": "Description of this version"});
  msg.value = state.draft;
  msg.oninput = () => { state.draft = msg.value; };
  const save = h("button", {class: "btn primary"}, "Save version");
  save.onclick = () => {
    if (!msg.value.trim()) { msg.focus(); return notice("bad", "Describe what changed before saving a version."); }
    return busy(save, "Saving...", async () => {
      try {
        const r = await api("/api/commit", {message: msg.value});
        state.draft = "";
        notice("ok", "Saved version " + r.short + ". Use “Sync now” to back it up.");
        if (r.kept_out && r.kept_out.length) notice("bad", "Not saved because they look like secrets: " + r.kept_out.join(", "));
      } catch (e) { if (e.message !== EXPIRED) notice("bad", "The version was not saved: " + (e.message || String(e))); }
      await refresh();
    });
  };
  msg.onkeydown = ev => { if (ev.key === "Enter") save.click(); };
  body.replaceChildren(h("div", {class: "grid"}, files, h("div", {id: "diff-pane", class: "diffpane"})),
    h("div", {class: "save"}, msg, save));
  const keep = d.changes.some(c => c.path === state.selected) ? state.selected : null;
  showDiff(keep);
}

// -- restore (history and checkpoints) ---------------------------------------
async function restore(body, what) {
  const ok = await ask("Restore files?", "Your files will be put back exactly as they were in " + what + ".\n\n"
    + "Files you have now that did not exist then will be removed. Everything you have now is saved as a checkpoint first, so you can undo this.",
    "Restore files", true);
  if (!ok) return;
  try {
    const r = await api("/api/restore", body);
    const undo = h("button", {class: "btn", onclick: () => restore({checkpoint: r.safety_checkpoint}, "safety checkpoint " + r.safety_checkpoint)}, "Undo");
    notice("ok", "Files restored to " + what + ". What was there before is saved as checkpoint ", [h("code", {}, r.safety_checkpoint), ".  ", undo]);
  } catch (e) { fail(e); }
  await refresh();
}

// -- history ----------------------------------------------------------------
function who(item) {
  return item.agent ? h("span", {class: "who agent", title: "made by an AI agent"}, item.agent)
    : h("span", {class: "who"}, item.author);
}
async function loadHistory() {
  const d = await api("/api/history?n=" + state.historyN);
  const list = $("history-list");
  if (!d.history.length) { list.replaceChildren(h("li", {class: "empty-state"}, "No versions saved yet. Save one from the Changes tab.")); $("history-more").hidden = true; return; }
  list.replaceChildren(...d.history.map((item, i) => h("li", {},
    h("div", {class: "main"}, h("div", {class: "title"}, item.subject),
      h("div", {class: "meta"}, who(item), h("span", {}, item.when), h("code", {title: "version id"}, item.sha),
        item.autosave ? h("span", {class: "badge", title: "saved automatically by iron autosave"}, "autosaved") : null, i === 0 ? h("span", {class: "ok"}, "current version") : null)),
    h("button", {class: "btn", onclick: () => restore({version: item.sha}, "version " + item.sha + " (“" + item.subject + "”)")}, "Restore files to this version"))));
  $("history-more").hidden = d.history.length < state.historyN;
}
$("history-more").onclick = () => { state.historyN += 50; loadHistory().catch(fail); };

// -- checkpoints ------------------------------------------------------------
async function loadCheckpoints() {
  const d = await api("/api/checkpoints");
  $("cp-list").replaceChildren(...(d.checkpoints.length ? d.checkpoints.map(c => h("li", {},
    h("div", {class: "main"}, h("div", {class: "title"}, c.message), h("div", {class: "meta"}, h("span", {}, c.date.replace("T", " ")), h("code", {}, c.id))),
    h("button", {class: "btn", onclick: () => restore({checkpoint: c.id}, "checkpoint " + c.id)}, "Restore")))
    : [h("li", {class: "empty-state"}, "No checkpoints yet.")]));
}
$("cp-make").onclick = () => busy($("cp-make"), "Saving...", async () => {
  try { const r = await api("/api/checkpoint", {message: $("cp-msg").value}); $("cp-msg").value = "";
    notice("ok", "Checkpoint " + r.checkpoint.id + " saved."); }
  catch (e) { fail(e); }
  await refresh();
});

// -- storage ----------------------------------------------------------------
function report(r) {
  const items = [];
  r.problems.forEach(p => items.push(h("li", {class: "bad"}, "✗ " + p)));
  r.warnings.forEach(p => items.push(h("li", {class: "warn"}, "! " + p)));
  r.info.forEach(p => items.push(h("li", {}, "· " + p)));
  items.unshift(h("li", {class: r.ok ? "ok" : "bad"}, r.ok ? (r.deep ? "✓ Full restore test passed." : "✓ The backup is complete and healthy.") : "✗ Problems found."));
  return h("ul", {class: "report"}, items);
}
async function loadStorage() {
  const body = $("storage-body");
  body.replaceChildren(h("div", {class: "empty-state"}, h("span", {class: "spin"}), "Checking storage..."));
  const d = await api("/api/storage");
  if (!d.storage.length) { body.replaceChildren(h("div", {class: "empty-state"}, "No backup storage yet. Run `iron setup` in a terminal to add one.")); return; }
  body.replaceChildren(...d.storage.map(s => {
    const out = h("div", {});
    const check = deep => async ev => {
      const b = ev.currentTarget;
      await busy(b, deep ? "Rebuilding..." : "Checking...", async () => {
        try { out.replaceChildren(report(await job("/api/verify", {remote: s.remote, deep}))); }
        catch (e) { out.replaceChildren(h("ul", {class: "report"}, h("li", {class: "bad"}, "✗ " + e.message))); }
      });
    };
    const health = s.healthy ? h("span", {class: "ok"}, "✓ Reachable (" + s.detail + ")") : h("span", {class: "bad"}, "✗ Not reachable: " + s.detail);
    return h("div", {class: "card"},
      h("h3", {}, s.remote), h("div", {class: "mono"}, s.location),
      h("div", {class: "meta"}, health, s.exists === false ? h("span", {class: "warn"}, "nothing published there yet") : null,
        h("span", {}, s.encrypted ? "🔒 encrypted" : "not encrypted")),
      s.encrypted ? h("div", {class: "key"}, s.key_on_this_computer === false
        ? "This backup is encrypted and this computer has no key for it. Import the recovery key in a terminal: iron key import <recovery key>"
        : "This backup is encrypted. Without its recovery key nobody, not even you, can read it on another computer. Make sure the key is in your password manager (show it in a terminal with: iron key show " + s.remote + ").") : null,
      h("div", {class: "row"}, h("button", {class: "btn", onclick: check(false)}, "Check backup"), h("button", {class: "btn", onclick: check(true)}, "Full restore test")),
      out);
  }));
}

// -- agents -----------------------------------------------------------------
async function loadAgents() {
  const d = await api("/api/agents");
  $("agents-list").replaceChildren(...(d.sessions.length ? d.sessions.map(s => h("li", {},
    h("div", {class: "main"}, h("div", {class: "title"}, h("span", {class: "who agent"}, s.agent), " ", s.latest),
      h("div", {class: "meta"}, s.session ? h("span", {}, "session ", h("code", {}, s.session)) : null,
        h("span", {}, plural(s.versions, "version", "versions") + ", " + plural(s.checkpoints, "checkpoint", "checkpoints")),
        h("span", {}, "last active " + s.last)))))
    : [h("li", {class: "empty-state"}, "No agent activity recorded yet. Run agents through `iron agent` to see them here.")]));
}

// -- tabs -------------------------------------------------------------------
const LOADERS = {changes: loadChanges, history: loadHistory, checkpoints: loadCheckpoints, storage: loadStorage, agents: loadAgents};
function select(tab) {
  state.tab = tab;
  document.querySelectorAll("nav.tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  for (const t of Object.keys(LOADERS)) $("tab-" + t).hidden = t !== tab;
  try { history.replaceState(null, "", "#" + tab); } catch (e) {}
  LOADERS[tab]().catch(fail);
}
document.querySelectorAll("nav.tabs button").forEach(b => b.onclick = () => select(b.dataset.tab));
async function refresh() {
  await Promise.all([loadStatus().catch(fail), (state.tab === "storage" ? Promise.resolve() : LOADERS[state.tab]().catch(fail))]);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) loadStatus().catch(() => {}); }, 15000);
if (!TOKEN) lockOut();
else {
  loadStatus().catch(fail);
  select(LOADERS[location.hash.slice(1)] ? location.hash.slice(1) : "changes");
}
</script>
</body>
</html>
"""
