#!/usr/bin/env python3
"""Agent Deck: live dashboard + macOS notifications for Claude Code sessions.

Tails ~/.agent-deck/events.jsonl (written by hook.sh), keeps one state per
session, serves the dashboard on http://localhost:7777 and notifies when a
session starts waiting for input or finishes its turn.
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.expanduser("~/.agent-deck")
EVENTS = os.path.join(DIR, "events.jsonl")
PORT = int(os.environ.get("AGENT_DECK_PORT", "7777"))
MAX_EVENTS_BYTES = 5 * 1024 * 1024
STALE_SECS = 12 * 3600
SUBAGENT_QUIET_SECS = 15 * 60
IDLE_SUBAGENT_SECS = 60 * 60

sessions = {}
lock = threading.Lock()
version = 0
changed = threading.Condition(lock)
started = time.time()


def notify(title, message, sound):
    script = 'display notification %s with title %s sound name %s' % (
        json.dumps(message[:200]), json.dumps(title), json.dumps(sound))
    subprocess.Popen(["/usr/bin/osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


ACCOUNTS = {".claude": "Work", ".claude-personal": "Personal"}


last_client = 0.0
OPENED = os.path.join(DIR, "last_open")
open_pending = False


def open_dashboard():
    """Open the dashboard once, after giving an existing tab time to reconnect."""
    global open_pending
    if open_pending:
        return
    open_pending = True
    threading.Timer(6, open_if_no_tab).start()


def open_if_no_tab():
    global open_pending
    open_pending = False
    now = time.time()
    try:
        # Persisted so a restarted server doesn't forget it just opened a tab.
        last_open = os.path.getmtime(OPENED)
    except OSError:
        last_open = 0
    if now - last_client < 40 or now - last_open < 60:
        return
    open(OPENED, "w").close()
    subprocess.Popen(["/usr/bin/open", "http://localhost:%d" % PORT],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


DISMISSED = os.path.join(DIR, "dismissed.json")
try:
    dismissed = json.load(open(DISMISSED))
except (OSError, ValueError):
    dismissed = {}


def save_dismissed():
    cutoff = time.time() - 2 * STALE_SECS
    for k in [k for k, t in dismissed.items() if t < cutoff]:
        del dismissed[k]
    with open(DISMISSED, "w") as f:
        json.dump(dismissed, f)


def account_of(cfg):
    # No CLAUDE_CONFIG_DIR means the default ~/.claude, the work account.
    name = os.path.basename(cfg.rstrip("/")) if cfg else ".claude"
    return ACCOUNTS.get(name, name.lstrip(".") or cfg)


def apply(ev, live):
    """Fold one hook event into the session table. Returns True if it changed."""
    sid = ev.get("session_id")
    name = ev.get("hook_event_name")
    if not sid or not name:
        return False
    ts = ev.get("_ts") or time.time()
    # Dismissed cards stay gone on replay; only activity after the dismissal revives them.
    for key in (sid, "%s:%s" % (sid, ev.get("agent_id"))):
        if ts <= dismissed.get(key, 0):
            return False
    if ev.get("agent_id"):
        return apply_subagent(ev, live)
    s = sessions.get(sid)
    if s is None:
        if name == "SessionEnd":
            return False
        s = sessions[sid] = {
            "id": sid, "state": "idle", "since": ts, "cwd": ev.get("cwd", ""),
            "account": account_of(ev.get("_config")), "iterm": ev.get("_iterm", ""),
            "term": ev.get("_term", ""), "prompt": "", "detail": "", "output": "",
            "tool": "", "seen": True, "started": ts,
            "app": "", "root": "", "ide_root": "", "branch": "",
        }
    s["last"] = ts
    if ev.get("transcript_path"):
        s["transcript"] = ev["transcript_path"]
    if ev.get("cwd"):
        s["cwd"] = ev["cwd"]
    if ev.get("_iterm"):
        s["iterm"] = ev["_iterm"]
    # The hook only gathers project/app context on these events; take it whole,
    # empty values included, so a stale root or branch doesn't linger.
    if name in ("SessionStart", "UserPromptSubmit") and "_app" in ev:
        for key in ("app", "root", "ide_root", "branch"):
            s[key] = ev.get("_" + key, "")
    if ev.get("_app") and not ev.get("_iterm") and "iTerm" not in ev["_app"]:
        s["iterm"] = ""

    def go(state, detail=None):
        if s["state"] != state:
            s["since"] = ts
            s["seen"] = state not in ("waiting", "done", "error")
        s["state"] = state
        if detail is not None:
            s["detail"] = detail

    where = s.get("root") or s["cwd"]
    project = os.path.basename(where.rstrip("/")) or where
    if name == "SessionStart":
        go("idle", "")
        if live:
            open_dashboard()
    elif name == "UserPromptSubmit":
        prompt = ev.get("prompt") or ""
        # A subagent's report arrives as a synthetic prompt; keep the user's own.
        if not prompt.lstrip().startswith(("<agent-message", "<task-notification")):
            s["prompt"] = prompt[:300]
        s["output"] = ""
        go("working", "")
    elif name == "PreToolUse":
        s["tool"] = ev.get("tool_name", "")
        go("working", describe_tool(ev))
    elif name in ("PostToolUse", "PostToolUseFailure"):
        go("working")
    elif name == "PermissionRequest":
        go("waiting", "Permission: " + describe_tool(ev))
        if live:
            notify("Waiting for input · " + project, s["detail"], "Funk")
    elif name == "Notification":
        kind = ev.get("notification_type", "")
        msg = ev.get("message", "") or "Needs your input"
        # The idle reminder after a finished turn shouldn't turn green back to amber.
        if kind == "idle_prompt" and s["state"] in ("done", "idle"):
            return True
        was_waiting = s["state"] == "waiting"
        go("waiting", msg)
        if live and not was_waiting:
            notify("Waiting for input · " + project, msg, "Funk")
    elif name == "Stop":
        s["output"] = (ev.get("last_assistant_message") or "")[:2000]
        s["stopped_at"] = ts
        s["stops"] = (s.get("stops") or [])[-49:] + [ts]
        go("done", "Output ready")
        # The main agent's turn ended after these subagents delivered: they're idle now.
        for a in sessions.values():
            if a.get("parent") == sid and a["state"] == "done":
                rest_subagent(a, ts)
        if live:
            notify("Output ready · " + project,
                   s["output"][:160] or "Turn finished", "Glass")
    elif name == "StopFailure":
        go("error", ev.get("error") or ev.get("message") or "Turn failed")
        if live:
            notify("Failed · " + project, s["detail"], "Basso")
    elif name == "SessionEnd":
        sessions.pop(sid, None)
        for key in [k for k, v in sessions.items() if v.get("parent") == sid]:
            sessions.pop(key)
    return True


def subagent_dir(parent):
    """<session>.jsonl sits beside <session>/subagents/, which holds each subagent's files."""
    t = parent.get("transcript") or ""
    return os.path.join(t[:-len(".jsonl")], "subagents") if t.endswith(".jsonl") else ""


