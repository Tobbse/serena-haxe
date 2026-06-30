"""Safety regression tests for :class:`SafeDeleteSymbol` (T3).

The safety property under test is the single most important guarantee of ``safe_delete_symbol``:

    A delete must NEVER proceed without a SUCCESSFUL reference check.

On large codebases the reference check (``request_references``) can time out (e.g. the Haxe
compiler crashes / keeps recompiling). A silent unsafe delete is far worse than a surfaced
timeout, so the tool must fail safe: if the reference check raises (``TimeoutError`` or any other
exception), the symbol must be left untouched and the error must surface.

These tests are fully deterministic — no real language server and no Haxe toolchain. They drive
``SafeDeleteSymbol.apply`` directly with hand-built stubs so the control flow can be exercised
exactly:

  * negative-safety (the crux): ``request_references`` raises -> ``delete_symbol`` is never called
    and the exception propagates;
  * positive controls: references EXIST -> tool refuses and reports the referencing locations;
    references NONE -> deletion proceeds (``delete_symbol`` called, SUCCESS returned).

They live under ``test/solidlsp/haxe`` and carry the ``haxe`` marker so they run with the rest of
the Haxe suite, but they exercise the shared (language-agnostic) ``SafeDeleteSymbol`` logic that the
Haxe-compiler-crash scenario stresses.
"""

import pytest

from serena.tools import SUCCESS_RESULT
from serena.tools.symbol_tools import SafeDeleteSymbol

pytestmark = [pytest.mark.haxe]

_REL_PATH = "src/Victim.hx"
_NAME_PATH = "Victim/doomed"


class _StubSymbol:
    """Minimal stand-in for a :class:`LanguageServerSymbol` located in ``_REL_PATH``."""

    def __init__(self, relative_path: str = _REL_PATH, line: int = 10, column: int = 4) -> None:
        self.relative_path = relative_path
        self.line = line
        self.column = column

    def get_name_path(self) -> str:
        return _NAME_PATH


class _StubLanguageServer:
    """Language server whose ``request_references`` behaviour is configurable per test."""

    def __init__(self, references: object) -> None:
        # ``references`` is either a list of location dicts to return, or an Exception instance to raise.
        self._references = references
        self.request_references_calls = 0

    def request_references(self, relative_file_path: str, line: int, column: int) -> list:
        self.request_references_calls += 1
        if isinstance(self._references, BaseException):
            raise self._references
        return self._references  # type: ignore[return-value]


class _StubSymbolRetriever:
    def __init__(self, symbol: _StubSymbol, lang_server: _StubLanguageServer) -> None:
        self._symbol = symbol
        self._lang_server = lang_server

    def find_unique(self, name_path_pattern: str, substring_matching: bool = False, within_relative_path: str | None = None) -> _StubSymbol:
        return self._symbol

    def get_language_server(self, relative_path: str) -> _StubLanguageServer:
        return self._lang_server


class _RecordingCodeEditor:
    """Records whether (and how) ``delete_symbol`` was invoked, so tests can assert no deletion."""

    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, str]] = []

    def delete_symbol(self, name_path: str, relative_file_path: str) -> None:
        self.delete_calls.append((name_path, relative_file_path))


def _make_tool(references: object) -> tuple[SafeDeleteSymbol, _RecordingCodeEditor, _StubLanguageServer]:
    """Build a :class:`SafeDeleteSymbol` wired to stubs.

    :param references: list of reference-location dicts to return from ``request_references``,
        OR an Exception instance to raise from it.
    :return: (tool, recording_code_editor, stub_language_server)
    """
    # Bypass Tool.__init__ (which requires a full SerenaAgent); we only exercise apply()'s logic.
    tool = SafeDeleteSymbol.__new__(SafeDeleteSymbol)

    symbol = _StubSymbol()
    lang_server = _StubLanguageServer(references)
    retriever = _StubSymbolRetriever(symbol, lang_server)
    code_editor = _RecordingCodeEditor()

    tool.create_language_server_symbol_retriever = lambda: retriever  # type: ignore[method-assign,assignment]
    tool.create_ls_code_editor = lambda: code_editor  # type: ignore[method-assign,assignment]

    return tool, code_editor, lang_server


# --------------------------------------------------------------------------------------------------
# Negative-safety tests (the crux): a failed reference check must NEVER reach the deletion.
# --------------------------------------------------------------------------------------------------


def test_timeout_during_reference_check_does_not_delete() -> None:
    """If ``request_references`` times out, the symbol must be left untouched and the error surface."""
    tool, code_editor, lang_server = _make_tool(TimeoutError("Request timed out (timeout=25)"))

    with pytest.raises(TimeoutError):
        tool.apply(name_path_pattern=_NAME_PATH, relative_path=_REL_PATH)

    assert lang_server.request_references_calls == 1, "the reference check must have been attempted"
    assert code_editor.delete_calls == [], "a timed-out reference check must NOT lead to a deletion"


def test_generic_exception_during_reference_check_does_not_delete() -> None:
    """If ``request_references`` raises any error (e.g. compiler crash), no deletion may happen."""
    tool, code_editor, lang_server = _make_tool(RuntimeError("haxe/haxeKeepsCrashing"))

    with pytest.raises(RuntimeError):
        tool.apply(name_path_pattern=_NAME_PATH, relative_path=_REL_PATH)

    assert lang_server.request_references_calls == 1
    assert code_editor.delete_calls == [], "a failed reference check must NOT lead to a deletion"


# --------------------------------------------------------------------------------------------------
# Positive controls: confirm normal behaviour is unchanged.
# --------------------------------------------------------------------------------------------------


def test_existing_references_block_deletion_and_are_reported() -> None:
    """References EXIST -> the tool refuses and reports the referencing file/line, no deletion."""
    references = [
        {"relativePath": "src/Caller.hx", "range": {"start": {"line": 42, "character": 8}}},
        {"relativePath": "src/Other.hx", "range": {"start": {"line": 7, "character": 2}}},
    ]
    tool, code_editor, _ = _make_tool(references)

    result = tool.apply(name_path_pattern=_NAME_PATH, relative_path=_REL_PATH)

    assert "Cannot delete" in result, result
    assert "src/Caller.hx" in result, result
    assert code_editor.delete_calls == [], "must not delete a symbol that is still referenced"


def test_no_references_allows_deletion() -> None:
    """References NONE -> deletion proceeds and SUCCESS is returned."""
    tool, code_editor, _ = _make_tool([])

    result = tool.apply(name_path_pattern=_NAME_PATH, relative_path=_REL_PATH)

    assert result == SUCCESS_RESULT, result
    assert code_editor.delete_calls == [(_NAME_PATH, _REL_PATH)], "expected exactly one delete_symbol call"


def test_references_without_relative_path_do_not_block_or_crash() -> None:
    """Reference locations lacking a relativePath are skipped; if none remain, deletion proceeds.

    This guards the ``ref_relative_path is None: continue`` branch: a None path must neither raise
    nor (on its own) block the deletion.
    """
    references = [{"range": {"start": {"line": 1, "character": 0}}}]  # no "relativePath"
    tool, code_editor, _ = _make_tool(references)

    result = tool.apply(name_path_pattern=_NAME_PATH, relative_path=_REL_PATH)

    assert result == SUCCESS_RESULT, result
    assert code_editor.delete_calls == [(_NAME_PATH, _REL_PATH)]
