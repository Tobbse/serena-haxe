package shapes;

/** In-project implementor of Shape. */
class Circle implements Shape {
	var radius:Float;

	public function new(radius:Float) {
		this.radius = radius;
	}

	public function area():Float {
		return 3.14159 * radius * radius;
	}
}
