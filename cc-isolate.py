#!/usr/bin/env python3
"""cc-isolate — keep a Claude Code session from messaging peer sessions.

Hook script, wired in by cc.sh via `claude --settings`. Two modes:
  record  PostToolUse on Agent|SendMessage|ListAgents: remember this session's own
          subagents (name, agentId, ref) in ~/.cache/cc/isolate/<session_id>.json
  guard   PreToolUse on SendMessage: allow only targets recorded above; anything else
          (another terminal's session) gets CC_ISOLATE's decision: ask (default) or deny.
"""
import contextlib, fcntl, json, os, re, sys, time

STATE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                     "cc", "isolate")


def path(sid):
    return os.path.join(STATE, re.sub(r"[^A-Za-z0-9_-]", "_", sid) + ".json")


@contextlib.contextmanager
def locked(sid):
    """Serialize record()'s read-modify-write. Parallel Agent launches fire their
    PostToolUse hooks concurrently; unlocked, the last writer silently dropped the
    others' agentIds and the guard then flagged the session's own subagents."""
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    with open(path(sid) + ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load(sid):
    try:
        with open(path(sid)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {k: set(d.get(k, [])) for k in ("names", "ids", "refs")}


def save(sid, st):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path(sid), os.getpid())
    with open(tmp, "w") as f:
        json.dump({k: sorted(v) for k, v in st.items()}, f)
    os.replace(tmp, path(sid))
    # Prune state of sessions untouched for 60 days (kept long: --continue can revive agents).
    cutoff = time.time() - 60 * 86400
    for n in os.listdir(STATE):
        if n.endswith(".lock"):
            continue                    # may be held right now; harmless to keep
        p = os.path.join(STATE, n)
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
                if p.endswith(".json"):
                    with contextlib.suppress(OSError):
                        os.unlink(p[:-5] + ".json.lock")
        except OSError:
            pass


def record(ev):
    tool, inp, resp = ev.get("tool_name"), ev.get("tool_input") or {}, ev.get("tool_response") or {}
    if not isinstance(resp, dict):
        return
    with locked(ev["session_id"]):
        _record(ev, tool, inp, resp)


def _record(ev, tool, inp, resp):
    st = load(ev["session_id"])
    before = {k: set(v) for k, v in st.items()}
    if tool == "Agent":
        if inp.get("name"):
            st["names"].add(inp["name"])
        if resp.get("agentId"):
            st["ids"].add(resp["agentId"])
    elif tool == "SendMessage":
        pin = resp.get("pin") or {}
        # Only learn from a send the guard let through to a known agent.
        if pin.get("id") in st["ids"] or pin.get("name") in st["names"]:
            for k, v in (("ids", pin.get("id")), ("names", pin.get("name")), ("refs", pin.get("ref"))):
                if v:
                    st[k].add(v)
    elif tool == "ListAgents":
        for sec in resp.get("sections") or []:
            if sec.get("kind") != "subagents":
                continue
            for row in sec.get("rows") or []:
                if row.get("id") in st["ids"] or row.get("name") in st["names"]:
                    if row.get("ref"):
                        st["refs"].add(row["ref"])
                    if row.get("id"):
                        st["ids"].add(row["id"])
    if st != before:
        save(ev["session_id"], st)


def guard(ev):
    inp = ev.get("tool_input") or {}
    to = str(inp.get("to") or inp.get("recipient") or "").strip()
    st = load(ev["session_id"])
    m = re.fullmatch(r"(.*?)\s*\[([0-9a-fA-F]+)\]", to)
    name, ref = (m.group(1), m.group(2)) if m else (to, None)
    if ref:
        ok = ref in st["refs"]
    else:
        ok = name in st["names"] or name in st["ids"]
    if ok:
        return
    decision = os.environ.get("CC_ISOLATE", "ask")
    decision = decision if decision in ("ask", "deny") else "ask"
    reason = (f"cc-isolate: '{to}' is not a subagent this session started. Other sessions "
              "belong to the user's other terminals: do not message them unless the user "
              "asks. (A subagent addressed as 'name [ref]' before its ref is known: use its "
              "bare name or agentId.)")
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": decision,
                                      "permissionDecisionReason": reason}}, sys.stdout)


def main():
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return
    if not ev.get("session_id"):
        return
    try:
        {"record": record, "guard": guard}[sys.argv[1]](ev)
    except Exception as e:  # a crashing hook fails open; the guard must fail to "ask"
        if sys.argv[1] == "guard":
            json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                       "permissionDecision": "ask",
                       "permissionDecisionReason": f"cc-isolate error: {e}"}}, sys.stdout)


if __name__ == "__main__":
    main()
