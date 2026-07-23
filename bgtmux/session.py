
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os, re, shlex, subprocess, time, uuid

Cmd = str | Sequence[str]
DEFAULT_CAPTURE_LINES = 80
DEFAULT_SESSION_PREFIX = "bgtmux-"


class TmuxError(RuntimeError):
    "Raised when a tmux command fails or a target cannot be resolved."


@dataclass(slots=True)
class PaneSummary:
    "Summary of a tmux pane from `list_panes()`."

    pane_id: str
    session_name: str
    window_id: str
    window_index: int
    window_name: str
    pane_index: int
    active: bool
    dead: bool
    current_command: str


@dataclass(slots=True)
class WindowSummary:
    "Summary of a tmux window from `list_windows()`."

    session_name: str
    window_id: str
    window_index: int
    window_name: str
    active: bool
    pane_count: int


@dataclass(slots=True)
class PaneInfo(PaneSummary):
    "Detailed metadata for a tmux pane."

    cursor_x: int
    cursor_y: int
    history_size: int
    pane_height: int
    running: bool
    exit_code: int | None

    @property
    def line_count(self):
        "Return the transcript line count up to the cursor."
        return self.history_size + self.cursor_y + 1


@dataclass(slots=True)
class CaptureResult:
    "Captured pane text plus scrollback metadata."

    text: str
    lines: tuple[str, ...]
    start_line: int
    end_line: int
    line_count: int
    cursor_line: int
    history_size: int
    pane_height: int
    running: bool
    exit_code: int | None
    pane_id: str
    session_name: str
    window_index: int
    window_name: str
    pane_index: int

    @property
    def line_span(self):
        "Return the number of captured transcript lines."
        return self.end_line - self.start_line


@dataclass(slots=True)
class SearchMatch:
    "Single line match from `search()` or `search_captures()`."

    path: str
    line_no: int
    line: str
    pane_id: str
    session_name: str
    window_index: int
    window_name: str
    pane_index: int


def _tmux_cmd(): return ["tmux"]


