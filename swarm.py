#!/usr/bin/env python3
"""AgentsSwarm: a tiny hub + CLI that lets AI coding agents (Claude Code, Codex,
OpenCode, ...) running on different machines work together on one git repository.

  * hub    - HTTP server: chat, goal, roles, task board, git remote, web chat UI
  * client - CLI that agents (and humans) call from a shell

Pure Python standard library (3.8+). Requires `git` in PATH.
Run `python swarm.py -h` for commands; see README.md for the guide.
"""
import argparse
import base64
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"
DEFAULT_PORT = 8765
REPO_NAME = "project.git"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
OFFLINE_AFTER = 15 * 60          # seconds without any request -> agent is offline
MAX_MESSAGES = 5000
MAX_WAIT = 300

PRESETS = {
    # prompt goes to stdin unless the command contains {prompt_file}
    "claude": "claude -p --dangerously-skip-permissions",
    "codex": "codex exec --dangerously-bypass-approvals-and-sandbox -",
    "opencode": 'opencode run "Read the file {prompt_file} and follow the instructions in it exactly."',
}

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def now():
    return time.time()


def die(msg, code=1):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)


def git(*args, cwd=None, input=None, env=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, input=input, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr.decode(errors="replace").strip()))
    return r.stdout.decode("utf-8", errors="replace").strip()


# --------------------------------------------------------------------------- protocol

PROTOCOL = """# AgentsSwarm protocol

You are **{name}**, one agent in a swarm of AI coding agents (possibly different products:
Claude Code, Codex, OpenCode, ...) running on different machines. Together you work on ONE
git repository - your current working directory is your clone of it. The hub ({hub})
relays chat messages, keeps the shared goal, roles and a task board, and is your git
remote `origin`. Humans can write into the chat too.

Talk to the hub ONLY with this shell command (run it from inside the repo):

    {sw} <command>

| command | what it does |
|---|---|
| `status` | goal, lead, agents + roles, task board |
| `inbox` | new messages for you (non-blocking) |
| `wait [--timeout 60]` | block until new messages arrive, then print them |
| `send "text" [--to NAME]` | message everyone (or one agent / human) |
| `tasks` | task board |
| `task add "title" [--desc "..."] [--to NAME]` | create a task (optionally assigned) |
| `task claim ID` / `task done ID --note "..."` | take a task / finish it |
| `task block ID --note "why"` / `task open ID` | mark blocked / release it |
| `task assign ID NAME` / `task note ID "text"` | reassign / add a note |
| `role NAME "role"` | set someone's role (lead, or human) |
| `goal "text"` | change the shared goal (lead, or human) |
| `history [-n 30]` | recent messages of everyone |

If your tool kills long-running commands, use a smaller `wait --timeout`.

## 1. Startup - distribute roles
1. `{sw} status` - read the goal, who is lead, who is online, roles, tasks.
2. `git pull --rebase` and get familiar with the repository.
3. If you are the LEAD:
   - give teammates time to join: `{sw} wait --timeout 60` a few times (about 3 minutes
     total) or until a human says "start". Read their intro messages.
   - decide roles by strengths and the goal (e.g. architect, backend, frontend,
     tester/reviewer, docs). Assign them to everyone, yourself included: `{sw} role NAME ROLE`.
   - split the goal into small, independent tasks - preferably by files/modules so agents
     don't conflict - and create them: `{sw} task add "..." --desc "..." --to NAME`.
   - broadcast a short plan with `{sw} send`.
4. If you are NOT the lead: introduce yourself with
   `{sw} send "Hi, I'm {name} (<your product/model>). Strengths: ..."`,
   then `{sw} wait` for your role and tasks. You may propose ideas or another split;
   discuss briefly - the lead decides, humans override everyone.

## 2. Work loop (repeat until the goal is done)
1. `{sw} inbox`. Messages of kind=human are TOP PRIORITY and override the plan.
2. Pick work: your assigned tasks first, else `{sw} task claim ID` an open one, else ask the lead.
3. `git pull --rebase`, implement, run the tests/build if the project has them.
4. Commit small and often: `git commit -m "[{name}] #ID: what and why"`, then
   `git pull --rebase && git push`. NEVER force-push. Resolve conflicts carefully,
   keeping teammates' work.
5. `{sw} task done ID --note "summary"`. Send a message only when teammates need it
   (API changes, decisions, blockers, review results).
6. Nothing to do? `{sw} wait` and repeat. Don't end your session while the goal is unfinished.

## 3. Rules
- Teammates' work arrives as commits. After a "pushed" notification: `git pull --rebase`,
  then `git log` / `git show` to see what changed. Reviewers review new commits and report
  problems as tasks or messages.
- Discuss before big or irreversible changes (architecture, new dependencies, mass renames):
  post a proposal and wait one `wait` cycle for objections.
- Keep messages short and concrete. Don't reply just to acknowledge.
- Don't edit files covered by another agent's in-progress task without agreeing first.
- Blocked? `{sw} task block ID --note "why"` and tell the lead (or a human).
- When the goal is complete, the lead posts a final summary and asks the humans what's next.
"""


def sw_command():
    """Shell command that runs this script, usable from bash, cmd and PowerShell."""
    py = "python"
    if not shutil.which("python"):
        py = "python3" if shutil.which("python3") else '"%s"' % sys.executable
    path = os.path.abspath(__file__).replace("\\", "/")
    if " " in path:
        path = '"%s"' % path
    return "%s %s" % (py, path)


def render_protocol(name, hub):
    return PROTOCOL.replace("{name}", name).replace("{hub}", hub).replace("{sw}", sw_command())


# --------------------------------------------------------------------------- hub

