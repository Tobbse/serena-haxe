package animals;

/** In-project subclass of Animal. */
class Dog extends Animal {
	public function new() {
		super();
	}

	override public function speak():String {
		return "Woof";
	}
}