def _tmux(*args: str, input: str | None = None):
    proc = subprocess.run([*_tmux_cmd(), *args], input=input, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise TmuxError(stderr or f"tmux command failed: {args!r}")
    return proc.stdout.rstrip("\n")


def _new_sid(sid=None, session_name=None):
    if sid and session_name and sid != session_name: raise ValueError("sid and session_name must match")
    return sid or session_name or f"{DEFAULT_SESSION_PREFIX}{uuid.uuid4().hex[:10]}"


def current_session():
    "Return the current tmux session name."
    try: return _tmux("display-message", "-p", "#{session_name}")
    except TmuxError as e: raise TmuxError("sid was omitted, but no current tmux session is available") from e


def current_pane():
    "Return the current tmux pane id."
    try: return _tmux("display-message", "-p", "#{pane_id}")
    except TmuxError as e: raise TmuxError("no current tmux pane is available") from e


def _current_session_or_none():
    try: return current_session()
    except TmuxError: return None


def _target_args(flag: str, target: str | None): return [flag, target] if target else []


def _session_target(sid: str | None = None):
    if sid and sid.startswith("%"): return _tmux("display-message", "-p", "-t", sid, "#{session_name}")  # a %pane_id means its owning session
    return sid or current_session()


def _window_target(sid: str | None = None, window: int | str | None = None):
    if sid is None and window is None: return ""
    sid = _session_target(sid)
    return sid if window is None else f"{sid}:{window}"


def _pane_target(target: str | None = None, sid: str | None = None, window: int | str | None = None, pane=None):
    if target is not None and any(o is not None for o in (sid, window, pane)): raise ValueError("target is mutually exclusive with sid/window/pane")
    if target is not None: return str(target)
    if pane is None and sid is None and window is None: return current_pane()
    if isinstance(pane, str) and pane.startswith("%") and sid is None and window is None: return pane
    if pane is None:
        listed = list_panes(sid, window)
        if listed: return listed[0].pane_id
        raise TmuxError("no pane found for requested target")
    base = _window_target(sid, window)
    return f"{base}.{pane}" if base else f".{pane}"


def _managed_pane_id(sid: str):
    try: return _tmux("show-options", "-qv", "-t", sid, "@bgtmux_pane_id")
    except TmuxError: return ""


def _primary_pane_id(sid: str | None = None):
    if sid is None: return current_pane()
    if sid.startswith("%"): return sid  # a %pane_id addresses that pane directly, whatever session owns it
    pane_id = _managed_pane_id(sid)
    if pane_id: return pane_id
    if sid == _current_session_or_none(): return current_pane()
    panes = _tmux("list-panes", "-t", sid, "-F", "#{pane_id}").splitlines()
    if panes: return panes[0]
    raise TmuxError(f"no pane found for session {sid!r}")


def _pane_info(target: str):
    fields = _tmux(
        "display-message",
        "-p",
        "-t",
        target,
        "#{pane_id}\t#{session_name}\t#{window_id}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_active}\t#{pane_dead}\t"
        "#{pane_current_command}\t#{cursor_x}\t#{cursor_y}\t#{history_size}\t#{pane_height}\t#{pane_dead_status}",
    ).split("\t")
    if len(fields) != 14: raise TmuxError(f"unexpected tmux pane info: {fields!r}")
    dead = fields[7] == "1"
    return PaneInfo(fields[0], fields[1], fields[2], int(fields[3]), fields[4], int(fields[5]), fields[6] == "1", dead, fields[8],
        int(fields[9]), int(fields[10]), int(fields[11]), int(fields[12]), not dead, int(fields[13]) if dead and fields[13] else None)


def _line_count(pane: PaneInfo):
    line_count = pane.line_count
    if line_count <= 0: return 0
    current = _tmux("capture-pane", "-p", "-t", pane.pane_id, "-S", str(pane.cursor_y), "-E", str(pane.cursor_y))
    return line_count - 1 if current == "" else line_count


def _normalize_range(line_count: int, start_line: int, end_line: int):
    start_line = min(max(start_line, 0), line_count)
    end_line = min(max(end_line, start_line), line_count)
    return start_line, end_line


def _capture(pane: PaneInfo, start_line: int, end_line: int):
    line_count = _line_count(pane)
    start_line, end_line = _normalize_range(line_count, start_line, end_line)
    if start_line == end_line: text = ""
    else:
        start = start_line - pane.history_size
        end = end_line - pane.history_size - 1
        text = _tmux("capture-pane", "-p", "-t", pane.pane_id, "-S", str(start), "-E", str(end))
    return CaptureResult(text, tuple(text.splitlines()), start_line, end_line, line_count, line_count - 1, pane.history_size,
        pane.pane_height, pane.running, pane.exit_code, pane.pane_id, pane.session_name, pane.window_index, pane.window_name,
        pane.pane_index)


def _snapshot(sid: str | None = None, lines=DEFAULT_CAPTURE_LINES):
    capture = display(sid, lines)
    return capture.text, capture.line_count, capture.cursor_line, capture.running, capture.exit_code


def _capture_panes(pane_list: Sequence[PaneSummary], lines=DEFAULT_CAPTURE_LINES):
    return {o.pane_id: pane(o.pane_id, lines=lines) for o in pane_list}


def _iter_matches(capture: CaptureResult, path: str, matcher):
    for i, line in enumerate(capture.lines):
        if matcher(line): yield SearchMatch(path, capture.start_line + i, line, capture.pane_id, capture.session_name, capture.window_index,
            capture.window_name, capture.pane_index)


def _make_matcher(pattern: str, regex=False, ignore_case=True):
    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags)
        return compiled.search
    if ignore_case:
        pattern = pattern.lower()
        return lambda line: pattern in line.lower()
    return lambda line: pattern in line


