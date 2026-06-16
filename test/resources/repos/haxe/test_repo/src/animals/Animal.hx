package animals;

/**
 * A base class with one in-project subclass (Dog).
 *
 * Used to exercise textDocument/implementation for class inheritance
 * (the common case in real Haxe projects, e.g. OpenFL Sprite subclasses).
 */
class Animal {
	public function new() {}

	public function speak():String {
		return "...";
	}
}
