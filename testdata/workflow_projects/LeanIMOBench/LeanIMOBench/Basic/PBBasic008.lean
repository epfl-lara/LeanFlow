import Mathlib

/-
Let $a,b,c$ be positive reals such that $a+b+c = 1$, prove that $\sqrt{a}+\sqrt{b}+\sqrt{c} \geq 3\sqrt{3}(ab+bc+ca)$.
-/
theorem PBBasic008
    (a b c : ℝ)
    (ha : 0 < a) (hb : 0 < b) (hc : 0 < c)
    (H : a + b + c = 1) :
    3 * √3 * (a * b + b * c + c * a) ≤ √a + √b + √c := by sorry
