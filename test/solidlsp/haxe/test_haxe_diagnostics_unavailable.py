"""Tests for distinguishing a clean file from an unavailable compiler in diagnostics (T4).

Baseline problem: on a large codebase ``get_diagnostics_for_file`` returns ``{}`` even when a real
broken reference exists, because the Haxe compiler has crashed / is unavailable and never publishes
diagnostics. A bare ``{}`` is ambiguous — it reads as "file is clean" when it actually means "the
compiler could not tell us". When the compiler is known to be in a crash / cache-build-failure state
the tool must clearly signal that instead of returning a misleading empty result.

Design under test:
  * ``SolidLanguageServer.get_diagnostics_unavailable_reason()`` is a base hook returning ``None``
    (non-Haxe servers => behaviour unchanged).
  * ``HaxeLanguageServer`` overrides it to return a message when ``_last_compiler_message`` indicates
    a crash / cache-build failure.
  * ``GetDiagnosticsForFileTool`` surfaces that reason when the diagnostics result is empty.

These tests are deterministic stubs — no real LS / Haxe toolchain. The end-to-end "real diagnostics
on the deliberately-broken sample still work" guarantee is covered by ``test_haxe_diagnostics.py``.
"""

import json
import threading

import pytest

from serena.tools.symbol_tools import GetDiagnosticsForFileTool
from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer

pytestmark = [pytest.mark.haxe]

_REL_PATH = "src/Broken.hx"


# --------------------------------------------------------------------------------------------------
# Unit tests for the language-server hook.
# --------------------------------------------------------------------------------------------------


def _bare_haxe_ls() -> HaxeLanguageServer:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._server_ready = threading.Event()
    ls._active_progress_tokens = set()
    ls._progress_lock = threading.Lock()
    ls._last_compiler_message = None
    ls._diagnostics_unavailable_reason = None
    return ls


def test_base_hook_returns_none() -> None:
    """The base ``SolidLanguageServer`` hook must return None (non-Haxe servers unchanged).

    ``SolidLanguageServer`` is abstract, so we invoke the concrete default method directly with a
    dummy ``self`` (the base implementation does not touch instance state).
    """
    from solidlsp.ls import SolidLanguageServer

    assert SolidLanguageServer.get_diagnostics_unavailable_reason(object()) is None  # type: ignore[arg-type]


def test_haxe_hook_none_when_healthy() -> None:
    """No captured compiler error and no active tokens => no unavailability reason."""
    ls = _bare_haxe_ls()
    assert ls.get_diagnostics_unavailable_reason() is None


def test_haxe_hook_reports_cache_build_failed() -> None:
    """A captured haxe/cacheBuildFailed message => the hook reports a reason."""
    ls = _bare_haxe_ls()
    ls._handle_cache_build_failed({})
    reason = ls.get_diagnostics_unavailable_reason()
    assert reason is not None
    assert "cache" in reason.lower()


def test_haxe_hook_reports_keeps_crashing() -> None:
    """A captured haxe/haxeKeepsCrashing message => the hook reports a reason."""
    ls = _bare_haxe_ls()
    ls._handle_haxe_keeps_crashing({})
    reason = ls.get_diagnostics_unavailable_reason()
    assert reason is not None
    assert "crash" in reason.lower()


# --------------------------------------------------------------------------------------------------
# Tool-level tests: empty diagnostics + a reason => clearly flag compiler-unavailable, not bare {}.
# --------------------------------------------------------------------------------------------------


class _StubLanguageServer:
    def __init__(self, unavailable_reason: str | None) -> None:
        self._unavailable_reason = unavailable_reason

    def get_diagnostics_unavailable_reason(self) -> str | None:
        return self._unavailable_reason


class _StubSymbolRetriever:
    """Returns the given diagnostics and a language server with a configurable unavailability reason."""

    def __init__(self, diagnostics: list, unavailable_reason: str | None) -> None:
        self._diagnostics = diagnostics
        self._lang_server = _StubLanguageServer(unavailable_reason)

    def get_file_diagnostics(self, relative_file_path: str, start_line: int = 0, end_line: int = -1, min_severity: int = 4) -> list:
        return self._diagnostics

    def find_diagnostic_owner_symbol(self, relative_file_path: str, line: int, column: int) -> None:
        return None

    def get_language_server(self, relative_path: str) -> _StubLanguageServer:
        return self._lang_server


def _make_tool(diagnostics: list, unavailable_reason: str | None) -> GetDiagnosticsForFileTool:
    tool = GetDiagnosticsForFileTool.__new__(GetDiagnosticsForFileTool)
    retriever = _StubSymbolRetriever(diagnostics, unavailable_reason)
    tool.create_language_server_symbol_retriever = lambda: retriever  # type: ignore[method-assign,assignment]
    return tool


def test_empty_diagnostics_with_crash_reason_flags_unavailable() -> None:
    """Empty diagnostics + a crash reason => output must clearly flag compiler-unavailable, not '{}'.

    This is the core T4 guarantee: a crashed compiler must not look like a clean file.
    """
    reason = "haxe/haxeKeepsCrashing: the Haxe compiler is repeatedly crashing"
    tool = _make_tool(diagnostics=[], unavailable_reason=reason)

    result = tool.apply(relative_path=_REL_PATH, min_severity=1, max_answer_chars=100000)

    assert result.strip() != "{}", "a crashed compiler must not be reported as a clean (empty) result"
    lowered = result.lower()
    assert "unavailable" in lowered, result
    assert "crash" in lowered, result


def test_clean_file_with_healthy_compiler_returns_empty() -> None:
    """Empty diagnostics + healthy compiler (no reason) => plain empty result, no unavailable signal."""
    tool = _make_tool(diagnostics=[], unavailable_reason=None)

    result = tool.apply(relative_path=_REL_PATH, min_severity=1, max_answer_chars=100000)

    assert json.loads(result) == {}, result
    assert "unavailable" not in result.lower(), result


def test_nonempty_diagnostics_are_returned_even_if_reason_present() -> None:
    """If diagnostics exist, they are returned normally even if a reason is also set (real signal wins)."""
    diagnostic = {
        "uri": "file:///x",
        "severity": 1,
        "message": "Type not found : Foo",
        "range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 10}},
    }
    tool = _make_tool(diagnostics=[diagnostic], unavailable_reason="haxe/cacheBuildFailed: ...")

    result = tool.apply(relative_path=_REL_PATH, min_severity=1, max_answer_chars=100000)

    parsed = json.loads(result)
    assert parsed != {}, result
    assert _REL_PATH in parsed, result
