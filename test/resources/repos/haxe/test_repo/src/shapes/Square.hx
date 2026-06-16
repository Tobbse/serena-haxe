package shapes;

/** In-project implementor of Shape. */
class Square implements Shape {
	var side:Float;

	public function new(side:Float) {
		this.side = side;
	}

	public function area():Float {
		return side * side;
	}
}
