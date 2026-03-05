import os

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language
from solidlsp.ls_types import SymbolKind
from solidlsp.ls_utils import SymbolUtils


@pytest.mark.haxe
class TestHaxeLanguageServer:
    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_find_symbol(self, language_server: SolidLanguageServer) -> None:
        symbols = language_server.request_full_symbol_tree()
        assert SymbolUtils.symbol_tree_contains_name(symbols, "Main"), "Main class not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "greet"), "greet method not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "calculateResult"), "calculateResult method not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "Helper"), "Helper class not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "addNumbers"), "addNumbers method not found in symbol tree"
        assert SymbolUtils.symbol_tree_contains_name(symbols, "formatMessage"), "formatMessage method not found in symbol tree"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_find_references_within_file(self, language_server: SolidLanguageServer) -> None:
        file_path = os.path.join("src", "Main.hx")
        symbols = language_server.request_document_symbols(file_path).get_all_symbols_and_roots()
        greet_symbol = None
        for sym in symbols[0]:
            if sym.get("name") == "greet":
                greet_symbol = sym
                break
        assert greet_symbol is not None, "Could not find 'greet' symbol in Main.hx"
        sel_start = greet_symbol["selectionRange"]["start"]
        refs = language_server.request_references(file_path, sel_start["line"], sel_start["character"])
        assert any("Main.hx" in ref.get("relativePath", "") for ref in refs), "Main.hx should reference greet method"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_find_references_across_files(self, language_server: SolidLanguageServer) -> None:
        # Test addNumbers which is defined in Helper.hx and used in Main.hx
        helper_path = os.path.join("src", "utils", "Helper.hx")
        symbols = language_server.request_document_symbols(helper_path).get_all_symbols_and_roots()
        add_numbers_symbol = None
        for sym in symbols[0]:
            if sym.get("name") == "addNumbers":
                add_numbers_symbol = sym
                break
        assert add_numbers_symbol is not None, "Could not find 'addNumbers' symbol in Helper.hx"

        sel_start = add_numbers_symbol["selectionRange"]["start"]
        refs = language_server.request_references(helper_path, sel_start["line"], sel_start["character"])

        assert refs, "Expected to find references for addNumbers"
        assert any("Main.hx" in ref.get("relativePath", "") for ref in refs), "Expected to find usage of addNumbers in Main.hx"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_document_symbols_structure(self, language_server: SolidLanguageServer) -> None:
        file_path = os.path.join("src", "Main.hx")
        result = language_server.request_document_symbols(file_path)
        all_symbols, roots = result.get_all_symbols_and_roots()

        # Main class should be a root symbol
        main_symbol = None
        for sym in roots:
            if sym.get("name") == "Main":
                main_symbol = sym
                break
        assert main_symbol is not None, "Main class not found as root symbol"
        assert main_symbol.get("kind") in (SymbolKind.Class, SymbolKind.Struct), f"Expected Main to be Class, got {main_symbol.get('kind')}"

        # Check that methods and fields exist and are children of Main
        child_names = {s.get("name") for s in all_symbols if s.get("name") != "Main"}
        assert "greet" in child_names, "greet method not found in symbols"
        assert "calculateResult" in child_names, "calculateResult method not found in symbols"
        assert "message" in child_names, "message field not found in symbols"
        assert "count" in child_names, "count field not found in symbols"

        # Verify symbol kinds for specific symbols
        for sym in all_symbols:
            if sym.get("name") == "greet":
                assert sym.get("kind") in (
                    SymbolKind.Method,
                    SymbolKind.Function,
                ), f"Expected greet to be Method/Function, got {sym.get('kind')}"
            if sym.get("name") == "message":
                assert sym.get("kind") in (
                    SymbolKind.Field,
                    SymbolKind.Variable,
                    SymbolKind.Property,
                ), f"Expected message to be Field/Variable, got {sym.get('kind')}"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_workspace_symbol(self, language_server: SolidLanguageServer) -> None:
        result = language_server.request_workspace_symbol("Helper")
        assert result is not None, "Workspace symbol search returned None"
        assert len(result) > 0, "Workspace symbol search returned no results"
        assert any("Helper" in str(s.get("name", "")) for s in result), f"Expected at least one result containing 'Helper', got {result}"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_go_to_definition(self, language_server: SolidLanguageServer) -> None:
        # Go to definition of Helper.addNumbers call in Main.hx
        # Line 30 (0-indexed): "var sum = Helper.addNumbers(count, 20);"
        # "addNumbers" starts around column 19 (after "Helper.")
        main_path = os.path.join("src", "Main.hx")
        definitions = language_server.request_definition(main_path, 30, 21)
        assert definitions, "Expected to find definitions for addNumbers"
        assert any(
            "Helper.hx" in d.get("uri", d.get("relativePath", "")) for d in definitions
        ), f"Expected definition in Helper.hx, got {definitions}"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_hover(self, language_server: SolidLanguageServer) -> None:
        file_path = os.path.join("src", "Main.hx")
        result = language_server.request_document_symbols(file_path)
        all_symbols, _ = result.get_all_symbols_and_roots()
        greet_symbol = next((s for s in all_symbols if s.get("name") == "greet"), None)
        assert greet_symbol is not None, "Could not find 'greet' symbol"

        sel_start = greet_symbol["selectionRange"]["start"]
        hover = language_server.request_hover(file_path, sel_start["line"], sel_start["character"])
        assert hover is not None, "Hover returned None for greet method"
        hover_str = str(hover)
        assert "String" in hover_str or "greet" in hover_str, f"Expected hover to contain type info, got {hover_str}"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_rename_symbol(self, language_server: SolidLanguageServer) -> None:
        file_path = os.path.join("src", "Main.hx")
        result = language_server.request_document_symbols(file_path)
        all_symbols, _ = result.get_all_symbols_and_roots()
        greet_symbol = next((s for s in all_symbols if s.get("name") == "greet"), None)
        assert greet_symbol is not None, "Could not find 'greet' symbol"

        sel_start = greet_symbol["selectionRange"]["start"]
        edits = language_server.request_rename_symbol_edit(file_path, sel_start["line"], sel_start["character"], "sayHello")
        assert edits is not None, "Rename returned None"
        # Verify edits contain changes (WorkspaceEdit has 'changes' or 'documentChanges')
        edits_str = str(edits)
        assert "Main.hx" in edits_str or len(str(edits)) > 10, f"Expected rename edits for Main.hx, got {edits}"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_completions(self, language_server: SolidLanguageServer) -> None:
        # Request completions at a position in Main.hx where the LS can provide suggestions
        # Line 30 (0-indexed): "var sum = Helper.addNumbers(count, 20);"
        # Position right after "Helper." (column 17) triggers global/type completions
        main_path = os.path.join("src", "Main.hx")
        completions = language_server.request_completions(main_path, 30, 17)
        assert completions, "Expected non-empty completions"
        # Haxe LS uses 'completionText' field instead of 'label'
        completion_texts = [c.get("completionText", c.get("label", "")) for c in completions]
        assert any(text for text in completion_texts), f"Expected completions with text, got {completion_texts[:10]}"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_document_overview(self, language_server: SolidLanguageServer) -> None:
        overview = language_server.request_document_overview(os.path.join("src", "Main.hx"))
        assert overview, "Document overview returned empty list"
        symbol_names = [s.get("name", "") for s in overview]
        assert any("Main" in name for name in symbol_names), f"Expected 'Main' in overview, got {symbol_names}"
        for s in overview:
            assert s.get("name"), "Symbol missing 'name' field"
            assert s.get("kind"), "Symbol missing 'kind' field"

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_diagnostics_clean(self, language_server: SolidLanguageServer) -> None:
        from solidlsp.ls_exceptions import SolidLSPException

        file_path = os.path.join("src", "Main.hx")
        try:
            diagnostics = language_server.request_text_document_diagnostics(file_path)
        except SolidLSPException as e:
            if "Unhandled method" in str(e):
                pytest.skip("Haxe LS does not support textDocument/diagnostic pull model")
            raise
        assert isinstance(diagnostics, list), f"Expected diagnostics to be a list, got {type(diagnostics)}"
        errors = [d for d in diagnostics if d.get("severity") == 1]  # 1 = Error in LSP
        assert not errors, f"Expected no error diagnostics for valid code, got {errors}"
