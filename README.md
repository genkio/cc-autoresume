# cc-autoresume

Watches running Claude Code sessions in tmux and automatically resumes them
after a usage-limit reset, so a `You've hit your session limit · resets 1pm
(Asia/Tokyo)` stop no longer means a session sits idle until you come back to
nudge it by hand.

## How it works

Detection and resume use different channels, each picked for what it does best:

- **Detect (read the transcript JSONL).** A usage-limit stop is written to the
  session log as an assistant entry with top-level `"error": "rate_limit"` and
  `"apiErrorStatus": 429`. That structured marker is authoritative: it never
  fires just because a message mentions the words "session limit". The reset
  time is parsed from the message text (`resets 1pm (Asia/Tokyo)`).
- **Discover + resume (tmux).** Claude runs as a child of a pane's shell, so the
  pane is found by walking the process tree. The matching session file is the
  newest `*.jsonl` under `~/.claude/projects/<munged-cwd>/` (claude does not hold
  the fd open, so `lsof` is only a best-effort hint). Resume is
  `tmux send-keys` into that pane.
- **Pass the limit dialog (tmux).** Hitting the limit first shows a modal
  "What do you want to do?" menu (`Stop and wait for limit to reset` / `Ask
  your admin for more usage`). That menu swallows typed text, so a resume
  message sent into it would be lost. On detection the watcher sends a single
  Enter, confirming the default "Stop and wait" option, which just dismisses
  the menu and leaves the `resets 6pm` message on a normal prompt. At a plain
  prompt (dialog already passed by hand) that Enter is a no-op.
- **Confirm the resume landed.** After sending the message, the transcript is
  re-read to confirm a user turn arrived. If something still swallowed it, the
  send is retried on later ticks (up to 3 attempts) instead of being marked
  done and silently stranding the session.

There is no Claude Code hook that fires on a usage-limit stop, and the status
line's `rate_limits` block is Pro/Max-only and not reachable from a daemon, so
the transcript is the reliable signal.

### Loop

Every `--interval` seconds:

1. Find all live claude panes and their active session files.
2. If a session's last real turn is a `rate_limit` stop, schedule a resume for
   `reset + buffer` and send one Enter to pass the limit dialog.
3. When a scheduled time arrives, re-confirm the session is still blocked, send
   the resume message, then verify it landed in the transcript (retrying next
   tick if not). A session you resumed by hand is dropped.

The `buffer` (default 60s) matters: resuming a few seconds early just trips the
limit again, so it fires slightly after the reset boundary.

## Usage

```sh
make run     # live: append to ~/.cc-autoresume.log and open lnav
make dry     # detect + schedule, never send keys (verbose)
make list    # show live claude panes and their status
```

`make run` is the daemon. It always appends to `~/.cc-autoresume.log` (via
`tee`) and pipes the live stream into `lnav`; if `lnav` is not installed it falls
back to the logfile plus plain terminal output. Stop with Ctrl-C (quitting lnav
alone may leave the watcher running until its next log write).

The targets just wrap the script, which can also be run directly:

```sh
./cc-autoresume.py                 # run in the foreground
./cc-autoresume.py --dry-run -v    # detect + schedule, never send keys
./cc-autoresume.py --list          # show live claude panes and their status
./cc-autoresume.py --check FILE    # print blocked-status of one transcript
```

Options: `--interval` (poll seconds, 30), `--buffer` (seconds past reset, 60),
`--message` (resume text), `--once` (single tick), `-v` (verbose).

## Limitations

- tmux only: sessions outside tmux are not seen.
- Two claude sessions in the same directory can be ambiguous (newest-mtime
  picks one); rare in practice.
- The reset time comes from parsing the message text; an unrecognized format is
  logged and retried rather than guessed.
