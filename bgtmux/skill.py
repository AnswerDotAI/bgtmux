"""Use tmux-backed background terminal sessions from Solveit. Useful to have a persistent terminal session that both you and the user can inspect and edit, and that you can send input to from Solveit.


`bgtmux` is for long-running or interactive shell work where a normal tool call
would block or lose context. It starts named tmux sessions, sends input to them,
and captures snapshots of the terminal so both Solveit and the user can inspect
the same state.

The key model:

- tmux owns the process, terminal, and scrollback
- `bgtmux` sends keystrokes/input into that tmux pane
- `bgtmux` reads snapshots from tmux; it does not maintain a private output stream
- `poll()` waits for the pane to visibly change since the last observed snapshot

This makes it useful for commands like test runners, dev servers, REPLs,
Stripe webhook listeners, training jobs, and other processes that should keep
running outside the notebook/kernel.

## Getting Started

Create or reuse a named session, send commands, then poll or display output:

```python
sid = "solveit-test"
start_session(sid=sid)
send(sid, "cd ~/aai-ws/solveit\n")
send(sid, "nbdev-test\n", yield_time_ms=1000)
poll(sid, yield_time_ms=1000)
```

The user can attach to the same session at any time:

```bash
tmux attach -t solveit-test
```

## Polling Mental Model

`poll()` is not just `sleep()` plus capture. It waits up to `yield_time_ms`
for the pane to look different from the last snapshot observed by `bgtmux`.
If output has already changed, it returns immediately. If nothing changes
before the timeout, it returns the latest snapshot anyway.

So the usual workflow is:

1. `send(...)` input
2. inspect the returned snapshot
3. `poll(...)` while more output is expected
4. stop when the output is sufficient or the process exits

## Manual Inspection

Use `display(sid)` for a readable recent snapshot, `capture_range(sid, ...)`
for precise scrollback ranges, and `info(sid)` to check whether the pane is
running, dead, or waiting at a shell prompt.

"""

from bgtmux import start_session, send, poll, display, capture_range, info, interrupt, send_keys, close, managed_sessions, list_sessions
from pyskills.core import allow

__all__ = ['start_session', 'send', 'poll', 'display', 'capture_range', 'info', 'interrupt', 'send_keys', 'close', 'managed_sessions', 'list_sessions']

allow(start_session, send, poll, display, capture_range, info, interrupt, send_keys, close, managed_sessions, list_sessions)