def start_session(cmd=None, sid=None, session_name=None, cwd=None, env=None, width=None, height=None, remain_on_exit=True):
    "Start a detached tmux session and return its session name."
    sid = _new_sid(sid, session_name)
    args = ["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", sid]
    if cwd is not None: args += ["-c", os.fspath(cwd)]
    if width is not None: args += ["-x", str(width)]
    if height is not None: args += ["-y", str(height)]
    envs = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
    pane_id = _tmux(*args, *envs)
    _tmux("set-option", "-t", sid, "@bgtmux_pane_id", pane_id)
    _tmux("set-option", "-t", sid, "@bgtmux_managed", "1")
    if remain_on_exit:
        _tmux("set-window-option", "-t", f"{sid}:0", "remain-on-exit", "on")
        _tmux("set-window-option", "-t", f"{sid}:0", "remain-on-exit-format", "")
    if cmd is not None:
        # Run the command only after the options above are in place: a fast-exiting
        # cmd passed straight to new-session can die before they land, so the pane
        # keeps a dead-banner drawn with the default remain-on-exit-format.
        cargs = ["respawn-pane", "-k", "-t", pane_id]
        if cwd is not None: cargs += ["-c", os.fspath(cwd)]
        cargs += envs + ([cmd] if isinstance(cmd, str) else list(cmd))
        _tmux(*cargs)
    return sid


def start(cmd=None, sid=None, session_name=None, cwd=None, env=None, width=None, height=None, remain_on_exit=True):
    "Alias for `start_session()`."
    return start_session(cmd, sid, session_name, cwd, env, width, height, remain_on_exit)


def attach_command(sid: str | None = None):
    "Return a tmux attach command for the target session."
    return f"{shlex.join(_tmux_cmd())} attach -t {shlex.quote(_session_target(sid))}"


