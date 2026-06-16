package shapes;

/**
 * An interface implemented by in-project types (Circle, Square).
 *
 * Used to exercise textDocument/implementation (find_implementations):
 * querying the interface should yield its implementors, and querying the
 * `area` method should yield the overriding methods.
 */
interface Shape {
	function area():Float;
}
