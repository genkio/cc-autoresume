#!/usr/bin/env python3
"""Watch running Claude Code tmux sessions and auto-resume them after a
usage-limit ("You've hit your session limit") reset.

Manual foreground daemon: `./cc-autoresume.py`.

Detection reads the session JSONL, which is authoritative: a usage-limit stop
is written as an assistant entry with top-level `"error": "rate_limit"` and
`"apiErrorStatus": 429`. Discovery of which sessions are live, and the resume
keystrokes, go through tmux.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

log = logging.getLogger("autoresume")

PROJECTS = Path.home() / ".claude" / "projects"

META_TYPES = {
    "mode", "permission-mode", "file-history-snapshot",
    "ai-title", "system", "summary", "attachment",
}

DEFAULT_MESSAGE = "Session limit has reset, please continue where we left off."


def run(cmd, ok_fail=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        if not ok_fail:
            log.error("missing binary: %s", cmd[0])
        return ""
    if r.returncode != 0 and not ok_fail:
        log.debug("cmd failed %s: %s", cmd, r.stderr.strip())
    return r.stdout


def tmux_panes():
    fmt = "#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_current_command}"
    out = run(["tmux", "list-panes", "-a", "-F", fmt], ok_fail=True)
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pane_id, pane_pid, cwd, cmd = parts
        panes.append({"id": pane_id, "pid": pane_pid, "cwd": cwd, "cmd": cmd})
    return panes


def ps_procs():
    out = run(["ps", "-axo", "pid=,ppid=,command="])
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = parts
        procs[pid] = (ppid, cmd)
    return procs


def is_claude(cmd):
    return re.search(r"(^|/)claude(\s|$)", cmd) is not None


def claude_panes():
    """pane_id -> {pane fields, claude_pid} for panes whose process tree runs claude."""
    by_pid = {p["pid"]: p for p in tmux_panes()}
    procs = ps_procs()
    found = {}
    for pid, (ppid, cmd) in procs.items():
        if not is_claude(cmd):
            continue
        cur, seen = pid, set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur in by_pid:
                found[by_pid[cur]["id"]] = {**by_pid[cur], "claude_pid": pid}
                break
            cur = procs.get(cur, (None, None))[0]
    return found


def project_dir(cwd):
    d = PROJECTS / cwd.replace("/", "-")
    if d.is_dir():
        return d
    alt = PROJECTS / re.sub(r"[/.]", "-", cwd)
    return alt if alt.is_dir() else None


def newest_jsonl(d):
    files = list(d.glob("*.jsonl"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def lsof_jsonl(pid):
    out = run(["lsof", "-a", "-p", str(pid), "-Fn"], ok_fail=True)
    for line in out.splitlines():
        if line.startswith("n") and "/projects/" in line and line.endswith(".jsonl"):
            return Path(line[1:])
    return None


def active_session_file(cwd, claude_pid):
    # claude usually does not hold the fd open, so newest-mtime is the workhorse
    f = lsof_jsonl(claude_pid)
    if f and f.exists():
        return f
    d = project_dir(cwd)
    return newest_jsonl(d) if d else None


def read_last_lines(path, max_bytes=262144):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if start > 0:  # drop the partial first line we seeked into
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else ""
    return [l for l in text.splitlines() if l.strip()]


def msg_text(obj):
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def is_synthetic_user(obj):
    """User turns that are not a human resuming: tool results, task wakeups, slash cmds."""
    if obj.get("type") != "user":
        return False
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list) and content and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return True
    t = msg_text(obj)
    return "<task-notification>" in t or "<local-command" in t


def session_status(path):
    """Inspect the transcript tail. Blocked == the last real turn is a rate_limit stop."""
    try:
        lines = read_last_lines(path)
    except OSError as e:
        log.debug("read fail %s: %s", path, e)
        return {"blocked": False}
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t in META_TYPES or (t == "user" and is_synthetic_user(obj)):
            continue
        if t not in ("assistant", "user"):
            continue
        if t == "assistant" and obj.get("error") == "rate_limit":
            return {
                "blocked": True,
                "text": msg_text(obj),
                "session_id": obj.get("sessionId"),
                "cwd": obj.get("cwd"),
                "branch": obj.get("gitBranch"),
                "ts": obj.get("timestamp"),
            }
        return {"blocked": False, "session_id": obj.get("sessionId"),
                "branch": obj.get("gitBranch")}
    return {"blocked": False}


def parse_ts(s):
    """ISO timestamp from a transcript entry -> aware datetime (None on failure)."""
    if not isinstance(s, str):
        return None
    norm = re.sub(r"\.\d+", "", s.strip().replace("Z", "+00:00"))  # 3.9-safe
    try:
        return datetime.fromisoformat(norm)
    except ValueError:
        return None


def parse_reset(text, ref):
    """'... resets 1pm (Asia/Tokyo)' / '... resets 3:45pm' -> the first such time
    after `ref` (the moment the limit was hit), as an aware datetime."""
    m = re.search(
        r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b(?:\s*\(([^)]+)\))?",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    minute = int(m.group(2) or 0)
    tz = ref.tzinfo
    if m.group(4) and ZoneInfo is not None:
        try:
            tz = ZoneInfo(m.group(4).strip())
        except Exception:
            log.warning("unknown tz %r, using local", m.group(4))
    ref_tz = ref.astimezone(tz)
    target = ref_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= ref_tz:  # the named time has already passed today, so it means tomorrow
        target += timedelta(days=1)
    return target


def send_resume(pane_id, message, dry):
    if dry:
        log.info("[dry-run] would send-keys -> %s: %r", pane_id, message)
        return
    run(["tmux", "send-keys", "-t", pane_id, "-l", message])
    time.sleep(0.4)  # let the TUI register the text before submitting
    run(["tmux", "send-keys", "-t", pane_id, "Enter"])
    log.info("sent resume -> %s", pane_id)


def scan(now):
    rows = []
    for pane_id, info in claude_panes().items():
        sf = active_session_file(info["cwd"], info["claude_pid"])
        if sf:
            rows.append((pane_id, info, session_status(sf), sf))
    return rows


def tick(state, args, now):
    pending, done = state["pending"], state["done"]
    live = set()
    for pane_id, info, st, sf in scan(now):
        live.add(pane_id)
        if st.get("blocked"):
            reset = parse_reset(st["text"], parse_ts(st.get("ts")) or now)
            epi = (st.get("session_id"), reset.isoformat() if reset else st.get("ts"))
            if epi in done or (pane_id in pending and pending[pane_id]["epi"] == epi):
                continue
            if reset is None:
                log.warning("%s blocked, can't parse reset from %r; retrying", pane_id, st["text"])
                continue
            fire = reset + timedelta(seconds=args.buffer)
            pending[pane_id] = {"epi": epi, "reset": reset, "fire": fire,
                                "branch": st.get("branch"), "file": str(sf)}
            log.info("DETECTED limit: %s [%s] resets %s -> resume %s", pane_id,
                     st.get("branch"), reset.isoformat(timespec="minutes"),
                     fire.isoformat(timespec="minutes"))
        elif pane_id in pending:
            log.info("%s resolved externally, cancelling", pane_id)
            del pending[pane_id]

    for pane_id, p in list(pending.items()):
        if pane_id not in live:
            log.info("%s gone, dropping pending", pane_id)
            del pending[pane_id]
            continue
        if now >= p["fire"]:
            sf = Path(p["file"])
            if not (session_status(sf).get("blocked") if sf.exists() else False):
                log.info("%s no longer blocked at fire time, skipping", pane_id)
                del pending[pane_id]
                continue
            log.info("RESUMING %s [%s]", pane_id, p["branch"])
            send_resume(pane_id, args.message, args.dry_run)
            done.add(p["epi"])
            del pending[pane_id]


def cmd_list(now):
    rows = scan(now)
    if not rows:
        print("no running claude tmux panes found")
        return
    for pane_id, info, st, sf in rows:
        if st.get("blocked"):
            reset = parse_reset(st["text"], now)
            state = f"BLOCKED resets={reset.isoformat(timespec='minutes') if reset else '?'}"
        else:
            state = "active"
        print(f"{pane_id:5} {st.get('branch') or '-':14} {state:34} {info['cwd']}")
        print(f"      session: {sf.name}")


def cmd_check(path, now):
    st = session_status(Path(path))
    print(json.dumps(st, indent=2))
    if st.get("blocked"):
        r = parse_reset(st["text"], parse_ts(st.get("ts")) or now)
        print("parsed reset:", r.isoformat() if r else "PARSE FAILED")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=30, help="poll seconds (default 30)")
    ap.add_argument("--buffer", type=int, default=60,
                    help="seconds to wait past reset before resuming (default 60)")
    ap.add_argument("--message", default=DEFAULT_MESSAGE, help="text sent to resume a session")
    ap.add_argument("--dry-run", action="store_true", help="detect and schedule but never send keys")
    ap.add_argument("--once", action="store_true", help="run a single scan tick and exit")
    ap.add_argument("--list", action="store_true", help="list claude panes + status, then exit")
    ap.add_argument("--check", metavar="JSONL", help="print blocked-status of one transcript, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout,
    )

    now = datetime.now().astimezone()
    if args.check:
        return cmd_check(args.check, now)
    if args.list:
        return cmd_list(now)

    state = {"pending": {}, "done": set()}
    log.info("cc-autoresume started (%s, interval=%ss, buffer=%ss)",
             "dry-run" if args.dry_run else "live", args.interval, args.buffer)
    while True:
        try:
            tick(state, args, datetime.now().astimezone())
        except Exception:
            log.exception("tick error")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
