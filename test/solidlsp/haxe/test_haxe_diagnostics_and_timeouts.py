"""Unit tests for Haxe compiler-failure surfacing and diagnosable request timeouts (WS1.3).

These guard two diagnosability fixes:
  * The Haxe LS reports build problems via ``window/logMessage`` (error severity) and the
    ``haxe/cacheBuildFailed`` / ``haxe/haxeKeepsCrashing`` notifications (previously logged as
    "Unhandled method"). We capture the latest such message so it can be surfaced.
  * A request that times out used to raise a bare ``TimeoutError`` with no context. We now wrap it
    with the request method, file and position, plus the server's busy-state (active progress
    tokens + last compiler message).

All tests use bare objects/stubs — no real language server, no toolchain — so they are fast.
"""

import threading

import pytest

from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer
from solidlsp.ls import SolidLanguageServer

pytestmark = [pytest.mark.haxe]


def _bare_haxe_ls() -> HaxeLanguageServer:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._server_ready = threading.Event()
    ls._active_progress_tokens = set()
    ls._progress_lock = threading.Lock()
    ls._last_compiler_message = None
    return ls


def test_window_log_message_captures_error_severity_only() -> None:
    ls = _bare_haxe_ls()

    # MessageType.Info (3) is not a compiler error -> not captured.
    ls._handle_window_log_message({"type": 3, "message": "just info"})
    # Read into a local before asserting: a direct ``assert ls._last_compiler_message is None``
    # narrows the *attribute* to ``None`` for the rest of the function, and mypy cannot see that
    # the later ``_handle_window_log_message`` call reassigns it -- which would collapse the
    # error-severity assertions below to ``Never`` ("unreachable"). Locals keep the narrowing local.
    msg_after_info = ls._last_compiler_message
    assert msg_after_info is None

    # MessageType.Error (1) -> captured.
    ls._handle_window_log_message({"type": 1, "message": "Type not found : Foo"})
    msg_after_error = ls._last_compiler_message
    assert msg_after_error is not None
    assert "Type not found : Foo" in msg_after_error


def test_cache_build_failed_is_captured() -> None:
    ls = _bare_haxe_ls()
    ls._handle_cache_build_failed({})
    assert ls._last_compiler_message is not None
    assert "cache" in ls._last_compiler_message.lower()


def test_haxe_keeps_crashing_is_captured() -> None:
    ls = _bare_haxe_ls()
    ls._handle_haxe_keeps_crashing({})
    assert ls._last_compiler_message is not None
    assert "crash" in ls._last_compiler_message.lower()


def test_describe_busy_state_mentions_active_tokens_and_last_message() -> None:
    ls = _bare_haxe_ls()
    ls._active_progress_tokens = {"tok1"}
    ls._last_compiler_message = "Type not found : Foo"

    state = ls.describe_busy_state()
    assert "1" in state, f"expected the active-token count in {state!r}"
    assert "Type not found : Foo" in state


def test_describe_busy_state_empty_when_idle_and_no_message() -> None:
    ls = _bare_haxe_ls()
    assert ls.describe_busy_state() == ""


class _BusyStubLS:
    """Minimal stand-in for a language server that reports an active compile."""

    def describe_busy_state(self) -> str:
        return "Haxe still compiling: 1 progress token(s) active"


def test_timeout_is_wrapped_with_request_context() -> None:
    req = SolidLanguageServer.DefinitionLocationRequest(_BusyStubLS(), "src/AppMain.hx", 31, 17)

    wrapped = req.map_exception(TimeoutError("Request timed out (timeout=25)"))

    assert isinstance(wrapped, TimeoutError)
    msg = str(wrapped)
    assert "request_definition" in msg, msg
    assert "src/AppMain.hx:31:17" in msg, msg
    assert "1 progress token" in msg, msg


def test_non_timeout_unknown_error_is_not_wrapped() -> None:
    req = SolidLanguageServer.DefinitionLocationRequest(_BusyStubLS(), "src/AppMain.hx", 31, 17)
    assert req.map_exception(ValueError("nope")) is None