def subagent_task(parent, aid):
    try:
        with open(os.path.join(subagent_dir(parent), "agent-%s.meta.json" % aid)) as f:
            return json.load(f).get("description", "")
    except (OSError, ValueError):
        return ""


def apply_subagent(ev, live):
    """Subagents run inside a session but get their own card, keyed session:agent."""
    sid, aid, name = ev["session_id"], ev["agent_id"], ev.get("hook_event_name")
    ts = ev.get("_ts") or time.time()
    parent = sessions.get(sid)
    key = "%s:%s" % (sid, aid)
    a = sessions.get(key)
    if a is None:
        if parent is None or name == "SubagentStop":
            return False
        a = sessions[key] = dict(
            parent, id=key, parent=sid, agent_id=aid, agent_type=ev.get("agent_type", ""),
            state="working", since=ts, started=ts, prompt="", detail="", output="",
            seen=True, task="")
    a["last"] = ts
    if not a["task"] and parent:
        a["task"] = subagent_task(parent, aid)
    label = a["task"] or a["agent_type"] or "Subagent"

    def go(state, detail=None):
        if a["state"] != state:
            a["since"] = ts
            a["seen"] = state not in ("waiting", "done", "error")
        a["state"] = state
        if detail is not None:
            a["detail"] = detail

    if name == "PreToolUse":
        go("working", describe_tool(ev))
    elif name in ("PostToolUse", "PostToolUseFailure"):
        go("working")
    elif name in ("PermissionRequest", "Notification"):
        msg = ("Permission: " + describe_tool(ev)) if name == "PermissionRequest" \
            else (ev.get("message") or "Needs your input")
        was_waiting = a["state"] == "waiting"
        go("waiting", msg)
        if live and not was_waiting:
            notify("Subagent waiting · " + label, msg, "Funk")
    elif name == "SubagentStop":
        finish_subagent(a, (ev.get("last_assistant_message") or ""), ts, live)
    return True


