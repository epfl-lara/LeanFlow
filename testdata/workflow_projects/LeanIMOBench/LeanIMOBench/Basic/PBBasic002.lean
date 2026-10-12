import Mathlib

/-
Show that $x^2 + y^2 + z^2 + t^2 \ge xyzt$ for any positive real numbers $x, y, z, t$ that satisfy $2(x + y + z + t) \ge xyzt$.
-/
theorem PBBasic002
    (x y z t : ℝ)
    (hx : 0 < x) (hy : 0 < y) (hz : 0 < z) (ht : 0 < t)
    (H : x * y * z * t ≤ 2 * (x + y + z + t)) :
    x * y * z * t ≤ x ^ 2 + y ^ 2 + z ^ 2 + t ^ 2 := by sorry