def list_sessions(prefix=None):
    "List tmux session names, optionally filtered by prefix."
    proc = subprocess.run([*_tmux_cmd(), "list-sessions", "-F", "#{session_name}"], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        if "no server running" in stderr: return []
        raise TmuxError(stderr or "tmux list-sessions failed")
    sessions = proc.stdout.splitlines()
    return sessions if prefix is None else [o for o in sessions if o.startswith(prefix)]


def managed_sessions(prefix=DEFAULT_SESSION_PREFIX):
    "List tmux sessions created by `bgtmux`."
    return list_sessions(prefix)


def list_windows(sid: str | None = None):
    "List windows in the target or current tmux session."
    lines = _tmux("list-windows", *_target_args("-t", _window_target(sid)), "-F",
        "#{session_name}\t#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{window_panes}")
    if not lines: return ()
    return tuple(WindowSummary(parts[0], parts[1], int(parts[2]), parts[3], parts[4] == "1", int(parts[5]))
        for parts in (line.split("\t") for line in lines.splitlines()))


def list_panes(sid: str | None = None, window: int | str | None = None):
    "List panes in the target or current tmux window."
    lines = _tmux("list-panes", *_target_args("-t", _window_target(sid, window)), "-F",
        "#{pane_id}\t#{session_name}\t#{window_id}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_active}\t#{pane_dead}\t"
        "#{pane_current_command}")
    if not lines: return ()
    return tuple(PaneSummary(parts[0], parts[1], parts[2], int(parts[3]), parts[4], int(parts[5]), parts[6] == "1",
        parts[7] == "1", parts[8]) for parts in (line.split("\t") for line in lines.splitlines()))


def info(sid: str | None = None):
    "Return metadata for the primary pane of the target session (`sid` may be a session name or a `%pane_id`)."
    return _pane_info(_primary_pane_id(sid))


def display(sid: str | None = None, lines=DEFAULT_CAPTURE_LINES):
    "Capture the latest visible lines from the primary pane."
    pane_info = info(sid)
    end_line = _line_count(pane_info)
    return _capture(pane_info, max(0, end_line - lines), end_line)


def pane(target: str | None = None, sid: str | None = None, window: int | str | None = None, pane=None, lines=DEFAULT_CAPTURE_LINES):
    "Capture the latest visible lines from a specific pane target."
    pane_info = _pane_info(_pane_target(target, sid, window, pane))
    end_line = _line_count(pane_info)
    return _capture(pane_info, max(0, end_line - lines), end_line)


def panes(sid: str | None = None, window: int | str | None = None, lines=DEFAULT_CAPTURE_LINES):
    "Capture the latest visible lines from every pane in a window."
    return _capture_panes(list_panes(sid, window), lines)


def windows(sid: str | None = None, lines=DEFAULT_CAPTURE_LINES):
    "Capture the latest visible lines from every pane in a session."
    return {f"{o.window_index}:{o.window_name}": _capture_panes(list_panes(o.session_name, o.window_index), lines) for o in list_windows(sid)}


def sessions(lines=DEFAULT_CAPTURE_LINES, prefix=None):
    "Capture the latest visible lines from panes across tmux sessions."
    return {sid: windows(sid, lines) for sid in list_sessions(prefix)}


def flatten_captures(captures: Mapping, parent_key="", sep="//"):
    "Flatten nested capture dictionaries into `(path, CaptureResult)` pairs."
    items = []
    for k, v in captures.items():
        key = f"{parent_key}{sep}{k}" if parent_key else str(k)
        if isinstance(v, CaptureResult): items.append((key, v))
        elif isinstance(v, Mapping): items.extend(flatten_captures(v, key, sep))
        else: raise TypeError(f"expected nested mappings of CaptureResult, got {type(v)!r} at {key!r}")
    return items


def search_captures(captures: Mapping, pattern: str, regex=False, ignore_case=True, sep="//"):
    "Search flattened capture trees and return per-line matches."
    matcher = _make_matcher(pattern, regex, ignore_case)
    return [match for path, capture in flatten_captures(captures, sep=sep) for match in _iter_matches(capture, path, matcher)]


def search(pattern: str, sid: str | None = None, window: int | str | None = None, lines=DEFAULT_CAPTURE_LINES, regex=False,
    ignore_case=True):
    "Search visible captures in the target or current tmux session."
    captures = panes(sid, window, lines) if window is not None else windows(sid, lines)
    return search_captures(captures, pattern, regex, ignore_case)


def search_sessions(pattern: str, lines=DEFAULT_CAPTURE_LINES, prefix=None, regex=False, ignore_case=True):
    "Search visible captures across tmux sessions."
    return search_captures(sessions(lines, prefix), pattern, regex, ignore_case)


def capture_range(sid: str | None = None, start_line: int = 0, end_line: int = 0):
    "Capture an absolute transcript line range from the primary pane."
    return _capture(info(sid), start_line, end_line)


def poll(sid: str | None = None, yield_time_ms=0, poll_interval_ms=50, lines=DEFAULT_CAPTURE_LINES):
    "Wait for visible pane output to change or timeout, then capture it."
    if yield_time_ms < 0: raise ValueError("yield_time_ms must be >= 0")
    if poll_interval_ms <= 0: raise ValueError("poll_interval_ms must be > 0")
    if yield_time_ms > 0:
        baseline = _snapshot(sid, lines)
        deadline = time.monotonic() + yield_time_ms / 1000
        while time.monotonic() < deadline:
            time.sleep(min(poll_interval_ms / 1000, max(0, deadline - time.monotonic())))
            if _snapshot(sid, lines) != baseline: break
    return display(sid, lines)


def send(sid: str | None = None, chars: str = "", yield_time_ms=0, poll_interval_ms=50, lines=DEFAULT_CAPTURE_LINES):
    "Paste text into the primary pane (`sid` may be a session name or a `%pane_id`), then poll for updated output."
    if chars:
        pane_id = _primary_pane_id(sid)
        buffer_name = f"{DEFAULT_SESSION_PREFIX}{uuid.uuid4().hex}"
        _tmux("load-buffer", "-b", buffer_name, "-", input=chars)
        try: _tmux("paste-buffer", "-d", "-b", buffer_name, "-t", pane_id)
        except Exception:
            try: _tmux("delete-buffer", "-b", buffer_name)
            except TmuxError: pass
            raise
    return poll(sid, yield_time_ms, poll_interval_ms, lines)


def send_keys(sid: str | None = None, *keys: str, yield_time_ms=0, poll_interval_ms=50, lines=DEFAULT_CAPTURE_LINES):
    "Send tmux key names to the primary pane (`sid` may be a session name or a `%pane_id`), then poll for output."
    if keys: _tmux("send-keys", "-t", _primary_pane_id(sid), *keys)
    return poll(sid, yield_time_ms, poll_interval_ms, lines)


def interrupt(sid: str | None = None, yield_time_ms=0, poll_interval_ms=50, lines=DEFAULT_CAPTURE_LINES):
    "Send `Ctrl-C` to the primary pane and return the updated capture."
    return send_keys(sid, "C-c", yield_time_ms=yield_time_ms, poll_interval_ms=poll_interval_ms, lines=lines)


def wait(sid: str | None = None, timeout_ms=None, poll_interval_ms=50):
    "Wait for the primary pane's command to exit and return its status."
    deadline = None if timeout_ms is None else time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        pane_info = info(sid)
        if not pane_info.running: return pane_info.exit_code
        if deadline is not None and time.monotonic() >= deadline: return None
        time.sleep(poll_interval_ms / 1000)


def terminate(sid: str | None = None):
    "Send `Ctrl-C` to the primary pane without polling."
    _tmux("send-keys", "-t", _primary_pane_id(sid), "C-c")


def close(sid: str | None = None):
    "Kill the target tmux session."
    _tmux("kill-session", "-t", _session_target(sid))


class TmuxSession:
    "Thin OO wrapper around the functional bgtmux API."

    def __init__(self, sid: str | None = None, default_lines=DEFAULT_CAPTURE_LINES, close_on_exit=True):
        self.sid = _session_target(sid)
        self.default_lines = default_lines
        self.close_on_exit = close_on_exit
        self._view = None
        self._latest = {}
        self._closed = False

    def _remember_latest(self, lines: int, out: CaptureResult):
        self._latest[lines] = out.pane_id, out.text, out.line_count, out.cursor_line, out.running, out.exit_code

    def _latest_changed(self, lines: int, out: CaptureResult):
        baseline = self._latest.get(lines)
        current = out.pane_id, out.text, out.line_count, out.cursor_line, out.running, out.exit_code
        return baseline is not None and current != baseline

    @classmethod
    def start(cls, cmd=None, sid=None, session_name=None, cwd=None, env=None, default_lines=DEFAULT_CAPTURE_LINES, width=None,
        height=None, remain_on_exit=True, close_on_exit=True):
        "Start a managed tmux session and wrap it in `TmuxSession`."
        sid = start_session(cmd, sid, session_name, cwd, env, width, height, remain_on_exit)
        return cls(sid, default_lines, close_on_exit)

    @classmethod
    def open(cls, sid: str | None = None, default_lines=DEFAULT_CAPTURE_LINES, close_on_exit=False):
        "Wrap an existing tmux session, defaulting to the current one."
        return cls(sid, default_lines, close_on_exit)

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb):
        if self.close_on_exit: self.close()
        return False

    @property
    def session_name(self):
        "Return the wrapped tmux session name."
        return self.sid

    @property
    def attach_command(self):
        "Return a tmux attach command for this session."
        return attach_command(self.sid)

    @property
    def running(self):
        "Return whether this session's primary pane is still running."
        return info(self.sid).running

    @property
    def exit_code(self):
        "Return the exit code of this session's primary pane, if dead."
        return info(self.sid).exit_code

    def info(self):
        "Return metadata for this session's primary pane."
        return info(self.sid)

    def list_panes(self, window: int | str | None = None):
        "List panes in this session or one of its windows."
        return list_panes(self.sid, window)

    def list_windows(self):
        "List windows in this session."
        return list_windows(self.sid)

    def panes(self, window: int | str | None = None, lines: int | None = None):
        "Capture the latest visible lines from panes in this session."
        return panes(self.sid, window, lines or self.default_lines)

    def windows(self, lines: int | None = None):
        "Capture the latest visible lines from all windows in this session."
        return windows(self.sid, lines or self.default_lines)

    def flatten_captures(self, captures: Mapping, parent_key="", sep="//"):
        "Flatten nested capture dictionaries into `(path, CaptureResult)` pairs."
        return flatten_captures(captures, parent_key, sep)

    def search_captures(self, captures: Mapping, pattern: str, regex=False, ignore_case=True, sep="//"):
        "Search flattened capture trees and return per-line matches."
        return search_captures(captures, pattern, regex, ignore_case, sep)

    def search(self, pattern: str, window: int | str | None = None, lines: int | None = None, regex=False, ignore_case=True):
        "Search visible captures in this tmux session."
        return search(pattern, self.sid, window, lines or self.default_lines, regex, ignore_case)

    def display(self, lines: int | None = None):
        "Capture the latest visible lines from this session's primary pane."
        lines = lines or self.default_lines
        out = display(self.sid, lines)
        self._view = out.start_line, out.end_line
        self._remember_latest(lines, out)
        return out

    def view(self):
        "Re-render the current local viewport or the latest output."
        if self._view is None: return self.display()
        return capture_range(self.sid, *self._view)

    def capture_range(self, start_line: int, end_line: int):
        "Capture an absolute transcript line range from this session."
        return capture_range(self.sid, start_line, end_line)

    def scroll_to(self, start_line: int, end_line: int | None = None, lines: int | None = None):
        "Move the local viewport to an absolute transcript range."
        pane_info = info(self.sid)
        size = lines or self.default_lines
        self._view = _normalize_range(_line_count(pane_info), start_line, end_line or start_line + size)
        return capture_range(self.sid, *self._view)

    def scroll(self, delta: int, lines: int | None = None):
        "Move the local viewport by a relative line delta."
        pane_info = info(self.sid)
        size = lines or (self._view[1] - self._view[0] if self._view else self.default_lines)
        line_count = _line_count(pane_info)
        start_line, end_line = self._view or (max(0, line_count - size), line_count)
        desired_start = min(max(start_line + delta, 0), max(0, line_count - size))
        self._view = _normalize_range(line_count, desired_start, desired_start + size)
        return capture_range(self.sid, *self._view)

    def send(self, chars: str = "", yield_time_ms=0, poll_interval_ms=50, lines: int | None = None):
        "Paste text into this session's primary pane and poll for output."
        lines = lines or self.default_lines
        out = send(self.sid, chars, yield_time_ms, poll_interval_ms, lines)
        self._view = out.start_line, out.end_line
        self._remember_latest(lines, out)
        return out

    def paste(self, chars: str = "", yield_time_ms=0, poll_interval_ms=50, lines: int | None = None):
        "Alias for `send()`."
        return self.send(chars, yield_time_ms, poll_interval_ms, lines)

    def send_keys(self, *keys: str, yield_time_ms=0, poll_interval_ms=50, lines: int | None = None):
        "Send tmux key names to this session's primary pane and poll."
        lines = lines or self.default_lines
        out = send_keys(self.sid, *keys, yield_time_ms=yield_time_ms, poll_interval_ms=poll_interval_ms, lines=lines)
        self._view = out.start_line, out.end_line
        self._remember_latest(lines, out)
        return out

    def interrupt(self, yield_time_ms=0, poll_interval_ms=50, lines: int | None = None):
        "Send `Ctrl-C` to this session's primary pane and poll."
        return self.send_keys("C-c", yield_time_ms=yield_time_ms, poll_interval_ms=poll_interval_ms, lines=lines)

    def poll(self, yield_time_ms=0, poll_interval_ms=50, lines: int | None = None):
        "Wait for visible output to change or timeout, then capture it."
        lines = lines or self.default_lines
        current = display(self.sid, lines)
        if self._latest_changed(lines, current):
            self._view = current.start_line, current.end_line
            self._remember_latest(lines, current)
            return current
        out = poll(self.sid, yield_time_ms, poll_interval_ms, lines)
        self._view = out.start_line, out.end_line
        self._remember_latest(lines, out)
        return out

    def wait(self, timeout_ms=None, poll_interval_ms=50):
        "Wait for this session's primary pane to exit and return its status."
        return wait(self.sid, timeout_ms, poll_interval_ms)

    def terminate(self):
        "Send `Ctrl-C` to this session's primary pane without polling."
        terminate(self.sid)

    def close(self):
        "Kill this tmux session once, ignoring repeated close calls."
        if self._closed: return
        try: close(self.sid)
        except TmuxError: pass
        self._closed = True


__all__ = ["DEFAULT_CAPTURE_LINES", "DEFAULT_SESSION_PREFIX", "CaptureResult", "PaneInfo", "PaneSummary", "SearchMatch",
    "TmuxError", "TmuxSession", "WindowSummary", "attach_command", "capture_range", "close", "current_pane",
    "current_session", "display", "flatten_captures", "info", "interrupt", "list_panes", "list_sessions", "list_windows",
    "managed_sessions", "pane", "panes", "poll", "search", "search_captures", "search_sessions", "sessions", "send",
    "send_keys", "start", "start_session", "terminate", "wait", "windows"]
