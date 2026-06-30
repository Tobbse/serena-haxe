/**
 * Self-contained fixture for T10.
 *
 * Exercises whether `documentSymbol` / get_symbols_overview enumerates
 * `public static inline final` constants as children of their owning class.
 * The Haxe LSP maps `static inline final` to SymbolKind.Constant (21).
 */
class OverviewConstants {
	public static inline final RELEASE:String = "release";
	public static inline final DEBUG:String = "debug";
	public static inline final TEST:String = "test";

	public static function describe():String {
		return RELEASE + "/" + DEBUG + "/" + TEST;
	}
}
