import Mathlib

/-
Define the sequence $x_1 = 6$ and $x_n = 2^{x_{n-1}} + 2$ for all integers $n \ge 2$. Prove that $x_{n-1}$ divides $x_n$ for all integers $n \ge 2$.
-/
theorem PBBasic021
    (x : ℕ → ℕ) (hx₁ : x 1 = 6)
    (hx_rec : ∀ n ≥ 1, x (n + 1) = 2 ^ (x n) + 2)
    (n : ℕ) (hn : 1 ≤ n) :
    x n ∣ x (n + 1) := by sorry
