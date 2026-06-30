"""Unit tests for the Haxe-only configurable request timeout and honest timeout-vs-crash messaging.

Background (confirmed on the user's large repo): the first compiler-backed request (find_declaration /
find_implementations / safe_delete_symbol) costs ~138 s cold but ultimately *succeeds*; warm calls are
~9 s. With a short per-request timeout the cold call died after ~25 s with a message that blamed a
compiler *crash* (a stale ``haxe/haxeKeepsCrashing`` left over from earlier work), which was both wrong
and confusing. The fixes under test:

  1. A Haxe-only, user-configurable request timeout (``ls_specific_settings.haxe.requestTimeout``,
     seconds) with a generous default, applied as a *floor* so it survives the short global
     ``ls_timeout`` that Serena passes to every server.
  2. Honest messaging: a request that merely *times out* must say TIMED OUT (naming the setting),
     never CRASH, and must never carry a stale "last compiler message" crash signal. A genuine,
     *current* crash (a crash notification that fired for THIS request) must say CRASH.

All tests are deterministic stubs/fakes -- no real language server, no Haxe toolchain. The real
large-codebase slow-compile behaviour is the user's to confirm; see the module README in the PR notes.
"""

import threading
import time

import pytest

from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer
from solidlsp.ls import SolidLanguageServer

pytestmark = [pytest.mark.haxe]


class _FakeServerInterface:
    """Minimal stand-in for the underlying LanguageServerInterface.

    ``SolidLanguageServer.set_request_timeout`` delegates to ``self.server.set_request_timeout``; this
    captures the *effective* timeout that would reach the real request loop, and reports the process as
    running so the crash-liveness check is exercised honestly.
    """

    def __init__(self) -> None:
        self._request_timeout: float | None = None
        self._running = True

    def set_request_timeout(self, timeout: float | None) -> None:
        self._request_timeout = timeout

    def is_running(self) -> bool:
        return self._running


def _bare_haxe_ls(*, configured_request_timeout: float = 600.0) -> HaxeLanguageServer:
    """A HaxeLanguageServer with only the attributes the timeout/crash logic touches.

    Bypasses ``__init__`` (which would resolve/launch a real server).
    """
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._server_ready = threading.Event()
    ls._active_progress_tokens = set()
    ls._progress_lock = threading.Lock()
    ls._last_compiler_message = None
    ls._diagnostics_unavailable_reason = None
    ls._last_crash_signal_at = None
    ls._configured_request_timeout = configured_request_timeout
    ls.server = _FakeServerInterface()  # type: ignore[assignment]
    return ls


# --------------------------------------------------------------------------------------------------
# 1. Configurable request timeout: resolution + generous default.
# --------------------------------------------------------------------------------------------------


def test_request_timeout_resolves_from_settings() -> None:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._custom_settings = {"requestTimeout": 600}
    assert ls._resolve_request_timeout() == 600.0


def test_request_timeout_defaults_when_unset() -> None:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._custom_settings = {}
    assert ls._resolve_request_timeout() == HaxeLanguageServer._REQUEST_TIMEOUT_DEFAULT


def test_request_timeout_default_is_modest() -> None:
    # Most Haxe projects are small and the first compiler-backed request is quick, so the default is
    # modest (60 s). Very large codebases raise ls_specific_settings.haxe.requestTimeout explicitly.
    assert HaxeLanguageServer._REQUEST_TIMEOUT_DEFAULT == 60.0


def test_request_timeout_invalid_value_falls_back_to_default() -> None:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._custom_settings = {"requestTimeout": "not-a-number"}
    assert ls._resolve_request_timeout() == HaxeLanguageServer._REQUEST_TIMEOUT_DEFAULT


# --------------------------------------------------------------------------------------------------
# 2. The configured timeout must actually govern requests: applied as a FLOOR.
#    Serena calls ls.set_request_timeout(global_ls_timeout) AFTER __init__ (ls.py:512); the Haxe
#    override must raise the effective per-request timeout to at least the configured value, so the
#    short global default cannot silently cap it.
# --------------------------------------------------------------------------------------------------


def test_set_request_timeout_floors_at_configured_value() -> None:
    ls = _bare_haxe_ls(configured_request_timeout=600.0)

    # Serena passes the small global ls_timeout (e.g. tool_timeout(240) - 5 = 235).
    ls.set_request_timeout(235.0)
    assert ls.server._request_timeout == 600.0, "a smaller global timeout must be raised to the configured floor"


def test_set_request_timeout_keeps_larger_global() -> None:
    ls = _bare_haxe_ls(configured_request_timeout=300.0)

    ls.set_request_timeout(900.0)
    assert ls.server._request_timeout == 900.0, "a larger global timeout must be honoured as-is"


def test_set_request_timeout_none_uses_configured_floor() -> None:
    ls = _bare_haxe_ls(configured_request_timeout=600.0)

    # tool_timeout <= 0 yields ls_timeout=None (block forever). The configured floor still applies as
    # a concrete, finite budget so a misconfigured-away timeout does not silently revert to "no limit".
    ls.set_request_timeout(None)
    assert ls.server._request_timeout == 600.0


# --------------------------------------------------------------------------------------------------
# 3. Honest timeout-vs-crash messaging.
# --------------------------------------------------------------------------------------------------


def _definition_request(ls: SolidLanguageServer, *, started_at: float | None) -> SolidLanguageServer.SymbolLocationRequest:
    req = SolidLanguageServer.DefinitionLocationRequest(ls, "src/AppMain.hx", 31, 17)
    req._request_started_at = started_at
    return req


