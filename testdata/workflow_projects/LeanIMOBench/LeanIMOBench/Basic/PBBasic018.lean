import Mathlib

/-
Let $x$ and $y$ be positive integers satisfying $2x^2 + x = 3y^2 + y$. Prove that $2x+2y+1$ is a perfect square.
-/
theorem PBBasic018
    (x y : ℕ) (hx : x ≠ 0) (hy : y ≠ 0)
    (H : 2 * x ^ 2 + x = 3 * y ^ 2 + y) :
    IsSquare (2 * x + 2 * y + 1) := by sorry
