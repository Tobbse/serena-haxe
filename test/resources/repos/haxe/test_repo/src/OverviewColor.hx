/**
 * Self-contained fixture for T10.
 *
 * Exercises whether `documentSymbol` / get_symbols_overview enumerates
 * enum cases as children of their owning enum. The Haxe LSP maps an
 * enum case to SymbolKind.EnumMember (20).
 */
enum OverviewColor {
	Red;
	Green;
	Blue;
}