def test_timeout_without_crash_says_timed_out_not_crashed() -> None:
    """A pure timeout (no crash signal for this request) must read as a TIMEOUT, never a crash."""
    ls = _bare_haxe_ls()
    # A STALE crash signal from earlier work must NOT contaminate this request.
    ls._handle_haxe_keeps_crashing({})  # sets _last_compiler_message + _last_crash_signal_at (in the past)
    time.sleep(0.01)

    req = _definition_request(ls, started_at=time.perf_counter())  # request starts AFTER the stale crash
    wrapped = req.map_exception(TimeoutError("Request timed out (timeout=600)"))

    assert isinstance(wrapped, TimeoutError)
    msg = str(wrapped)
    lowered = msg.lower()
    assert "timed out" in lowered or "timeout" in lowered, msg
    # Must NOT frame it as a crash, and must NOT carry the stale crash signal as the cause.
    assert "crash" not in lowered, f"a pure timeout must never be reported as a crash: {msg}"
    assert "keepscrashing" not in lowered.replace(" ", ""), f"stale crash signal leaked into a timeout: {msg}"
    # Must name the configurable setting and the request context.
    assert "requesttimeout" in lowered.replace(" ", "").replace("_", ""), msg
    assert "src/AppMain.hx:31:17" in msg, msg


def test_timeout_message_mentions_slow_compiler_and_raising_timeout() -> None:
    ls = _bare_haxe_ls()
    req = _definition_request(ls, started_at=time.perf_counter())
    wrapped = req.map_exception(TimeoutError("Request timed out (timeout=600)"))
    lowered = str(wrapped).lower()
    assert "slow" in lowered or "large" in lowered, lowered
    assert "raise" in lowered or "increase" in lowered, lowered


def test_genuine_current_crash_is_reported_as_crash() -> None:
    """A crash signal that fired AFTER the request started => report a CRASH (current evidence)."""
    ls = _bare_haxe_ls()
    started = time.perf_counter()
    req = _definition_request(ls, started_at=started)

    time.sleep(0.01)
    ls._handle_haxe_keeps_crashing({})  # crash fires DURING this request

    wrapped = req.map_exception(TimeoutError("Request timed out (timeout=600)"))
    lowered = str(wrapped).lower()
    assert "crash" in lowered, f"a genuine current crash must be reported as a crash: {wrapped}"


def test_crash_freshness_is_strict_about_ordering() -> None:
    """The freshness check compares the crash timestamp against the request start.

    A crash strictly before the request start is stale (=> timeout); a crash at/after the start is
    current (=> crash). This guards the hard requirement that a timeout is never inferred as a crash.
    """
    ls = _bare_haxe_ls()

    # Stale: crash, then (later) request start.
    ls._handle_cache_build_failed({})
    stale_crash_at = ls._last_crash_signal_at
    assert stale_crash_at is not None
    req_stale = _definition_request(ls, started_at=stale_crash_at + 1.0)
    assert "crash" not in str(req_stale.map_exception(TimeoutError("t"))).lower()

    # Current: request start, then crash.
    req_current = _definition_request(ls, started_at=stale_crash_at + 2.0)
    ls._last_crash_signal_at = stale_crash_at + 3.0  # crash after this request started
    assert "crash" in str(req_current.map_exception(TimeoutError("t"))).lower()


def test_crash_handlers_record_a_timestamp() -> None:
    ls = _bare_haxe_ls()
    # Read into locals before asserting: a direct ``assert ls._last_crash_signal_at is None`` narrows
    # the *attribute* to None for the rest of the function, and mypy cannot see that the opaque crash
    # handlers below reassign it -- which would mark the later reads as unreachable.
    before = ls._last_crash_signal_at
    assert before is None

    ls._handle_haxe_keeps_crashing({})
    first = ls._last_crash_signal_at
    assert first is not None

    time.sleep(0.01)
    ls._handle_cache_build_failed({})
    second = ls._last_crash_signal_at
    assert second is not None and second >= first


# --------------------------------------------------------------------------------------------------
# 4. Non-Haxe behaviour must be byte-for-byte unchanged: the base hook returns None, so the generic
#    map_exception framing (request context + describe_busy_state) is used verbatim.
# --------------------------------------------------------------------------------------------------


class _NonHaxeBusyStubLS:
    """A non-Haxe-style language server: only the generic busy-state hook, no Haxe timeout framing."""

    def describe_busy_state(self) -> str:
        return "indexing: 3 files remaining"


def test_base_describe_request_timeout_returns_none() -> None:
    """The base hook must return None so non-Haxe servers keep the generic timeout framing."""
    assert (
        SolidLanguageServer.describe_request_timeout(
            object(),  # type: ignore[arg-type]
            request_name="request_definition",
            relative_file_path="x.py",
            line=1,
            column=2,
            elapsed=None,
            request_started_at=None,
        )
        is None
    )


def test_non_haxe_timeout_uses_generic_framing_unchanged() -> None:
    req = SolidLanguageServer.DefinitionLocationRequest(_NonHaxeBusyStubLS(), "src/App.py", 10, 4)  # type: ignore[arg-type]
    wrapped = req.map_exception(TimeoutError("Request timed out (timeout=25)"))
    assert isinstance(wrapped, TimeoutError)
    msg = str(wrapped)
    # Generic framing: request name + position + busy-state appended, no Haxe-specific copy.
    assert "request_definition" in msg, msg
    assert "src/App.py:10:4" in msg, msg
    assert "indexing: 3 files remaining" in msg, msg
    assert "requestTimeout" not in msg, "non-Haxe message must not mention the Haxe setting"