class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class Hub:
    def __init__(self, data_dir, key="", goal=None, repo_from=None):
        self.data_dir = os.path.abspath(data_dir)
        self.repos_dir = os.path.join(self.data_dir, "repos")
        self.state_path = os.path.join(self.data_dir, "state.json")
        os.makedirs(self.repos_dir, exist_ok=True)
        self.key = key or ""
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.push_lock = threading.Lock()
        self.state = {"goal": "", "lead": None, "agents": {}, "messages": [],
                      "tasks": [], "next_msg": 1, "next_task": 1}
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                self.state.update(json.load(f))
        if goal:
            self.state["goal"] = goal
        self.init_repo(repo_from)
        self.save()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---- persistence / helpers
    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        try:
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def init_repo(self, repo_from):
        path = os.path.join(self.repos_dir, REPO_NAME)
        if os.path.isdir(path):
            if repo_from:
                print("note: %s already exists, --repo-from ignored" % path)
        elif repo_from:
            src = os.path.abspath(repo_from) if os.path.exists(repo_from) else repo_from
            print("Creating hub repository from %s ..." % src)
            git("clone", "--bare", src, path)
        else:
            git("init", "--bare", path)
            git("symbolic-ref", "HEAD", "refs/heads/main", cwd=path)
            env = dict(os.environ, GIT_AUTHOR_NAME="swarm-hub", GIT_AUTHOR_EMAIL="hub@swarm.local",
                       GIT_COMMITTER_NAME="swarm-hub", GIT_COMMITTER_EMAIL="hub@swarm.local")
            readme = "# Project\n\nCreated by AgentsSwarm hub.\n\nGoal: %s\n" % (self.state.get("goal") or "(not set yet)")
            blob = git("hash-object", "-w", "--stdin", cwd=path, input=readme.encode("utf-8"))
            tree = git("mktree", cwd=path, input=("100644 blob %s\tREADME.md\n" % blob).encode())
            commit = git("commit-tree", tree, "-m", "Initial commit", cwd=path, env=env)
            git("update-ref", "refs/heads/main", commit, cwd=path)
        git("config", "http.receivepack", "true", cwd=path)

    def _online(self, a):
        return now() - a.get("last_seen", 0) < OFFLINE_AFTER

    def _post(self, frm, to, text, kind, about=None):
        st = self.state
        msg = {"id": st["next_msg"], "ts": now(), "from": frm, "to": to or "*",
               "text": text, "kind": kind}
        if about:
            msg["about"] = about
        st["next_msg"] += 1
        st["messages"].append(msg)
        if len(st["messages"]) > MAX_MESSAGES:
            del st["messages"][:len(st["messages"]) - MAX_MESSAGES]
        self.cond.notify_all()
        return msg

    def _check_lead(self):
        st = self.state
        lead = st["agents"].get(st["lead"]) if st["lead"] else None
        if lead and self._online(lead):
            return
        online = sorted((a for a in st["agents"].values() if self._online(a)), key=lambda a: a["joined"])
        if online and online[0]["name"] != st["lead"]:
            old = st["lead"]
            st["lead"] = online[0]["name"]
            self._post("system", "*", "%s is now the LEAD%s." % (
                st["lead"], " (%s went offline)" % old if old else ""), "system")
            self.save()

    def _watchdog(self):
        while True:
            time.sleep(60)
            with self.lock:
                self._check_lead()

    def _touch(self, name):
        a = self.state["agents"].get(name)
        if a:
            a["last_seen"] = now()

    def _actor(self, name):
        if not name or not NAME_RE.match(str(name)):
            raise ApiError("invalid name (allowed: letters, digits, _ . -, max 40)")
        self._touch(name)
        return name

    def snapshot(self):
        with self.lock:
            self._check_lead()
            st = self.state
            agents = []
            for a in sorted(st["agents"].values(), key=lambda a: a["joined"]):
                agents.append({k: a.get(k) for k in ("name", "kind", "role", "joined", "last_seen")})
                agents[-1]["online"] = self._online(a)
                agents[-1]["lead"] = a["name"] == st["lead"]
            return {"goal": st["goal"], "lead": st["lead"], "agents": agents,
                    "tasks": st["tasks"], "last_msg": st["next_msg"] - 1,
                    "repo": "/git/" + REPO_NAME, "version": VERSION}

    # ---- API operations
    def join(self, name, kind):
        with self.lock:
            name = self._actor(name)
            st = self.state
            a = st["agents"].get(name)
            if a is None:
                a = st["agents"][name] = {"name": name, "kind": kind or "agent", "role": "",
                                          "joined": now(), "cursor": st["next_msg"] - 1}
                verb = "joined"
            else:
                a["kind"] = kind or a.get("kind")
                verb = "reconnected"
            a["last_seen"] = now()
            self._check_lead()
            if not st["lead"]:
                st["lead"] = name
            self._post("system", "*", "%s (%s) %s. Lead: %s." % (name, a["kind"], verb, st["lead"]),
                       "system", about=name)
            self.save()
            return {"name": name, "lead": st["lead"], "is_lead": st["lead"] == name,
                    "role": a["role"], "goal": st["goal"]}

    def leave(self, name):
        with self.lock:
            a = self.state["agents"].get(name)
            if a:
                a["last_seen"] = 0
                self._post("system", "*", "%s left." % name, "system", about=name)
                self._check_lead()
                self.save()
            return {"ok": True}

    def send(self, frm, to, text):
        with self.lock:
            frm = self._actor(frm)
            text = (text or "").strip()
            if not text:
                raise ApiError("empty message")
            kind = "agent" if frm in self.state["agents"] else "human"
            msg = self._post(frm, to or "*", text, kind)
            self.save()
            return msg

    def _visible(self, m, name):
        return m["from"] != name and m["to"] in ("*", name) and m.get("about") != name

    def inbox(self, name, wait=0.0, peek=False):
        deadline = now() + min(max(wait, 0), MAX_WAIT)
        with self.cond:
            name = self._actor(name)
            a = self.state["agents"].get(name)
            if a is None:
                raise ApiError("unknown agent %s - run setup/join first" % name, 404)
            while True:
                cursor = a.get("cursor", 0)
                msgs = []
                for m in reversed(self.state["messages"]):
                    if m["id"] <= cursor:
                        break
                    if self._visible(m, name):
                        msgs.append(m)
                msgs.reverse()
                left = deadline - now()
                if msgs or left <= 0:
                    break
                self.cond.wait(min(left, 30))
                self._touch(name)
            if not peek:
                a["cursor"] = self.state["next_msg"] - 1
            a["last_seen"] = now()
            return {"messages": msgs}

    def messages(self, since=0, limit=200):
        with self.lock:
            msgs = [m for m in self.state["messages"] if m["id"] > since]
            return {"messages": msgs[-limit:]}

    def set_goal(self, by, text):
        with self.lock:
            by = self._actor(by)
            self.state["goal"] = text.strip()
            self._post("system", "*", "GOAL changed by %s:\n%s" % (by, self.state["goal"]), "system", about=by)
            self.save()
            return {"goal": self.state["goal"]}

    def set_role(self, by, agent, role):
        with self.lock:
            by = self._actor(by)
            a = self.state["agents"].get(agent)
            if not a:
                raise ApiError("unknown agent %s" % agent, 404)
            a["role"] = (role or "").strip()
            self._post("system", "*", "%s set role of %s: %s" % (by, agent, a["role"] or "(none)"), "system", about=by)
            self.save()
            return {"agent": agent, "role": a["role"]}

    def set_lead(self, by, agent):
        with self.lock:
            by = self._actor(by)
            if agent not in self.state["agents"]:
                raise ApiError("unknown agent %s" % agent, 404)
            self.state["lead"] = agent
            self._post("system", "*", "%s made %s the LEAD." % (by, agent), "system", about=by)
            self.save()
            return {"lead": agent}

    def add_task(self, by, title, desc="", assignee=None):
        with self.lock:
            by = self._actor(by)
            if not (title or "").strip():
                raise ApiError("empty title")
            if assignee and assignee not in self.state["agents"]:
                raise ApiError("unknown agent %s" % assignee, 404)
            t = {"id": self.state["next_task"], "title": title.strip(), "desc": (desc or "").strip(),
                 "status": "open", "assignee": assignee or None, "created_by": by,
                 "created": now(), "updated": now(), "notes": []}
            self.state["next_task"] += 1
            self.state["tasks"].append(t)
            self._post("system", assignee or "*", "Task #%d created by %s%s: %s" % (
                t["id"], by, " for " + assignee if assignee else "", t["title"]), "system", about=by)
            self.save()
            return t

    def update_task(self, tid, by, action, assignee=None, note=None):
        with self.lock:
            by = self._actor(by)
            t = next((t for t in self.state["tasks"] if t["id"] == tid), None)
            if not t:
                raise ApiError("no task #%s" % tid, 404)
            if action == "claim":
                t["assignee"], t["status"] = by, "in_progress"
            elif action == "done":
                t["status"] = "done"
            elif action == "block":
                t["status"] = "blocked"
            elif action == "open":
                t["status"], t["assignee"] = "open", None
            elif action == "assign":
                if assignee not in self.state["agents"]:
                    raise ApiError("unknown agent %s" % assignee, 404)
                t["assignee"] = assignee
                if t["status"] == "done":
                    t["status"] = "open"
            elif action != "note":
                raise ApiError("unknown action %s" % action)
            if note:
                t["notes"].append({"by": by, "ts": now(), "text": note})
            t["updated"] = now()
            text = "Task #%d %s by %s: %s" % (t["id"], {"claim": "claimed", "done": "DONE", "block": "BLOCKED",
                                                        "open": "reopened", "assign": "assigned to " + str(assignee),
                                                        "note": "note"}[action], by, t["title"])
            if note:
                text += "\n  note: " + note
            self._post("system", "*", text, "system", about=by)
            self.save()
            return t

    # ---- git push notifications
    def refs(self):
        out = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads",
                  cwd=os.path.join(self.repos_dir, REPO_NAME), check=False)
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def notify_push(self, user, before, after):
        repo = os.path.join(self.repos_dir, REPO_NAME)
        lines = []
        for ref, new in after.items():
            old = before.get(ref)
            if old == new:
                continue
            branch = ref[len("refs/heads/"):]
            rng = ["%s..%s" % (old, new)] if old else ["-n", "5", new]
            log = git("log", "--format=  %h %an: %s", "-n", "15", *rng, cwd=repo, check=False)
            lines.append("%s pushed to %s:\n%s" % (user, branch, log or "  (history rewritten)"))
        for ref in before:
            if ref not in after:
                lines.append("%s deleted branch %s" % (user, ref[len("refs/heads/"):]))
        if lines:
            with self.lock:
                self._touch(user)
                self._post("system", "*", "\n".join(lines) + "\n-> run `git pull --rebase` to get it.",
                           "system", about=user)
                self.save()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentsSwarm/" + VERSION
    hub = None  # type: Hub

    def log_message(self, fmt, *args):
        if os.environ.get("SWARM_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))

    # ---- low level
    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _basic(self):
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                return user, pw
            except Exception:
                pass
        return None, None

    def _authorized(self, query):
        key = self.hub.key
        if not key or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return True
        cands = [self.headers.get("X-Swarm-Key", ""), (query.get("key") or [""])[0], self._basic()[1] or ""]
        return any(c and hmac.compare_digest(c, key) for c in cands)

    def _read_body(self):
        """Read the request body (plain or chunked) into a temp file. Returns (file, size)."""
        f = tempfile.TemporaryFile()
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                left = size
                while left:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                self.rfile.readline()
        else:
            left = int(self.headers.get("Content-Length") or 0)
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        size = f.tell()
        f.seek(0)
        return f, size

    # ---- routing
    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path.startswith("/git/"):
                return self._git(u, q)
            if u.path in ("/", "/index.html"):
                return self._send(200, UI_HTML, "text/html; charset=utf-8")
            if u.path == "/swarm.py":
                with open(os.path.abspath(__file__), "rb") as f:
                    return self._send(200, f.read(), "text/x-python; charset=utf-8")
            if u.path == "/protocol":
                name = (q.get("name") or ["<your-name>"])[0]
                return self._send(200, render_protocol(name, "http://" + (self.headers.get("Host") or "hub")),
                                  "text/markdown; charset=utf-8")
            if u.path.startswith("/api/"):
                if not self._authorized(q):
                    return self._json({"error": "unauthorized: wrong or missing swarm key"}, 401)
                body = {}
                if self.command == "POST":
                    f, size = self._read_body()
                    raw = f.read()
                    body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                return self._json(self._api(u.path, q, body))
            self._json({"error": "not found"}, 404)
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the hub alive
            try:
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def _api(self, path, q, b):
        hub, get = self.hub, (lambda k, d=None: (q.get(k) or [d])[0])
        post = self.command == "POST"
        if path == "/api/state":
            return hub.snapshot()
        if path == "/api/messages":
            return hub.messages(int(get("since", 0)), int(get("limit", 200)))
        if path == "/api/inbox":
            return hub.inbox(get("name"), float(get("wait", 0)), get("peek") == "1")
        if path == "/api/tasks" and not post:
            return {"tasks": hub.snapshot()["tasks"]}
        if not post:
            raise ApiError("use POST", 405)
        if path == "/api/join":
            return hub.join(b.get("name"), b.get("kind"))
        if path == "/api/leave":
            return hub.leave(b.get("name"))
        if path == "/api/send":
            return hub.send(b.get("from"), b.get("to"), b.get("text"))
        if path == "/api/goal":
            return hub.set_goal(b.get("by"), b.get("text", ""))
        if path == "/api/role":
            return hub.set_role(b.get("by"), b.get("agent"), b.get("role"))
        if path == "/api/lead":
            return hub.set_lead(b.get("by"), b.get("agent"))
        if path == "/api/tasks":
            return hub.add_task(b.get("by"), b.get("title"), b.get("desc"), b.get("assignee"))
        m = re.match(r"^/api/tasks/(\d+)$", path)
        if m:
            return hub.update_task(int(m.group(1)), b.get("by"), b.get("action"), b.get("assignee"), b.get("note"))
        raise ApiError("not found", 404)

    def _git(self, u, q):
        if not self._authorized(q):
            return self._send(401, "auth required\n", "text/plain",
                              {"WWW-Authenticate": 'Basic realm="AgentsSwarm"'})
        m = re.match(r"^/git/([A-Za-z0-9_.-]+\.git)(/.*)?$", u.path)
        if not m or not os.path.isdir(os.path.join(self.hub.repos_dir, m.group(1))):
            return self._send(404, "no such repository\n", "text/plain")
        user = self._basic()[0] or "anonymous"
        body, size = self._read_body()
        env = dict(os.environ, GIT_PROJECT_ROOT=self.hub.repos_dir, GIT_HTTP_EXPORT_ALL="1",
                   PATH_INFO="/" + m.group(1) + (m.group(2) or "/"), REQUEST_METHOD=self.command,
                   QUERY_STRING=u.query, CONTENT_TYPE=self.headers.get("Content-Type", ""),
                   CONTENT_LENGTH=str(size), REMOTE_USER=user, REMOTE_ADDR=self.client_address[0])
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        is_push = self.command == "POST" and (m.group(2) or "").endswith("/git-receive-pack")
        out = tempfile.TemporaryFile()
        if is_push:
            self.hub.push_lock.acquire()
        try:
            before = self.hub.refs() if is_push else None
            r = subprocess.run(["git", "http-backend"], stdin=body, stdout=out, stderr=subprocess.PIPE, env=env)
            if r.stderr and os.environ.get("SWARM_DEBUG"):
                sys.stderr.write(r.stderr.decode(errors="replace"))
            if is_push:
                self.hub.notify_push(user, before, self.hub.refs())
        finally:
            if is_push:
                self.hub.push_lock.release()
        out.seek(0)
        code, headers = 200, []
        while True:
            line = out.readline().rstrip(b"\r\n")
            if not line:
                break
            k, _, v = line.decode("latin-1").partition(":")
            if k.lower() == "status":
                code = int(v.strip().split()[0])
            else:
                headers.append((k.strip(), v.strip()))
        start = out.tell()
        out.seek(0, 2)
        length = out.tell() - start
        out.seek(start)
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(length))
        self.end_headers()
        shutil.copyfileobj(out, self.wfile)


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    for probe in ("10.255.255.255", "26.0.0.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    ips.discard("0.0.0.0")
    return sorted(ips, key=lambda ip: (not ip.startswith("26."), ip.startswith("127."), ip))


def cmd_hub(a):
    if not shutil.which("git"):
        die("git is not installed / not in PATH")
    hub = Hub(a.data, a.key, a.goal, a.repo_from)
    Handler.hub = hub
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print("AgentsSwarm hub v%s listening on %s:%d   (data: %s)" % (VERSION, a.host, a.port, hub.data_dir))
    print("  web chat (you):   http://localhost:%d/" % a.port)
    for ip in local_ips():
        tag = "   <- Radmin VPN" if ip.startswith("26.") else ""
        print("  for others:       http://%s:%d/%s" % (ip, a.port, tag))
    print("  swarm key:        %s" % (a.key or "(none - anyone in the network can connect)"))
    print("  goal:             %s" % (hub.state["goal"] or "(not set - set it in the web chat)"))
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


# --------------------------------------------------------------------------- client

def find_config():
    p = os.environ.get("SWARM_CONFIG")
    if p:
        return p
    d = os.getcwd()
    while True:
        c = os.path.join(d, ".git", "swarm.json")
        if os.path.isfile(c):
            return c
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


class Client:
    def __init__(self, hub, name, key):
        if not hub:
            die("no hub configured: run `setup` first, run inside your workspace, or pass --hub URL")
        if "://" not in hub:
            hub = "http://" + hub
        self.hub = hub.rstrip("/")
        self.name = name
        self.key = key or ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, data=None, timeout=30, **params):
        url = self.hub + path
        params = {k: v for k, v in params.items() if v is not None}
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "X-Swarm-Key": self.key})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:
                msg = str(e)
            die("hub: %s" % msg)
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            die("cannot reach hub at %s (%s). Is the hub running and Radmin VPN connected?"
                % (self.hub, getattr(e, "reason", e)))

    def need_name(self):
        if not self.name:
            die("no name: run inside your workspace or pass --name")
        return self.name

    def inbox(self, wait=0):
        return self.call("/api/inbox", timeout=wait + 30, name=self.need_name(), wait=wait)["messages"]


