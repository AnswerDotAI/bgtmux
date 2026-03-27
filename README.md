# bgtmux

`bgtmux` runs background terminal sessions by driving `tmux` entirely through `tmux` CLI commands.

It is aimed at the same use case as an LLM-style background terminal controller:

1. start a terminal-backed process in the background
2. send more input later
3. wait a bounded amount of time
4. inspect a terminal snapshot
5. move around in scrollback by line range

The important mental model is:

- tmux owns the terminal and its scrollback
- `bgtmux` asks tmux for snapshots of that state
- interaction is based on pane history and visible changes, not a private Python-side output stream

## How It Works

Each `bgtmux` session is a normal tmux session.

`bgtmux` uses the user's normal tmux server and respects their existing tmux config. The session name is the `sid`, and generated ids are prefixed `bgtmux-`. You can still attach manually:

```bash
tmux attach -t <sid>
```

The library interacts with the pane only by shelling out to tmux commands such as:

- `new-session`
- `load-buffer`
- `paste-buffer`
- `send-keys`
- `capture-pane`
- `display-message`
- `kill-session`

There is no PTY reader thread inside `bgtmux`. tmux owns the terminal and its scrollback; Python just asks tmux for snapshots.

## Why The Default View Follows The Cursor

tmux scrollback is screen-based, not append-only-byte-stream-based.

If you always capture the "last N screen lines" from the bottom of the pane, short REPL sessions mostly return blank lines because the cursor starts near the top of a fresh pane.

So `bgtmux` defines the default "latest output" window as the last `N` lines ending at the current cursor line, not the physical bottom of the pane. That makes short interactions with shells and REPLs behave much more like the built-in background sessions we discussed.

Created sessions also enable tmux `remain-on-exit`, so finished commands stay inspectable instead of disappearing immediately.

## Sending Input

`send()` pastes exact text into the pane using tmux buffers:

1. `tmux load-buffer -b <temp-buffer> -`
2. `tmux paste-buffer -d -b <temp-buffer> -t <pane>`

This is deliberate:

- it preserves embedded newlines
- it works well for REPL input
- it avoids the key-name parsing quirks of `send-keys`

For control keys such as `Ctrl-C`, use `send_keys()` or `interrupt()`.

## Polling And Waiting

There is no event subscription in tmux for "your command finished".

`poll(yield_time_ms=...)` works by:

1. capturing a baseline bottom-following snapshot
2. sleeping in small intervals
3. recapturing until the snapshot changes, the pane dies, or the timeout expires
4. returning the current bottom-following snapshot

This means the semantics are:

- return as soon as tmux visibly changes
- not return only when a command is semantically complete

So if the pane echoes your input before the program prints its response, that echo may be the first visible change. This matches the practical behavior of terminal polling systems.

## Scrollback Model

`bgtmux` exposes scrollback as absolute line ranges.

Line numbering is:

- zero-based
- counted from the top of captured pane history
- capped at the current cursor line

That means trailing blank rows below the cursor are not part of the default transcript view.

Useful operations:

- `display()` shows the latest `default_lines`
- `capture_range(start, end)` returns an exact absolute slice
- `scroll_to(start, lines=...)` jumps the viewport to a range
- `scroll(delta)` moves the current viewport by line count
- `view()` re-renders the current viewport

Positive scroll deltas move toward newer output. Negative deltas move toward older output.

## API

The primary interface is functional. For session-oriented calls, `sid` is optional:

- if `sid` is omitted, `bgtmux` uses the current tmux session
- for pane-driving calls such as `display()`, `poll()`, `send()`, and `info()`, that means the current pane in the current session
- for managed `bgtmux-*` sessions, the stored managed pane still wins

The OO wrapper is a thin convenience layer on top.

```python
from bgtmux import poll, send, start_session

sid = start_session(["ipython", "--simple-prompt", "--no-confirm-exit", "--no-banner"])
startup = poll(sid, yield_time_ms=5000)
print(startup.text)

out = send(sid, "2+2\n", yield_time_ms=500)
print(out.text)
```

For ambient tmux inspection, you can omit `sid` and browse panes directly:

```python
from bgtmux import display, list_panes, pane

print(display(lines=20).text)   # current pane in the current session
print([o.pane_id for o in list_panes()])
print(pane("%1", lines=20).text)
```

If you prefer an object:

```python
from bgtmux import TmuxSession

with TmuxSession.start(["ipython", "--simple-prompt", "--no-confirm-exit", "--no-banner"]) as sess:
    print(sess.attach_command)
    print(sess.poll(yield_time_ms=5000).text)
```

For searching across captured panes:

```python
from bgtmux import search, search_sessions

print(search("Traceback"))          # current session
print(search_sessions("CUDA"))      # all tmux sessions
```