def rest_subagent(a, ts, detail="Finished · result delivered to the main agent"):
    a.update(state="idle", since=ts, seen=True, detail=detail)


def finish_subagent(a, output, ts, live):
    if a["state"] in ("done", "idle"):
        return
    a["output"] = output[:2000]
    parent = sessions.get(a["parent"]) or {}
    # Seen late (e.g. on replay) after the main agent already wrapped up: straight to idle.
    if parent.get("stopped_at", 0) >= a["last"]:
        # Idle since the main agent's first stop after this subagent's last activity,
        # so a restart doesn't reset the idle clock.
        rest_subagent(a, min(t for t in parent.get("stops") or [parent["stopped_at"]] if t >= a["last"]))
        return
    a.update(state="done", since=ts, seen=False, detail="Output ready")
    if live:
        notify("Subagent done · " + (a["task"] or a["agent_type"] or "subagent"),
               a["output"][:160] or "Finished", "Glass")


def subagent_result(a):
    """Final text if the subagent's transcript ends on a finished turn, else None."""
    path = os.path.join(subagent_dir(sessions.get(a["parent"], a)), "agent-%s.jsonl" % a["agent_id"])
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 256 * 1024))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    # Finished = the last turn ends the conversation (end_turn), or a background
    # subagent delivered its report through SubagentHandback.
    rows = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") in ("user", "assistant"):
            rows.append(d)
    for d in reversed(rows):
        m = d.get("message") or {}
        c = m.get("content") if isinstance(m.get("content"), list) else []
        if d["type"] == "assistant":
            for b in c:
                if b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
                    return str((b.get("input") or {}).get("message", ""))
            if m.get("stop_reason") == "end_turn":
                return "\n".join(b.get("text", "") for b in c if b.get("type") == "text")
            if m.get("stop_reason") == "tool_use" and any(b.get("type") == "tool_use" for b in c):
                return None
        else:
            r = d.get("toolUseResult")
            # Keep looking back only past the handback's own confirmation.
            if not (isinstance(r, dict) and "delivered to your caller" in str(r.get("message", ""))):
                return None
    return None


def watch_subagents():
    # Without a SubagentStop hook, a quiet subagent's transcript tells us it finished.
    while True:
        time.sleep(5)
        with lock:
            now = time.time()
            dirty = False
            for a in list(sessions.values()):
                if a.get("parent") and a["state"] == "working" and now - a["last"] > 10:
                    out = subagent_result(a)
                    if out is not None:
                        # Only notify for subagents active since this server started.
                        finish_subagent(a, out, now, a["last"] >= started - 60)
                        dirty = True
                    elif now - a["last"] > SUBAGENT_QUIET_SECS:
                        # Stopped without a handback (cancelled, or the session died).
                        p = sessions.get(a["parent"]) or {}
                        if p.get("stopped_at", 0) >= a["last"] or not p:
                            rest_subagent(a, now, "Stopped · no activity")
                            dirty = True
                if a.get("parent") and not a.get("task"):
                    a["task"] = subagent_task(sessions.get(a["parent"], a), a["agent_id"])
                    dirty |= bool(a["task"])
            if dirty:
                bump()


def describe_tool(ev):
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input") or {}
    if not isinstance(inp, dict):
        return tool
    arg = (inp.get("description") or inp.get("command") or inp.get("file_path")
           or inp.get("pattern") or inp.get("url") or inp.get("prompt") or "")
    return ("%s: %s" % (tool, arg) if arg else tool)[:200]


def bump():
    global version
    version += 1
    changed.notify_all()