def fmt_time(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def fmt_msg(m):
    tag = {"system": "[system]", "human": "[HUMAN]"}.get(m["kind"], "")
    to = "" if m["to"] == "*" else " -> " + m["to"]
    head = "#%d %s %s%s%s:" % (m["id"], fmt_time(m["ts"]), m["from"], to, " " + tag if tag else "")
    text = m["text"]
    return head + ("\n    " + text.replace("\n", "\n    ") if "\n" in text else " " + text)


def fmt_task(t):
    s = "#%-3d %-11s %-14s %s" % (t["id"], t["status"], t["assignee"] or "-", t["title"])
    if t.get("desc"):
        s += "\n       " + t["desc"].replace("\n", "\n       ")
    for n in t.get("notes", [])[-3:]:
        s += "\n       note (%s): %s" % (n["by"], n["text"])
    return s


def print_status(st, me=None):
    print("GOAL: " + (st["goal"] or "(not set)"))
    print("\nAGENTS (lead: %s):" % (st["lead"] or "-"))
    for a in st["agents"]:
        print("  %s %-18s %-12s role: %s%s%s" % (
            "*" if a["online"] else " ", a["name"], a["kind"] or "", a["role"] or "(unassigned)",
            "   [LEAD]" if a["lead"] else "", "   <- you" if a["name"] == me else ""))
    print("  (* = online)")
    active = [t for t in st["tasks"] if t["status"] != "done"]
    done = len(st["tasks"]) - len(active)
    print("\nTASKS (%d active, %d done):" % (len(active), done))
    for t in active:
        print("  " + fmt_task(t).replace("\n", "\n  "))
    if not active:
        print("  (none)")


def print_msgs(msgs, empty="(no new messages)"):
    if not msgs:
        print(empty)
    for m in msgs:
        print(fmt_msg(m))


def client_from(a):
    cfg = {}
    path = find_config()
    if path:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    hub = a.hub or os.environ.get("SWARM_HUB") or cfg.get("hub")
    name = a.name or os.environ.get("SWARM_NAME") or cfg.get("name")
    key = a.key if a.key is not None else os.environ.get("SWARM_KEY", cfg.get("key", ""))
    return Client(hub, name, key), cfg


def cmd_setup(a):
    if not a.hub or not a.name:
        die("setup needs --hub URL and --name NAME")
    if not NAME_RE.match(a.name):
        die("name may contain only letters, digits, _ . - (max 40)")
    if not shutil.which("git"):
        die("git is not installed / not in PATH")
    c = Client(a.hub, a.name, a.key or "")
    st = c.call("/api/state")
    ws = os.path.abspath(a.dir or os.path.join("workspaces", a.name))
    u = urllib.parse.urlsplit(c.hub)
    cred = "%s:%s@" % (urllib.parse.quote(a.name, safe=""), urllib.parse.quote(c.key or "x", safe=""))
    remote = "%s://%s%s%s" % (u.scheme, cred, u.netloc, st["repo"])
    if os.path.isdir(os.path.join(ws, ".git")):
        print("Workspace exists, updating remote: %s" % ws)
        if subprocess.run(["git", "remote", "set-url", "origin", remote], cwd=ws).returncode != 0:
            git("remote", "add", "origin", remote, cwd=ws)
    else:
        print("Cloning project into %s ..." % ws, flush=True)
        os.makedirs(os.path.dirname(ws), exist_ok=True)
        r = subprocess.run(["git", "clone", remote, ws])
        if r.returncode != 0:
            die("git clone failed")
    git("config", "user.name", a.name, cwd=ws)
    git("config", "user.email", "%s@swarm.local" % a.name, cwd=ws)
    git("config", "pull.rebase", "true", cwd=ws)
    cfg = {"hub": c.hub, "name": a.name, "key": c.key, "kind": a.kind}
    with open(os.path.join(ws, ".git", "swarm.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    info = c.call("/api/join", {"name": a.name, "kind": a.kind})
    prompt = render_protocol(a.name, c.hub)
    with open(os.path.join(ws, ".git", "swarm-prompt.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    print("\nJoined the swarm as %s. Lead: %s%s" % (a.name, info["lead"], " (you)" if info["is_lead"] else ""))
    print("Workspace: %s\n" % ws)
    print("Next - start your agent INSIDE the workspace:")
    print("  cd \"%s\"" % ws)
    print("  a) interactive: open Claude Code / Codex / OpenCode there and send it:")
    print("       Read .git/swarm-prompt.md and follow it. Start now.")
    print("  b) autonomous:  %s run --agent claude      (or codex / opencode / --cmd \"...\")" % sw_command())


def cmd_prompt(a):
    c, _ = client_from(a)
    print(render_protocol(c.need_name(), c.hub))


def cmd_join(a):
    c, cfg = client_from(a)
    info = c.call("/api/join", {"name": c.need_name(), "kind": a.kind or cfg.get("kind") or "agent"})
    print("joined as %s; lead: %s" % (info["name"], info["lead"]))


def cmd_status(a):
    c, _ = client_from(a)
    if c.name:
        c.call("/api/inbox", name=c.name, wait=0, peek=1)  # heartbeat
    print_status(c.call("/api/state"), c.name)


def cmd_inbox(a):
    c, _ = client_from(a)
    print_msgs(c.inbox(0))


def cmd_wait(a):
    c, _ = client_from(a)
    print_msgs(c.inbox(max(1, min(a.timeout, MAX_WAIT))),
               "(no new messages after %ds - continue your work or wait again)" % a.timeout)


def cmd_send(a):
    c, _ = client_from(a)
    m = c.call("/api/send", {"from": c.need_name(), "to": a.to or "*", "text": " ".join(a.text)})
    print("sent #%d" % m["id"])


def cmd_history(a):
    c, _ = client_from(a)
    print_msgs(c.call("/api/messages", since=0, limit=a.n)["messages"], "(no messages)")


def cmd_tasks(a):
    c, _ = client_from(a)
    tasks = c.call("/api/tasks")["tasks"]
    if not a.all:
        tasks = [t for t in tasks if t["status"] != "done"]
    for t in tasks:
        print(fmt_task(t))
    if not tasks:
        print("(no tasks)")


def cmd_task(a):
    c, _ = client_from(a)
    me = c.need_name()
    if a.action == "add":
        if not a.args:
            die('usage: task add "title" [--desc ...] [--to NAME]')
        t = c.call("/api/tasks", {"by": me, "title": " ".join(a.args), "desc": a.desc, "assignee": a.to})
        print("created task #%d" % t["id"])
        return
    if not a.args or not a.args[0].lstrip("#").isdigit():
        die("usage: task %s ID ..." % a.action)
    tid = int(a.args[0].lstrip("#"))
    rest = " ".join(a.args[1:])
    body = {"by": me, "action": a.action, "note": a.note}
    if a.action == "assign":
        body["assignee"] = a.to or rest
    elif a.action == "note":
        body["note"] = a.note or rest
    t = c.call("/api/tasks/%d" % tid, body)
    print(fmt_task(t))


def cmd_goal(a):
    c, _ = client_from(a)
    if not a.text:
        print(c.call("/api/state")["goal"] or "(not set)")
        return
    c.call("/api/goal", {"by": c.need_name(), "text": " ".join(a.text)})
    print("goal updated")


def cmd_role(a):
    c, _ = client_from(a)
    c.call("/api/role", {"by": c.need_name(), "agent": a.agent, "role": " ".join(a.role)})
    print("role of %s set" % a.agent)


def cmd_lead(a):
    c, _ = client_from(a)
    c.call("/api/lead", {"by": c.need_name(), "agent": a.agent})
    print("%s is now lead" % a.agent)


def cmd_chat(a):
    """Terminal chat for humans."""
    c, _ = client_from(a)
    name = a.name or "host"
    last = [c.call("/api/state")["last_msg"]]
    for m in c.call("/api/messages", since=max(0, last[0] - 20))["messages"]:
        print(fmt_msg(m))

    def poll():
        while True:
            time.sleep(2)
            try:
                for m in c.call("/api/messages", since=last[0])["messages"]:
                    last[0] = m["id"]
                    if m["from"] != name:
                        print("\r" + fmt_msg(m))
            except SystemExit:
                pass

    threading.Thread(target=poll, daemon=True).start()
    print("--- chatting as %s. '@agent text' = private message, /status, /quit ---" % name)
    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/status":
            print_status(c.call("/api/state"))
            continue
        to = "*"
        if line.startswith("@") and " " in line:
            to, line = line[1:].split(" ", 1)
        m = c.call("/api/send", {"from": name, "to": to, "text": line})
        last[0] = max(last[0], m["id"])


def build_run_prompt(c, msgs):
    st = c.call("/api/state")
    me = next((x for x in st["agents"] if x["name"] == c.name), {})
    mine = [t for t in st["tasks"] if t["assignee"] == c.name and t["status"] != "done"]
    free = [t for t in st["tasks"] if not t["assignee"] and t["status"] == "open"]
    parts = [render_protocol(c.name, c.hub), "\n---\n# CURRENT SITUATION (autonomous runner mode)\n",
             "You are started repeatedly by a runner. Do ONE meaningful step of work now (pull, act, "
             "commit, push, report with `send`/`task`), then finish your reply. Do NOT call `wait` - "
             "the runner waits for new messages and starts you again. Messages below are already "
             "marked as read.\n",
             "GOAL: %s" % (st["goal"] or "(not set - ask the humans)"),
             "LEAD: %s%s" % (st["lead"], "  <- that's you" if st["lead"] == c.name else ""),
             "YOUR ROLE: %s" % (me.get("role") or "(unassigned)"),
             "AGENTS: " + ", ".join("%s(%s%s)" % (x["name"], x["role"] or "no role",
                                                  "" if x["online"] else ", offline") for x in st["agents"]),
             "\nYOUR TASKS:\n" + ("\n".join(fmt_task(t) for t in mine) or "(none)"),
             "\nUNASSIGNED OPEN TASKS:\n" + ("\n".join(fmt_task(t) for t in free) or "(none)"),
             "\nNEW MESSAGES:\n" + ("\n".join(fmt_msg(m) for m in msgs) or "(none)")]
    return "\n".join(parts), mine


def cmd_run(a):
    c, cfg = client_from(a)
    c.need_name()
    cmd = a.cmd or PRESETS.get(a.agent or "")
    if not cmd:
        die("pass --agent %s or --cmd \"your agent command\"" % "|".join(PRESETS))
    ws = os.path.dirname(os.path.dirname(find_config() or os.path.join(os.getcwd(), ".git", "x")))
    c.call("/api/join", {"name": c.name, "kind": cfg.get("kind") or a.agent or "agent"})
    print("runner: %s in %s\n  command: %s\n  Ctrl+C to stop." % (c.name, ws, cmd))
    runs, first, last_run = 0, True, 0.0
    try:
        while a.max_runs <= 0 or runs < a.max_runs:
            wait = 0 if first else a.idle
            msgs = c.inbox(wait)
            if not first and msgs:
                gap = a.cooldown - (now() - last_run)
                if gap > 0:  # batch messages that arrive in quick succession
                    time.sleep(gap)
                    msgs += c.inbox(0)
            prompt, mine = build_run_prompt(c, msgs)
            if not (first or msgs or mine):
                continue
            first = False
            runs += 1
            print("\n=== run %d at %s (%d new messages, %d own tasks) ===" % (
                runs, time.strftime("%H:%M:%S"), len(msgs), len(mine)), flush=True)
            pf = os.path.join(ws, ".git", "swarm-run-prompt.md")
            with open(pf, "w", encoding="utf-8") as f:
                f.write(prompt)
            if "{prompt_file}" in cmd:
                r = subprocess.run(cmd.replace("{prompt_file}", pf.replace("\\", "/")), shell=True, cwd=ws)
            else:
                r = subprocess.run(cmd, shell=True, cwd=ws, input=prompt.encode("utf-8"))
            last_run = now()
            if r.returncode != 0:
                print("runner: agent exited with code %d, retrying after %ds" % (r.returncode, a.cooldown))
                time.sleep(a.cooldown)
    except KeyboardInterrupt:
        print("\nrunner stopped")


def cmd_protocol(a):
    print(render_protocol(a.name or "<your-name>", a.hub or "<hub>"))


# --------------------------------------------------------------------------- web UI

UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agents Swarm</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2129;--muted:#6b7280;--line:#e3e6ea;--accent:#2563eb;--human:#fff4e0;--sys:#f0f2f5;--ok:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--panel:#1a1d24;--text:#e6e8eb;--muted:#9aa1ad;--line:#2a2f39;--accent:#6ea0ff;--human:#3a2e1a;--sys:#20242c;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{display:grid;grid-template-columns:1fr 340px;gap:12px;height:100vh;padding:12px}
@media (max-width:820px){.wrap{grid-template-columns:1fr;height:auto}#log{height:60vh}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;min-height:0}
.main{display:flex;flex-direction:column;min-height:0}
#log{flex:1;overflow-y:auto;padding-right:4px}
.msg{padding:6px 10px;border-radius:8px;margin:4px 0;white-space:pre-wrap;word-wrap:break-word}
.msg.system{background:var(--sys);color:var(--muted);font-size:13px}.msg.human{background:var(--human)}
.meta{font-size:12px;color:var(--muted)}.from{font-weight:600;color:var(--accent)}
form{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
input,textarea,select,button{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
textarea{flex:1;min-width:200px;resize:vertical}button{background:var(--accent);color:#fff;border:0;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line);padding:2px 6px;font-size:12px}
.side{display:flex;flex-direction:column;gap:12px;overflow-y:auto}
h3{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.agent,.task{padding:5px 0;border-bottom:1px solid var(--line)}.agent:last-child,.task:last-child{border:0}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);margin-right:6px}.dot.on{background:var(--ok)}
.st{font-size:11px;padding:1px 6px;border-radius:9px;background:var(--sys)}code{font-size:12px;word-break:break-all}
#goal{width:100%;min-height:70px}
</style></head><body>
<div class="wrap">
 <div class="card main">
  <div id="log"></div>
  <form id="f"><input id="who" size="8" title="your name in chat"><select id="to"><option value="*">to: everyone</option></select>
   <textarea id="text" rows="2" placeholder="Write to the agents (Enter = send, Shift+Enter = new line)"></textarea><button>Send</button></form>
 </div>
 <div class="side">
  <div class="card"><h3>Goal</h3><textarea id="goal"></textarea><button id="saveGoal" style="margin-top:6px">Save goal</button></div>
  <div class="card"><h3>Agents</h3><div id="agents"></div></div>
  <div class="card"><h3>Tasks</h3><div id="tasks"></div>
   <form id="tf"><input id="tt" placeholder="new task title" style="flex:1"><button>Add</button></form></div>
  <div class="card"><h3>Connect an agent</h3><div class="meta">On the friend's machine:</div>
   <code id="how"></code></div>
 </div>
</div>
<script>
let key=localStorage.getItem('swarmKey')||'',since=0,goalDirty=false,state=null;
const $=id=>document.getElementById(id);
$('who').value=localStorage.getItem('swarmWho')||'host';
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,body){
  for(let i=0;i<2;i++){
    const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Swarm-Key':key},body:body?JSON.stringify(body):undefined});
    if(r.status===401){key=prompt('Swarm key:')||'';localStorage.setItem('swarmKey',key);continue}
    const j=await r.json();if(j.error){alert(j.error);throw j.error}return j}
}
const who=()=>{const w=$('who').value.trim()||'host';localStorage.setItem('swarmWho',w);return w};
function addMsg(m){
  const d=document.createElement('div');d.className='msg '+m.kind;
  const t=new Date(m.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  d.innerHTML='<span class="meta">#'+m.id+' '+t+'</span> <span class="from">'+esc(m.from)+'</span>'+(m.to!=='*'?' <span class="meta">&rarr; '+esc(m.to)+'</span>':'')+'<br>'+esc(m.text);
  const log=$('log'),bottom=log.scrollHeight-log.scrollTop-log.clientHeight<60;log.appendChild(d);if(bottom)log.scrollTop=log.scrollHeight}
function render(s){
  state=s;if(!goalDirty)$('goal').value=s.goal||'';
  $('agents').innerHTML=s.agents.map(a=>'<div class="agent"><span class="dot '+(a.online?'on':'')+'"></span><b>'+esc(a.name)+'</b>'+(a.lead?' &#9733; lead':'')+' <span class="meta">'+esc(a.kind||'')+'</span><br><span class="meta">role: '+esc(a.role||'unassigned')+'</span> <button class="ghost" onclick="setRole(\''+a.name+'\')">role</button>'+(a.lead?'':' <button class="ghost" onclick="setLead(\''+a.name+'\')">make lead</button>')+'</div>').join('')||'<span class="meta">nobody yet</span>';
  const act=s.tasks.filter(t=>t.status!=='done'),done=s.tasks.length-act.length;
  $('tasks').innerHTML=act.map(t=>'<div class="task"><span class="st">'+t.status+'</span> #'+t.id+' '+esc(t.title)+'<br><span class="meta">'+esc(t.assignee||'unassigned')+'</span></div>').join('')+(done?'<div class="meta">'+done+' done</div>':'')||'<span class="meta">no tasks</span>';
  const sel=$('to'),cur=sel.value;sel.innerHTML='<option value="*">to: everyone</option>'+s.agents.map(a=>'<option>'+esc(a.name)+'</option>').join('');sel.value=cur;if(!sel.value)sel.value='*';
  $('how').textContent='curl -o swarm.py http://'+location.host+'/swarm.py\npython swarm.py setup --hub http://'+location.host+' --name THEIR_NAME --key KEY';}
async function poll(){
  try{const r=await api('/api/messages?since='+since);r.messages.forEach(m=>{addMsg(m);since=m.id});render(await api('/api/state'))}catch(e){}
  setTimeout(poll,2000)}
async function setRole(n){const r=prompt('Role for '+n+':');if(r!==null){await api('/api/role',{by:who(),agent:n,role:r});poll1()}}
async function setLead(n){if(confirm('Make '+n+' the lead?')){await api('/api/lead',{by:who(),agent:n});poll1()}}
async function poll1(){render(await api('/api/state'))}
$('f').onsubmit=async e=>{e.preventDefault();const t=$('text').value.trim();if(!t)return;await api('/api/send',{from:who(),to:$('to').value,text:t});$('text').value=''};
$('text').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('f').requestSubmit()}};
$('goal').oninput=()=>goalDirty=true;
$('saveGoal').onclick=async()=>{await api('/api/goal',{by:who(),text:$('goal').value});goalDirty=false;poll1()};
$('tf').onsubmit=async e=>{e.preventDefault();const t=$('tt').value.trim();if(!t)return;await api('/api/tasks',{by:who(),title:t});$('tt').value='';poll1()};
poll();
</script></body></html>
"""


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="swarm.py", description="AgentsSwarm - hub and client for a swarm of AI coding agents.")
    p.add_argument("--hub", help="hub URL, e.g. http://26.1.2.3:8765 (default: from workspace config)")
    p.add_argument("--name", help="your agent/human name (default: from workspace config)")
    p.add_argument("--key", help="swarm key (password)")
    common = argparse.ArgumentParser(add_help=False)  # same options accepted after the command
    common.add_argument("--hub", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--name", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--key", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)

    s = sub.add_parser("hub", help="run the hub server (host only)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--data", default="hub-data", help="where the hub stores state and the git repo")
    s.add_argument("--goal", help="initial goal / task for the swarm")
    s.add_argument("--repo-from", help="initialize the shared repo from this path or git URL")
    s.set_defaults(fn=cmd_hub)

    s = sub.add_parser("setup", help="join a swarm: clone the repo into a workspace and register")
    s.add_argument("--kind", default="agent", help="claude-code / codex / opencode / ...")
    s.add_argument("--dir", help="workspace dir (default: ./workspaces/NAME)")
    s.set_defaults(fn=cmd_setup)

    sub.add_parser("prompt", help="print the protocol prompt for your agent").set_defaults(fn=cmd_prompt)
    sub.add_parser("protocol", help="print the generic protocol").set_defaults(fn=cmd_protocol)
    s = sub.add_parser("join", help="(re)register with the hub")
    s.add_argument("--kind")
    s.set_defaults(fn=cmd_join)
    sub.add_parser("status", help="goal, lead, agents, tasks").set_defaults(fn=cmd_status)
    sub.add_parser("inbox", help="new messages (non-blocking)").set_defaults(fn=cmd_inbox)
    s = sub.add_parser("wait", help="block until new messages arrive")
    s.add_argument("--timeout", type=int, default=60)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("send", help="send a message")
    s.add_argument("text", nargs="+")
    s.add_argument("--to", help="recipient name (default: everyone)")
    s.set_defaults(fn=cmd_send)
    s = sub.add_parser("history", help="recent messages of everyone")
    s.add_argument("-n", type=int, default=30)
    s.set_defaults(fn=cmd_history)
    s = sub.add_parser("tasks", help="task board")
    s.add_argument("--all", action="store_true", help="include done tasks")
    s.set_defaults(fn=cmd_tasks)
    s = sub.add_parser("task", help="add / claim / done / block / open / assign / note")
    s.add_argument("action", choices=["add", "claim", "done", "block", "open", "assign", "note"])
    s.add_argument("args", nargs="*")
    s.add_argument("--desc", default="")
    s.add_argument("--to", help="assignee")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_task)
    s = sub.add_parser("goal", help="show or change the goal")
    s.add_argument("text", nargs="*")
    s.set_defaults(fn=cmd_goal)
    s = sub.add_parser("role", help="set an agent's role")
    s.add_argument("agent")
    s.add_argument("role", nargs="+")
    s.set_defaults(fn=cmd_role)
    s = sub.add_parser("lead", help="make an agent the lead")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_lead)
    sub.add_parser("chat", help="terminal chat for humans").set_defaults(fn=cmd_chat)
    s = sub.add_parser("run", help="autonomous mode: start your agent CLI whenever there is work")
    s.add_argument("--agent", choices=sorted(PRESETS), help="preset command")
    s.add_argument("--cmd", help="custom agent command; prompt goes to stdin, or use {prompt_file}")
    s.add_argument("--idle", type=int, default=600, help="max seconds to wait for messages between runs")
    s.add_argument("--cooldown", type=int, default=20, help="min seconds between runs")
    s.add_argument("--max-runs", type=int, default=0, help="stop after N runs (0 = forever)")
    s.set_defaults(fn=cmd_run)

    a = p.parse_args()
    if not getattr(a, "fn", None):
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
