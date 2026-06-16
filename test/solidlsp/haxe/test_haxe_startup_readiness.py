"""Unit tests for the Haxe language server startup readiness logic (WS1.1).

These tests drive the ``$/progress`` handlers and the stable-idle wait directly, without a
real language server, so they are fast and deterministic. They guard the fix for the
"server signals ready too early" bug: the Haxe LS emits a first ``Building Cache`` progress
run, goes briefly idle, and only *then* starts ``Parsing Classpaths`` / ``Building Refactoring
Cache`` (which run for ~80s). Declaring the server ready at the first idle lets the first
compiler-backed request collide with that work. Readiness must instead require the server to
stay idle for a short settle window.
"""

import threading
import time

import pytest

from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer

pytestmark = [pytest.mark.haxe]


def _bare_haxe_ls() -> HaxeLanguageServer:
    """A HaxeLanguageServer with only the progress-tracking state initialised.

    Bypasses ``__init__`` (which would resolve/launch a real server) and sets up exactly the
    attributes the readiness logic touches.
    """
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._server_ready = threading.Event()
    ls._active_progress_tokens = set()
    ls._progress_lock = threading.Lock()
    return ls


def _begin(token: str, title: str = "") -> dict:
    return {"token": token, "value": {"kind": "begin", "title": title}}


def _end(token: str) -> dict:
    return {"token": token, "value": {"kind": "end"}}


def test_handle_progress_tracks_tokens_and_readiness() -> None:
    ls = _bare_haxe_ls()

    ls._handle_progress(_begin("a", "Building Cache"))
    assert ls._active_progress_tokens == {"a"}
    assert not ls._server_ready.is_set(), "an active progress token must keep the server not-ready"

    ls._handle_progress(_end("a"))
    assert ls._active_progress_tokens == set()
    assert ls._server_ready.is_set(), "the server must become idle once all tokens end"


def test_stable_idle_waits_through_a_post_idle_progress_token() -> None:
    """The decisive case: a new progress token (the refactoring cache) appears *after* the first
    idle. ``_await_stable_idle`` must not return until that token finishes and the settle window
    has elapsed.
    """
    ls = _bare_haxe_ls()
    settle = 0.4
    result: dict[str, object] = {}

    def run() -> None:
        result["reached"] = ls._await_stable_idle(timeout=10.0, settle_window=settle, poll_interval=0.02)

    waiter = threading.Thread(target=run)
    waiter.start()

    # 1) initial compile begins -> busy
    ls._handle_progress(_begin("t1", "Building Cache"))
    time.sleep(0.1)
    assert "reached" not in result, "must still be waiting while the initial compile runs"

    # 2) initial compile ends -> first (transient) idle
    ls._handle_progress(_end("t1"))
    time.sleep(0.1)  # shorter than the settle window

    # 3) the refactoring cache token appears before the settle window elapses
    ls._handle_progress(_begin("t2", "Building Refactoring Cache"))
    time.sleep(settle + 0.1)  # long enough that a naive "first idle" impl would have returned
    assert "reached" not in result, "must not declare ready while a post-idle token is active"

    # 4) the refactoring cache finishes -> now the server can settle
    ls._handle_progress(_end("t2"))
    waiter.join(timeout=5.0)
    assert result.get("reached") is True


def test_await_stable_idle_returns_false_on_timeout() -> None:
    ls = _bare_haxe_ls()
    ls._handle_progress(_begin("forever", "Building Cache"))  # never ends -> never idle

    start = time.monotonic()
    reached = ls._await_stable_idle(timeout=0.5, settle_window=0.2, poll_interval=0.02)
    elapsed = time.monotonic() - start

    assert reached is False
    assert elapsed >= 0.5, "must wait out the full timeout before giving up"


def test_compilation_timeout_resolves_from_settings_else_default() -> None:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)

    ls._custom_settings = {"compilationTimeout": 123}
    assert ls._resolve_compilation_timeout() == 123.0

    ls._custom_settings = {}
    assert ls._resolve_compilation_timeout() == HaxeLanguageServer._COMPILATION_TIMEOUT_DEFAULT

    # default must cover the measured one-time cold cost (~180s) with margin
    assert HaxeLanguageServer._COMPILATION_TIMEOUT_DEFAULT >= 180.0

    ls._custom_settings = {"compilationTimeout": "not-a-number"}
    assert ls._resolve_compilation_timeout() == HaxeLanguageServer._COMPILATION_TIMEOUT_DEFAULT