def tail():
    pos = 0
    while True:
        try:
            size = os.path.getsize(EVENTS)
        except OSError:
            size = 0
        if size < pos:
            pos = 0
        if size > pos:
            with open(EVENTS, "r", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
            # Only consume complete lines; a hook may be mid-write.
            end = chunk.rfind("\n") + 1
            pos += len(chunk[:end].encode())
            with lock:
                dirty = False
                for line in chunk[:end].splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    # Replayed history updates state silently; events from just
                    # before startup (the hook that launched us) still count.
                    fresh = (ev.get("_ts") or 0) >= started - 5
                    dirty |= apply(ev, fresh)
                if dirty:
                    bump()
        if pos > MAX_EVENTS_BYTES:
            compact()
            pos = 0
        time.sleep(0.4)


def compact():
    """Truncate the event log, persisting current state as synthetic events."""
    with lock:
        snap = json.dumps(list(sessions.values()))
        open(os.path.join(DIR, "state.json"), "w").write(snap)
        open(EVENTS, "w").close()


def load_snapshot():
    try:
        for s in json.load(open(os.path.join(DIR, "state.json"))):
            sessions[s["id"]] = s
    except (OSError, ValueError):
        pass


def prune():
    while True:
        time.sleep(60)
        with lock:
            now = time.time()
            for sid in [k for k, s in sessions.items()
                        if now - s.get("last", now) > STALE_SECS
                        or (s.get("parent") and s["state"] == "idle" and now - s["since"] > IDLE_SUBAGENT_SECS)]:
                sessions.pop(sid)
            bump()


def focus_app(app, project_dir):
    """Bring the host app forward; for a JetBrains IDE, its window for the project."""
    if not app.endswith(".app") or not os.path.isdir(app):
        return
    # Opening an already-open project in its IDE just raises that window.
    jetbrains = os.path.exists(os.path.join(app, "Contents/bin/idea.properties"))
    cmd = ["/usr/bin/open", "-a", app] + ([project_dir] if jetbrains and project_dir else [])
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def focus_iterm(iterm_id):
    uuid = iterm_id.split(":", 1)[-1]
    script = '''
tell application "iTerm2"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if unique id of s is "%s" then
          select w
          select t
          select s
          return
        end if
      end repeat
    end repeat
  end repeat
end tell''' % uuid.replace('"', "")
    subprocess.Popen(["/usr/bin/osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def snapshot(self):
        return json.dumps({"version": version, "now": time.time(),
                           "sessions": sorted(sessions.values(),
                                              key=lambda s: -s.get("last", 0))})

    def do_GET(self):
        if self.path == "/":
            self.send(200, open(os.path.join(DIR, "index.html"), "rb").read(),
                      "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            # Long-poll: wait for the version to move past what the client has.
            try:
                have = int(self.path.split("v=", 1)[1])
            except (IndexError, ValueError):
                have = -1
            global last_client
            last_client = time.time()
            with lock:
                changed.wait_for(lambda: version != have, timeout=25)
                body = self.snapshot()
            last_client = time.time()
            self.send(200, body)
        else:
            self.send(404, "{}")

    def do_POST(self):
        # Only the dashboard page (same origin) may trigger actions.
        origin = self.headers.get("Origin", "")
        if origin and origin != "http://localhost:%d" % PORT:
            return self.send(403, "{}")
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "api":
            return self.send(404, "{}")
        action, sid = parts[1], parts[2]
        with lock:
            s = sessions.get(sid)
            if s is None:
                return self.send(404, "{}")
            if action == "seen":
                s["seen"] = True
            elif action == "dismiss":
                sessions.pop(sid)
                for key in [k for k, v in sessions.items() if v.get("parent") == sid]:
                    sessions.pop(key)
                dismissed[sid] = time.time()
                save_dismissed()
            elif action == "focus" and s.get("iterm"):
                s["seen"] = True
                focus_iterm(s["iterm"])
            elif action == "focus" and s.get("app"):
                s["seen"] = True
                focus_app(s["app"], s.get("ide_root"))
            bump()
        self.send(200, "{}")


def main():
    os.makedirs(DIR, exist_ok=True)
    load_snapshot()
    threading.Thread(target=tail, daemon=True).start()
    threading.Thread(target=prune, daemon=True).start()
    threading.Thread(target=watch_subagents, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
