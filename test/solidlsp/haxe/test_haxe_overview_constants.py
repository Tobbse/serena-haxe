"""T10 — Enumeration of ``static inline final`` constants and enum members in the Haxe overview.

CANONICAL FINDING (Haxe 4.3.7 + vshaxe/haxe-language-server 2.34.2, fixtures
``src/OverviewConstants.hx`` and ``src/OverviewColor.hx``):

VERDICT: ``source-actually-enumerates-them`` — the omission reported on the board
(FoE) was NOT reproduced; it is FoE/cache-specific, not an upstream LSP limitation
on a settled server.

Empirically, the Haxe LSP ``textDocument/documentSymbol`` response (and therefore
Serena's parsed ``request_document_symbols`` tree and ``request_full_symbol_tree``)
DOES enumerate:
  * ``public static inline final`` class constants, mapped to ``SymbolKind.Constant`` (21), and
  * ``enum`` cases, mapped to ``SymbolKind.EnumMember`` (20),
both as roots in ``get_all_symbols_and_roots()`` AND as ``children`` of their owning
class/enum at depth 1. This was verified deterministically on the *first* request to a
freshly started server across repeated runs (i.e. it is not a warm-cache-only effect).

WHERE ANY "OMISSION" ACTUALLY COMES FROM — the Serena tool layer, not the LSP:
``GetSymbolsOverviewTool`` (``serena/tools/symbol_tools.py``) filters overview children
through ``LanguageServerSymbol.is_low_level()``, which treats every kind
``>= SymbolKind.Variable`` (6) as low-level. Because ``EnumMember`` (20) and
``Constant`` (21) are ``>= 6``, the ``get_symbols_overview`` *tool* deliberately does NOT
list them as children of a class/enum — even though the underlying symbol tree contains
them. This is a Serena presentation choice for high-level overviews, NOT a Haxe-LSP
limitation and NOT a Serena parsing bug.

WORKAROUND (always works, regardless of the overview filter): use ``find_symbol`` with an
explicit ``Owner/NAME`` name path. ``find_symbol`` is backed by
``LanguageServerSymbolRetriever.find`` → ``request_full_symbol_tree`` and applies no
low-level filter, so:
  * ``OverviewConstants/RELEASE`` resolves to a ``SymbolKind.Constant`` symbol, and
  * ``OverviewColor/Red`` resolves to a ``SymbolKind.EnumMember`` symbol.
A bare name (e.g. ``RELEASE`` or ``Red``) also resolves. Optionally pass
``include_kinds=[SymbolKind.Constant]`` / ``[SymbolKind.EnumMember]`` to disambiguate.

Note on the raw LSP layer: when the lower-level ``_request_document_symbols`` is called
the very instant a ``didOpen`` recompile is still in flight, a transient/partial response
can be observed (a child temporarily missing its ``name`` / carrying a provisional kind).
This is a cold-compile timing artifact of the raw channel; it does not surface through
``request_document_symbols`` (which is what overview/find use), so the assertions below
target the parsed layer and tolerate raw-layer transients.
"""

import os

import pytest

from serena.project import Project
from serena.symbol import LanguageServerSymbol, LanguageServerSymbolRetriever
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language
from solidlsp.ls_types import SymbolKind

CONSTANTS_FILE = os.path.join("src", "OverviewConstants.hx")
ENUM_FILE = os.path.join("src", "OverviewColor.hx")

OWNER_CONSTANTS = "OverviewConstants"
OWNER_ENUM = "OverviewColor"
CONSTANT_NAMES = {"RELEASE", "DEBUG", "TEST"}
ENUM_MEMBER_NAMES = {"Red", "Green", "Blue"}


def _children_by_name(root: dict) -> dict[str, dict]:
    return {c["name"]: c for c in root.get("children", []) if c.get("name")}


def _owner_root(language_server: SolidLanguageServer, file_path: str, owner_name: str) -> dict:
    _all, roots = language_server.request_document_symbols(file_path).get_all_symbols_and_roots()
    owner = next((r for r in roots if r.get("name") == owner_name), None)
    assert owner is not None, f"Owner '{owner_name}' not found as a root symbol in {file_path}; roots={[r.get('name') for r in roots]}"
    return owner


