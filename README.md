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

There is no Claude Code hook that fires on a usage-limit stop, and the status
line's `rate_limits` block is Pro/Max-only and not reachable from a daemon, so
the transcript is the reliable signal.

### Loop

Every `--interval` seconds:

1. Find all live claude panes and their active session files.
2. If a session's last real turn is a `rate_limit` stop, schedule a resume for
   `reset + buffer`.
3. When a scheduled time arrives, re-confirm the session is still blocked, then
   send the resume message. A session you resumed by hand is dropped.

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
