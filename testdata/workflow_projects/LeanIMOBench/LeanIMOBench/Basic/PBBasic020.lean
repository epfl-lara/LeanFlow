import Mathlib

/-
Find all pairs of primes $(a, b)$ such that $a^2 - ab - b^3 = 1$.

Solution: $(p,q) = (7,3)$
-/
theorem PBBasic020 :
    {(a, b) : ℕ × ℕ | a.Prime ∧ b.Prime ∧ (a ^ 2 - a * b - b ^ 3 : ℕ) = 1}
      = {(7, 3)} := by sorry