### `start_session(...) -> sid`

Starts a detached tmux session and returns its tmux session name.

Important arguments:

- `cmd`: command string or argv list; `None` starts the default shell
- `sid` / `session_name`: explicit tmux session name; default is `bgtmux-<uuid>`
- `cwd`: start directory
- `env`: environment passed via tmux `-e`
- `width`, `height`: pane size hints for deterministic captures
- `remain_on_exit`: keep finished panes inspectable after the command exits

### `send(sid, chars, yield_time_ms=0, poll_interval_ms=50, lines=80)`

Paste text into the pane, then wait and return the latest bottom-following capture.

### `send_keys(sid, *keys, yield_time_ms=0, ...)`

Send tmux key names such as `C-c`, `Enter`, `Up`, or `Down`.

### `interrupt(sid)`

Convenience wrapper for `send_keys("C-c")`.

### `poll(sid, yield_time_ms=0, ...)`

Wait for visible pane change, then return the latest bottom-following capture.

### `display(sid, lines=80)`

Return the latest bottom-following capture immediately and reset the viewport to the latest output.

### `pane(target=None, sid=None, window=None, pane=None, lines=80)`

Capture a specific pane by direct target such as `%1`, or by `sid`/`window`/`pane` coordinates.

### `capture_range(sid, start_line, end_line)`

Return an exact absolute line slice without changing to bottom-follow mode.

### `info(sid)`

Return pane metadata such as running status, exit code, cursor position, and transcript size.

### `wait(sid, timeout_ms=None)`

Poll until the pane's main command exits. Because `remain-on-exit` is enabled for created sessions, final output remains inspectable afterward.

### `close(sid)`

Kill the tmux session.

### `list_panes(sid=None, window=None)` / `list_windows(sid=None)`

List panes or windows from the current tmux context when `sid` is omitted.

### `panes(...)` / `windows(...)` / `sessions(...)`

Capture nested pane/window/session snapshots as dictionaries keyed by pane id or `window_index:window_name`.

### `flatten_captures(captures)` / `search_captures(captures, pattern)`

Flatten nested capture dictionaries or search them line-by-line.

### `search(pattern, sid=None, window=None)` / `search_sessions(pattern, ...)`

Search visible tmux output within a session or across all sessions.

### `list_sessions(prefix=None)`

List tmux session names. Pass `prefix=DEFAULT_SESSION_PREFIX` or use `managed_sessions()` to restrict to `bgtmux-*` sessions.

### `TmuxSession`

Thin convenience wrapper around the functional API. It stores `sid`, optionally closes on context-manager exit, adds local viewport state for `view()`, `scroll_to()`, and `scroll()`, exposes `list_panes()`, `list_windows()`, `panes()`, `windows()`, and `search()`, and remembers the last latest-output snapshot so `poll()` can return immediately if output already changed between object calls.

## `PaneInfo`

`info()` returns:

- `history_size`
- `pane_height`
- `cursor_x`, `cursor_y`
- `running`
- `exit_code`
- `current_command`
- `line_count`

`line_count` is the number of transcript lines up to the cursor line and is what the scrolling APIs use.

## `CaptureResult`

Each display/capture method returns:

- `text`: full captured text
- `lines`: split text as a tuple of lines
- `start_line`, `end_line`: absolute line range, end exclusive
- `line_count`: total currently available transcript lines
- `cursor_line`: current absolute cursor line
- `running`, `exit_code`
- `pane_id`, `session_name`
- `window_index`, `window_name`, `pane_index`

## Notes

- This is best for shell and REPL workloads such as `python`, `ipython`, `sqlite3`, or CLI tools with normal terminal output.
- tmux snapshot polling is not a byte-perfect log transport. For huge structured outputs, write to a file instead.
- Full-screen alternate-screen programs are a different problem; this library is aimed at scrollback-driven terminal interaction rather than terminal UI emulation.

## Development

```bash
pip install -e .[dev]
pytest
```

## Versioning

Version lives in `bgtmux/__init__.py` as `__version__`.

```bash
ship-bump --part 2   # patch
ship-bump --part 1   # minor
ship-bump --part 0   # major
```

## Release

1. Ensure GitHub issues are labeled `bug`, `enhancement`, or `breaking`.
2. Run:

```bash
ship-gh
ship-pypi
```

If you want the lighter-weight version of this idea inside a single Python process, see [`bgterm`](../bgterm/README.md). `bgterm` keeps an in-process registry of raw PTY-backed sessions and is often the better choice when you do not need tmux features, named sessions, or manual reattachment. `bgtmux` is the better fit when you want tmux to be the visible session registry and you care about persistence, manual inspection, and pane/window-oriented workflows.
