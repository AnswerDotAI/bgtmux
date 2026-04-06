
import os, shutil, sys, time

import pytest

from bgtmux import DEFAULT_SESSION_PREFIX, TmuxSession, capture_range, close, current_pane, current_session, display, flatten_captures, \
    list_panes, list_sessions, list_windows, managed_sessions, pane, panes, poll, search, search_captures, search_sessions, send, sessions, \
    start_session, wait, windows

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def test_functional_start_and_display_follow_recent_lines():
    cmd = [sys.executable, "-u", "-c", "import time; [print(f\"line{i}\", flush=True) for i in range(5)]; time.sleep(30)"]
    sid = start_session(cmd, width=80, height=12)
    try:
        assert sid.startswith("bgtmux-")
        assert sid in managed_sessions()
        out = poll(sid, yield_time_ms=1500, lines=3)
        assert out.lines[-3:] == ("line2", "line3", "line4")
    finally: close(sid)


def test_functional_send_can_drive_a_repl_like_process():
    cmd = [
        sys.executable,
        "-u",
        "-c",
        "print('ready', flush=True)\n"
        "import sys\n"
        "for line in sys.stdin:\n"
        "    print(f'ACK:{line.rstrip()}', flush=True)\n"]
    sid = start_session(cmd, width=80, height=12)
    try:
        ready = poll(sid, yield_time_ms=1500, lines=6)
        assert "ready" in ready.text

        reply = send(sid, "hello\n", yield_time_ms=1500, lines=6)
        assert "ACK:hello" in reply.text
    finally: close(sid)


def test_object_wrapper_keeps_local_scroll_state():
    cmd = [sys.executable, "-u", "-c", "import time; [print(f\"line{i}\", flush=True) for i in range(12)]; time.sleep(30)"]
    with TmuxSession.start(cmd, default_lines=4, width=80, height=8) as sess:
        bottom = sess.poll(yield_time_ms=1500)
        assert bottom.lines[-4:] == ("line8", "line9", "line10", "line11")

        top = sess.capture_range(0, 3)
        assert top.lines == ("line0", "line1", "line2")

        middle = sess.scroll_to(4, lines=3)
        assert middle.lines == ("line4", "line5", "line6")

        newer = sess.scroll(2)
        assert newer.lines == ("line6", "line7", "line8")


def test_object_poll_returns_immediately_if_latest_output_already_changed():
    cmd = [
        sys.executable,
        "-u",
        "-c",
        "print('ready', flush=True)\n"
        "import sys\n"
        "for line in sys.stdin:\n"
        "    print(f'ACK:{line.rstrip()}', flush=True)\n"]
    with TmuxSession.start(cmd, default_lines=6, width=80, height=12) as sess:
        ready = sess.poll(yield_time_ms=1500)
        assert "ready" in ready.text

        send(sess.sid, "hello\n", yield_time_ms=1500, lines=6)

        start = time.monotonic()
        out = sess.poll(yield_time_ms=1200)
        elapsed = time.monotonic() - start
        assert "ACK:hello" in out.text
        assert elapsed < 0.4


def test_listing_helpers_and_nested_views():
    cmd = [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"]
    sid = start_session(cmd, width=80, height=8)
    try:
        assert sid in list_sessions()
        wins = list_windows(sid)
        panes_info = list_panes(sid)
        assert len(wins) == 1
        assert len(panes_info) == 1
        assert wins[0].session_name == sid
        assert panes_info[0].pane_id.startswith("%")
        assert "ready" in pane(sid=sid, lines=5).text
        assert list(panes(sid).keys()) == [panes_info[0].pane_id]
        assert list(windows(sid).keys()) == [f"{wins[0].window_index}:{wins[0].window_name}"]
        assert sid in sessions(prefix=DEFAULT_SESSION_PREFIX)
    finally: close(sid)


def test_flatten_and_search_helpers():
    cmd = [sys.executable, "-u", "-c", "import time; print('alpha', flush=True); print('error: boom', flush=True); time.sleep(30)"]
    sid = start_session(cmd, width=80, height=8)
    try:
        poll(sid, yield_time_ms=1500, lines=6)
        nested = windows(sid, 6)
        flat = flatten_captures(nested)
        assert len(flat) == 1
        assert "error: boom" in flat[0][1].text

        hits = search("error", sid, lines=6)
        assert [o.line for o in hits] == ["error: boom"]
        assert hits[0].path == flat[0][0]

        nested_hits = search_captures(nested, "alpha")
        assert [o.line for o in nested_hits] == ["alpha"]

        session_hits = search_sessions("boom", lines=6, prefix=sid)
        assert [o.line for o in session_hits] == ["error: boom"]
    finally: close(sid)


@pytest.mark.skipif(os.environ.get("TMUX") is None, reason="not running inside tmux")
def test_current_session_defaults_follow_current_pane():
    sid = current_session()
    pane_id = current_pane()
    out = display(lines=5)
    assert out.session_name == sid
    assert out.pane_id == pane_id

    panes_info = list_panes()
    assert any(o.pane_id == pane_id for o in panes_info)

    other = next((o.pane_id for o in panes_info if o.pane_id != pane_id), pane_id)
    other_out = pane(other, lines=5)
    assert other_out.session_name == sid
    assert other_out.pane_id == other


def test_finished_sessions_remain_inspectable():
    cmd = [sys.executable, "-u", "-c", "print('bye', flush=True); raise SystemExit(7)"]
    sid = start_session(cmd, width=80, height=12)
    try:
        assert wait(sid, timeout_ms=3000) == 7
        out = display(sid, lines=5)
        assert "bye" in out.text
        assert out.exit_code == 7
        top = capture_range(sid, 0, 1)
        assert top.lines == ("bye",)
    finally: close(sid)