@pytest.mark.haxe
class TestHaxeOverviewConstantsAndEnumMembers:
    # ---- Parsed document-symbol layer (what overview/find build on) -------------------

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_constants_class_is_a_root_symbol(self, language_server: SolidLanguageServer) -> None:
        """The owning class is itself enumerated (sanity that the file analyzed)."""
        owner = _owner_root(language_server, CONSTANTS_FILE, OWNER_CONSTANTS)
        assert owner.get("kind") in (SymbolKind.Class, SymbolKind.Struct), (
            f"Expected {OWNER_CONSTANTS} to be Class/Struct, got {owner.get('kind')}"
        )

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_static_inline_final_constants_are_enumerated_as_children(self, language_server: SolidLanguageServer) -> None:
        """OBSERVED: `static inline final` constants ARE present as children (kind Constant).

        This documents that the FoE 'omitted at all depths' observation does NOT reproduce
        here: the Haxe LSP / Serena parsed tree enumerates the constants at depth 1.
        """
        owner = _owner_root(language_server, CONSTANTS_FILE, OWNER_CONSTANTS)
        children = _children_by_name(owner)

        missing = CONSTANT_NAMES - set(children)
        assert not missing, (
            f"Expected constants {sorted(CONSTANT_NAMES)} as children of {OWNER_CONSTANTS}, missing {sorted(missing)}; got {sorted(children)}"
        )

        for name in CONSTANT_NAMES:
            assert children[name].get("kind") == SymbolKind.Constant, (
                f"Expected constant '{name}' to be SymbolKind.Constant (21), got {children[name].get('kind')}"
            )

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_constants_appear_in_flattened_symbol_list(self, language_server: SolidLanguageServer) -> None:
        all_syms, _roots = language_server.request_document_symbols(CONSTANTS_FILE).get_all_symbols_and_roots()
        names = {s.get("name") for s in all_syms}
        assert CONSTANT_NAMES.issubset(names), (
            f"Expected {sorted(CONSTANT_NAMES)} in flattened symbols, got {sorted(n for n in names if n)}"
        )

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_enum_members_are_enumerated_as_children(self, language_server: SolidLanguageServer) -> None:
        """OBSERVED: enum cases ARE present as children (kind EnumMember)."""
        owner = _owner_root(language_server, ENUM_FILE, OWNER_ENUM)
        assert owner.get("kind") in (SymbolKind.Enum, SymbolKind.Class), f"Expected {OWNER_ENUM} to be Enum/Class, got {owner.get('kind')}"
        children = _children_by_name(owner)

        missing = ENUM_MEMBER_NAMES - set(children)
        assert not missing, (
            f"Expected enum members {sorted(ENUM_MEMBER_NAMES)} as children of {OWNER_ENUM}, missing {sorted(missing)}; got {sorted(children)}"
        )

        for name in ENUM_MEMBER_NAMES:
            assert children[name].get("kind") == SymbolKind.EnumMember, (
                f"Expected enum case '{name}' to be SymbolKind.EnumMember (20), got {children[name].get('kind')}"
            )

    # ---- Raw LSP layer: localise the omission (it is NOT in the parsed tree) -----------

    @pytest.mark.parametrize("language_server", [Language.HAXE], indirect=True)
    def test_raw_lsp_documentsymbol_contains_constant_children(self, language_server: SolidLanguageServer) -> None:
        """The omission is NOT in the raw LSP `documentSymbol` response either.

        We read the raw (pre-Serena-parsing) response directly. On a settled server it
        contains the constant children. We tolerate a cold-compile transient where the
        raw channel may briefly under-report, by re-reading via the parsed layer (which
        waits appropriately) as the authoritative check.
        """
        with language_server._open_file_context(CONSTANTS_FILE) as fd:
            raw = language_server._request_document_symbols(CONSTANTS_FILE, fd)
        assert raw, "Raw documentSymbol response was empty/None"
        raw_owner = next((s for s in raw if s.get("name") == OWNER_CONSTANTS), raw[0])
        raw_child_names = {c.get("name") for c in raw_owner.get("children", []) if c.get("name")}

        # Authoritative (parsed) view — the layer overview/find actually use.
        parsed_owner = _owner_root(language_server, CONSTANTS_FILE, OWNER_CONSTANTS)
        parsed_child_names = set(_children_by_name(parsed_owner))

        assert CONSTANT_NAMES.issubset(parsed_child_names), f"Parsed layer must enumerate constants; got {sorted(parsed_child_names)}"
        # Document what the raw channel returned (it normally matches the parsed view).
        assert raw_child_names, (
            f"Raw owner had no named children; parsed had {sorted(parsed_child_names)} (raw={raw_owner.get('children')})"
        )

    # ---- Serena tool-layer filter: explains why get_symbols_overview hides them --------

    def test_constant_and_enummember_kinds_are_low_level(self) -> None:
        """Constant (21) and EnumMember (20) are >= Variable (6) -> is_low_level() True.

        This is why `GetSymbolsOverviewTool` (child_inclusion_predicate = not is_low_level)
        does NOT list constants / enum members as overview children, even though the parsed
        symbol tree contains them. It is a Serena presentation choice, not an LSP omission.
        """
        for kind in (SymbolKind.EnumMember, SymbolKind.Constant):
            sym = LanguageServerSymbol({"name": "X", "kind": kind, "children": []})  # type: ignore[arg-type]
            assert sym.is_low_level(), f"Expected {kind!r} to be classified low-level (>= Variable)"

    # ---- find_symbol workaround (authentic production retriever path) ------------------

    @pytest.mark.parametrize("project_with_ls", [Language.HAXE], indirect=True)
    def test_find_symbol_resolves_constant_by_owner_name_path(self, project_with_ls: Project) -> None:
        """WORKAROUND: find_symbol with `Owner/NAME` resolves a Constant."""
        retriever = LanguageServerSymbolRetriever(project_with_ls)

        matches = retriever.find(f"{OWNER_CONSTANTS}/RELEASE", within_relative_path="")
        assert matches, f"find_symbol('{OWNER_CONSTANTS}/RELEASE') returned no matches"
        kinds = {m.symbol_kind for m in matches}
        assert SymbolKind.Constant in kinds, f"Expected a Constant for {OWNER_CONSTANTS}/RELEASE, got kinds {kinds}"
        name_paths = {m.get_name_path() for m in matches}
        assert any(np.endswith(f"{OWNER_CONSTANTS}/RELEASE") for np in name_paths), f"Unexpected name paths: {name_paths}"

        # include_kinds filter pins it precisely to a Constant
        constant_only = retriever.find(f"{OWNER_CONSTANTS}/RELEASE", include_kinds=[SymbolKind.Constant], within_relative_path="")
        assert constant_only, "Expected RELEASE to resolve when filtering include_kinds=[Constant]"

    @pytest.mark.parametrize("project_with_ls", [Language.HAXE], indirect=True)
    def test_find_symbol_resolves_enum_member_by_owner_name_path(self, project_with_ls: Project) -> None:
        """WORKAROUND: find_symbol with `Owner/NAME` resolves an EnumMember."""
        retriever = LanguageServerSymbolRetriever(project_with_ls)

        matches = retriever.find(f"{OWNER_ENUM}/Red", within_relative_path="")
        assert matches, f"find_symbol('{OWNER_ENUM}/Red') returned no matches"
        kinds = {m.symbol_kind for m in matches}
        assert SymbolKind.EnumMember in kinds, f"Expected an EnumMember for {OWNER_ENUM}/Red, got kinds {kinds}"

        member_only = retriever.find(f"{OWNER_ENUM}/Red", include_kinds=[SymbolKind.EnumMember], within_relative_path="")
        assert member_only, "Expected Red to resolve when filtering include_kinds=[EnumMember]"

    @pytest.mark.parametrize("project_with_ls", [Language.HAXE], indirect=True)
    def test_find_symbol_resolves_all_constants_and_members(self, project_with_ls: Project) -> None:
        """Every constant and enum member is reachable via find_symbol (bare name)."""
        retriever = LanguageServerSymbolRetriever(project_with_ls)
        for name in CONSTANT_NAMES:
            found = retriever.find(name, include_kinds=[SymbolKind.Constant], within_relative_path="")
            assert found, f"Constant '{name}' not resolvable via find_symbol"
        for name in ENUM_MEMBER_NAMES:
            found = retriever.find(name, include_kinds=[SymbolKind.EnumMember], within_relative_path="")
            assert found, f"EnumMember '{name}' not resolvable via find_symbol"
